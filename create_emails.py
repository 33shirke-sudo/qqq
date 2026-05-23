"""Массовая регистрация почтовых ящиков на pinmx.com (@pingmx.com).

Берёт ники из 'имена для имейлов.txt' по одному в строке и регистрирует
ящики на https://www.pinmx.com/ru. Капча решается локально через ddddocr.

Что делает скрипт:
    1. Открывает сайт.
    2. Вводит ник, переключает суффикс на @pingmx.com.
    3. Получает капчу (картинка с цифрами).
    4. Решает её через ddddocr; принимает только результат из 6 цифр.
    5. Если сайт говорит «Email Already Exists» — записывает ник в taken.txt
       и переходит к следующему.
    6. Если «Invalid Captcha Code» — обновляет капчу и пробует снова.
    7. При успехе парсит email и пароль из диалога и пишет в
       'имейлы pingmx.txt' строкой email:password.

Свойства:
    - Идемпотентен: пропускает ники, которые уже есть в
      'имейлы pingmx.txt' и в taken.txt.
    - Резистентен к перебоям: можно прервать (Ctrl+C) и запустить снова.
    - Делает резервную копию 'имейлы pingmx.txt' перед стартом
      ('имейлы pingmx.txt.bak').

Опции командной строки:
    --head            показать окно браузера (по умолчанию headless)
    --limit N         обработать только первые N ников из очереди
    --no-skip-taken   не пропускать ники из taken.txt (попробовать заново)

Зависимости:
    Python 3.11+, playwright, ddddocr.
    Установлены в .venv (см. README.md).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import ddddocr
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from browser_modes import add_browser_mode_arg, launch_browser
from logging_utils import setup_logging, log_step, log_exception, log_timing
from storage import AccountDB

ROOT = Path(__file__).parent
NICKS_PATH = ROOT / "имена для имейлов.txt"
RESULTS_PATH = ROOT / "имейлы pingmx.txt"
TAKEN_PATH = ROOT / "taken.txt"
DB_PATH = ROOT / "accounts.db"
DEBUG_DIR = ROOT / "captcha_debug"
URL = "https://www.pinmx.com/ru"
WANTED_SUFFIX = "@pingmx.com"

# Лимиты повторов в одной попытке регистрации
CAPTCHA_REFRESHES = 15        # сколько раз обновлять/решать капчу

# Лимит полных перезаходов на страницу для одного ника
RETRIES_PER_NICK = 8


# ---------------------------------------------------------------------------
# Файлы: загрузка/сохранение (legacy compatibility)
# ---------------------------------------------------------------------------

def backup_results() -> None:
    """Backup existing .txt files before migration."""
    if RESULTS_PATH.exists() and RESULTS_PATH.stat().st_size > 0:
        backup = RESULTS_PATH.with_suffix(RESULTS_PATH.suffix + ".bak")
        shutil.copy2(RESULTS_PATH, backup)


# ---------------------------------------------------------------------------
# Браузерные хелперы
# ---------------------------------------------------------------------------

def select_pingmx(page: Page) -> bool:
    chip = page.locator('.domain-options .domain-option', has_text="@pingmx.com").first
    chip.click()
    for _ in range(20):
        current = page.evaluate(
            "() => { const el = document.querySelector('.email-domain'); return el ? el.textContent.trim() : ''; }"
        )
        if current == WANTED_SUFFIX:
            return True
        page.wait_for_timeout(100)
    return False


def install_message_observer(page: Page) -> None:
    """Ставит MutationObserver, который копит появляющиеся el-message сообщения.

    Тексты сообщений (Email Already Exists, Invalid Captcha Code) живут
    всего пару секунд, поэтому собираем их в массив на стороне страницы.
    Также параллельно перехватываем XHR-ответы — они дают точный код ошибки.
    """
    page.evaluate(
        """
        () => {
          if (window.__pinmxObserverInstalled) return;
          window.__pinmxObserverInstalled = true;
          window.__pinmxMessages = [];
          window.__pinmxApiResponses = [];

          const obs = new MutationObserver(muts => {
            for (const m of muts) for (const node of m.addedNodes) {
              if (node.nodeType !== 1) continue;
              const cls = (node.className || '').toString();
              if (cls.includes('el-message') && !cls.includes('el-message-box')) {
                setTimeout(() => {
                  const t = (node.innerText || '').trim();
                  if (t && t !== 'undefined') window.__pinmxMessages.push(t);
                }, 100);
              }
            }
          });
          obs.observe(document.body, { childList: true, subtree: true });

          // Перехват fetch — самый надёжный способ узнать код ответа сервера.
          const origFetch = window.fetch;
          window.fetch = async function(...args) {
            const resp = await origFetch.apply(this, args);
            try {
              const url = (typeof args[0] === 'string') ? args[0] : (args[0] && args[0].url);
              if (url && url.includes('/random-mail/create-by-device')) {
                const clone = resp.clone();
                const body = await clone.text();
                window.__pinmxApiResponses.push({url, body});
              }
            } catch (e) { /* ignore */ }
            return resp;
          };

          // То же для XMLHttpRequest на случай если сайт его использует.
          const origOpen = XMLHttpRequest.prototype.open;
          const origSend = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.open = function(method, url, ...rest) {
            this.__pinmxUrl = url;
            return origOpen.call(this, method, url, ...rest);
          };
          XMLHttpRequest.prototype.send = function(...args) {
            this.addEventListener('load', () => {
              if (this.__pinmxUrl && this.__pinmxUrl.includes('/random-mail/create-by-device')) {
                window.__pinmxApiResponses.push({url: this.__pinmxUrl, body: this.responseText});
              }
            });
            return origSend.apply(this, args);
          };
        }
        """
    )


def reset_messages(page: Page) -> None:
    page.evaluate("() => { window.__pinmxMessages = []; window.__pinmxApiResponses = []; }")


def collect_messages(page: Page) -> list[str]:
    return page.evaluate("() => (window.__pinmxMessages || [])")


def collect_api_responses(page: Page) -> list[dict]:
    """Возвращает массив объектов с полями url и body (тело ответа в виде строки)."""
    return page.evaluate("() => (window.__pinmxApiResponses || [])")


def fetch_captcha_bytes(page: Page) -> bytes:
    src = page.evaluate(
        "() => { const el = document.querySelector('#weiqu_captcha_img_url'); return el ? el.src : ''; }"
    )
    if not src:
        raise RuntimeError("captcha img not found")
    response = page.request.get(src)
    if response.status != 200:
        raise RuntimeError(f"captcha img fetch HTTP {response.status}")
    return response.body()


def refresh_captcha(page: Page) -> None:
    page.evaluate(
        "() => { if (typeof WeiquChangeCaptchaImg === 'function') WeiquChangeCaptchaImg(); "
        "else { const el = document.querySelector('#weiqu_captcha_img_change'); if (el) el.click(); } }"
    )
    page.wait_for_timeout(350)


def solve_digits(png_bytes: bytes, ocr: ddddocr.DdddOcr) -> str:
    raw = ocr.classification(png_bytes)
    return "".join(ch for ch in raw if ch.isdigit())


PASSWORD_RE = re.compile(r"\bпароль:\s*([^\s\n]+)")

# P1-10: возможные имена поля с паролем в ответе pinmx /random-mail/create-by-device.
# Наблюдавшиеся версии API иногда возвращали его по-разному; пробуем все
# разумные пути перед тем, как фоллбэкнуть на DOM-парсер.
_PASSWORD_API_KEYS: tuple[str, ...] = ("password", "pwd", "pass", "passwd", "password_plain")


def extract_password_from_api(payload: dict) -> str | None:
    """Извлечь пароль из JSON-ответа ``/random-mail/create-by-device``.

    P1-10: раньше пароль брался только из диалога успеха (вёрстка
    pinmx любые правки ломают регекс). Но в большинстве версий API
    тот же пароль лежит в ``data.mail.password`` (реже — в ``data.password`` или
    под ключем ``pwd``). Проверяем все разумные варианты и возвращаем
    первый непустой. ``None`` — значит API не вернул пароль и надо
    фоллбэкнуть на DOM.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    candidates: list[dict] = [data]
    mail_obj = data.get("mail")
    if isinstance(mail_obj, dict):
        candidates.append(mail_obj)
    for source in candidates:
        for key in _PASSWORD_API_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_password_from_success_dialog(page: Page) -> str | None:
    """Fallback: достать пароль из диалога успеха.

    Используется, когда :func:`extract_password_from_api` вернула ``None``.
    Парсит ``innerText`` модального окна регексом ``PASSWORD_RE`` — хрупко
    и ломается при любых правках вёрстки pinmx.
    """
    try:
        text = page.evaluate(
            """
            () => {
              const dlg = Array.from(document.querySelectorAll('div[role="dialog"]'))
                .find(d => d.innerText && d.innerText.includes('Почта:') && d.innerText.includes('пароль:'));
              return dlg ? dlg.innerText : '';
            }
            """
        )
    except Exception:
        return None
    if not text:
        return None
    m = PASSWORD_RE.search(text)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------

