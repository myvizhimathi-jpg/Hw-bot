"""
FirstCry Hot Wheels watcher (v10)
- Page-la "New Arrivals" sort-a click pannitu padikkum
- PUTHU product / restock-ku Telegram alert
- In stock = card-la "ADD TO CART" irundha mattum
- Alert anuppum munnadi product page-la stock verify pannum
- 30 min-ku oru silent heartbeat message
"""
import asyncio
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.async_api import async_playwright

BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "PASTE_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID", "PASTE_CHAT_ID")

LISTING_URLS = [
    "https://www.firstcry.com/hotwheels/5/0/113?sort=bestseller&q=as_hotwheels&asid=53241#sort=newarrivals",
]

MIN_WAIT, MAX_WAIT = 15, 25
NEW_TOP_N = 25  # puthu listing-na New Arrivals list-oda mela 25-kulla irukkanum
HB_EVERY = 1800  # bot uyiroda irukkaanu kaattura silent Telegram message, every 30 min
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


JS_STOCK = """
() => {
  const vis = e => {
    const r = e.getBoundingClientRect();
    const st = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  let add = false, out = false;
  const seen = [];
  for (const e of document.querySelectorAll('button, a, div, span')) {
    if (e.children.length > 2) continue;
    const t = (e.innerText || '').trim().toLowerCase();
    if (!t || t.length > 30 || !vis(e)) continue;
    if (t === 'add to cart' || t === 'buy now') add = true;
    if (t === 'out of stock' || t === 'notify me' || t === 'sold out') out = true;
    if (/cart|buy|stock|notify|sold|left/.test(t) && !seen.includes(t) && seen.length < 12) seen.push(t);
  }
  return {add: add, out: out, seen: seen};
}
"""


def ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%I:%M:%S %p IST")


async def verify_stock(ctx, url):
    """Product page-a open panni stock check. Returns (status, info). status: True/False/None(theriyala)"""
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)
        r = {"add": False, "out": False, "seen": []}
        for _ in range(8):  # button load aaga 8 sec varaikkum wait pannum
            r = await page.evaluate(JS_STOCK)
            if r["add"] != r["out"]:
                break
            await page.wait_for_timeout(1000)
        if r["out"] and not r["add"]:
            return False, {"seen": r["seen"]}
        if r["add"] and not r["out"]:
            return True, {"seen": r["seen"]}
        shot = None
        try:
            shot = await page.screenshot(type="jpeg", quality=55)
        except Exception:
            pass
        return None, {"seen": r["seen"], "shot": shot}
    except Exception as e:
        print("verify error:", e)
        return None, {"seen": ["error"]}
    finally:
        try:
            await page.close()
        except Exception:
            pass


def tg_photo(caption: str, jpg: bytes):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption[:1000]},
            files={"photo": ("page.jpg", jpg)},
            timeout=30,
        )
    except Exception as e:
        print("Telegram photo error:", e)


def slim(d, miss=0):
    return {"name": d["name"], "url": d["url"], "in_stock": d["in_stock"], "miss": miss}


def push_state():
    """GitHub Actions-la odumbodhu state-a udane repo-la save pannum (run naduvula nindrunthaalum spam varaadhu)."""
    if not os.getenv("GITHUB_ACTIONS"):
        return
    cwd = str(Path(__file__).parent)
    try:
        run = lambda cmd: subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
        run('git config user.name "hw-bot"')
        run('git config user.email "hw-bot@users.noreply.github.com"')
        run("git add hw_state.json")
        if run("git diff --cached --quiet").returncode != 0:
            run('git commit -m "state"')
            for _ in range(3):
                if run("git pull --rebase -X theirs origin main && git push").returncode == 0:
                    break
                time.sleep(2)
    except Exception as e:
        print("push_state error:", e)


