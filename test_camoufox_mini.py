"""test_camoufox_mini: запускает Camoufox, открывает mail-client.pinmx.com,
держит окно 5 минут чтобы можно было посмотреть.
"""

import asyncio
from camoufox.async_api import AsyncCamoufox


async def amain():
    async with AsyncCamoufox(headless=False, humanize=True) as browser:
        page = await browser.new_page()
        print("[info] навигирую на mail-client.pinmx.com")
        await page.goto("https://mail-client.pinmx.com/", wait_until="domcontentloaded")
        print(f"[info] URL после goto: {page.url}")
        try:
            title = await page.title()
            print(f"[info] title: {title!r}")
        except Exception as e:
            print(f"[info] title fail: {e}")

        try:
            html = await page.content()
            print(f"[info] HTML length: {len(html)}")
            # Покажем первые 1000 символов чтобы понять что внутри.
            print(f"[info] HTML[:1500]:\n{html[:1500]}")
        except Exception as e:
            print(f"[info] content fail: {e}")

        print()
        print("Браузер открыт 5 минут. Посмотри что там.")
        await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(amain())
