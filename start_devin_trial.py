"""start_devin_trial: залогиниться в Devin под уже зарегистрированным
аккаунтом и пройти UI-цепочку до окна «Start free trial».

Шаги (точно повторяют действия пользователя):

1. Запускаем Chrome через Playwright (channel="chrome", visible).
2. В одной вкладке логинимся в `mail-client.pinmx.com` (для перехвата
   письма с login-кодом от Devin) — используем уже готовую функцию
   ``login_to_mailclient`` из ``register_devin``.
3. В соседней вкладке открываем `https://app.devin.ai/auth/login`,
   вводим email, нажимаем «Log in».
4. Возвращаемся на mail-вкладку, ждём письмо от Devin с кодом —
   используем ``wait_for_devin_email_code``.
5. Возвращаемся в Devin-вкладку, вводим код — ``submit_devin_code``.
6. Дожидаемся, пока URL уйдёт с ``/auth/...`` и кабинет отрисуется.
7. Кликаем «My Team» в сайдбаре.
8. Кликаем «Upgrade».
9. На странице планов кликаем «Start free trial».
10. Делаем скриншот появившейся модалки.
11. **Не закрываем браузер** — ждём ввода Enter в консоли.

Скрипт намеренно не вызывается из ``run_all.cmd`` — это интерактивный
шаг, который должен оставить окно открытым для ручного решения, что
делать с появившейся модалкой. Запуск из проектной папки::

    .venv\\Scripts\\python.exe start_devin_trial.py [--email EMAIL]

Если ``--email`` не передан, берётся первый ``email`` из
``аккаунты devin.txt`` (lower-case), для которого в
``имейлы pingmx.txt`` есть пара ``email:password``.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from browser_modes import add_browser_mode_arg, launch_browser

from register_devin import (
    Account,
    DEVIN_SIGNUP_URL,
    StepError,
    extract_code,
    load_accounts,
    load_done,
    login_to_mailclient,
    submit_devin_code,
    wait_for_devin_email_code,
    DEVIN_DONE_PATH,
    RESULTS_PATH,
    IDENTITIES_PATH,
    MAIL_LIST_ITEM_SELECTOR,
    MAIL_REFRESH_BUTTON_SELECTOR,
)


# ---------------------------------------------------------------------------
# Identity (имя + адрес) для подстановки в Stripe Checkout
# ---------------------------------------------------------------------------


from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    """Личность для заполнения адреса в Stripe Checkout.

    Парсится из строки ``личности.txt`` после ``\\t``-разделителя:
    ``email<TAB>Имя Фамилия, Улица + номер, Индекс, Город``.
    """

    full_name: str
    street: str   # `Paderborner Strasse 74`
    zip_code: str  # `86529`
    city: str    # `Schrobenhausen`


def parse_identity(identity_str: str) -> Identity | None:
    """Извлечь :class:`Identity` из identity-строки.

    Формат: ``Имя Фамилия, Улица 12, 12345, Город``.
    Возвращает ``None``, если формат не подходит.
    """
    parts = [p.strip() for p in identity_str.split(",")]
    if len(parts) != 4:
        return None
    full_name, street, zip_code, city = parts
    if not (full_name and street and zip_code and city):
        return None
    return Identity(full_name=full_name, street=street, zip_code=zip_code, city=city)


def find_identity_for_email(email: str) -> Identity | None:
    """Найти identity по email в ``личности.txt`` (TSV-формат).

    Сравнение по lower-case email. Возвращает ``None`` если не нашли.
    """
    if not IDENTITIES_PATH.exists():
        return None
    target = email.lower()
    for raw in IDENTITIES_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" not in line:
            continue
        e, identity_str = line.split("\t", 1)
        if e.strip().lower() == target:
            return parse_identity(identity_str.strip())
    return None


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

DEVIN_LOGIN_URL = "https://app.devin.ai/auth/login"

# Какие тексты ищем на боковой панели и кнопках. Точные имена будут
# уточнены в первом живом запуске; пока выкладываем самый очевидный
# вариант + список fallback-ов через регулярку (case-insensitive).
_MY_TEAM_LABELS: tuple[str, ...] = (
    "My Team",
    "Team",
)
_UPGRADE_LABELS: tuple[str, ...] = (
    "Upgrade",
)
_START_FREE_TRIAL_LABELS: tuple[str, ...] = (
    "Start free trial",
    "Start Free Trial",
    "Start trial",
)

# Сколько ждём, пока кабинет отрисуется после логина (URL ушёл с /auth/).
_POST_LOGIN_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.4


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


def _pick_account(email_arg: str | None) -> Account:
    """Найти аккаунт для входа: либо по ``--email``, либо первый из
    ``аккаунты devin.txt`` ∩ ``имейлы pingmx.txt``.
    """
    accounts = load_accounts(RESULTS_PATH)
    if not accounts:
        raise SystemExit(f"Не найден или пуст {RESULTS_PATH}")
    by_email = {a.email: a for a in accounts}

    if email_arg:
        target = email_arg.lower()
        if target not in by_email:
            raise SystemExit(
                f"Email {target!r} не найден в {RESULTS_PATH}. "
                "Доступные: " + ", ".join(by_email)
            )
        return by_email[target]

    done = load_done(DEVIN_DONE_PATH)
    if not done:
        raise SystemExit(
            f"{DEVIN_DONE_PATH} пуст или отсутствует — "
            "сначала зарегистрируй хотя бы один Devin-аккаунт."
        )
    for email in done:
        if email in by_email:
            return by_email[email]
    raise SystemExit(
        f"Ни один email из '{DEVIN_DONE_PATH.name}' не нашёлся в "
        f"'{RESULTS_PATH.name}' — обнови файлы или передай --email явно."
    )


def _click_first_match(
    page: Page,
    labels: tuple[str, ...],
    *,
    timeout_ms: int = 10_000,
    description: str = "",
) -> bool:
    """Попробовать кликнуть первый видимый элемент, у которого текст /
    accessible name совпадает с одним из ``labels``.

    Сначала пробуем ``get_by_role("link"|"button", name=...)``; если не
    видно — пробуем ``get_by_text(...)``. Возвращает True, если клик
    прошёл, иначе False.
    """
    for label in labels:
        for role in ("link", "button"):
            try:
                loc = page.get_by_role(role, name=label, exact=True).first
                if loc.is_visible(timeout=1_000):
                    print(f"  click: role={role!r} name={label!r}{(' ('+description+')') if description else ''}")
                    loc.click(timeout=timeout_ms)
                    return True
            except Exception:
                continue
        # Текстовый матч как фолбэк (для произвольных <div>/<span> с обработчиком клика).
        try:
            loc = page.get_by_text(label, exact=True).first
            if loc.is_visible(timeout=1_000):
                print(f"  click: text={label!r}{(' ('+description+')') if description else ''}")
                loc.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


def _wait_until_logged_in(devin_page: Page, timeout_s: float) -> str:
    """Дождаться, пока URL уйдёт с ``/auth/...``; вернуть финальный URL."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            url = devin_page.url
        except Exception:
            url = ""
        if url and "/auth/" not in url:
            return url
        time.sleep(_POLL_INTERVAL_S)
    raise StepError(
        f"login outcome unclear: всё ещё на {devin_page.url} после {timeout_s}с"
    )


