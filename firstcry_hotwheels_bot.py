"""
FirstCry Hot Wheels watcher
- Listing page-a every 60-90 sec check pannum
- PUTHU product vandhaalum, out-of-stock -> in-stock aanaalum Telegram-la alert anuppum

Setup:
  pip install playwright requests
  playwright install chromium

Run:
  export TG_BOT_TOKEN="123456:ABC..."
  export TG_CHAT_ID="123456789"
  python firstcry_hotwheels_bot.py --once    # test: enna kandupidikkuthu nu print pannum
  python firstcry_hotwheels_bot.py           # continuous watch
"""
import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from playwright.async_api import async_playwright

BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "PASTE_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID", "PASTE_CHAT_ID")

# FirstCry-la Hot Wheels listing page URL-a inga podunga (newest first sort pannina nalladhu).
# Rendu moonu pages venumna list-la serunga.
LISTING_URLS = [
    "https://www.firstcry.com/search?q=hot+wheels",
]

MIN_WAIT, MAX_WAIT = 60, 90          # seconds between checks
STATE_FILE = Path(__file__).with_name("hw_state.json")

OUT_OF_STOCK_WORDS = ("out of stock", "notify me", "sold out")

JS_EXTRACT = """
() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="product-detail"]').forEach(a => {
    const href = a.href.split('?')[0];
    if (seen.has(href)) return;
    seen.add(href);
    let node = a, text = '';
    for (let i = 0; i < 5 && node; i++) {
      node = node.parentElement;
      if (node && node.innerText && node.innerText.length > text.length
          && node.innerText.length < 600) text = node.innerText;
    }
    out.push({href, text: (text || a.innerText || '').replace(/\\s+/g, ' ').trim()});
  });
  return out;
}
"""


def tg(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": False},
            timeout=15,
        )
    except Exception as e:
        print("Telegram error:", e)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def product_id(href: str) -> str:
    m = re.search(r"/(\d+)/product-detail", href)
    return m.group(1) if m else href


async def scrape(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)
    # lazy-load aagura items vara scroll
    for _ in range(4):
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(1200)
    items = await page.evaluate(JS_EXTRACT)
    result = {}
    for it in items:
        txt = it["text"]
        in_stock = not any(w in txt.lower() for w in OUT_OF_STOCK_WORDS)
        result[product_id(it["href"])] = {
            "name": txt[:120],
            "url": it["href"],
            "in_stock": in_stock,
        }
    return result


async def check_once(page):
    current = {}
    for url in LISTING_URLS:
        try:
            current.update(await scrape(page, url))
        except Exception as e:
            print("Scrape error:", e)
    return current


async def main():
    once = "--once" in sys.argv
    duration = 0  # GitHub Actions-ku: indha seconds varaikkum loop pannitu exit aagum
    if "--duration" in sys.argv:
        duration = int(sys.argv[sys.argv.index("--duration") + 1])
    start = time.time()
    state = load_state()
    empty_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="en-IN",
            viewport={"width": 1366, "height": 900},
        )
        page = await ctx.new_page()

        while True:
            current = await check_once(page)
            print(f"Found {len(current)} products")

            if once:
                for pid, d in current.items():
                    print(("IN " if d["in_stock"] else "OUT"), pid, d["name"][:70])
                break

            if not current:
                empty_count += 1
                if empty_count == 3:
                    state = state or {}
                    if not state.get("_warned"):  # spam aagama oru dhadava mattum
                        tg("⚠️ Bot-ku products kedaikala (site block pannirukkalam / page maariruchu). Check pannunga.")
                        state["_warned"] = True
                        save_state(state)
            else:
                empty_count = 0
                if not any(not k.startswith("_") for k in (state or {})):
                    state = dict(current)
                    save_state(state)
                    tg(f"✅ Bot start aachu. {len(current)} products track pannuren.")
                else:
                    for pid, d in current.items():
                        old = state.get(pid)
                        if old is None and d["in_stock"]:
                            tg(f"🆕 PUTHU LISTING!\n{d['name']}\n{d['url']}")
                        elif old and not old["in_stock"] and d["in_stock"]:
                            tg(f"🔥 RESTOCK!\n{d['name']}\n{d['url']}")
                    state.pop("_warned", None)
                    state.update(current)
                    save_state(state)

            if duration and time.time() - start > duration:
                break
            await asyncio.sleep(random.uniform(MIN_WAIT, MAX_WAIT))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
