"""Тест запуска нескольких изолированных браузеров Camoufox с расширением.

Каждый браузер получает свою копию профиля с расширением.

Запуск:
    .venv/Scripts/python.exe test_multiple_browsers.py
"""

import asyncio
import shutil
from pathlib import Path
from camoufox.async_api import AsyncNewBrowser
from playwright.async_api import async_playwright


async def launch_browser_with_profile(browser_id: int, base_profile: Path, temp_dir: Path):
    """Запустить один браузер с изолированным профилем."""

    # Создать копию профиля для этого браузера
    profile_copy = temp_dir / f"profile_{browser_id}"

    if profile_copy.exists():
        shutil.rmtree(profile_copy)

    shutil.copytree(base_profile, profile_copy)
    print(f"[Browser {browser_id}] Profil' skopirovan: {profile_copy}")

    async with async_playwright() as playwright:
        try:
            # Запустить браузер с копией профиля
            browser = await AsyncNewBrowser(
                playwright,
                headless=False,
                humanize=True,
                persistent_context=True,
                user_data_dir=str(profile_copy),
            )

            page = await browser.new_page()
            await page.goto("https://www.whatismybrowser.com/")

            print(f"[Browser {browser_id}] Zapushchen i otkryta stranitsa")

            # Держать браузер открытым
            await asyncio.sleep(3600)  # 1 час

        except Exception as e:
            print(f"[Browser {browser_id}] Oshibka: {e}")
        finally:
            try:
                await browser.close()
                print(f"[Browser {browser_id}] Zakryt")
            except:
                pass


async def test_multiple_browsers(num_browsers: int = 3):
    """Запустить несколько изолированных браузеров параллельно."""

    # Базовый профиль с расширением
    base_profile = Path(r"C:\Users\Admin\ai\create_accs\playwright_firefoxdev_profile-n5CT4n")

    if not base_profile.exists():
        print(f"[ERROR] Bazovyy profil' ne nayden: {base_profile}")
        return

    # Временная папка для копий профилей
    temp_dir = Path(r"C:\Users\Admin\ai\create_accs\temp_profiles")
    temp_dir.mkdir(exist_ok=True)

    print(f"[OK] Bazovyy profil': {base_profile}")
    print(f"[OK] Vremennaya papka: {temp_dir}")
    print(f"\n=== Zapusk {num_browsers} brauzerov ===\n")

    # Запустить все браузеры параллельно
    tasks = [
        launch_browser_with_profile(i, base_profile, temp_dir)
        for i in range(1, num_browsers + 1)
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\n[OK] Zavershenie vsekh brauzerov...")


if __name__ == "__main__":
    try:
        # Запустить 2 браузера для теста (можно изменить количество)
        asyncio.run(test_multiple_browsers(num_browsers=2))
    except KeyboardInterrupt:
        print("\n[OK] Test zavershen")