# ---------------------------------------------------------------------------
# Главные шаги
# ---------------------------------------------------------------------------


def login_to_devin(devin_page: Page, mail_page: Page, account: Account) -> str:
    """Полный цикл «email → код → залогинен».

    Возвращает URL, на котором мы оказались после логина.
    """
    # Запомним baseline количества писем в inbox — после клика «Log in»
    # будем ждать, пока появится НОВОЕ письмо (старые login-коды могут
    # быть просрочены, и взять первое в списке = взять старый код).
    try:
        baseline_items = mail_page.locator(MAIL_LIST_ITEM_SELECTOR).count()
    except Exception:
        baseline_items = 0
    print(f"[mail] в inbox сейчас {baseline_items} писем — ждём появления нового")

    print(f"[devin] open {DEVIN_LOGIN_URL}")
    devin_page.goto(DEVIN_LOGIN_URL, wait_until="domcontentloaded")

    email_field = devin_page.get_by_role("textbox", name="Email address")
    try:
        email_field.wait_for(state="visible", timeout=30_000)
    except PWTimeout as exc:
        raise StepError("login failed: email field not visible") from exc
    email_field.fill(account.email)

    print('[devin] click "Log in"')
    devin_page.get_by_role("button", name="Log in", exact=True).click()

    # Поллим inbox, пока количество писем не вырастет — это и есть наше
    # новое login-письмо. Лимит — 90с (письма Devin приходят за 5–30с).
    print(f"[mail] жду появления нового письма (baseline={baseline_items})...")
    deadline = time.monotonic() + 90.0
    saw_new = False
    while time.monotonic() < deadline:
        try:
            mail_page.click(MAIL_REFRESH_BUTTON_SELECTOR, timeout=2_000)
        except Exception:
            pass
        time.sleep(2.0)
        try:
            current = mail_page.locator(MAIL_LIST_ITEM_SELECTOR).count()
        except Exception:
            current = baseline_items
        if current > baseline_items:
            print(f"[mail] inbox: {current} (+{current - baseline_items}), ок")
            saw_new = True
            break
    if not saw_new:
        print("[mail] WARN: новое письмо так и не появилось за 90с — пробуем читать что есть")

    print("[mail] извлекаю код из самого свежего письма...")
    code = wait_for_devin_email_code(mail_page, account.email)
    print(f"[mail] получили код: {code}")

    print('[devin] ввод кода в поле "one-time-code"')
    submit_devin_code(devin_page, code)

    final_url = _wait_until_logged_in(devin_page, _POST_LOGIN_TIMEOUT_S)
    print(f"[devin] залогинены, URL: {final_url}")
    return final_url


