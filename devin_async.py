"""Async-версии функций для пайплайна Devin: login в mail-client, login
в Devin (через email-код), навигация My Team → Upgrade → Start free
trial и заполнение адреса в Stripe Checkout.

Модуль импортирует константы (URLs, селекторы) из ``register_devin``,
чтобы один источник правды и в sync-, и в async-сценариях.

Не дублирует CLI или большую часть docstring — это «рабочая лошадка» для
``find_and_pay.py``, и подразумевается, что чтобь понять алгоритм, ты
уже посмотрел ``register_devin.py`` и ``start_devin_trial.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

import ddddocr
from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

from logging_utils import log_step, log_exception, log_timing

# Переиспользуем все константы (URL'ы, селекторы) из синхронного модуля —
# они одинаково применимы и для async API.
from register_devin import (
    Account,
    MAIL_LOGIN_URL,
    MAIL_INBOX_URL_HASH,
    MAIL_LIST_ITEM_SELECTOR,
    MAIL_REFRESH_BUTTON_SELECTOR,
    MAIL_DETAIL_BODY_SELECTOR,
    MAIL_DETAIL_BODY_FALLBACK_SELECTORS,
    StepError,
    InvalidCodeError,
    extract_code,
    IDENTITIES_PATH,
)

# P2-8: URL в config.py (переопределяется через ENV QQQ_DEVIN_LOGIN_URL).
from config import (
    DEVIN_LOGIN_URL as _CFG_DEVIN_LOGIN_URL,
    STRIPE_CHECKOUT_PREFIX as _CFG_STRIPE_PREFIX,
)

DEVIN_LOGIN_URL = _CFG_DEVIN_LOGIN_URL

# Селекторы mail-client login + capcha (повторяют register_devin).
# Основной селектор — по русскому placeholder; fallback — type=email
# (для случая когда браузер открывает английскую версию UI, например
# Camoufox-Firefox с дефолтной локалью).
_MAIL_LOGIN_EMAIL_SELECTOR = 'input[placeholder="Введите свой адрес электронной почты"]'
_MAIL_LOGIN_EMAIL_FALLBACK = 'input[type="email"], input[placeholder*="email" i], input[placeholder*="address" i]'
_MAIL_LOGIN_PASSWORD_SELECTOR = 'input[placeholder="Введите пароль"]'
_MAIL_LOGIN_PASSWORD_FALLBACK = 'input[type="password"]'
_MAIL_LOGIN_SUBMIT_NAME = "Авторизоваться"
_MAIL_LOGIN_SUBMIT_FALLBACK_NAME = "Log in"
_MAIL_LOGIN_CAPTCHA_DIALOG_SELECTOR = 'div[role="dialog"][aria-label="Введите капчу"]'
_MAIL_LOGIN_CAPTCHA_IMG_SELECTOR = "#weiqu_captcha_img_url"
_MAIL_LOGIN_CAPTCHA_REFRESH_SELECTOR = "#weiqu_captcha_img_change"
_MAIL_LOGIN_CAPTCHA_INPUT_SELECTOR = ".el-message-box__input input"
_MAIL_LOGIN_CAPTCHA_SUBMIT_SELECTOR = '.el-message-box__btns button:has-text("проверять")'

CAPTCHA_REFRESHES = 30
_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS = 5
_MAIL_LOGIN_CAPTCHA_LENGTH = 6
_MAIL_LOGIN_CAPTCHA_DIALOG_TIMEOUT_MS = 3_000
_MAIL_LOGIN_CAPTCHA_CLICK_TIMEOUT_MS = 3_000
_MAIL_LOGIN_CAPTCHA_SUBMIT_CLICK_TIMEOUT_MS = 8_000
_MAIL_LOGIN_CAPTCHA_POST_SUBMIT_S = 1.5

_DEVIN_CODE_INPUT_SELECTOR = 'input[autocomplete="one-time-code"]'
_DEVIN_INVALID_CODE_PHRASES = ("invalid", "incorrect", "expired")
_DEVIN_POST_SUBMIT_TIMEOUT_S = 30.0
_DEVIN_BODY_READ_TIMEOUT_MS = 1_000

_DEVIN_KEYWORDS = ("devin", "cognition", "verification code", "verify your", "your code")
_MAIL_CLICK_TIMEOUT_MS = 2_000
_MAIL_LIST_WAIT_TIMEOUT_MS = 5_000
_MAIL_BODY_READ_TIMEOUT_MS = 3_000

_ocr: ddddocr.DdddOcr | None = None


def _get_ocr() -> ddddocr.DdddOcr:
    global _ocr
    if _ocr is None:
        instance = ddddocr.DdddOcr(show_ad=False)
        instance.set_ranges("0123456789")
        _ocr = instance
    return _ocr


# ---------------------------------------------------------------------------
# Капча-солвер (async)
# ---------------------------------------------------------------------------


async def solve_digit_captcha_async(
    page: Page,
    img_selector: str,
    refresh_callable: Callable[[], Awaitable[None]],
    expected_len: int,
) -> str | None:
    ocr = _get_ocr()
    last_src = ""
    empty_in_row = 0
    for _ in range(CAPTCHA_REFRESHES):
        src = await page.evaluate(
            "(sel) => { const el = document.querySelector(sel); "
            "return el ? el.src : ''; }",
            img_selector,
        )
        if not src:
            empty_in_row += 1
            if empty_in_row >= 3:
                return None
            await refresh_callable()
            await asyncio.sleep(0.35)
            continue
        empty_in_row = 0

        if src == last_src:
            await asyncio.sleep(0.35)
            continue
        last_src = src

        response = await page.request.get(src)
        if response.status != 200:
            await refresh_callable()
            await asyncio.sleep(0.35)
            continue

        raw = ocr.classification(await response.body())
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == expected_len:
            return digits

        await refresh_callable()
        await asyncio.sleep(0.35)

    return None


async def _captcha_dialog_visible(page: Page) -> bool:
    try:
        loc = page.locator(_MAIL_LOGIN_CAPTCHA_DIALOG_SELECTOR).first
        return await loc.is_visible()
    except Exception:
        return False


async def _solve_mailclient_login_captcha(page: Page) -> bool:
    """Закрыть модалку капчи на форме mail-client.

    True — модалка появилась и была закрыта (код принят).
    False — модалки вообще не появилось.
    Raise StepError — модалка появилась, но за inner_attempts мы её не закрыли.
    """
    try:
        await page.wait_for_selector(
            _MAIL_LOGIN_CAPTCHA_DIALOG_SELECTOR,
            timeout=_MAIL_LOGIN_CAPTCHA_DIALOG_TIMEOUT_MS,
        )
    except PWTimeout:
        return False
    except Exception:
        return False

    async def _refresh() -> None:
        try:
            await page.locator(_MAIL_LOGIN_CAPTCHA_REFRESH_SELECTOR).click(
                timeout=_MAIL_LOGIN_CAPTCHA_CLICK_TIMEOUT_MS
            )
        except Exception:
            pass

    for _ in range(_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS):
        if not await _captcha_dialog_visible(page):
            return True

        code = await solve_digit_captcha_async(
            page,
            _MAIL_LOGIN_CAPTCHA_IMG_SELECTOR,
            _refresh,
            expected_len=_MAIL_LOGIN_CAPTCHA_LENGTH,
        )
        if code is None:
            if not await _captcha_dialog_visible(page):
                return True
            continue

        try:
            await page.locator(_MAIL_LOGIN_CAPTCHA_INPUT_SELECTOR).fill(
                code, timeout=_MAIL_LOGIN_CAPTCHA_CLICK_TIMEOUT_MS
            )
            await page.locator(_MAIL_LOGIN_CAPTCHA_SUBMIT_SELECTOR).click(
                timeout=_MAIL_LOGIN_CAPTCHA_SUBMIT_CLICK_TIMEOUT_MS
            )
        except (PWTimeout, Exception) as exc:
            raise StepError(f"login failed: cannot submit captcha: {exc}") from exc

        await asyncio.sleep(_MAIL_LOGIN_CAPTCHA_POST_SUBMIT_S)
        if not await _captcha_dialog_visible(page):
            return True

    raise StepError(
        f"login failed: captcha unsolvable after {_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS} attempts"
    )


def _is_post_login_url(url: str) -> bool:
    if MAIL_INBOX_URL_HASH in url:
        return True
    if "#" not in url:
        return False
    fragment = url.rsplit("#", 1)[-1].strip("/")
    return fragment not in ("", "login")


# ---------------------------------------------------------------------------
# Mail-client login (async)
# ---------------------------------------------------------------------------


@log_timing
async def login_to_mailclient_async(page: Page, account: Account, *, timeout: int = 30_000) -> None:
    logger = logging.getLogger(__name__)
    log_step(logger, "mail-login", f"account={account.email}, timeout={timeout}ms")

    try:
        await page.goto(MAIL_LOGIN_URL, wait_until="domcontentloaded")
        logger.debug(f"[mail-login] goto {MAIL_LOGIN_URL} успешен")
    except Exception as exc:
        log_exception(logger, exc, "mail-login goto")
        raise StepError(f"login failed: cannot open {MAIL_LOGIN_URL}: {exc}") from exc

    # Если в браузере уже есть залогиненная сессия (persistent profile,
    # ручной логин до запуска скрипта), mail-client показывает «Вы вошли
    # в систему» и кнопку «Переключиться на другой почтовый ящик».
    # Кликаем эту кнопку — попадаем на форму логина.
    try:
        await asyncio.sleep(0.6)  # Дать UI отрендериться.
        switch_btn = page.get_by_text(
            "Переключиться на другой почтовый ящик",
            exact=False,
        ).first
        if await switch_btn.is_visible(timeout=2_000):
            logger.info("[mail-login] вижу 'Вы вошли в систему' — переключаю")
            await switch_btn.click(timeout=3_000)
            await asyncio.sleep(1.0)
    except (PWTimeout, Exception) as e:
        logger.debug(f"[mail-login] switch button не найдена (это норма): {e}")
        pass

    # Найти форму (русский плейсхолдер ИЛИ type=email).
    email_loc = None
    pwd_loc = None
    deadline_form = asyncio.get_event_loop().time() + timeout / 1000.0
    form_wait_start = asyncio.get_event_loop().time()

    while asyncio.get_event_loop().time() < deadline_form:
        try:
            for sel in (_MAIL_LOGIN_EMAIL_SELECTOR, _MAIL_LOGIN_EMAIL_FALLBACK):
                cand = page.locator(sel).first
                if await cand.is_visible(timeout=300):
                    email_loc = cand
                    logger.debug(f"[mail-login] email field найден через selector: {sel}")
                    break
        except Exception as e:
            logger.debug(f"[mail-login] email field поиск failed: {e}")
            email_loc = None
        if email_loc is not None:
            break
        await asyncio.sleep(0.4)

    form_wait_elapsed = asyncio.get_event_loop().time() - form_wait_start
    if email_loc is None:
        logger.error(f"[mail-login] email field не найден за {form_wait_elapsed:.1f}s")
        raise StepError("login failed: email field not visible")

    logger.debug(f"[mail-login] email field найден за {form_wait_elapsed:.1f}s")

    try:
        for sel in (_MAIL_LOGIN_PASSWORD_SELECTOR, _MAIL_LOGIN_PASSWORD_FALLBACK):
            cand = page.locator(sel).first
            if await cand.is_visible(timeout=1_000):
                pwd_loc = cand
                break
    except Exception:
        pass
    if pwd_loc is None:
        raise StepError("login failed: password field not visible")

    try:
        await email_loc.fill(account.email)
        logger.debug("[mail-login] email заполнен")
        await pwd_loc.fill(account.password)
        logger.debug("[mail-login] password заполнен")

        # Кнопка submit — пробуем оба варианта.
        submit_clicked = False
        for label in (_MAIL_LOGIN_SUBMIT_NAME, _MAIL_LOGIN_SUBMIT_FALLBACK_NAME):
            try:
                btn = page.get_by_role("button", name=label).first
                if await btn.is_visible(timeout=600):
                    await btn.click()
                    submit_clicked = True
                    logger.info(f"[mail-login] submit кнопка '{label}' нажата")
                    break
            except Exception:
                continue
        if not submit_clicked:
            # Последний шанс — Enter в поле пароля.
            logger.debug("[mail-login] submit кнопка не найдена, пробую Enter")
            await pwd_loc.press("Enter")
    except PWTimeout as exc:
        log_exception(logger, exc, "mail-login form submit")
        raise StepError(f"login failed: cannot submit form: {exc}") from exc

    # Капча — до 5 попыток.
    captcha_attempts = 0
    for attempt in range(5):
        try:
            had = await _solve_mailclient_login_captcha(page)
            if had:
                captcha_attempts += 1
                logger.info(f"[mail-login] капча решена (попытка {captcha_attempts})")
        except StepError as e:
            log_exception(logger, e, "mail-login captcha")
            raise
        if not had:
            logger.debug("[mail-login] капчи не было")
            break
        if _is_post_login_url(page.url):
            logger.info("[mail-login] пост-логин URL достигнут после капчи")
            break
    else:
        logger.error("[mail-login] капча не решена после 5 попыток")
        raise StepError("login failed: captcha unsolvable after 5 attempts")

    # Ждём пост-логин-URL или текста «Входящие»/«Inbox» до 90с.
    # Под нагрузкой Patchright + параллельный чекинг — mail-client
    # медленнее загружается, 40с впритык.
    log_step(logger, "mail-login", "ожидание пост-логин URL (до 90s)")
    deadline = asyncio.get_event_loop().time() + 90.0
    post_login_start = asyncio.get_event_loop().time()
    check_iteration = 0

    while asyncio.get_event_loop().time() < deadline:
        check_iteration += 1
        if _is_post_login_url(page.url):
            elapsed = asyncio.get_event_loop().time() - post_login_start
            logger.info(f"[mail-login] SUCCESS: пост-логин URL достигнут за {elapsed:.1f}s")
            return
        for marker in ("Входящие", "Inbox"):
            try:
                m = page.locator(f"text={marker}").first
                if await m.is_visible():
                    elapsed = asyncio.get_event_loop().time() - post_login_start
                    logger.info(f"[mail-login] SUCCESS: маркер '{marker}' найден за {elapsed:.1f}s")
                    return
            except Exception:
                pass

        # Логировать каждые 10 секунд
        if check_iteration % 40 == 1:
            elapsed = asyncio.get_event_loop().time() - post_login_start
            remaining = deadline - asyncio.get_event_loop().time()
            logger.debug(f"[mail-login] ожидание {elapsed:.1f}s, осталось {remaining:.1f}s, URL: {page.url[:60]}")

        await asyncio.sleep(0.25)

    elapsed = asyncio.get_event_loop().time() - post_login_start
    logger.error(f"[mail-login] TIMEOUT: всё ещё на login page после {elapsed:.1f}s, URL: {page.url}")
    raise StepError("login failed: still on login page after 90s")


# ---------------------------------------------------------------------------
# Ожидание нового письма Devin и извлечение кода (async)
# ---------------------------------------------------------------------------


@log_timing
async def wait_for_devin_email_code_async(
    mail_page: Page,
    *,
    baseline_count: int = 0,
    total_timeout_ms: int = 300_000,
    poll_ms: int = 5_000,
) -> str:
    """Поллить inbox, пока не появится письмо от Devin с 6-значным кодом.

    Универсальная логика, работающая с любым mail-UI:
    1. Каждые ``poll_ms`` обновляем inbox (клик кнопки refresh / reload).
    2. Ищем САМЫЙ ВЕРХНИЙ visible элемент со словом "Devin Login Code"
       или "Your Devin" — это всегда самое свежее письмо (rainloop +
       PinMX-mail оба сортируют по дате убывания).
    3. Кликаем по элементу, читаем тело, извлекаем 6 цифр.

    Если найдено старое письмо (baseline_count хранит count до запроса
    кода) — это нормально, при возможности перезапросим клик и body
    через несколько секунд: Devin отправляет код 5-15 секунд после
    клика «Log in», и сначала его в inbox нет.

    Возвращает 6 цифр, или ``StepError`` после ``total_timeout_ms``.
    """
    logger = logging.getLogger(__name__)
    log_step(logger, "wait-email", f"baseline={baseline_count}, timeout={total_timeout_ms}ms, poll={poll_ms}ms")

    deadline = asyncio.get_event_loop().time() + total_timeout_ms / 1000.0

    # Селекторы строки письма. Главный — `.messageListItem.unseen` —
    # непрочитанное письмо (rainloop добавляет .unseen сразу после
    # прихода). После клика .unseen снимается — это удобно для
    # "не брать одно и то же письмо повторно".
    unseen_selector = ".messageListItem.unseen"
    # Если новых нет — fallback на все письма (берём верхнее с Devin).
    all_messages_selectors = (
        ".messageListItem",
        ".messageListPlace .messageListItem",
        '[class*="messageListItem"]',
    )

    # Ключевые фразы для поиска письма Devin.
    devin_phrases = (
        "Your Devin Login Code",
        "Devin Login Code",
        "Devin",
        "no-reply@cognition.ai",
        "verification code",
        "verify your",
        "your code",
    )

    last_attempted_code: str | None = None
    iteration = 0
    last_inbox_count = 0
    stale_iterations = 0  # Счётчик итераций без изменений

    while asyncio.get_event_loop().time() < deadline:
        iteration += 1
        remaining_time = deadline - asyncio.get_event_loop().time()

        if iteration % 10 == 1:
            logger.debug(f"[wait-email] iter {iteration}, remaining {remaining_time:.1f}s")

        # Refresh inbox.
        refresh_method = "none"
        try:
            await mail_page.click(MAIL_REFRESH_BUTTON_SELECTOR, timeout=2_000)
            refresh_method = "button"
        except Exception:
            try:
                for label in ("Обновить", "Refresh", "Обновить Список Писем"):
                    btn = mail_page.get_by_role(
                        "button", name=label, exact=False
                    ).first
                    try:
                        if await btn.is_visible(timeout=400):
                            await btn.click(timeout=2_000)
                            refresh_method = f"role-button:{label}"
                            break
                    except Exception:
                        continue
                else:
                    await mail_page.reload(wait_until="domcontentloaded")
                    refresh_method = "reload"
            except Exception as reload_exc:
                logger.warning(f"[wait-email] iter {iteration}: refresh failed: {reload_exc}")
                refresh_method = "failed"

        if iteration % 10 == 1:
            logger.debug(f"[wait-email] iter {iteration}: refresh method={refresh_method}")

        await asyncio.sleep(2.0)

        # Шаг 1: ищем НЕПРОЧИТАННОЕ письмо от Devin.
        target = None
        try:
            unseen = await mail_page.locator(unseen_selector).all()
        except Exception as e:
            logger.debug(f"[wait-email] iter {iteration}: не удалось получить unseen: {e}")
            unseen = []

        # Подсчитать общее количество писем для отслеживания застревания
        try:
            all_items = await mail_page.locator(MAIL_LIST_ITEM_SELECTOR).all()
            current_inbox_count = len(all_items)
        except Exception:
            current_inbox_count = 0

        if current_inbox_count == last_inbox_count:
            stale_iterations += 1
        else:
            stale_iterations = 0
            last_inbox_count = current_inbox_count

        if iteration % 10 == 1:
            logger.debug(f"[wait-email] iter {iteration}: inbox_count={current_inbox_count}, "
                        f"unseen={len(unseen)}, stale_iters={stale_iterations}")

        # Если inbox не меняется 15 итераций подряд (30 секунд) — возможно застряли
        if stale_iterations >= 15:
            logger.warning(f"[wait-email] iter {iteration}: inbox не меняется {stale_iterations} итераций, "
                          "пробую reload страницы")
            try:
                await mail_page.reload(wait_until="domcontentloaded")
                stale_iterations = 0
                await asyncio.sleep(2.0)
            except Exception as e:
                logger.error(f"[wait-email] iter {iteration}: reload failed: {e}")

        for item in unseen:
            try:
                txt = (await item.inner_text(timeout=400)).lower()
            except Exception:
                continue
            if any(p.lower() in txt for p in devin_phrases):
                target = item
                logger.info(f"[wait-email] iter {iteration}: нашёл unseen Devin письмо")
                break

        # Если непрочитанных нет — fallback на любое верхнее с Devin.
        if target is None:
            for sel in all_messages_selectors:
                try:
                    items = await mail_page.locator(sel).all()
                except Exception:
                    items = []
                if not items:
                    continue
                for item in items:
                    try:
                        txt = (await item.inner_text(timeout=400)).lower()
                    except Exception:
                        continue
                    if any(p.lower() in txt for p in devin_phrases):
                        target = item
                        if iteration % 4 == 1:
                            logger.debug(f"[wait-email] iter {iteration}: unseen нет, "
                                        "беру верхнее Devin (может быть старое)")
                        break
                if target is not None:
                    break

        if target is None:
            if iteration % 4 == 1:
                logger.debug(f"[wait-email] iter {iteration}: писем от Devin не видно "
                            f"(URL: {mail_page.url[:60]})")
            await asyncio.sleep(poll_ms / 1000.0)
            continue

        # Шаг 2: кликаем по строке (3 попытки на случай detached).
        click_ok = False
        click_error = None
        for click_attempt in range(3):
            try:
                await target.click(timeout=_MAIL_CLICK_TIMEOUT_MS)
                click_ok = True
                logger.debug(f"[wait-email] iter {iteration}: клик по письму успешен (попытка {click_attempt + 1})")
                break
            except (PWTimeout, Exception) as e:
                click_error = e
                await asyncio.sleep(0.4)

        if not click_ok:
            logger.warning(f"[wait-email] iter {iteration}: не удалось кликнуть по письму после 3 попыток: {click_error}")
            await asyncio.sleep(poll_ms / 1000.0)
            continue

        # Шаг 3: читаем тело письма.
        await asyncio.sleep(0.6)
        body_text = ""
        body_source = ""
        try:
            body_text = await mail_page.locator(
                MAIL_DETAIL_BODY_SELECTOR
            ).first.inner_text(timeout=_MAIL_BODY_READ_TIMEOUT_MS)
            if body_text:
                body_source = "primary"
        except (PWTimeout, Exception) as e:
            logger.debug(f"[wait-email] iter {iteration}: primary selector failed: {e}")
            body_text = ""

        if not body_text:
            for fb in MAIL_DETAIL_BODY_FALLBACK_SELECTORS:
                try:
                    if fb == ".messageView iframe":
                        body_text = await mail_page.frame_locator(
                            fb
                        ).first.locator("body").inner_text(
                            timeout=_MAIL_BODY_READ_TIMEOUT_MS
                        )
                    else:
                        body_text = await mail_page.locator(
                            fb
                        ).first.inner_text(
                            timeout=_MAIL_BODY_READ_TIMEOUT_MS
                        )
                except (PWTimeout, Exception):
                    body_text = ""
                if body_text:
                    body_source = f"fallback={fb}"
                    logger.debug(f"[wait-email] iter {iteration}: body read via {body_source}")
                    break

        # Финальный fallback — взять весь body страницы.
        if not body_text:
            try:
                body_text = await mail_page.locator("body").inner_text(timeout=2_000)
                body_source = "page-body"
                logger.debug(f"[wait-email] iter {iteration}: body read via page-body fallback")
            except Exception as e:
                logger.warning(f"[wait-email] iter {iteration}: все попытки чтения body failed: {e}")
                body_text = ""

        code = extract_code(body_text)
        logger.info(f"[wait-email] iter {iteration}: body src={body_source!r}, "
                   f"length={len(body_text)}, code={code!r}")

        if code is not None:
            # Защита: если это **тот же** код что мы уже пробовали —
            # это старое письмо. Ждём ещё.
            if code == last_attempted_code:
                logger.info(f"[wait-email] iter {iteration}: код {code} уже видели — жду свежий")
                await asyncio.sleep(poll_ms / 1000.0)
                continue
            last_attempted_code = code
            logger.info(f"[wait-email] SUCCESS: получен код {code} на итерации {iteration}")
            return code

        await asyncio.sleep(poll_ms / 1000.0)

    logger.error(f"[wait-email] TIMEOUT после {iteration} итераций")
    raise StepError("verification email timeout")


# ---------------------------------------------------------------------------
# Submit code в Devin (async)
# ---------------------------------------------------------------------------


async def submit_devin_code_async(devin_page: Page, code: str) -> None:
    """Заполнить input one-time-code; auto-submit Devin отправит код сам."""
    inp = devin_page.locator(_DEVIN_CODE_INPUT_SELECTOR)
    try:
        await inp.fill(code)
    except (PWTimeout, Exception) as exc:
        raise StepError(f"submit code failed: cannot fill input: {exc}") from exc

    deadline = asyncio.get_event_loop().time() + _DEVIN_POST_SUBMIT_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        try:
            url = devin_page.url
        except Exception:
            url = ""
        if url and "/auth/" not in url:
            return

        try:
            body_text = (
                await devin_page.locator("body").inner_text(
                    timeout=_DEVIN_BODY_READ_TIMEOUT_MS
                )
            ).lower()
        except (PWTimeout, Exception):
            body_text = ""
        for phrase in _DEVIN_INVALID_CODE_PHRASES:
            if phrase in body_text:
                raise InvalidCodeError(f"Devin rejected code: phrase '{phrase}' on page")

        await asyncio.sleep(0.5)

    raise StepError("submit code failed: still on /auth/* after 30s")


# ---------------------------------------------------------------------------
# Login в Devin: full flow
# ---------------------------------------------------------------------------


async def login_to_devin_async(devin_page: Page, mail_page: Page, account: Account) -> str:
    """Логин в Devin: open /auth/login → email → ждём НОВОЕ письмо →
    код → ждём смены URL с /auth/. Возвращает финальный URL.

    Если бэкенд Devin вернул 502 / другую серверную ошибку, делаем
    до 3 попыток с задержкой 30с между ними.
    """
    last_err: Exception | None = None
    for outer_attempt in range(3):
        try:
            return await _login_to_devin_async_once(
                devin_page, mail_page, account
            )
        except StepError as exc:
            last_err = exc
            msg = str(exc)
            transient = (
                "502" in msg
                or "503" in msg
                or "504" in msg
                or "Request failed" in msg
                or "code 5" in msg
            )
            if not transient or outer_attempt == 2:
                raise
            print(
                f"[devin-async] login attempt {outer_attempt + 1}/3 "
                f"got transient: {exc}"
            )
            print("[devin-async] жду 30с перед retry...")
            await asyncio.sleep(30.0)
    if last_err is not None:
        raise last_err
    raise StepError("login_to_devin_async: unreachable")


async def _login_to_devin_async_once(
    devin_page: Page, mail_page: Page, account: Account
) -> str:
    """Одна попытка логина в Devin (без внешнего retry)."""
    # Перед проверкой baseline убедимся, что mail-page реально на
    # странице inbox (а не остался на login). Если mail-login упал —
    # baseline=0 и потом мы можем подхватить старое письмо.
    cur_url = mail_page.url
    if "/mail/" not in cur_url and "#/inbox" not in cur_url:
        # Mail не залогинен — пробуем повторно.
        print(f"[devin-async] mail_page не на inbox (url={cur_url[:60]}), "
              "повторяю логин...")
        try:
            await login_to_mailclient_async(mail_page, account)
        except StepError as exc:
            raise StepError(
                f"login to mail failed before Devin: {exc}"
            ) from exc

    try:
        baseline = await mail_page.locator(MAIL_LIST_ITEM_SELECTOR).count()
    except Exception:
        baseline = 0

    # Если baseline=0, но mail-login прошёл (URL содержит /mail/) —
    # rainloop мог ещё не отрисовать список. Подождём до 5с и переснимем.
    if baseline == 0:
        for _ in range(10):
            await asyncio.sleep(0.5)
            try:
                baseline = await mail_page.locator(MAIL_LIST_ITEM_SELECTOR).count()
            except Exception:
                continue
            if baseline > 0:
                break

    print(f"[devin-async] baseline писем в inbox: {baseline}")

    await devin_page.goto(DEVIN_LOGIN_URL, wait_until="domcontentloaded")
    email_field = devin_page.get_by_role("textbox", name="Email address")
    try:
        await email_field.wait_for(state="visible", timeout=30_000)
    except PWTimeout as exc:
        raise StepError("login failed: email field not visible") from exc
    await email_field.fill(account.email)
    # Click может зависнуть в Camoufox+humanize — сначала пробуем
    # обычный click с коротким таймаутом, fallback на Enter в email-поле.
    try:
        await devin_page.get_by_role(
            "button", name="Log in", exact=True
        ).click(timeout=5_000)
    except (PWTimeout, Exception) as exc:
        print(f"[devin-async] click 'Log in' failed ({exc}), пробую Enter")
        try:
            await email_field.press("Enter")
        except Exception:
            # Last resort — submit формы через JS.
            try:
                await devin_page.evaluate(
                    "() => { const f = document.querySelector('form'); "
                    "if (f) f.submit(); }"
                )
            except Exception:
                raise StepError(
                    f"failed to submit Devin email form: {exc}"
                )
    print("[devin-async] клик 'Log in', жду появление поля кода...")

    # КРИТИЧНО: дожидаемся, что Devin переключился на экран ввода кода,
    # ДО того как уйдём ждать письмо. Иначе если страница задержалась
    # (или код пришёл моментально), submit_devin_code_async упадёт по
    # таймауту fill().
    try:
        await devin_page.wait_for_selector(
            _DEVIN_CODE_INPUT_SELECTOR, timeout=30_000, state="visible"
        )
        print(f"[devin-async] поле кода готово, URL: {devin_page.url}")
    except PWTimeout:
        # Запишем в чём дело и упадём с понятной ошибкой.
        try:
            cur_url = devin_page.url
        except Exception:
            cur_url = "?"
        try:
            body_snippet = (
                await devin_page.locator("body").inner_text(timeout=2_000)
            )[:300]
        except Exception:
            body_snippet = "?"
        raise StepError(
            f"login failed: после Log in поле кода не появилось за 30с. "
            f"URL={cur_url!r}, body[:300]={body_snippet!r}"
        )

    code = await wait_for_devin_email_code_async(mail_page, baseline_count=baseline)
    print(f"[devin-async] получили код {code}")

    await submit_devin_code_async(devin_page, code)

    # Ждём пост-логин URL.
    deadline = asyncio.get_event_loop().time() + 30.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            url = devin_page.url
        except Exception:
            url = ""
        if url and "/auth/" not in url:
            print(f"[devin-async] залогинены, URL: {url}")
            return url
        await asyncio.sleep(0.4)
    raise StepError(f"login outcome unclear: {devin_page.url}")


# ---------------------------------------------------------------------------
# Identity helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    full_name: str
    street: str
    zip_code: str
    city: str


def find_identity_for_email(email: str) -> Identity | None:
    if not IDENTITIES_PATH.exists():
        return None
    target = email.lower()
    for raw in IDENTITIES_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#") or "\t" not in line:
            continue
        e, identity_str = line.split("\t", 1)
        if e.strip().lower() != target:
            continue
        parts = [p.strip() for p in identity_str.strip().split(",")]
        if len(parts) != 4:
            return None
        full_name, street, zip_code, city = parts
        if not (full_name and street and zip_code and city):
            return None
        return Identity(full_name=full_name, street=street, zip_code=zip_code, city=city)
    return None


# ---------------------------------------------------------------------------
# UI: My Team → Upgrade → Start free trial → ввод адреса в Stripe
# ---------------------------------------------------------------------------


async def _click_first_match_async(
    page: Page, labels: tuple[str, ...], *, timeout_ms: int = 10_000
) -> bool:
    for label in labels:
        for role in ("link", "button"):
            try:
                loc = page.get_by_role(role, name=label, exact=True).first
                if await loc.is_visible(timeout=2_000):
                    print(f"  [ui] click role={role!r} name={label!r}")
                    await loc.click(timeout=timeout_ms)
                    return True
            except Exception:
                continue
        try:
            loc = page.get_by_text(label, exact=True).first
            if await loc.is_visible(timeout=2_000):
                print(f"  [ui] click text={label!r}")
                await loc.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


async def _wait_for_any_label_async(
    page: Page, labels: tuple[str, ...], *, timeout_s: float = 20.0
) -> bool:
    """Подождать, пока на странице появится любой из ``labels`` (как
    видимый link/button/text). Используется перед первым кликом по
    сайдбару Devin: после login UI рендерится не моментально."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        for label in labels:
            for role in ("link", "button"):
                try:
                    loc = page.get_by_role(role, name=label, exact=True).first
                    if await loc.is_visible(timeout=500):
                        return True
                except Exception:
                    continue
            try:
                loc = page.get_by_text(label, exact=True).first
                if await loc.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return False