@dataclass
class AttemptResult:
    creds: tuple[str, str] | None = None
    taken: bool = False
    reason: str = ""


@log_timing
def attempt_register(page: Page, nick: str, logger, ocr: ddddocr.DdddOcr) -> AttemptResult:
    """Одна попытка регистрации (один заход на страницу).

    Возвращает результат:
    - creds если успех
    - taken=True если сайт прислал «Email Already Exists»
    - reason описывает что пошло не так
    """
    log_step(logger, "attempt_register", f"nick={nick}")
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_selector('input[placeholder="Введите префикс электронной почты"]')
    install_message_observer(page)
    reset_messages(page)

    page.fill('input[placeholder="Введите префикс электронной почты"]', nick)

    if not select_pingmx(page):
        logger.warning(f"[{nick}] не удалось переключить суффикс")
        return AttemptResult(reason="не удалось переключить суффикс")

    page.get_by_role("button", name="Создать почтовый ящик").click()

    captcha_img = page.locator("#weiqu_captcha_img_url")
    captcha_img.wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(300)

    for refresh in range(1, CAPTCHA_REFRESHES + 1):
        try:
            png = fetch_captcha_bytes(page)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{nick}] r{refresh}: ошибка скачивания капчи: {exc}")
            print(f"  [{nick}] r{refresh}: ошибка скачивания капчи: {exc}")
            refresh_captcha(page)
            continue

        code = solve_digits(png, ocr)

        # Принимаем только ровно 6 цифр.
        if len(code) != 6:
            logger.debug(f"[{nick}] r{refresh}: ddddocr дал {code!r} (len={len(code)}), обновляем")
            print(f"  [{nick}] r{refresh}: ddddocr дал {code!r} (len={len(code)}), обновляем")
            refresh_captcha(page)
            continue

        logger.debug(f"[{nick}] r{refresh}: пробуем код {code}")
        print(f"  [{nick}] r{refresh}: пробуем код {code}")

        # Сохраняем PNG только если код в итоге окажется неверным.
        DEBUG_DIR.mkdir(exist_ok=True)
        debug_path = DEBUG_DIR / f"{nick}-{refresh}-{code}.png"
        debug_path.write_bytes(png)

        input_locator = page.locator('.el-overlay-message-box input.el-input__inner').first
        if input_locator.count() == 0:
            input_locator = page.locator('div[role="dialog"] input[type="text"]').first
        input_locator.fill(code)

        reset_messages(page)
        page.get_by_role("button", name="проверять").click()

        # Ждём API-ответа от /random-mail/create-by-device — он есть всегда.
        try:
            page.wait_for_function(
                "() => (window.__pinmxApiResponses || []).length > 0",
                timeout=8_000,
            )
        except PWTimeout:
            logger.warning(f"[{nick}] r{refresh}: не дождались ответа сервера")
            print(f"  [{nick}] r{refresh}: не дождались ответа сервера")
            return AttemptResult(reason="api timeout")

        responses = collect_api_responses(page)
        # Берём последний ответ
        last = responses[-1] if responses else None
        if not last:
            logger.error(f"[{nick}] r{refresh}: no api response")
            return AttemptResult(reason="no api response")

        try:
            payload = json.loads(last.get("body") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}

        api_code = payload.get("code")
        api_msg = (payload.get("msg") or "").strip()

        logger.debug(f"[{nick}] r{refresh}: API ответ code={api_code} msg={api_msg!r}")

        # Успех: code == 200 и есть data.mail.mail. Пароль пробуем
        # вытащить из API (P1-10), иначе фоллбэкнем на DOM-парсер диалога.
        if api_code == 200 and isinstance(payload.get("data"), dict):
            data = payload["data"]
            mail_obj = data.get("mail") or {}
            email = (mail_obj.get("mail") or "").strip()

            # P1-10: предпочтительный источник — сам API; он устойчив
            # к любым правкам вёрстки pinmx.
            password = extract_password_from_api(payload)
            password_source = "api"
            if not password:
                # Fallback: версии API, которые пароля не отдают (JWT-хеш).
                # Парсим модальное окно успеха — хрупко, пишем warn.
                logger.warning(
                    f"[{nick}] r{refresh}: API не вернул пароль — фоллбэк на DOM"
                )
                page.wait_for_timeout(400)
                password = extract_password_from_success_dialog(page) or ""
                password_source = "dom"
            else:
                logger.debug(
                    f"[{nick}] r{refresh}: password взят из API ({password_source})"
                )

            if email and password:
                _delete(debug_path)
                logger.info(f"[{nick}] r{refresh}: SUCCESS email={email}")
                try:
                    page.get_by_role("button", name="Отмена").click(timeout=2_000)
                except PWTimeout:
                    pass
                return AttemptResult(creds=(email, password))
            logger.warning(
                f"[{nick}] r{refresh}: API сказал успех, но не достал email/password "
                f"(email={email!r}, password={password!r})"
            )
            print(
                f"  [{nick}] r{refresh}: API сказал успех, но не достал email/password "
                f"(email={email!r}, password={password!r})"
            )
            return AttemptResult(reason="success but no creds in DOM")

        # Известные коды ошибок
        if "Email Already Exists" in api_msg or api_code == 81002:
            _delete(debug_path)
            logger.info(f"[{nick}] r{refresh}: Email Already Exists")
            print(f"  [{nick}] r{refresh}: сайт говорит — Email Already Exists")
            return AttemptResult(taken=True, reason="Email Already Exists")

        if "Invalid Captcha Code" in api_msg or api_code == 81001:
            # Сайт обычно закрывает диалог капчи при неверном вводе.
            # Полноценная попытка: вернуться и зайти заново.
            logger.debug(f"[{nick}] r{refresh}: Invalid Captcha Code")
            print(f"  [{nick}] r{refresh}: код капчи неверен — перезаход")
            return AttemptResult(reason="invalid captcha")

        # Иногда сайт возвращает «Invalid Parameters» (1001) — обычно перезагрузка помогает.
        if api_code == 1001 or "Invalid Parameters" in api_msg:
            logger.warning(f"[{nick}] r{refresh}: Invalid Parameters")
            print(f"  [{nick}] r{refresh}: API ответил Invalid Parameters, перезагружаю страницу")
            return AttemptResult(reason="invalid parameters")

        # Неизвестный ответ
        logger.error(f"[{nick}] r{refresh}: неизвестный ответ API code={api_code} msg={api_msg!r}")
        print(f"  [{nick}] r{refresh}: неизвестный ответ API code={api_code} msg={api_msg!r}")
        return AttemptResult(reason=f"unknown api code {api_code}: {api_msg}")

    logger.warning(f"[{nick}] исчерпан лимит {CAPTCHA_REFRESHES} обновлений капчи")
    return AttemptResult(reason=f"исчерпан лимит {CAPTCHA_REFRESHES} обновлений капчи")