def find_stripe_checkout_frame(devin_page: Page):
    """Вернуть Frame со Stripe Checkout (`checkout.stripe.com/c/pay/...`)."""
    for fr in devin_page.frames:
        if fr.url.startswith("https://checkout.stripe.com/c/pay/"):
            return fr
    return None


def select_card_payment_method(stripe_frame) -> bool:
    """Раскрыть аккордеон «Оплатить картой» в Stripe Checkout.

    Stripe рендерит способ оплаты как accordion-item: visible-кнопка
    (``button[data-testid="card-accordion-item-button"]``) перекрывает
    скрытый radio. Кликаем по самой кнопке — это эквивалент пользова-
    тельского выбора пункта «Карта», и Stripe раскрывает форму карты +
    адрес.

    Возвращает True, если клик прошёл; иначе False.
    """
    # Самый надёжный путь — по data-testid.
    selectors = (
        'button[data-testid="card-accordion-item-button"]',
        'button[aria-label="Оплатить картой"]',
        'button[aria-label="Pay with card"]',
        'button[aria-label*="card" i][aria-label*="pay" i]',
    )
    for selector in selectors:
        try:
            btn = stripe_frame.locator(selector).first
            if btn.is_visible(timeout=1_500):
                print(f"[stripe] раскрываю Card-accordion: {selector}")
                btn.click(timeout=5_000)
                return True
        except Exception:
            continue

    # Последний фолбэк — клик по координатам radio (мимо overlay-кнопки
    # это не сработает, поэтому только если ни один селектор выше не нашёлся).
    print("[stripe] card-accordion-button не нашёлся, fallback на radio")
    try:
        radios = stripe_frame.get_by_role("radio").all()
    except Exception as exc:
        print(f"[stripe] не получилось прочитать radios: {exc}")
        return False
    for r in radios:
        try:
            if r.is_visible():
                r.click(timeout=3_000, force=True)
                return True
        except Exception:
            continue
    return False


