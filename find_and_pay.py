"""find_and_pay: объединяет два пайплайна в одном браузере и одной
сессии Playwright.

Что происходит за один запуск
=============================

1. Открывается единый Chromium-браузер (по выбору ``--browser-mode``).
2. **Параллельно**, в одном ``BrowserContext``:
   * **Таски Devin** (2 вкладки):
     ``open_devin_trial_with_address`` — логин в почту, логин в Devin
     (через email-код), переход «My Team → Upgrade → Start free trial»,
     раскрытие Card-accordion в Stripe Checkout и ввод адреса +
     имени держателя. **Карта НЕ вводится** — пользователь сделает это
     вручную после подтверждённых живых карт.
   * **Таски чекинга** (N вкладок, ``--tabs``, default 3):
     ``run_check_cards_pipeline`` — генерация карт из BIN-ов
     (``бины.txt``), параллельный чекинг на chkr.cc, сбор живых карт
     в ``живые карты.txt``, фаза 2 (recheck) → ``подтверждённые
     живые карты.txt``.
3. Когда обе фазы завершены — Stripe-окно остаётся открытым,
   скрипт «висит» на ``input(...)``, ожидая Enter. Пользователь видит
   введённый адрес и подтверждённые карты — может вставить любую из них.

Запуск (из папки проекта)::

    .venv\\Scripts\\python.exe find_and_pay.py [опции]

Опции
-----
--email EMAIL          какой Devin-аккаунт использовать (default — первый
                       в ``аккаунты devin.txt``).
--quantity N           карт на партию для чекинга (default 10).
--tabs N               параллельных вкладок чекинга (default 3).
--check-timeout S      таймаут одной партии (default 300с).
--no-recheck           пропустить фазу 2 (recheck).
--keep-existing        не очищать ``живые карты.txt`` /
                       ``подтверждённые живые карты.txt`` перед запуском.
--browser-mode MODE    clean / incognito / system (default clean).
--head / --headless    показать или скрыть окно (default — показать).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Iterable

from playwright.async_api import async_playwright

from browser_modes import VALID_MODES, DEFAULT_MODE, launch_browser_async
from check_cards import (
    BINS_PATH,
    load_bins,
    run_check_cards_pipeline,
    _DEFAULT_CHECK_TIMEOUT_S,
)
from devin_async import (
    Identity,
    find_identity_for_email,
    open_devin_trial_with_address,
)
from register_devin import (
    Account,
    DEVIN_DONE_PATH,
    RESULTS_PATH,
    load_accounts,
)
from logging_utils import setup_logging, log_step


# ---------------------------------------------------------------------------
# Поиск аккаунта + identity
# ---------------------------------------------------------------------------


def pick_account(email_hint: str | None) -> Account:
    """Найти аккаунт в ``имейлы pingmx.txt``.

    * Если ``email_hint`` задан — берём его (требуем точное совпадение).
    * Иначе берём первый из ``аккаунты devin.txt`` (читаем файл по
      порядку, не через ``load_done``, потому что тот возвращает
      ``set`` и порядок не сохраняет).
    """
    accounts = load_accounts(RESULTS_PATH)
    if not accounts:
        raise SystemExit(
            f"В {RESULTS_PATH.name} нет аккаунтов.\n\n"
            f"Это шаг 3 пайплайна. Сначала нужно:\n"
            f"  1. Создать email-аккаунты: .venv\\Scripts\\python.exe create_emails.py --debug --limit 5\n"
            f"  2. Зарегистрировать Devin: .venv\\Scripts\\python.exe register_devin.py --debug --limit 5\n"
        )

    by_email = {a.email.lower(): a for a in accounts}

    if email_hint:
        key = email_hint.strip().lower()
        if key not in by_email:
            raise SystemExit(
                f"В {RESULTS_PATH.name} нет аккаунта {email_hint!r}. "
                "Доступные: " + ", ".join(sorted(by_email))
            )
        return by_email[key]

    # Читаем 'аккаунты devin.txt' по порядку и возвращаем первый,
    # который есть в имейлах pingmx.
    if DEVIN_DONE_PATH.exists():
        for raw in DEVIN_DONE_PATH.read_text(encoding="utf-8").splitlines():
            email = raw.strip().lstrip("\ufeff").lower()
            if email and email in by_email:
                return by_email[email]

    return accounts[0]


def pick_identity(account: Account) -> Identity:
    identity = find_identity_for_email(account.email)
    if identity is None:
        raise SystemExit(
            f"Для {account.email} нет identity в личности.txt.\n\n"
            f"Это шаг 3 пайплайна. Перед find_and_pay.py нужно:\n"
            f"  1. Создать email: .venv\\Scripts\\python.exe create_emails.py --debug\n"
            f"  2. Зарегистрировать Devin: .venv\\Scripts\\python.exe register_devin.py --debug\n"
            f"  3. Сгенерировать identity: .venv\\Scripts\\python.exe add_identities.py\n"
        )
    return identity


# ---------------------------------------------------------------------------
# Async-main
# ---------------------------------------------------------------------------


async def amain(args: argparse.Namespace) -> int:
    logger = setup_logging(debug=args.debug, log_to_file=True)
    logger.info(f"find_and_pay запущен с флагами: {args}")

    account = pick_account(args.email)
    identity = pick_identity(account)

    bins = load_bins(BINS_PATH)
    if not bins:
        raise SystemExit(
            f"В {BINS_PATH.name} нет BIN-ов (или все закомментированы). "
            "Положи туда хотя бы один 6-значный BIN."
        )

    logger.info(f"Devin аккаунт: {account.email}")
    logger.info(f"Identity: {identity.full_name}, {identity.street}, {identity.zip_code}, {identity.city}")
    logger.info(f"BIN-ов: {len(bins)}, вкладок чека: {args.tabs}, quantity={args.quantity}")

    print("=" * 60)
    print(" find_and_pay")
    print("=" * 60)
    print(f"Devin аккаунт: {account.email}")
    print(f"Identity:      {identity.full_name}, {identity.street}, "
          f"{identity.zip_code}, {identity.city}")
    print(f"BIN-ов:        {len(bins)} ({', '.join(bins)})")
    print(f"Вкладок чека:  {args.tabs}, quantity={args.quantity}")
    print(f"Браузер:       --browser-mode={args.browser_mode}, "
          f"{'visible' if args.head else 'headless'}")
    print("=" * 60)
    print()

    async with async_playwright() as pw:
        _browser, context, cleanup = await launch_browser_async(
            pw, args.browser_mode, head=args.head
        )
        try:
            log_step(logger, "main", "стартую обе фазы параллельно")
            print("[main] стартую обе фазы параллельно...")
            print("  - Devin: логин → Stripe Checkout → ввод адреса")
            print(f"  - Check: {args.tabs} вкладок чекинга на chkr.cc")
            print()

            devin_task = asyncio.create_task(
                open_devin_trial_with_address(
                    context,
                    account=account,
                    identity=identity,
                ),
                name="devin-trial",
            )
            check_task = asyncio.create_task(
                run_check_cards_pipeline(
                    context,
                    bins=bins,
                    quantity=args.quantity,
                    tabs=args.tabs,
                    check_timeout_s=args.check_timeout,
                    do_recheck=not args.no_recheck,
                    clean_first=not args.keep_existing,
                ),
                name="check-cards",
            )

            results = await asyncio.gather(
                devin_task, check_task, return_exceptions=True
            )

            devin_result, check_result = results

            print()
            print("=" * 60)
            print(" Итоги")
            print("=" * 60)

            if isinstance(devin_result, BaseException):
                logger.error(f"[devin] ОШИБКА: {devin_result!r}")
                print(f"[devin] ОШИБКА: {devin_result!r}")
            else:
                logger.info(f"[devin] OK — Stripe Checkout открыт с адресом ({account.email})")
                print(f"[devin] OK — Stripe Checkout открыт с адресом "
                      f"({account.email})")

            if isinstance(check_result, BaseException):
                logger.error(f"[check] ОШИБКА: {check_result!r}")
                print(f"[check] ОШИБКА: {check_result!r}")
            else:
                phase1, phase2 = check_result
                logger.info(f"[check] OK — фаза 1: +{phase1} живых, фаза 2: +{phase2} подтверждённых")
                print(f"[check] OK — фаза 1: +{phase1} живых, "
                      f"фаза 2: +{phase2} подтверждённых")

            print("=" * 60)

            if isinstance(devin_result, BaseException):
                # Если Devin-флоу упал — нет смысла держать окно с адресом.
                return 2

            print()
            print("Окно браузера открыто. Stripe Checkout с введённым адресом —")
            print("во вкладке Devin. Подтверждённые карты — в файле:")
            print(r"   pinmx-mailer\pinmx-mailer\подтверждённые живые карты.txt")
            print()
            print("Когда закончишь работу с картой — нажми Enter здесь, ")
            print("и я закрою браузер.")
            print()
            try:
                # Запускаем blocking input() в отдельном потоке, чтобы
                # не блокировать event loop.
                await asyncio.get_event_loop().run_in_executor(
                    None, input, "Press Enter to close browser... "
                )
            except (EOFError, KeyboardInterrupt):
                logger.info("[main] прерывание, закрываю браузер")
                print("\n[main] прерывание, закрываю браузер")
        finally:
            await cleanup()

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--email",
        default=None,
        help=(
            "email Devin-аккаунта (default — первый из "
            "'аккаунты devin.txt')."
        ),
    )
    p.add_argument(
        "--quantity",
        type=int,
        default=10,
        help="сколько карт генерировать на каждый BIN (default 10)",
    )
    p.add_argument(
        "--tabs",
        type=int,
        default=3,
        help="параллельных вкладок чекинга (default 3)",
    )
    p.add_argument(
        "--check-timeout",
        type=float,
        default=_DEFAULT_CHECK_TIMEOUT_S,
        metavar="SECONDS",
        help=(
            "максимальное время одной партии чекинга "
            f"(default {int(_DEFAULT_CHECK_TIMEOUT_S)}с)"
        ),
    )
    p.add_argument(
        "--no-recheck",
        action="store_true",
        help="пропустить финальную пере-проверку живых карт",
    )
    p.add_argument(
        "--keep-existing",
        action="store_true",
        help=(
            "не очищать 'живые карты.txt' / 'подтверждённые живые карты.txt' "
            "перед запуском (по умолчанию — очищаются)"
        ),
    )
    p.add_argument(
        "--browser-mode",
        choices=VALID_MODES,
        default=DEFAULT_MODE,
        help=(
            "Режим запуска Chrome: 'clean' (default), 'incognito', "
            "'system' (требует ./browser_profile/, см. copy_chrome_profile.py)."
        ),
    )
    p.add_argument(
        "--head",
        dest="head",
        action="store_true",
        default=True,
        help="показать окно браузера (default)",
    )
    p.add_argument(
        "--headless",
        dest="head",
        action="store_false",
        help="скрыть окно браузера",
    )
    p.add_argument("--debug", action="store_true", help="включить детальное логирование")
    return p.parse_args(list(argv))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n[main] Ctrl+C")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