async def go_to_plans_and_start_trial_async(devin_page: Page) -> None:
    print("[ui-async] жду отрисовки сайдбара Devin (до 20с)...")
    if not await _wait_for_any_label_async(
        devin_page, ("My Team", "Team"), timeout_s=20.0
    ):
        # Не упали — может, Devin сразу открыл меню. Дадим ещё 2 сек на UI.
        await asyncio.sleep(2.0)

    print('[ui-async] click "My Team"')
    if not await _click_first_match_async(devin_page, ("My Team", "Team")):
        raise StepError("UI: не нашёл «My Team»")
    await devin_page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(1.5)

    print('[ui-async] click "Upgrade"')
    if not await _click_first_match_async(devin_page, ("Upgrade",)):
        raise StepError("UI: не нашёл «Upgrade»")
    await devin_page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(2.0)

    print(f'[ui-async] на странице планов: {devin_page.url}')

    plan_labels = (
        "Start free trial",
        "Start Free Trial",
        "Start trial",
        "Subscribe",
        "Continue",
        "Get started",
        "Upgrade",
    )
    print(f'[ui-async] click первая из {plan_labels}')
    if not await _click_first_match_async(devin_page, plan_labels):
        raise StepError("UI: не нашёл кнопку начала trial на странице планов")
    await asyncio.sleep(2.5)