def _delete(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


@log_timing
def register_one(page: Page, nick: str, logger, ocr: ddddocr.DdddOcr) -> AttemptResult:
    last = AttemptResult(reason="не пробовали")
    for retry in range(1, RETRIES_PER_NICK + 1):
        if retry > 1:
            logger.debug(f"[{nick}] retry #{retry}")
            print(f"  [{nick}] retry #{retry}")
        try:
            last = attempt_register(page, nick, logger, ocr)
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, exc, f"[{nick}] исключение в attempt_register")
            print(f"  [{nick}] исключение: {exc}")
            last = AttemptResult(reason=f"exception: {exc}")
        if last.creds or last.taken:
            return last
        time.sleep(0.8)
    return last


# ---------------------------------------------------------------------------
# Worker function for parallel processing
# ---------------------------------------------------------------------------


def process_nick_worker(
    nick: str,
    worker_id: int,
    headless: bool,
    browser_mode: str,
    db_path: Path,
) -> tuple[str, bool, tuple[str, str] | None, str | None]:
    """Обработать один ник в отдельном воркере (для ThreadPoolExecutor).

    Каждый воркер запускает свой браузер, страницу и ocr объект для полной изоляции.

    Args:
        nick: Nickname для регистрации
        worker_id: ID воркера (для логирования)
        headless: Режим браузера (True = headless)
        browser_mode: Режим браузера (clean/incognito/system)
        db_path: Путь к БД SQLite (не используется, но передаётся для единообразия)

    Returns:
        (nick, success, creds, error) где:
        - success: True если создан email или найден taken
        - creds: (email, password) если создан
        - error: причина если не получилось или "taken"
    """
    logger = logging.getLogger(f"create_emails.worker{worker_id}")
    logger.info(f"[Worker-{worker_id}] Начало обработки {nick}")

    # Создать локальный ocr для этого воркера
    ocr = ddddocr.DdddOcr(show_ad=False)
    ocr.set_ranges("0123456789")

    try:
        with sync_playwright() as p:
            launched = launch_browser(p, browser_mode, head=not headless)
            context = launched.context
            page = context.new_page()

            try:
                result = register_one(page, nick, logger, ocr)

                if result.creds:
                    logger.info(f"[Worker-{worker_id}] {nick} - УСПЕХ: {result.creds[0]}")
                    return (nick, True, result.creds, None)
                elif result.taken:
                    logger.info(f"[Worker-{worker_id}] {nick} - ЗАНЯТ")
                    return (nick, True, None, "taken")
                else:
                    logger.warning(f"[Worker-{worker_id}] {nick} - пропущен: {result.reason}")
                    return (nick, False, None, result.reason)

            finally:
                launched.cleanup()

    except Exception as exc:
        error_msg = f"Worker exception: {exc}"
        logger.exception(f"[Worker-{worker_id}] {nick} - КРИТИЧЕСКАЯ ОШИБКА")
        return (nick, False, None, error_msg)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--head", action="store_true", help="показать окно браузера")
    p.add_argument("--limit", type=int, default=None, help="обработать только N ников")
    p.add_argument(
        "--no-skip-taken",
        action="store_true",
        help="не пропускать ники из taken.txt (попробовать ещё раз)",
    )
    p.add_argument("--debug", action="store_true", help="включить детальное логирование")
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="количество параллельных воркеров (по умолчанию 1 - последовательно)",
    )
    p.add_argument(
        "--worker-delay",
        type=float,
        default=5.0,
        metavar="SEC",
        help="задержка между запуском воркеров в секундах (rate limiting, по умолчанию 5.0)",
    )
    p.add_argument(
        "--nicks-file",
        type=str,
        default=str(NICKS_PATH),
        help=f"путь к файлу с никами (по умолчанию {NICKS_PATH.name})",
    )
    add_browser_mode_arg(p)
    return p.parse_args(list(argv))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    logger = setup_logging(debug=args.debug, log_to_file=True)
    logger.info(f"create_emails запущен с флагами: {args}")

    # Initialize database
    db = AccountDB(DB_PATH)

    # Import existing data from .txt files if database is new
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        logger.info("Импорт существующих данных из .txt файлов...")
        counts = db.import_from_txt_files(
            RESULTS_PATH, TAKEN_PATH,
            ROOT / "аккаунты devin.txt",
            ROOT / "devin_errors.txt",
            ROOT / "личности.txt"
        )
        logger.info(f"Импортировано: {counts}")
        print(f"Импортировано из .txt файлов: {counts}")

    # Import nicks from specified file
    nicks_file = Path(args.nicks_file)
    if nicks_file.exists():
        nicks_imported = db.import_nicks_from_txt(nicks_file)
        logger.info(f"Импортировано ников: {nicks_imported}")
        print(f"Импортировано ников из {nicks_file.name}: {nicks_imported}")

    # Get pending nicks from database
    pending = db.get_pending_nicks(limit=args.limit)

    logger.info(
        f"Ников в очереди: {len(pending)}"
    )
    print(
        f"Ников в очереди: {len(pending)}"
    )

    if not pending:
        logger.info("Нечего делать. Добавьте ники через GUI или импортируйте из файла.")
        print("Нечего делать. Добавьте ники через GUI или импортируйте из файла.")
        return 0

    backup_results()

    ok = 0
    skipped = 0
    found_taken = 0

    # Выбор режима: последовательный (workers=1) или параллельный (workers>1)
    if args.workers == 1:
        # ===== ПОСЛЕДОВАТЕЛЬНЫЙ РЕЖИМ (старая логика) =====
        logger.info("Режим: последовательная обработка (1 воркер)")

        # Создать локальный ocr для последовательного режима
        ocr = ddddocr.DdddOcr(show_ad=False)
        ocr.set_ranges("0123456789")

        with sync_playwright() as p:
            launched = launch_browser(p, args.browser_mode, head=args.head)
            context = launched.context
            page = context.new_page()

            try:
                for i, nick in enumerate(pending, 1):
                    logger.info(f"[{i}/{len(pending)}] >>> {nick}")
                    print(f"\n[{i}/{len(pending)}] >>> {nick}")
                    result = register_one(page, nick, logger, ocr)

                    if result.creds:
                        email, password = result.creds
                        db.add_email(email, password, nick)
                        db.mark_nick_done(nick)
                        ok += 1
                        marker = "" if email.endswith(WANTED_SUFFIX) else "  ⚠ ДОМЕН НЕ ТОТ"
                        logger.info(f"[{nick}] OK -> {email} : {password}{marker}")
                        print(f"  [{nick}] OK -> {email} : {password}{marker}")
                    elif result.taken:
                        db.mark_nick_taken(nick)
                        db.mark_nick_taken_in_nicks(nick)
                        found_taken += 1
                        logger.info(f"[{nick}] ЗАНЯТ")
                        print(f"  [{nick}] ЗАНЯТ")
                    else:
                        skipped += 1
                        logger.warning(f"[{nick}] пропущен ({result.reason})")
                        print(f"  [{nick}] пропущен ({result.reason})")

                    time.sleep(0.4)
            except KeyboardInterrupt:
                logger.info("Остановлено пользователем (Ctrl+C)")
                print("\nОстановлено пользователем (Ctrl+C). Прогресс сохранён.")
            finally:
                launched.cleanup()

    else:
        # ===== ПАРАЛЛЕЛЬНЫЙ РЕЖИМ (ThreadPoolExecutor) =====
        logger.info(f"Режим: параллельная обработка ({args.workers} воркеров)")
        logger.info(f"Rate limiting: задержка {args.worker_delay}s между запуском воркеров")

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {}

            try:
                # Запустить воркеры с rate limiting
                for i, nick in enumerate(pending):
                    if i > 0 and args.worker_delay > 0:
                        time.sleep(args.worker_delay)

                    worker_id = i % args.workers
                    future = executor.submit(
                        process_nick_worker,
                        nick,
                        worker_id=worker_id,
                        headless=not args.head,
                        browser_mode=args.browser_mode,
                        db_path=DB_PATH,
                    )
                    futures[future] = nick
                    logger.info(f"[{i+1}/{len(pending)}] Запущен воркер для {nick}")

                # Собрать результаты по мере завершения
                for future in as_completed(futures):
                    nick = futures[future]
                    try:
                        nick_result, success, creds, error = future.result()

                        if success and creds:
                            email, password = creds
                            db.add_email(email, password, nick)
                            db.mark_nick_done(nick)
                            ok += 1
                            marker = "" if email.endswith(WANTED_SUFFIX) else "  ⚠"
                            print(f"✓ {nick} -> {email}{marker}")
                        elif success and error == "taken":
                            db.mark_nick_taken(nick)
                            db.mark_nick_taken_in_nicks(nick)
                            found_taken += 1
                            print(f"✓ {nick} - ЗАНЯТ")
                        else:
                            skipped += 1
                            print(f"✗ {nick} - {error}")

                    except Exception as exc:
                        skipped += 1
                        logger.exception(f"Ошибка при обработке future для {nick}")
                        print(f"✗ {nick} - КРИТИЧЕСКАЯ ОШИБКА: {exc}")

            except KeyboardInterrupt:
                print("\nОстановка... Ждём завершения активных воркеров...")
                logger.info("Получен Ctrl+C, останавливаем воркеры...")
                executor.shutdown(wait=True, cancel_futures=True)
                print("Воркеры остановлены. Прогресс сохранён.")

    # Export to .txt files for backward compatibility
    logger.info("Экспорт в .txt файлы...")
    db.export_emails_txt(RESULTS_PATH)
    db.export_taken_txt(TAKEN_PATH)

    total_emails = len(db.get_done_nicks())
    summary = (
        f"Итого: создано {ok}, занято {found_taken}, не получилось {skipped}. "
        f"Всего в БД теперь: {total_emails}"
    )
    logger.info(summary)
    print(f"\n{summary}")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