def fill_address_and_card(stripe_frame, identity: Identity) -> None:
    """Заполнить форму адреса и (тестовыми) данными карты в Stripe Checkout.

    Шаги (на основе Stripe Checkout layout):

    1. Найти поле адреса (autocomplete-search) — обычно
       ``input[autocomplete*="address"]`` или поле с placeholder/aria
       «Address line 1» / «Введите адрес».
    2. Ввести ``identity.street`` посимвольно (`type(delay=80)`), чтобы
       сработал Google Places autocomplete.
    3. Подождать выпадающий список вариантов и кликнуть первый.
    4. Stripe сам подставит ZIP, City, Country. ``Address line 2``
       не трогаем (по требованию).
    5. Найти поле имени держателя карты (если есть) — заполнить
       ``identity.full_name``.
    6. Найти Stripe Elements iframes для card-number/expiry/cvc и
       заполнить тестовыми значениями.

    Заполнение карты — **тестовое**, чтобы убедиться, что мы корректно
    добрались до Stripe Elements iframes. **Кнопку покупки не нажимаем.**
    """
    print()
    print("=" * 60)
    print(" Заполнение Stripe Checkout формы")
    print("=" * 60)

    # --- Адрес: найти поле и ввести улицу с номером ---------------------
    address_input = None
    for selector in (
        'input[autocomplete="street-address"]',
        'input[autocomplete="address-line1"]',
        'input[autocomplete*="address"]',
        'input[name="billingAddressLine1"]',
        'input[name*="address" i][type="text"]',
        'input[placeholder*="Адрес" i]',
        'input[placeholder*="Address" i]',
    ):
        try:
            loc = stripe_frame.locator(selector).first
            if loc.is_visible(timeout=1_500):
                address_input = loc
                print(f"[stripe] адрес-инпут найден: {selector}")
                break
        except Exception:
            continue

    if address_input is None:
        print("[stripe] адрес-инпут не нашёлся — дамп DOM фрейма для отладки:")
        _dump_in_scope(stripe_frame, prefix="stripe-form")
        raise StepError("Stripe: не найден инпут адреса")

    print(f"[stripe] печатаю улицу: {identity.street!r}")
    address_input.click()
    address_input.fill("")  # очистить, если что-то уже введено
    # type посимвольно — нужен Google Places autocomplete
    address_input.type(identity.street, delay=80)
    time.sleep(1.5)

    # --- Выбрать первый предложенный вариант autocomplete --------------
    # Google Places dropdown в Stripe — это `[role="listbox"]` или
    # просто ul с `[role="option"]`.
    suggestion_clicked = False
    for selector in (
        '[role="option"]',
        '[role="listbox"] li',
        'ul[role="listbox"] li',
        '[data-testid*="suggestion"]',
        'li[id^="downshift"]',
    ):
        try:
            items = stripe_frame.locator(selector).all()
        except Exception:
            continue
        for item in items:
            try:
                if not item.is_visible():
                    continue
                item.click(timeout=2_000)
                print(f"[stripe] кликнул suggestion ({selector})")
                suggestion_clicked = True
                break
            except Exception:
                continue
        if suggestion_clicked:
            break

    if not suggestion_clicked:
        print(
            "[stripe] не нашёл выпадающий список адресов — "
            "Stripe мог не показать suggestions. Дамп фрейма:"
        )
        _dump_in_scope(stripe_frame, prefix="stripe-after-typing")
        # Не валим скрипт — может быть, форма уже валидна без autocomplete.

    # Дадим Stripe время заполнить ZIP/город из выбранного suggestion.
    time.sleep(1.5)

    # --- Имя держателя карты (если есть отдельное поле) ----------------
    for selector in (
        'input[autocomplete="cc-name"]',
        'input[name="cardholderName" i]',
        'input[placeholder*="имя" i]',
        'input[placeholder*="name on card" i]',
    ):
        try:
            loc = stripe_frame.locator(selector).first
            if loc.is_visible(timeout=1_000):
                print(f"[stripe] имя держателя: {selector}")
                loc.fill(identity.full_name)
                break
        except Exception:
            continue

    # --- Карта: Stripe Elements (вложенные iframes) --------------------
    # Тестовые данные (Stripe test card 4242 4242 4242 4242 у нас не сработает
    # на live ключе — но мы и не отправляем форму, нам нужен только сам
    # факт, что мы добрались до iframes). Используем правдоподобный
    # placeholder-номер: 16 цифр, валидный по Лун-чек, и просто чтобы
    # форма приняла ввод визуально.
    test_card = {
        "number": "4242 4242 4242 4242",  # Stripe test card — отображается красиво
        "expiry": "12 / 30",
        "cvc": "123",
    }

    # Поля карты обычно лежат в iframes, у которых url содержит
    # `__privateStripeFrame` или title типа «Card number input frame».
    card_filled = False
    for fr in stripe_frame.page.frames:
        title = ""
        try:
            title = (fr.name or "") + " " + (fr.url or "")
        except Exception:
            pass
        if "card-number" in fr.url.lower() or "cardnumber" in fr.url.lower():
            try:
                inp = fr.locator('input[name="cardnumber"]').first
                inp.fill(test_card["number"])
                print(f"[stripe] card number → введён в iframe ({fr.url[:60]}...)")
                card_filled = True
            except Exception as exc:
                print(f"[stripe] не получилось заполнить card number: {exc}")

    if not card_filled:
        # Альтернативный подход: прямой поиск по placeholder/aria в любом
        # iframe Stripe.
        for fr in stripe_frame.page.frames:
            if "stripe.com" not in fr.url:
                continue
            try:
                num = fr.locator('input[autocomplete="cc-number"]').first
                if num.is_visible(timeout=500):
                    num.fill(test_card["number"])
                    print(f"[stripe] card number введён в {fr.url[:60]}...")
                    card_filled = True
            except Exception:
                continue
            try:
                exp = fr.locator('input[autocomplete="cc-exp"]').first
                if exp.is_visible(timeout=500):
                    exp.fill(test_card["expiry"])
                    print("[stripe] card expiry введена")
            except Exception:
                pass
            try:
                cvc = fr.locator('input[autocomplete="cc-csc"]').first
                if cvc.is_visible(timeout=500):
                    cvc.fill(test_card["cvc"])
                    print("[stripe] card CVC введён")
            except Exception:
                pass

    if not card_filled:
        print(
            "[stripe] не удалось найти Stripe Elements iframes для карты. "
            "Это значит, layout формы изменился — нужно обновить селекторы."
        )

    print("=" * 60)
    print()