async def find_stripe_checkout_frame_async(devin_page: Page):
    for fr in devin_page.frames:
        if fr.url.startswith(_CFG_STRIPE_PREFIX):
            return fr
    return None


async def select_card_payment_method_async(stripe_frame) -> bool:
    """Раскрыть «Оплатить картой» в Stripe Checkout.

    Сначала пробуем button[data-testid=card-accordion-item-button]
    (самый надёжный путь). Если её нет — fallback на radio
    payment-method-accordion-item-title (ищем тот, в котором текст
    «Карта»/«Card»/Visa-иконка).
    """
    selectors = (
        'button[data-testid="card-accordion-item-button"]',
        'button[aria-label="Оплатить картой"]',
        'button[aria-label="Pay with card"]',
        'button[aria-label*="card" i][aria-label*="pay" i]',
    )
    for selector in selectors:
        try:
            btn = stripe_frame.locator(selector).first
            if await btn.is_visible(timeout=1_500):
                print(f"[stripe-async] раскрываю Card-accordion: {selector}")
                await btn.click(timeout=5_000)
                return True
        except Exception:
            continue

    # Fallback 1: текст «Карта»/«Card» в accordion-item.
    print("[stripe-async] card-accordion-button не нашёлся, fallback на текст")
    for label in ("Карта", "Card", "Bank card", "Платёжная карта"):
        try:
            loc = stripe_frame.get_by_text(label, exact=False).first
            if await loc.is_visible(timeout=1_000):
                print(f"  [stripe-async] click text={label!r}")
                await loc.click(timeout=5_000, force=True)
                return True
        except Exception:
            continue

    # Fallback 2: radio. У Stripe обычно первая (после Apple/Google Pay).
    print("[stripe-async] fallback на radio")
    try:
        radios = await stripe_frame.locator(
            'input[name="payment-method-accordion-item-title"]'
        ).all()
        for r in radios:
            try:
                # check() умеет force-clickать через label.
                await r.check(timeout=3_000, force=True)
                print("[stripe-async] кликнул первый radio")
                return True
            except Exception:
                continue
    except Exception as exc:
        print(f"[stripe-async] не получилось прочитать radios: {exc}")

    return False


