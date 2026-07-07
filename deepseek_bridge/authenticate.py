from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from .bridge import CHAT_URL, COMPOSER_SELECTORS, profile_dir


async def main() -> int:
    data_dir = profile_dir()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(data_dir),
        headless=False,
        viewport={"width": 1280, "height": 900},
    )
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60_000)

    print("Complete DeepSeek login in the opened browser.", flush=True)
    print("Waiting up to five minutes for the chat input...", flush=True)

    authenticated = False
    deadline = asyncio.get_running_loop().time() + 300
    while asyncio.get_running_loop().time() < deadline and not authenticated:
        for selector in COMPOSER_SELECTORS:
            locator = page.locator(selector).last
            if await locator.count() and await locator.is_visible():
                authenticated = True
                break
        if not authenticated:
            await asyncio.sleep(1)

    await context.close()
    await playwright.stop()
    if authenticated:
        print("Authentication profile saved.")
        return 0
    print("A chat input was not detected; authentication may be incomplete.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
