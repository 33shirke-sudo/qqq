"""test_hcaptcha: быстрый dev-инструмент для итерации по hCaptcha.

Идея
====
Полный цикл (логин в почту → логин в Devin → Upgrade → trial → Stripe
→ адрес → карта → submit) занимает 60-120 секунд. Мы итерируем
**только** по последнему шагу (обработка hCaptcha), поэтому каждый
тест с нуля — пустая трата времени.

Этот скрипт сохраняет storage_state (cookies + localStorage) в
``hcaptcha_state.json`` после первого успешного логина. На втором
и далее запусках восстанавливает состояние и сразу идёт на Stripe
Checkout — путь занимает 10-15 секунд.

Запуск
------
Первый раз (логин + сохранение состояния)::

    .venv\\Scripts\\python.exe test_hcaptcha.py --login

Все последующие (быстрый цикл к hCaptcha)::

    .venv\\Scripts\\python.exe test_hcaptcha.py

Опции::

    --login            принудительно перелогиниться (стереть state).
    --email EMAIL      какой аккаунт использовать (default — первый
                       из «аккаунты devin.txt»).
    --pause            оставить браузер открытым после клика «Подписаться»
                       (по умолчанию закрывает через 5 минут или Ctrl+C).
    --no-card          не заполнять карту, остановиться на адресе
                       (для исследования DOM Stripe-формы).
    --card NUM|MM|YYYY|CVV
                       подсунуть конкретную карту (default — первая
                       из «подтверждённые живые карты.txt»).

Поведение скрипта в hCaptcha-точке: вызывает
``activate_accounts._try_solve_hcaptcha_async`` и печатает результат.
После этого ждёт 5 мин или Ctrl+C — чтобы можно было руками докликать
капчу или подсмотреть DOM через DevTools.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from check_cards import (
    CONFIRMED_LIVE_CARDS_PATH,
    LIVE_CARDS_PATH,
    extract_card_credentials,
    load_existing_live_cards,
)
from devin_async import (
    Identity,
    StepError,
    _click_first_match_async,
    fill_address_async,
    find_identity_for_email,
    find_stripe_checkout_frame_async,
    go_to_plans_and_start_trial_async,
    login_to_devin_async,
    login_to_mailclient_async,
    select_card_payment_method_async,
)
from register_devin import (
    Account,
    DEVIN_DONE_PATH,
    RESULTS_PATH,
    load_accounts,
)
from activate_accounts import (
    _try_solve_hcaptcha_async,
    check_consent_checkboxes_async,
    click_submit_async,
    fill_card_async,
)


ROOT = Path(__file__).parent
STATE_PATH = ROOT / "hcaptcha_state.json"


def _pick_account(email_hint: str | None) -> Account:
    accounts = load_accounts(RESULTS_PATH)
    if not accounts:
        raise SystemExit(f"В {RESULTS_PATH.name} нет аккаунтов")

    by_email = {a.email.lower(): a for a in accounts}

    if email_hint:
        key = email_hint.strip().lower()
        if key not in by_email:
            raise SystemExit(
                f"Нет аккаунта {email_hint!r}. "
                "Доступные: " + ", ".join(sorted(by_email))
            )
        return by_email[key]

    if DEVIN_DONE_PATH.exists():
        for raw in DEVIN_DONE_PATH.read_text(encoding="utf-8").splitlines():
            email = raw.strip().lstrip("\ufeff").lower()
            if email and email in by_email:
                return by_email[email]

    return accounts[0]


def _pick_identity(account: Account) -> Identity:
    identity = find_identity_for_email(account.email)
    if identity is None:
        raise SystemExit(
            f"Для {account.email} нет identity в личности.txt. "
            "Запусти сначала: .venv\\Scripts\\python.exe add_identities.py"
        )
    return identity


def _pick_card(card_arg: str | None) -> str | None:
    """Вернуть карту в формате NUMBER|MM|YYYY|CVV или None."""
    if card_arg:
        if card_arg.count("|") != 3:
            raise SystemExit(
                "--card должен быть в формате NUMBER|MM|YYYY|CVV"
            )
        return card_arg

    # Первая из подтверждённых живых карт.
    for path in (CONFIRMED_LIVE_CARDS_PATH, LIVE_CARDS_PATH):
        for line in load_existing_live_cards(path):
            cred = extract_card_credentials(line) or line.strip()
            if cred and cred.count("|") == 3:
                return cred
    return None


# ---------------------------------------------------------------------------
# Шаги
# ---------------------------------------------------------------------------


async def _do_login(context, account: Account) -> None:
    """Полный логин: mail-client + Devin. Используется только при --login."""
    mail_page = await context.new_page()
    devin_page = await context.new_page()

    print(f"[login] mail-client как {account.email}")
    await login_to_mailclient_async(mail_page, account)

    print(f"[login] Devin как {account.email}")
    await login_to_devin_async(devin_page, mail_page, account)
    print(f"[login] OK, devin URL = {devin_page.url}")

    # mail_page больше не нужен — закрываем.
    try:
        await mail_page.close()
    except Exception:
        pass


async def _select_card_radio(stripe_frame) -> bool:
    """Кликнуть radio «Карта» в Stripe Checkout.

    В новой версии Stripe Checkout три radio с одинаковым name
    ``payment-method-accordion-item-title`` (Cashapp, Card, прочее).
    Нам нужен тот, чей <label> содержит «Карта»/«Card». Найдём через
    locator — родительский <label> внутри ищет input[type=radio].
    """
    # Способ 1: ищем accordion-item с текстом «Карта»/«Card».
    for label_text in ("Карта", "Card"):
        try:
            # accordion-item — это контейнер вокруг radio + текста.
            container = stripe_frame.locator(
                f'label:has-text("{label_text}")'
            ).first
            if await container.is_visible(timeout=1_500):
                print(f"[fast] кликаю label '{label_text}'")
                await container.click(timeout=4_000, force=True)
                await asyncio.sleep(2.0)
                # Проверим, появилось ли поле cardNumber.
                try:
                    if await stripe_frame.locator(
                        'input[name="cardNumber"]'
                    ).first.is_visible(timeout=2_000):
                        print("[fast] поле cardNumber появилось")
                        return True
                except Exception:
                    pass
        except Exception as exc:
            print(f"[fast] label '{label_text}': {exc}")
            continue

    # Способ 2: перебираем все radio и кликаем по очереди, проверяя,
    # появилось ли поле cardNumber.
    print("[fast] перебираю radio-ы")
    try:
        radios = await stripe_frame.locator(
            'input[name="payment-method-accordion-item-title"]'
        ).all()
        for i, r in enumerate(radios):
            try:
                await r.check(timeout=3_000, force=True)
                await asyncio.sleep(1.5)
                if await stripe_frame.locator(
                    'input[name="cardNumber"]'
                ).first.is_visible(timeout=1_500):
                    print(f"[fast] radio[{i}] раскрыл card-форму")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


async def _open_stripe_fast(context, account: Account, identity: Identity):
    """Быстрый путь до Stripe + адрес. Предполагает, что Devin уже залогинен
    (storage_state восстановлен). Возвращает (devin_page, stripe_frame)."""
    devin_page = await context.new_page()
    print(f"[fast] открываю app.devin.ai...")
    await devin_page.goto("https://app.devin.ai/", wait_until="domcontentloaded")
    await asyncio.sleep(1.5)

    cur_url = devin_page.url
    print(f"[fast] текущий URL: {cur_url}")
    if "/auth/" in cur_url or "/login" in cur_url:
        raise SystemExit(
            "Не залогинен — storage_state устарел. "
            "Запусти: .venv\\Scripts\\python.exe test_hcaptcha.py --login"
        )

    # Если уже на /org/<slug>/... — перейдём на /plans напрямую и
    # сразу жмём Start free trial, минуя «My Team → Upgrade».
    if "/org/" in cur_url:
        org_slug = cur_url.split("/org/")[1].split("/")[0]
        plans_url = f"https://app.devin.ai/org/{org_slug}/plans"
        print(f"[fast] navigate → {plans_url}")
        await devin_page.goto(plans_url, wait_until="domcontentloaded")
        await asyncio.sleep(2.5)

        # На /plans уже — сразу ищем кнопку start trial.
        plan_labels = (
            "Start free trial",
            "Start Free Trial",
            "Start trial",
            "Subscribe",
            "Continue",
            "Get started",
            "Upgrade",
        )
        print(f"[fast] click первую из {plan_labels}")
        if not await _click_first_match_async(devin_page, plan_labels):
            raise StepError("UI: не нашёл кнопку начала trial")
        await asyncio.sleep(2.5)
    else:
        # Не на /org/... — fallback на полный путь.
        print("[fast] не на /org/... — полный путь через My Team")
        await go_to_plans_and_start_trial_async(devin_page)
        await asyncio.sleep(2.5)

    stripe_frame = await find_stripe_checkout_frame_async(devin_page)
    if stripe_frame is None:
        raise StepError("Stripe iframe не найден")

    # Сначала пробуем встроенный select_card_payment_method.
    await select_card_payment_method_async(stripe_frame)
    await asyncio.sleep(2.0)

    # Если поле cardNumber не появилось — наш fallback с правильным radio.
    try:
        has_card = await stripe_frame.locator(
            'input[name="cardNumber"]'
        ).first.is_visible(timeout=1_500)
    except Exception:
        has_card = False

    if not has_card:
        print("[fast] cardNumber не виден — пробую _select_card_radio")
        if not await _select_card_radio(stripe_frame):
            raise StepError("не удалось раскрыть Card-форму в Stripe")

    print("[fast] заполняю адрес")
    await fill_address_async(stripe_frame, identity)
    return devin_page, stripe_frame


async def amain(args: argparse.Namespace) -> int:
    account = _pick_account(args.email)
    identity = _pick_identity(account)
    card = None if args.no_card else _pick_card(args.card)

    if not args.no_card and card is None:
        print("[warn] не нашёл подходящую карту — карту не буду вводить")

    print("=" * 60)
    print(" test_hcaptcha")
    print("=" * 60)
    print(f"Аккаунт: {account.email}")
    print(f"Identity: {identity.full_name}, {identity.street}")
    if card:
        print(f"Карта:   {card[:6]}...{card[-3:]}")
    print(f"State:   {STATE_PATH.name} "
          f"(exists={STATE_PATH.exists()})")
    print("=" * 60)

    if args.login and STATE_PATH.exists():
        STATE_PATH.unlink()
        print("[main] стёр старый state")

    has_state = STATE_PATH.exists()
    do_login = args.login or not has_state

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=False)
        try:
            if do_login:
                # Чистый context для логина.
                context = await browser.new_context()
                await _do_login(context, account)
                # Сохраняем state для следующих запусков.
                await context.storage_state(path=str(STATE_PATH))
                print(f"[main] state сохранён в {STATE_PATH.name}")
                # Закрываем context, открываем заново ИЗ state — чтобы
                # дальше работать ровно так же, как «быстрый путь».
                await context.close()

            # Быстрый путь.
            context = await browser.new_context(storage_state=str(STATE_PATH))
            try:
                devin_page, stripe_frame = await _open_stripe_fast(
                    context, account, identity
                )

                if args.no_card:
                    print("[main] --no-card — Stripe открыт, "
                          "адрес введён, выхожу")
                    print("[main] держу браузер 5 мин (Ctrl+C для выхода)")
                    await asyncio.sleep(300)
                    return 0

                if card is None:
                    raise SystemExit(
                        "Нет доступной карты. Положи в "
                        "'подтверждённые живые карты.txt' или "
                        "передай --card NUMBER|MM|YYYY|CVV"
                    )

                print(f"[main] вписываю карту {card[:6]}...{card[-3:]}")
                await fill_card_async(stripe_frame, card, identity.full_name)
                await check_consent_checkboxes_async(stripe_frame)

                print("[main] жму Подписаться")
                if not await click_submit_async(stripe_frame):
                    print("[main] кнопка submit не сработала — выхожу")
                    return 2

                # Ровно момент, ради которого всё затеяно:
                # тестируем _try_solve_hcaptcha_async.
                print()
                print(">" * 30, "HCAPTCHA TEST", "<" * 30)
                result = await _try_solve_hcaptcha_async(
                    devin_page, timeout_s=args.hcap_timeout
                )
                print(f">>> результат: {result!r}")
                print(">" * 73)
                print()

                if args.pause or result != "clicked":
                    print(f"[main] держу браузер открытым {args.pause_s}с "
                          "(Ctrl+C — выйти раньше).")
                    print("[main] можешь докликать капчу руками или "
                          "посмотреть DOM в DevTools (F12).")
                    try:
                        await asyncio.sleep(args.pause_s)
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        pass
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    return 0


def parse_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--login",
        action="store_true",
        help="принудительно перелогиниться (стирает state-файл)",
    )
    p.add_argument(
        "--email",
        default=None,
        help="email Devin-аккаунта (default — первый из 'аккаунты devin.txt')",
    )
    p.add_argument(
        "--card",
        default=None,
        help="конкретная карта в формате NUMBER|MM|YYYY|CVV",
    )
    p.add_argument(
        "--no-card",
        action="store_true",
        help="не заполнять карту — остановиться на адресе",
    )
    p.add_argument(
        "--pause",
        action="store_true",
        default=True,
        help="оставить браузер открытым после теста (default True)",
    )
    p.add_argument(
        "--no-pause",
        dest="pause",
        action="store_false",
        help="не оставлять браузер открытым",
    )
    p.add_argument(
        "--pause-s",
        type=int,
        default=300,
        help="на сколько секунд оставлять браузер (default 300)",
    )
    p.add_argument(
        "--hcap-timeout",
        type=float,
        default=30.0,
        help="таймаут поиска hCaptcha-iframe (default 30с)",
    )
    return p.parse_args(list(argv))


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n[main] Ctrl+C")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
