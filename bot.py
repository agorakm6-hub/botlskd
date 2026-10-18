# Парсер NFT-подарков Telegram: бот на вебхуке + пользовательская сессия (Telethon) для чтения маркета
import os, asyncio, html, time, uuid
from aiohttp import web, ClientSession, ClientTimeout
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
try:
    from telethon.tl.functions.payments import GetStarGiftsRequest, GetResaleStarGiftsRequest
except ImportError:
    raise SystemExit("Обнови Telethon: pip install -U telethon (нужна версия с поддержкой подарков)")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID, API_HASH, SESSION = int(os.environ["API_ID"]), os.environ["API_HASH"], os.environ["STRING_SESSION"]
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
SECRET = os.environ.get("WEBHOOK_SECRET") or BOT_TOKEN.split(":")[1][:32]  # защита вебхука
API = f"{os.environ.get('TG_API', 'https://api.telegram.org')}/bot{BOT_TOKEN}"
PAGE = 25  # ссылок на странице

tele = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
http: ClientSession = None
cache, views = {}, {}


async def bot(method, **p):
    async with http.post(f"{API}/{method}", json=p) as r:
        d = await r.json()
        if not d.get("ok") and "not modified" not in str(d):
            print("bot api:", method, d)
        return d


async def collections():
    c = cache.get("cols")
    if c and time.time() - c[0] < 300:
        return c[1]
    res = await tele(GetStarGiftsRequest(hash=0))
    gifts = [g for g in getattr(res, "gifts", []) if getattr(g, "availability_resale", 0)]
    out = sorted(((g.id, getattr(g, "title", None) or f"Gift {g.id}") for g in gifts), key=lambda x: x[1].lower())
    cache["cols"] = (time.time(), out)
    return out


def price(g):
    for a in getattr(g, "resell_amount", None) or []:
        return f"{a.amount / 1e9:g} TON" if "Ton" in type(a).__name__ else f"{a.amount} ★"
    s = getattr(g, "resell_stars", None)
    return f"{s} ★" if s else "—"


async def page(gift_id, offset):
    key = ("pg", gift_id, offset)
    c = cache.get(key)
    if c and time.time() - c[0] < 60:
        return c[1]
    if len(cache) > 300:
        cache.clear()
    res = await tele(GetResaleStarGiftsRequest(gift_id=gift_id, offset=offset, limit=PAGE, sort_by_price=True))
    items = [(g.title, g.num, g.slug, price(g)) for g in res.gifts if hasattr(g, "slug")]
    data = (items, getattr(res, "next_offset", None), getattr(res, "count", len(items)))
    cache[key] = (time.time(), data)
    return data


async def home_view():
    cols = (await collections())[:60]
    if not cols:
        return "Сейчас нет коллекций на продаже.", {"inline_keyboard": []}
    btns = [{"text": t, "callback_data": f"c:{i}"} for i, t in cols]
    return "Выбери коллекцию подарков:", {"inline_keyboard": [btns[i:i + 2] for i in range(0, len(btns), 2)]}


async def render(v, idx):
    items, nxt, count = await page(v["gift"], v["offsets"][idx])
    if nxt and len(v["offsets"]) == idx + 1:
        v["offsets"].append(nxt)
    lines = [f"<b>{html.escape(v['title'])}</b> · на продаже: {count}\n"]
    for i, (t, n, slug, pr) in enumerate(items, idx * PAGE + 1):
        lines.append(f'{i}. <a href="https://t.me/nft/{html.escape(slug)}">{html.escape(t)} #{n}</a> — {pr}')
    if not items:
        lines.append("Пусто")
    row = []
    if idx > 0:
        row.append({"text": "← Назад", "callback_data": f"p:{v['id']}:{idx - 1}"})
    if len(v["offsets"]) > idx + 1:
        row.append({"text": "Дальше →", "callback_data": f"p:{v['id']}:{idx + 1}"})
    kb = ([row] if row else []) + [[{"text": "К коллекциям", "callback_data": "home"}]]
    return "\n".join(lines), {"inline_keyboard": kb}


async def on_update(u):
    chat = None
    try:
        if "message" in u:
            m = u["message"]; chat = m["chat"]["id"]
            if (m.get("text") or "").startswith(("/start", "/gifts")):
                text, kb = await home_view()
                await bot("sendMessage", chat_id=chat, text=text, reply_markup=kb)
        elif "callback_query" in u:
            q = u["callback_query"]; d = q["data"]; chat = q["message"]["chat"]["id"]; mid = q["message"]["message_id"]
            await bot("answerCallbackQuery", callback_query_id=q["id"])
            text = kb = None
            if d.startswith("c:"):
                gid = int(d[2:])
                title = next((t for i, t in await collections() if i == gid), f"Gift {gid}")
                v = {"id": uuid.uuid4().hex[:8], "gift": gid, "title": title, "offsets": [""]}
                views[v["id"]] = v
                if len(views) > 500:
                    views.pop(next(iter(views)))
                text, kb = await render(v, 0)
            elif d.startswith("p:"):
                _, vid, idx = d.split(":")
                if vid in views:
                    text, kb = await render(views[vid], int(idx))
            if text is None:
                text, kb = await home_view()
            await bot("editMessageText", chat_id=chat, message_id=mid, text=text, reply_markup=kb,
                      parse_mode="HTML", link_preview_options={"is_disabled": True})
    except FloodWaitError as e:
        if chat:
            await bot("sendMessage", chat_id=chat, text=f"Telegram просит подождать {e.seconds} сек. Попробуй позже.")
    except Exception as e:
        print("update error:", repr(e))


async def webhook(req):
    if req.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        return web.Response(status=403)
    asyncio.create_task(on_update(await req.json()))
    return web.Response(text="ok")


async def keepalive():  # анти-сон для бесплатного Render: пингуем сами себя каждые 10 минут
    while True:
        await asyncio.sleep(600)
        try:
            async with http.get(f"{PUBLIC_URL}/health") as r:
                await r.read()
        except Exception as e:
            print("keepalive:", e)


async def main():
    global http
    http = ClientSession(timeout=ClientTimeout(total=30))
    await tele.connect()
    if not await tele.is_user_authorized():
        raise SystemExit("STRING_SESSION не рабочая: создай заново через make_session.py")
    app = web.Application()
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    app.router.add_post("/webhook", webhook)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    if PUBLIC_URL:
        await bot("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=SECRET,
                  allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        asyncio.create_task(keepalive())
    print("gifts bot started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