def go_to_plans_and_start_trial(devin_page: Page) -> None:
    """My Team → Upgrade → Start free trial. Не закрывает модалку."""
    print('[ui] click "My Team"')
    if not _click_first_match(devin_page, _MY_TEAM_LABELS, description="sidebar My Team"):
        raise StepError("UI: не нашёл «My Team» в сайдбаре")
    devin_page.wait_for_load_state("domcontentloaded")
    time.sleep(1.5)

    print('[ui] click "Upgrade"')
    if not _click_first_match(devin_page, _UPGRADE_LABELS, description="header Upgrade"):
        raise StepError("UI: не нашёл «Upgrade»")
    devin_page.wait_for_load_state("domcontentloaded")
    time.sleep(1.5)

    print(f"[ui] на странице планов: {devin_page.url}")

    # Подождём, чтобы JS-чанк страницы успел подгрузиться.
    time.sleep(2.0)

    # Список потенциальных подписей: новый аккаунт, старый аккаунт, и
    # возможные варианты после возврата к flow в середине.
    plan_button_labels = (
        "Start free trial",
        "Start Free Trial",
        "Start trial",
        "Subscribe",
        "Continue",
        "Get started",
        "Upgrade",
    )
    print(f'[ui] click первая из {plan_button_labels}')
    if not _click_first_match(
        devin_page, plan_button_labels, description="plans Start/Subscribe/Continue"
    ):
        # Не нашли — дамп DOM прямо здесь, чтобы понять, что на странице.
        print("[ui] не нашёл подходящую кнопку — дамп DOM текущей страницы:")
        _dump_in_scope(devin_page, prefix="plans-page")
        raise StepError("UI: не нашёл кнопку начала trial на странице планов")

    # Даём модалке отрисоваться. НЕ закрываем — ждём ручного решения.
    time.sleep(2.5)
    print(f"[ui] модалка должна быть открыта; URL: {devin_page.url}")


def dump_modal_dom(devin_page: Page) -> None:
    """Распечатать все видимые textbox/combobox/button/heading на странице.

    Используется для исследования модалки «Start free trial»: чтобы
    написать корректные селекторы для заполнения адреса и карты.
    Дополнительно дампит содержимое первого «полезного» iframe (Stripe
    Checkout живёт внутри iframe ``checkout.stripe.com/c/pay/...``).
    """
    print()
    print("=" * 60)
    print(" DOM dump (видимые интерактивные элементы на странице)")
    print("=" * 60)
    _dump_in_scope(devin_page, prefix="page")

    # iframes: ищем Stripe Checkout и тоже дампим его.
    try:
        frames = devin_page.frames
        print(f"\n  iframes: {len(frames)}")
        for i, fr in enumerate(frames):
            try:
                print(f"    [{i}] url={fr.url!r}")
            except Exception:
                pass
    except Exception as exc:
        print(f"  iframes read failed: {exc}")

    # Дамп DOM каждого Stripe-фрейма с формой.
    for i, fr in enumerate(devin_page.frames):
        if not fr.url.startswith("https://checkout.stripe.com/c/pay/"):
            continue
        print()
        print(f" iframe[{i}] DOM dump ({fr.url[:80]}...)")
        print("=" * 60)
        _dump_in_scope(fr, prefix=f"frame{i}")
    print("=" * 60)
    print()


