"""activate_accounts: низкоуровневые async-helpers для Stripe Checkout
и hCaptcha при активации Devin-триала.

Этот модуль не запускает свой собственный pipeline — его функции
вызываются из ``activate_trials.activate_single_account`` (массовый
flow) и из ``test_hcaptcha.py`` (dev-итерация по hCaptcha с
сохранённым storage_state).

Что экспортируется
==================

* :func:`fill_card_async` — впечатать карту (``NUMBER|MM|YYYY|CVV``) и
  имя держателя в Stripe Checkout. Поля Stripe рендерятся через
  ElementsApp, поэтому название/имя селектора может варьироваться;
  helper перебирает несколько fallback'ов.

* :func:`check_consent_checkboxes_async` — проставить все видимые
  чекбоксы согласия (TOS, marketing-consent, и т.п.) перед submit'ом.
  Stripe Checkout периодически добавляет required-чекбоксы; без них
  кнопка «Подписаться» disabled.

* :func:`click_submit_async` — кликнуть финальную кнопку Stripe
  Checkout. Возвращает True/False — успешен ли клик (был ли элемент
  кликабельным).

* :func:`_try_solve_hcaptcha_async` — попытаться решить hCaptcha
  checkbox (антибот-фильтр Stripe). Возвращает строку:

  * ``"clicked"`` — checkbox прокликался, ``aria-checked=true`` или
    iframe детачнулся → флоу можно продолжать.
  * ``"none"`` — за указанный timeout iframe hcaptcha вообще не
    появился (значит, Stripe нас не попросил капчу).
  * ``"challenge"`` — после клика hCaptcha показала визуальную задачу
    (выбор картинок). Решать её мы не умеем — карта считается
    declined.
  * ``"failed"`` — кликнули checkbox, но ``aria-checked`` так и не
    стал true и iframe не детачнулся за разумный таймаут (~30с).

Все ошибки внутри helper'ов глотаются и превращаются в False/строку —
вызывающему коду нужны исходы, а не исключения, чтобы спокойно идти к
следующему аккаунту.
"""

from __future__ import annotations

import asyncio
import re
from typing import Iterable

from playwright.async_api import Page, TimeoutError as PWTimeout


# ---------------------------------------------------------------------------
# Stripe Checkout: ввод карты
# ---------------------------------------------------------------------------


# Парсим NUMBER|MM|YYYY|CVV. Принимаем и пробелы в номере карты — Stripe
# их сам отформатирует, но нам надо отдать чистые цифры.
_CARD_PATTERN = re.compile(
    r"^\s*(?P<num>[\d\s]+)\|(?P<mm>\d{1,2})\|(?P<yyyy>\d{2,4})\|(?P<cvv>\d{3,4})\s*$"
)


def _parse_card(card_str: str) -> tuple[str, str, str, str]:
    """Распарсить ``NUMBER|MM|YYYY|CVV`` → ``(number, mm, yy, cvv)``.

    Год нормализуется к 2 цифрам (Stripe Checkout поле ``MM / YY``).
    Номер карты — без пробелов.
    """
    m = _CARD_PATTERN.match(card_str)
    if not m:
        raise ValueError(
            f"карта не в формате NUMBER|MM|YYYY|CVV: {card_str!r}"
        )
    number = re.sub(r"\s+", "", m.group("num"))
    mm = m.group("mm").zfill(2)
    yyyy = m.group("yyyy")
    yy = yyyy[-2:] if len(yyyy) == 4 else yyyy.zfill(2)
    cvv = m.group("cvv")
    return number, mm, yy, cvv


# Селекторы карточных полей в Stripe Checkout. Имена ``card*`` —
# актуальная разметка ElementsApp (2024+); ``number/expiry/cvc/name`` —
# историческая. Порядок важен: первая видимая локатор побеждает.
_CARD_NUMBER_SELECTORS = (
    'input[name="cardNumber"]',
    'input[autocomplete="cc-number"]',
    'input[name="number"]',
    'input[placeholder*="1234 1234" i]',
)
_CARD_EXPIRY_SELECTORS = (
    'input[name="cardExpiry"]',
    'input[autocomplete="cc-exp"]',
    'input[name="expiry"]',
    'input[placeholder*="MM" i][placeholder*="YY" i]',
)
_CARD_CVC_SELECTORS = (
    'input[name="cardCvc"]',
    'input[autocomplete="cc-csc"]',
    'input[name="cvc"]',
    'input[placeholder*="CVC" i]',
)
_CARD_NAME_SELECTORS = (
    'input[name="billingName"]',
    'input[autocomplete="cc-name"]',
    'input[name="cardholderName"]',
    'input[name="name"]',
    'input[placeholder*="имя" i]',
    'input[placeholder*="name on card" i]',
)


