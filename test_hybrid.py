"""test_hybrid: гибридный flow.

Playwright (Chrome) — mail-client.pinmx.com (там цифровая капча,
которую ddddocr решает уверенно).
Camoufox (Firefox) — Devin + Stripe (там hCaptcha, которая может
пройти через stealth-фингерпринт Camoufox).

Передача данных между браузерами — обычные Python-переменные в одном
процессе (общая RAM).

Запуск::

    .venv\\Scripts\\python.exe test_hybrid.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright
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
    fill_address_async,
    find_identity_for_email,
    find_stripe_checkout_frame_async,
    go_to_plans_and_start_trial_async,
    login_to_mailclient_async,
    select_card_payment_method_async,
    submit_devin_code_async,
    wait_for_devin_email_code_async,
)
from devin_async import (
    DEVIN_LOGIN_URL,
    MAIL_LIST_ITEM_SELECTOR,
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


def _pick_account() -> Account:
    accounts = load_accounts(RESULTS_PATH)
    if not accounts:
        raise SystemExit("Нет аккаунтов")
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


# ---------------------------------------------------------------------------
# Devin login через Camoufox (без mail-страницы — она в другом браузере)
# ---------------------------------------------------------------------------


async def _devin_login_camoufox(
    devin_page,
    mail_page,
    account: Account,
) -> str:
    """Логин в Devin внутри Camoufox-Firefox.

    Mail-page живёт в Playwright-Chrome — мы передаём его сюда чтобы
    дёрнуть `wait_for_devin_email_code_async` (тот сам найдёт письмо).
    """
    # Baseline до отправки email на Devin.
    try:
        baseline = await mail_page.locator(MAIL_LIST_ITEM_SELECTOR).count()
    except Exception:
        baseline = 0
    print(f"[hybrid] baseline писем: {baseline}")

    print("[hybrid] открываю Devin login")
    await devin_page.goto(DEVIN_LOGIN_URL, wait_until="domcontentloaded")
    email_field = devin_page.get_by_role("textbox", name="Email address")
    await email_field.wait_for(state="visible", timeout=30_000)
    await email_field.fill(account.email)
    await devin_page.get_by_role("button", name="Log in", exact=True).click()
    print("[hybrid] клик 'Log in', жду код в почте...")

    code = await wait_for_devin_email_code_async(
        mail_page, baseline_count=baseline
    )
    print(f"[hybrid] получили код {code}")

    await submit_devin_code_async(devin_page, code)
    print(f"[hybrid] вошли в Devin: {devin_page.url}")
    return devin_page.url


async def amain() -> int:
    account = _pick_account()
    identity = _pick_identity(account)
    card = _pick_card()
    if card is None:
        print("[warn] нет карты — буду без карты, дойду до hCaptcha-теста")

    print("=" * 60)
    print(" test_hybrid")
    print("=" * 60)
    print(f"Mail browser:  Playwright Chrome")
    print(f"Devin browser: Camoufox Firefox (locale=ru-RU)")
    print(f"Аккаунт:       {account.email}")
    print(f"Identity:      {identity.full_name}, {identity.street}")
    if card:
        print(f"Карта:         {card[:6]}...{card[-3:]}")
    print("=" * 60)

    async with async_playwright() as pw:
        chrome_browser = await pw.chromium.launch(channel="chrome", headless=False)
        chrome_ctx = await chrome_browser.new_context()
        mail_page = await chrome_ctx.new_page()

        try:
            print("[step] [Chrome] login mail-client")
            await login_to_mailclient_async(mail_page, account)
            print("[step] [Chrome] mail OK")

            async with AsyncCamoufox(
                headless=False,
                humanize=True,
                locale="ru-RU",
            ) as fox_browser:
                devin_page = await fox_browser.new_page()

                try:
                    print("[step] [Camoufox] Devin login")
                    await _devin_login_camoufox(devin_page, mail_page, account)

                    print("[step] [Camoufox] My Team → Upgrade → trial")
                    await go_to_plans_and_start_trial_async(devin_page)
                    await asyncio.sleep(2.5)

                    stripe_frame = await find_stripe_checkout_frame_async(
                        devin_page
                    )
                    if stripe_frame is None:
                        raise StepError("Stripe iframe не найден")
                    await select_card_payment_method_async(stripe_frame)
                    await asyncio.sleep(2.0)

                    print("[step] [Camoufox] адрес")
                    await fill_address_async(stripe_frame, identity)

                    if card:
                        print(f"[step] [Camoufox] карта {card[:6]}...{card[-3:]}")
                        await fill_card_async(
                            stripe_frame, card, identity.full_name
                        )
                        await check_consent_checkboxes_async(stripe_frame)

                        print("[step] [Camoufox] submit")
                        await click_submit_async(stripe_frame)

                        # Жду hCaptcha checkbox-iframe в Camoufox.
                        print("[step] [Camoufox] жду hCaptcha (до 30с)")
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
                            print("[step] [Camoufox] hCaptcha НЕ появилась — "
                                  "Stripe сразу обработал (или ошибка)")
                        else:
                            print(f"[step] [Camoufox] iframe найден")
                            cb = cb_iframe.locator("#checkbox").first
                            await cb.click(timeout=4_000)
                            print("[step] [Camoufox] кликнул #checkbox")

                            # Ждём 20с — aria-checked='true' или детач.
                            deadline2 = asyncio.get_event_loop().time() + 20.0
                            verdict = "unknown"
                            while asyncio.get_event_loop().time() < deadline2:
                                try:
                                    if cb_iframe.is_detached():
                                        verdict = "detached"
                                        break
                                except Exception:
                                    pass
                                try:
                                    checked = await cb.get_attribute("aria-checked")
                                    if checked == "true":
                                        verdict = "checked-true"
                                        break
                                except Exception:
                                    pass
                                await asyncio.sleep(0.5)

                            print()
                            print(">" * 30, "ВЕРДИКТ hCAPTCHA", "<" * 30)
                            print(f">>> {verdict}")
                            if verdict == "unknown":
                                try:
                                    final = await cb.get_attribute(
                                        "aria-checked", timeout=500
                                    )
                                    print(f">>> aria-checked={final!r}")
                                except Exception:
                                    pass
                            print(">" * 78)

                    print()
                    print("Окно открыто 5 мин (Ctrl+C чтобы выйти).")
                    try:
                        await asyncio.sleep(300)
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        pass
                finally:
                    pass
        finally:
            try:
                await chrome_ctx.close()
            except Exception:
                pass
            try:
                await chrome_browser.close()
            except Exception:
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