def _dump_in_scope(scope, *, prefix: str) -> None:
    """Common helper: проходит по ролям и печатает видимые элементы.

    ``scope`` — это либо ``Page``, либо ``Frame`` (у обоих одинаковый API
    ``get_by_role``).
    """
    for role in (
        "heading",
        "textbox",
        "combobox",
        "button",
        "tab",
        "radio",
        "checkbox",
        "link",
    ):
        try:
            entries = scope.get_by_role(role).all()
        except Exception as exc:
            print(f"  [{prefix}/{role}] read failed: {exc}")
            continue
        for i, loc in enumerate(entries):
            try:
                if not loc.is_visible():
                    continue
            except Exception:
                continue
            try:
                name = loc.evaluate(
                    "el => el.getAttribute('aria-label') || el.textContent || el.getAttribute('placeholder') || ''"
                )
            except Exception:
                name = "<unreadable>"
            name_short = (name or "").strip().replace("\n", " ")[:80]
            try:
                box = loc.bounding_box()
                box_str = f"x={int(box['x'])},y={int(box['y'])}" if box else "no-bbox"
            except Exception:
                box_str = "no-bbox"
            print(f"  [{prefix}/{role}#{i}] {name_short!r}  ({box_str})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--email",
        default=None,
        help="email из 'имейлы pingmx.txt' (по умолчанию — первый из 'аккаунты devin.txt')",
    )
    parser.add_argument(
        "--screenshot",
        default="trial_dialog.png",
        help="куда сохранить скриншот после клика «Start free trial»",
    )
    add_browser_mode_arg(parser)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    account = _pick_account(args.email)
    print(f"[main] аккаунт: {account.email}")

    with sync_playwright() as pw:
        launched = launch_browser(pw, args.browser_mode, head=True)
        context = launched.context
        try:
            mail_page = context.new_page()
            devin_page = context.new_page()

            login_to_mailclient(mail_page, account)
            login_to_devin(devin_page, mail_page, account)

            # Перешли в кабинет. Возвращаем фокус на Devin-вкладку перед
            # любыми кликами (Playwright bring_to_front не обязателен,
            # но не помешает — иногда хелперы видимости капризничают).
            try:
                devin_page.bring_to_front()
            except Exception:
                pass

            go_to_plans_and_start_trial(devin_page)

            # Снимаем скриншот и DOM модалки сразу — это поможет
            # отлаживать структуру формы (Stripe iframe, address-search и т.д.).
            screenshot_path = Path(args.screenshot).resolve()
            try:
                devin_page.screenshot(path=str(screenshot_path), full_page=False)
                print(f"[main] скриншот: {screenshot_path}")
            except Exception as exc:
                print(f"[main] не удалось сделать скриншот: {exc}")

            # Stripe Checkout рендерится в iframe (checkout.stripe.com).
            # Дадим iframe-у время инициализироваться.
            time.sleep(2.5)
            stripe_frame = find_stripe_checkout_frame(devin_page)
            if stripe_frame is None:
                print("[main] Stripe Checkout iframe не найден; полный DOM-дамп:")
                dump_modal_dom(devin_page)
            else:
                print(f"[main] Stripe Checkout iframe: {stripe_frame.url[:80]}...")

                # Шаг 1: выбрать «Карта» в списке методов оплаты.
                if select_card_payment_method(stripe_frame):
                    time.sleep(2.0)

                # Шаг 2: заполнить адрес + тестовые данные карты.
                identity = find_identity_for_email(account.email)
                if identity is None:
                    print(
                        f"[main] identity для {account.email} не найдена в "
                        f"{IDENTITIES_PATH}. Запусти "
                        "`add_identities.py --only-from \"аккаунты devin.txt\"` сначала."
                    )
                else:
                    print(f"[main] identity: {identity}")
                    try:
                        fill_address_and_card(stripe_frame, identity)
                    except StepError as exc:
                        print(f"[main] ошибка заполнения формы: {exc}")

                # Скриншот после заполнения, чтобы видеть результат.
                filled_path = Path(args.screenshot).with_name("trial_filled.png")
                try:
                    devin_page.screenshot(path=str(filled_path), full_page=False)
                    print(f"[main] скриншот после заполнения: {filled_path}")
                except Exception as exc:
                    print(f"[main] не удалось снять скриншот: {exc}")

            print()
            print("=" * 60)
            print(" Окно «Start free trial» открыто.")
            print(" Браузер НЕ ЗАКРЫВАЕТСЯ — сообщи следующий шаг.")
            print(" Когда закончишь — вернись в эту консоль и нажми Enter.")
            print("=" * 60)
            try:
                input()
            except (KeyboardInterrupt, EOFError):
                pass

        finally:
            launched.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