async def _first_visible(stripe_frame, selectors: Iterable[str], *, timeout_ms: int = 1_500):
    """Найти первый видимый локатор из ``selectors``, или вернуть None."""
    for sel in selectors:
        try:
            loc = stripe_frame.locator(sel).first
            if await loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:
            continue
    return None


async def _fill_with_typing(loc, value: str, *, delay_ms: int = 40) -> None:
    """Очистить инпут и впечатать значение по символу — Stripe-инпуты
    часто реагируют на ввод hint-форматирования (пробелы каждые 4
    цифры), и обычный ``fill`` иногда не триггерит правильно."""
    try:
        await loc.click()
    except Exception:
        pass
    try:
        await loc.fill("")
    except Exception:
        pass
    await loc.type(value, delay=delay_ms)


async def fill_card_async(
    stripe_frame, card_str: str, holder_name: str
) -> None:
    """Заполнить поля карты в Stripe Checkout.

    Args:
        stripe_frame: iframe checkout.stripe.com/c/pay/... (получается
            из :func:`devin_async.find_stripe_checkout_frame_async`).
            Перед вызовом адрес и accordion «Card» должны быть уже
            раскрыты — см. ``activate_trials.activate_single_account``.
        card_str: карта в формате ``NUMBER|MM|YYYY|CVV``.
        holder_name: ``identity.full_name`` для поля cardholder.

    Raises:
        ValueError: если ``card_str`` не парсится.
        RuntimeError: если не нашли поля карты в Stripe-фрейме (значит,
            раскрытие Card-accordion не сработало).
    """
    number, mm, yy, cvv = _parse_card(card_str)
    print(f"[stripe-async] заполняю карту {number[:6]}...{number[-3:]} {mm}/{yy}")

    number_input = await _first_visible(stripe_frame, _CARD_NUMBER_SELECTORS, timeout_ms=3_000)
    if number_input is None:
        raise RuntimeError("Stripe: не найдено поле номера карты")
    await _fill_with_typing(number_input, number)

    expiry_input = await _first_visible(stripe_frame, _CARD_EXPIRY_SELECTORS)
    if expiry_input is None:
        raise RuntimeError("Stripe: не найдено поле expiry")
    await _fill_with_typing(expiry_input, f"{mm}{yy}")

    cvc_input = await _first_visible(stripe_frame, _CARD_CVC_SELECTORS)
    if cvc_input is None:
        raise RuntimeError("Stripe: не найдено поле CVC")
    await _fill_with_typing(cvc_input, cvv)

    name_input = await _first_visible(stripe_frame, _CARD_NAME_SELECTORS)
    if name_input is not None:
        await _fill_with_typing(name_input, holder_name)
    else:
        # Поле имени держателя в новой разметке Stripe Checkout
        # необязательное — не падаем, только лог.
        print("[stripe-async] поле имени держателя не найдено — пропускаю")


# ---------------------------------------------------------------------------
# Stripe Checkout: чекбоксы согласия + кнопка submit
# ---------------------------------------------------------------------------


async def check_consent_checkboxes_async(stripe_frame) -> int:
    """Проставить все видимые чекбоксы согласия в Stripe Checkout.

    Stripe иногда требует TOS/marketing-checkbox перед активацией кнопки
    Submit. Атрибутов хватает разных (``role=checkbox``,
    ``type=checkbox``, ``aria-checked=false``), поэтому идём по
    нескольким локаторам.

    Returns:
        Сколько чекбоксов поставили (0 — нормально, если их вообще нет).
    """
    candidates = (
        'input[type="checkbox"]:not(:disabled)',
        '[role="checkbox"][aria-checked="false"]',
    )
    checked = 0
    for sel in candidates:
        try:
            boxes = await stripe_frame.locator(sel).all()
        except Exception:
            continue
        for box in boxes:
            try:
                if not await box.is_visible():
                    continue
                already = False
                try:
                    already = bool(await box.is_checked())
                except Exception:
                    aria = await box.get_attribute("aria-checked")
                    already = (aria == "true")
                if already:
                    continue
                await box.check(timeout=2_500, force=True)
                checked += 1
            except Exception:
                continue
    if checked:
        print(f"[stripe-async] проставил {checked} чекбокс(ов) согласия")
    return checked


_SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'button.SubmitButton',
    '[data-testid="hosted-payment-submit-button"]',
)
_SUBMIT_LABELS = (
    "Подписаться",
    "Subscribe",
    "Pay",
    "Start trial",
    "Start free trial",
    "Подтвердить",
)