async def fill_address_async(stripe_frame, identity: Identity) -> None:
    """Заполнить ТОЛЬКО адрес и имя держателя — БЕЗ карты.

    Поллим селекторы адреса до 20с — Stripe Checkout рендерит форму в
    несколько XHR'ов, и сразу после раскрытия Card-accordion инпута
    может ещё не быть в DOM.
    """
    print()
    print("=" * 60)
    print(" Заполнение адреса в Stripe Checkout")
    print("=" * 60)

    address_selectors = (
        'input[autocomplete="street-address"]',
        'input[autocomplete="address-line1"]',
        'input[autocomplete*="address"]',
        'input[name="billingAddressLine1"]',
        'input[name*="address" i][type="text"]',
        'input[placeholder*="Адрес" i]',
        'input[placeholder*="Address" i]',
    )

    address_input = None
    address_deadline = asyncio.get_event_loop().time() + 20.0
    while asyncio.get_event_loop().time() < address_deadline:
        for sel in address_selectors:
            try:
                loc = stripe_frame.locator(sel).first
                if await loc.is_visible(timeout=500):
                    address_input = loc
                    print(f"[stripe-async] адрес-инпут найден: {sel}")
                    break
            except Exception:
                continue
        if address_input is not None:
            break
        await asyncio.sleep(0.5)

    if address_input is None:
        # Дамп для диагностики.
        try:
            url = stripe_frame.url
        except Exception:
            url = "?"
        try:
            inputs = await stripe_frame.locator("input").all()
            attrs = []
            for inp in inputs[:30]:
                try:
                    a_name = await inp.get_attribute("name")
                    a_ph = await inp.get_attribute("placeholder")
                    a_ac = await inp.get_attribute("autocomplete")
                    a_t = await inp.get_attribute("type")
                    attrs.append(
                        f"  type={a_t!r} name={a_name!r} ac={a_ac!r} ph={a_ph!r}"
                    )
                except Exception:
                    attrs.append("  <bad-input>")
            print(f"[stripe-async] DOM frame={url[:80]!r}, inputs:\n" + "\n".join(attrs))
        except Exception as exc:
            print(f"[stripe-async] не удалось распечатать inputs: {exc}")
        raise StepError("Stripe: не найден инпут адреса")

    print(f"[stripe-async] печатаю улицу: {identity.street!r}")
    await address_input.click()
    await address_input.fill("")
    await address_input.type(identity.street, delay=80)
    await asyncio.sleep(1.5)

    suggestion_clicked = False
    for sel in (
        '[role="option"]',
        '[role="listbox"] li',
        'ul[role="listbox"] li',
        '[data-testid*="suggestion"]',
        'li[id^="downshift"]',
    ):
        try:
            items = await stripe_frame.locator(sel).all()
        except Exception:
            continue
        for it in items:
            try:
                if not await it.is_visible():
                    continue
                await it.click(timeout=2_000)
                print(f"[stripe-async] кликнул suggestion ({sel})")
                suggestion_clicked = True
                break
            except Exception:
                continue
        if suggestion_clicked:
            break

    if not suggestion_clicked:
        print("[stripe-async] suggestion не нашёлся — продолжаем")

    await asyncio.sleep(1.5)

    # Имя держателя карты (если такое поле есть на этой стадии).
    for sel in (
        'input[autocomplete="cc-name"]',
        'input[name="cardholderName" i]',
        'input[placeholder*="имя" i]',
        'input[placeholder*="name on card" i]',
    ):
        try:
            loc = stripe_frame.locator(sel).first
            if await loc.is_visible(timeout=1_000):
                print(f"[stripe-async] имя держателя: {sel}")
                await loc.fill(identity.full_name)
                break
        except Exception:
            continue


