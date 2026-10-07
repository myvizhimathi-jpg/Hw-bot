"""
FirstCry Hot Wheels watcher
- PUTHU product vandhaalum, out-of-stock -> in-stock aanaalum Telegram alert anuppum
- In stock = card-la "ADD TO CART" irundha mattum
"""
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

import requests
from playwright.async_api import async_playwright

BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "PASTE_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID", "PASTE_CHAT_ID")

LISTING_URLS = [
    "https://www.firstcry.com/hotwheels/5/0/113?sort=bestseller&q=as_hotwheels&asid=53241#sort=newarrivals",
]

MIN_WAIT, MAX_WAIT = 60, 90
STATE_FILE = Path(__file__).with_name("hw_state.json")
STATE_VERSION = 2

OUT_OF_STOCK_WORDS = (
    "out of stock",
    "notify me",
    "sold out",
    "currently unavailable",
    "coming soon",
)

JS_EXTRACT = """
() => {
  const slugOf = h => {
    const p = h.split('?')[0].split('/').filter(Boolean);
    return p[p.length - 3];
  };
  const anchors = [...document.querySelectorAll('a[href*="product-detail"]')]
    .filter(a => !a.href.includes('ref2=brand_listing'));
  const out = [];
  const seen = new Set();
  anchors.forEach(a => {
    const slug = slugOf(a.href);
    if (!slug || seen.has(slug)) return;
    seen.add(slug);
    let node = a;
    for (let i = 0; i < 8; i++) {
      const p = node.parentElement;
      if (!p || !p.innerText || p.innerText.length > 1500) break;
      const slugs = new Set(
        [...p.querySelectorAll('a[href*="product-detail"]')].map(x => slugOf(x.href))
      );
      if (slugs.size > 1) break;
      node = p;
    }
    out.push({slug: slug, href: a.href.split('?')[0], text: node.innerText || ''});
  });
  return out;
}
"""


def tg(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=15,
        )
    except Exception as e:
        print("Telegram error:", e)


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
        except Exception:
            return None
        if data.get("_v") != STATE_VERSION:
            return None  # pazhaya format -> fresh baseline
        return data
    return None


def save_state(state):
    state["_v"] = STATE_VERSION
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def has_products(state):
    return any(not k.startswith("_") for k in (state or {}))


async def scrape(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    for _ in range(3):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(1200)
    items = await page.evaluate(JS_EXTRACT)
    result = {}
    for it in items:
        txt = " ".join(it["text"].split()).lower()
        in_stock = ("add to cart" in txt) and not any(w in txt for w in OUT_OF_STOCK_WORDS)
        result[it["slug"]] = {
            "name": it["slug"].replace("-", " ").title()[:100],
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
    duration = 0
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
                for key, d in current.items():
                    print(("IN " if d["in_stock"] else "OUT"), d["name"][:70])
                break

            if not current:
                empty_count += 1
                if empty_count == 3:
                    state = state or {}
                    if not state.get("_warned"):
                        tg("⚠️ Bot-ku products kedaikala (site block pannirukkalam / page maariruchu). Check pannunga.")
                        state["_warned"] = True
                        save_state(state)
            else:
                empty_count = 0
                if not has_products(state):
                    state = dict(current)
                    save_state(state)
                    top = ", ".join(d["name"][:35] for d in list(current.values())[:3])
                    in_count = sum(1 for d in current.values() if d["in_stock"])
                    tg(
                        f"✅ Bot start aachu. {len(current)} products track pannuren "
                        f"({in_count} in stock).\nTop 3: {top}"
                    )
                else:
                    for key, d in current.items():
                        old = state.get(key)
                        if old is None and d["in_stock"]:
                            tg(f"🆕 PUTHU LISTING!\n{d['name']}\n{d['url']}")
                        elif old and not old.get("in_stock") and d["in_stock"]:
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
