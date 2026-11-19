import asyncio, sys
from patchright.async_api import async_playwright

async def main():
    site = sys.argv[1] if len(sys.argv) > 1 else "https://chatgpt.com/"
    pw = await async_playwright().start()
    b = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
    ctx = b.contexts[0] if b.contexts else await b.new_context()
    page = None
    for p in ctx.pages:
        if p.url.startswith(site.split("/")[0] + "//" + site.split("/")[2]):
            page = p
            break
    if page is None:
        page = await ctx.new_page()
    await page.goto(site, wait_until="domcontentloaded")
    await asyncio.sleep(4)
    print("reset ->", page.url)
    await pw.stop()

asyncio.run(main())