async def open_devin_trial_with_address(
    context: BrowserContext,
    *,
    account: Account,
    identity: Identity,
) -> Page:
    """Полный flow: 2 вкладки (mail + devin), логин в почту, логин в
    Devin, переход к Stripe, заполнение адреса.

    Возвращает devin-вкладку (на ней открыт Stripe).
    """
    mail_page = await context.new_page()
    devin_page = await context.new_page()

    print(f"[trial-async] аккаунт: {account.email}")
    print(f"[trial-async] identity: {identity}")

    print("[trial-async] логин в mail-client.pinmx.com...")
    await login_to_mailclient_async(mail_page, account)
    print("[trial-async] логин в mail-client OK")

    await login_to_devin_async(devin_page, mail_page, account)

    try:
        await devin_page.bring_to_front()
    except Exception:
        pass

    await go_to_plans_and_start_trial_async(devin_page)

    await asyncio.sleep(2.5)
    stripe_frame = await find_stripe_checkout_frame_async(devin_page)
    if stripe_frame is None:
        raise StepError("Stripe Checkout iframe не найден")
    print(f"[trial-async] Stripe iframe: {stripe_frame.url[:80]}...")

    await select_card_payment_method_async(stripe_frame)
    # Дать Stripe время отрисовать форму после клика на Card.
    await asyncio.sleep(3.0)

    await fill_address_async(stripe_frame, identity)

    print("[trial-async] адрес введён, Stripe Checkout готов к карте")
    return devin_page
