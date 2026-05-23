"""test_camoufox: тестирует прохождение hCaptcha через Camoufox.

Camoufox — patched Firefox с инъекциями anti-fingerprint на C++ уровне
(не JS-патчи, как Patchright). По отзывам, проходит hCaptcha без
визуальных задач даже с silent click.

Этот скрипт:
1. Запускает Camoufox.
2. Логинится в почту → Devin.
3. Идёт через My Team → Upgrade → Start free trial → Stripe.
4. Заполняет адрес + карту, жмёт «Подписаться».
5. Ждёт hCaptcha и пытается её кликнуть.
6. Печатает aria-checked после клика.

Запуск::

    .venv\\Scripts\\python.exe test_camoufox.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from camoufox.async_api import AsyncCamoufox

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
    check_consent_checkboxes_async,
    click_submit_async,
    fill_card_async,
)


ROOT = Path(__file__).parent


def _pick_account() -> Account:
    accounts = load_accounts(RESULTS_PATH)
    if not accounts:
        raise SystemExit(f"Нет аккаунтов в {RESULTS_PATH.name}")
    by_email = {a.email.lower(): a for a in accounts}
    if DEVIN_DONE_PATH.exists():
        for raw in DEVIN_DONE_PATH.read_text(encoding="utf-8").splitlines():
            email = raw.strip().lstrip("\ufeff").lower()
            if email and email in by_email:
                return by_email[email]
    return accounts[0]


def _pick_identity(account: Account) -> Identity:
    identity = find_identity_for_email(account.email)
    if identity is None:
        raise SystemExit(f"Нет identity для {account.email}")
    return identity


def _pick_card() -> str | None:
    for path in (CONFIRMED_LIVE_CARDS_PATH, LIVE_CARDS_PATH):
        for line in load_existing_live_cards(path):
            cred = extract_card_credentials(line) or line.strip()
            if cred and cred.count("|") == 3:
                return cred
    return None


async def amain() -> int:
    account = _pick_account()
    identity = _pick_identity(account)
    card = _pick_card()

    print(f"Аккаунт: {account.email}")
    print(f"Identity: {identity.full_name}, {identity.street}")
    print(f"Карта:   {card[:6] if card else '?'}...{card[-3:] if card else '?'}")

    async with AsyncCamoufox(
        headless=False,
        humanize=True,  # Имитация человеческих паттернов на C++ уровне
        locale="ru-RU",  # Русская локаль (mail-client.pinmx.com — русский UI)
    ) as browser:
        context = browser
        # AsyncCamoufox возвращает уже готовый context.
        mail_page = await context.new_page()
        devin_page = await context.new_page()

        print("[step] логин в mail-client")
        await login_to_mailclient_async(mail_page, account)

        print("[step] логин в Devin")
        await login_to_devin_async(devin_page, mail_page, account)

        print("[step] My Team → Upgrade → trial")
        await go_to_plans_and_start_trial_async(devin_page)
        await asyncio.sleep(2.5)

        stripe_frame = await find_stripe_checkout_frame_async(devin_page)
        if stripe_frame is None:
            raise StepError("Stripe iframe не найден")
        await select_card_payment_method_async(stripe_frame)
        await asyncio.sleep(2.0)

        print("[step] адрес")
        await fill_address_async(stripe_frame, identity)

        if card:
            print(f"[step] карта {card[:6]}...{card[-3:]}")
            await fill_card_async(stripe_frame, card, identity.full_name)
            await check_consent_checkboxes_async(stripe_frame)

            print("[step] submit")
            await click_submit_async(stripe_frame)

            # Жду hCaptcha checkbox-iframe.
            print("[step] жду hCaptcha (до 30с)")
            cb_iframe = None
            deadline = asyncio.get_event_loop().time() + 30.0
            while asyncio.get_event_loop().time() < deadline:
                for fr in devin_page.frames:
                    url = (fr.url or "").lower()
                    if (
                        "hcaptcha.com" in url
                        and "frame=checkbox" in url
                        and "frame=checkbox-invisible" not in url
                    ):
                        cb_iframe = fr
                        break
                if cb_iframe:
                    break
                await asyncio.sleep(0.5)

            if cb_iframe is None:
                print("[step] hCaptcha не появилась")
            else:
                print(f"[step] iframe найден: {cb_iframe.url[:80]}")
                # Простой клик по #checkbox.
                cb = cb_iframe.locator("#checkbox").first
                await cb.click(timeout=4_000)
                print("[step] кликнул #checkbox")

                await asyncio.sleep(3.0)
                checked = await cb.get_attribute("aria-checked")
                print(f"[step] aria-checked={checked!r}")

                # Подождём 20с — возможно hCaptcha сама верифицирует.
                deadline2 = asyncio.get_event_loop().time() + 20.0
                while asyncio.get_event_loop().time() < deadline2:
                    try:
                        if cb_iframe.is_detached():
                            print("[step] iframe детачен — капча прошла!")
                            break
                    except Exception:
                        pass
                    try:
                        if await cb.get_attribute("aria-checked") == "true":
                            print("[step] aria-checked=true — капча прошла!")
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)

        print()
        print("Браузер открыт 5 минут (или Ctrl+C). Можешь смотреть DOM.")
        try:
            await asyncio.sleep(300)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass

    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nCtrl+C")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
