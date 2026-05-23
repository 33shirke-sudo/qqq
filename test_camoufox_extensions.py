"""Тест запуска Camoufox с расширениями из профиля.

Запуск:
    .venv/Scripts/python.exe test_camoufox_extensions.py
"""

import asyncio
from pathlib import Path
from camoufox.async_api import AsyncNewBrowser
from playwright.async_api import async_playwright


async def test_camoufox_with_extensions():
    """Запустить Camoufox с профилем, содержащим расширения."""

    # Путь к профилю Firefox с установленным расширением
    profile_path = Path(r"C:\Users\Admin\ai\create_accs\playwright_firefoxdev_profile-n5CT4n")

    if not profile_path.exists():
        print(f"[ERROR] Profil' ne nayden: {profile_path}")
        return

    print(f"[OK] Ispol'zuetsya profil': {profile_path}")
    print("\n=== Zapusk Camoufox s profilem ===")
    print("Brauzer otkroetsya v vidimom rezhime s ustanovlennym rasshireniem 1VPN.")
    print("Prover'te, chto rasshirenie zagruzheno (ikonka v paneli instrumentov).")
    print("Nazhmite Ctrl+C v konsoli, kogda zakonchite proverku.\n")

    async with async_playwright() as playwright:
        # Запустить с persistent context (профилем)
        browser = await AsyncNewBrowser(
            playwright,
            headless=False,
            humanize=True,
            persistent_context=True,
            user_data_dir=str(profile_path),
        )
        # Для persistent context browser - это уже context
        page = await browser.new_page()

        # Открыть тестовую страницу
        await page.goto("https://www.whatismybrowser.com/")

        print("[OK] Brauzer zapushchen")
        print("[OK] Stranitsa zagruzhena")
        print("\nProver'te rasshireniya v brauzere...")
        print("Nazhmite Ctrl+C kogda zakonchite.\n")

        # Ждем, пока пользователь не нажмет Ctrl+C
        try:
            await asyncio.sleep(3600)  # 1 час
        except KeyboardInterrupt:
            print("\n[OK] Zavershenie...")
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(test_camoufox_with_extensions())
    except KeyboardInterrupt:
        print("\n[OK] Test zavershen")