async def click_submit_async(stripe_frame) -> bool:
    """Кликнуть кнопку финального submit'а в Stripe Checkout.

    Сначала пробуем по селекторам (``button[type="submit"]`` и т.п.),
    потом — по подписи. Возвращает True если клик произошёл.
    """
    for sel in _SUBMIT_SELECTORS:
        try:
            loc = stripe_frame.locator(sel).first
            if await loc.is_visible(timeout=2_000):
                if await loc.is_enabled():
                    print(f"[stripe-async] click submit ({sel})")
                    await loc.click(timeout=8_000)
                    return True
                else:
                    print(f"[stripe-async] submit ({sel}) disabled — продолжаю поиск")
        except Exception:
            continue

    # Fallback по тексту кнопки.
    for label in _SUBMIT_LABELS:
        try:
            loc = stripe_frame.get_by_role("button", name=label, exact=False).first
            if await loc.is_visible(timeout=1_500):
                if await loc.is_enabled():
                    print(f"[stripe-async] click submit by label={label!r}")
                    await loc.click(timeout=8_000)
                    return True
        except Exception:
            continue

    return False


# ---------------------------------------------------------------------------
# hCaptcha checkbox solver
# ---------------------------------------------------------------------------


async def _try_solve_hcaptcha_async(page: Page, *, timeout_s: float = 20.0) -> str:
    """Попробовать решить hCaptcha-checkbox после submit'а Stripe.

    Алгоритм:
    1. Ждать iframe ``hcaptcha.com`` до ``timeout_s`` секунд. Если не
       появился — возвращаем ``"none"`` (значит, нас не попросили).
    2. Внутри iframe найти ``#checkbox`` и кликнуть его. Camoufox в
       persistent_context с ``humanize=True`` имитирует движение мыши,
       поэтому checkbox-only проход срабатывает в большинстве случаев.
    3. Подождать до 30с, пока ``aria-checked='true'`` (успех) **или**
       iframe детачнется (на «успех» hCaptcha вообще убирает iframe).
       Параллельно следим, не появилась ли визуальная задача (новый
       iframe ``hcaptcha-challenge``) — это означает, что нас отправили
       выбирать картинки, и для нашего пайплайна это считается failure.
    4. Если ни одно из условий не сработало — ``"failed"``.
    """
    # Шаг 1: ждём чекбокс-iframe.
    deadline = asyncio.get_event_loop().time() + timeout_s
    checkbox_frame = None
    while asyncio.get_event_loop().time() < deadline:
        for fr in page.frames:
            url = fr.url or ""
            if "hcaptcha.com" in url and "challenge" not in url and "frame=checkbox" in url:
                checkbox_frame = fr
                break
            if "hcaptcha.com" in url and "checkbox" in url:
                checkbox_frame = fr
                break
        if checkbox_frame is None:
            # Fallback: любой hcaptcha-фрейм, если frame=checkbox ещё не
            # выставлен в URL.
            for fr in page.frames:
                url = fr.url or ""
                if "newassets.hcaptcha.com" in url or url.startswith("https://newassets.hcaptcha.com/"):
                    checkbox_frame = fr
                    break
        if checkbox_frame is not None:
            break
        await asyncio.sleep(0.5)

    if checkbox_frame is None:
        return "none"

    print(f"[hcaptcha] iframe найден: {checkbox_frame.url[:80]!r}")

    # Шаг 2: кликнуть #checkbox.
    try:
        cb = checkbox_frame.locator("#checkbox").first
        await cb.wait_for(state="visible", timeout=5_000)
        await cb.click(timeout=5_000)
        print("[hcaptcha] кликнул #checkbox")
    except (PWTimeout, Exception) as exc:
        print(f"[hcaptcha] клик #checkbox упал: {exc!r}")
        return "failed"

    # Шаг 3: ждём успех / детач / появление challenge-iframe.
    success_deadline = asyncio.get_event_loop().time() + 30.0
    while asyncio.get_event_loop().time() < success_deadline:
        # Появилась визуальная задача?
        for fr in page.frames:
            url = fr.url or ""
            if "hcaptcha.com" in url and "challenge" in url:
                print(f"[hcaptcha] показана визуальная задача: {url[:80]!r}")
                return "challenge"

        # iframe чекбокса детачнулся?
        if checkbox_frame.is_detached():
            print("[hcaptcha] iframe чекбокса детачнулся — успех")
            return "clicked"

        # aria-checked=true?
        try:
            cb = checkbox_frame.locator("#checkbox").first
            aria = await cb.get_attribute("aria-checked")
            if aria == "true":
                print("[hcaptcha] aria-checked=true — успех")
                return "clicked"
        except Exception:
            # Iframe мог детачнуться между итерациями — следующий цикл
            # это поймает через is_detached().
            pass

        await asyncio.sleep(0.5)

    return "failed"