def tg(msg: str, silent: bool = False):
    try:
        data = {"chat_id": CHAT_ID, "text": msg}
        if silent:
            data["disable_notification"] = "true"  # sound / vibration illa
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data,
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
            "card": txt[:140],
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
    last_push = time.time()
    checks = 0
    sort_fail = 0
    last_hb = time.time() - HB_EVERY + 120  # first heartbeat ~2 min kalichi

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
            checks += 1
            if not sort_ok:
                sort_fail += 1
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
                    state = {k: slim(d) for k, d in current.items()}
                    save_state(state)
                    push_state()
                    last_push = time.time()
                    top = "\n".join("- " + d["name"][:60] for d in list(current.values())[:5])
                    in_count = sum(1 for d in current.values() if d["in_stock"])
                    tg(
                        f"✅ Bot start aachu. {len(current)} products ({in_count} in stock).\n"
                        f"New Arrivals sort: {'OK' if sort_ok else 'FAILED'}\n"
                        f"Top 5:\n{top}"
                    )
                else:
                    alerted = False
                    if sort_ok:
                        # one-time self test: oru product-a state-la irundhu eduthu fake "puthu" alert
                        test_key = None
                        if not state.get("_selftest"):
                            test_key = next((k for k, d in current.items() if d["in_stock"]), None)
                            if test_key:
                                state.pop(test_key, None)
                            state["_selftest"] = True
                        candidates = []
                        pos = {k: i + 1 for i, k in enumerate(current.keys())}
                        for key, d in current.items():
                            old = state.get(key)
                            if d["in_stock"] and (old is None or not old.get("in_stock")):
                                kind = "new" if old is None else "restock"
                                if kind == "new" and pos[key] > NEW_TOP_N:
                                    continue  # list-la deep-aa irukku -> pazhaya product, silent-aa state-la serkkum
                                candidates.append((key, kind))
                        verified = {}
                        debug = {}
                        for key, kind in candidates[:8]:
                            status, info = await verify_stock(ctx, current[key]["url"])
                            verified[key] = status
                            debug[key] = info
                            if status is False:
                                current[key]["in_stock"] = False  # out of stock -> alert illa
                        shots = 0
                        stamp = ist_now()
                        for key, kind in candidates:
                            d = current[key]
                            if not d["in_stock"]:
                                continue
                            note = (
                                "✅ Stock confirmed"
                                if verified.get(key) is True
                                else "⚠️ Stock check aagala, open panni paarunga"
                            )
                            tag = "🧪 TEST - " if key == test_key else ""
                            head = "🆕 PUTHU LISTING!" if kind == "new" else "🔥 RESTOCK!"
                            tg(f"{tag}{head}\n{note}\n{d['name']}\n{d['url']}\n🕐 {stamp} | 📍 #{pos[key]} of {len(current)}")
                            alerted = True
                            info = debug.get(key)
                            if verified.get(key) is None and info and info.get("shot") and shots < 2:
                                shots += 1
                                tg_photo(
                                    "DEBUG stock check\nbuttons: " + ", ".join(info.get("seen", []))
                                    + "\ncard: " + d.get("card", ""),
                                    info["shot"],
                                )
                        # state update: in-stock -> out-of-stock-ku 3 dhadava thodarndhu paartha apram thaan maathum
                        for key, d in current.items():
                            old = state.get(key)
                            if old is None or d["in_stock"] or not old.get("in_stock"):
                                state[key] = slim(d)
                            else:
                                miss = old.get("miss", 0) + 1
                                if miss >= 3:
                                    state[key] = slim(d)
                                else:
                                    old["miss"] = miss
                        state.pop("_sortwarned", None)
                    elif not state.get("_sortwarned"):
                        tg("⚠️ New Arrivals sort apply aagala. Alerts nirutthi vechirukken. Telegram-la sollunga, fix pannuren.")
                        state["_sortwarned"] = True
                    state.pop("_warned", None)
                    save_state(state)
                    if alerted or time.time() - last_push > 300:
                        push_state()
                        last_push = time.time()

            if time.time() - last_hb >= HB_EVERY:
                last_hb = time.time()
                top3 = [
                    f"{i + 1}. {'✅' if d['in_stock'] else '❌'} {d['name'][:55]}"
                    for i, d in enumerate(list(current.values())[:3])
                ]
                tg(
                    f"💓 Bot alive {ist_now()}\n"
                    f"checks: {checks} | sort fail: {sort_fail} | products: {len(current)}\n"
                    + "\n".join(top3),
                    silent=True,
                )

            if duration and time.time() - start > duration:
                break
            await asyncio.sleep(random.uniform(MIN_WAIT, MAX_WAIT))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
