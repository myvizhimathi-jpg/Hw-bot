"""
FirstCry Hot Wheels watcher (v4)
- Page-la "New Arrivals" sort-a click pannitu padikkum
- PUTHU product / restock-ku Telegram alert
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

MIN_WAIT, MAX_WAIT = 15, 25
STATE_FILE = Path(__file__).with_name("hw_state.json")
STATE_VERSION = 3

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


async def click_visible(page, text, exact):
    loc = page.get_by_text(text, exact=exact)
    try:
        n = await loc.count()
    except Exception:
        return False
    for i in range(n):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                await el.click(timeout=4000)
                return True
        except Exception:
            continue
    return False


async def apply_new_arrivals(page):
    """Sort dropdown-la 'New Arrivals' select pannum. Success na True."""
    for opener in (None, "Sort by", "Best Seller"):
        if opener:
            await click_visible(page, opener, False)
            await page.wait_for_timeout(800)
        if await click_visible(page, "New Arrivals", True):
            await page.wait_for_timeout(4500)
            return True
    return False


async def scrape(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    sort_ok = await apply_new_arrivals(page)
    if "product-detail" in page.url:  # thappaa vera page-ku poiduchu
        sort_ok = False
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
    for _ in range(2):
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
    return result, sort_ok


async def check_once(page):
    current = {}
    sort_ok = True
    for url in LISTING_URLS:
        try:
            res, ok = await scrape(page, url)
            current.update(res)
            sort_ok = sort_ok and ok
        except Exception as e:
            print("Scrape error:", e)
            sort_ok = False
    return current, sort_ok


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
            current, sort_ok = await check_once(page)
            print(f"Found {len(current)} products | sort_ok={sort_ok}")

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
                    top = "\n".join("- " + d["name"][:60] for d in list(current.values())[:5])
                    in_count = sum(1 for d in current.values() if d["in_stock"])
                    tg(
                        f"✅ Bot start aachu. {len(current)} products ({in_count} in stock).\n"
                        f"New Arrivals sort: {'OK' if sort_ok else 'FAILED'}\n"
                        f"Top 5:\n{top}"
                    )
                else:
                    if sort_ok:
                        # one-time self test: oru product-a state-la irundhu eduthu fake "puthu" alert
                        test_key = None
                        if not state.get("_selftest"):
                            test_key = next((k for k, d in current.items() if d["in_stock"]), None)
                            if test_key:
                                state.pop(test_key, None)
                            state["_selftest"] = True
                        for key, d in current.items():
                            old = state.get(key)
                            tag = "🧪 TEST - " if key == test_key else ""
                            if old is None and d["in_stock"]:
                                tg(f"{tag}🆕 PUTHU LISTING!\n{d['name']}\n{d['url']}")
                            elif old and not old.get("in_stock") and d["in_stock"]:
                                tg(f"🔥 RESTOCK!\n{d['name']}\n{d['url']}")
                        # state update: in-stock -> out-of-stock-ku 3 dhadava thodarndhu paartha apram thaan maathum
                        for key, d in current.items():
                            old = state.get(key)
                            if old is None or d["in_stock"] or not old.get("in_stock"):
                                state[key] = {**d, "miss": 0}
                            else:
                                miss = old.get("miss", 0) + 1
                                if miss >= 3:
                                    state[key] = {**d, "miss": 0}
                                else:
                                    old["miss"] = miss
                        state.pop("_sortwarned", None)
                    elif not state.get("_sortwarned"):
                        tg("⚠️ New Arrivals sort apply aagala. Alerts nirutthi vechirukken. Telegram-la sollunga, fix pannuren.")
                        state["_sortwarned"] = True
                    state.pop("_warned", None)
                    save_state(state)

            if duration and time.time() - start > duration:
                break
            await asyncio.sleep(random.uniform(MIN_WAIT, MAX_WAIT))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
