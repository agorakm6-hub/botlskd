# Парсер NFT-подарков Telegram: бот на вебхуке + пользовательская сессия (Telethon) для чтения маркета
import os, asyncio, html, time, uuid, json
from aiohttp import web, ClientSession, ClientTimeout
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
try:
    from telethon.tl.functions.payments import GetStarGiftsRequest, GetResaleStarGiftsRequest
    from telethon.tl.types import StarGiftAttributeIdModel, StarGiftAttributeIdPattern, StarGiftAttributeIdBackdrop
except ImportError:
    raise SystemExit("Обнови Telethon: pip install -U telethon (нужна версия с поддержкой подарков)")
try:
    from telethon.tl.functions.payments import GetSavedStarGiftsRequest
except ImportError:
    GetSavedStarGiftsRequest = None

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID, API_HASH, SESSION = int(os.environ["API_ID"]), os.environ["API_HASH"], os.environ["STRING_SESSION"]
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
SECRET = os.environ.get("WEBHOOK_SECRET") or BOT_TOKEN.split(":")[1][:32]  # защита вебхука
API = f"{os.environ.get('TG_API', 'https://api.telegram.org')}/bot{BOT_TOKEN}"
PAGE = 25  # ссылок на странице
FAST_EVERY = int(os.environ.get("FAST_EVERY", 60))  # как часто опрашивать рынок (сек): быстрый опрос ловит выгодные лоты, пока их не купили
WATCH_FILE = os.environ.get("WATCH_FILE", "watch.json")

tele = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
http: ClientSession = None
cache, views, watches, waiting, users, feeds = {}, {}, [], {}, {}, {}


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


def price_val(g):
    for a in getattr(g, "resell_amount", None) or []:
        return (a.amount / 1e9, "TON") if "Ton" in type(a).__name__ else (a.amount, "★")
    st = getattr(g, "resell_stars", None)
    return (st, "★") if st else (None, "")


def price(g):
    v, u = price_val(g)
    return f"{v:g} {u}" if v is not None else "—"


def attr_ids(filt):
    out = []
    for k, (name, i) in filt.items():
        out.append({"m": StarGiftAttributeIdModel(document_id=i), "p": StarGiftAttributeIdPattern(document_id=i),
                    "b": StarGiftAttributeIdBackdrop(backdrop_id=i)}[k])
    return out


def attr_name(g, cls):
    return next((a.name for a in getattr(g, "attributes", []) if type(a).__name__ == cls), "")


def read_attrs(res):
    out = {"m": [], "b": [], "p": []}
    for a in getattr(res, "attributes", None) or []:
        n = type(a).__name__
        if n == "StarGiftAttributeModel":
            out["m"].append((a.name, a.document.id))
        elif n == "StarGiftAttributePattern":
            out["p"].append((a.name, a.document.id))
        elif n == "StarGiftAttributeBackdrop" and getattr(a, "backdrop_id", None) is not None:
            out["b"].append((a.name, a.backdrop_id))
    return out


async def page(v, idx):
    offset = v["offsets"][idx]
    key = ("pg", v["gift"], offset, v["sort"], tuple(sorted((k, x[1]) for k, x in v["filt"].items())))
    c = cache.get(key)
    if c and time.time() - c[0] < 60:
        return c[1]
    if len(cache) > 300:
        cache.clear()
    kw = dict(gift_id=v["gift"], offset=offset, limit=PAGE)
    if v["sort"] == "price":
        kw["sort_by_price"] = True
    elif v["sort"] == "num":
        kw["sort_by_num"] = True
    if v["filt"]:
        kw["attributes"] = attr_ids(v["filt"])
    if not v.get("attrs"):
        kw["attributes_hash"] = 0
    try:
        res = await tele(GetResaleStarGiftsRequest(**kw))
    except TypeError:
        kw.pop("attributes_hash", None)
        res = await tele(GetResaleStarGiftsRequest(**kw))
    if not v.get("attrs"):
        v["attrs"] = read_attrs(res)
    items = [(g.title, g.num, g.slug, price(g), attr_name(g, "StarGiftAttributeModel"), attr_name(g, "StarGiftAttributeBackdrop"))
             for g in res.gifts if hasattr(g, "slug")]
    data = (items, getattr(res, "next_offset", None), getattr(res, "count", len(items)))
    cache[key] = (time.time(), data)
    return data


# ---------- слежение за рынком: сигналы на выгодные покупки ----------
DEAL, DROP, GAP = [5, 10, 15, 20, 30], [3, 5, 10, 15], [10, 25, 50, 100, 250, 500]
OPTS, DEF = {"deal": DEAL, "drop": DROP, "gap": GAP}, {"deal": 10, "drop": 5, "gap": 50}


def save_watches():
    try:
        with open(WATCH_FILE, "w", encoding="utf-8") as f:
            json.dump({"watches": watches, "feeds": feeds}, f)
    except Exception as e:
        print("save:", e)


def load_watches():
    try:
        with open(WATCH_FILE, encoding="utf-8") as f:
            d = json.load(f)
        watches.extend(d.get("watches", [])); feeds.update(d.get("feeds", {}))
    except Exception:
        pass


def fkey(gift, filt):
    return f"{gift}|" + ",".join(f"{k}{x[1]}" for k, x in sorted(filt.items()))


def fsum(w):
    return " · ".join(n for n, _ in w["filt"].values()) or "без фильтров"


def dt(ts):
    return time.strftime("%d.%m %H:%M", time.gmtime(ts)) + " UTC"


def med(v):
    v = sorted(v); n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def link(slug, text):
    return f'<a href="https://t.me/nft/{html.escape(slug)}">{html.escape(text)}</a>'


async def send(chat, text):
    await bot("sendMessage", chat_id=chat, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True})


async def poll(feed):
    """Один замер: 10 самых дешёвых лотов. Возвращает список (цена, ед., лот)."""
    kw = dict(gift_id=feed["gift"], offset="", limit=10, sort_by_price=True)
    if feed["filt"]:
        kw["attributes"] = attr_ids(feed["filt"])
    res = await tele(GetResaleStarGiftsRequest(**kw))
    lots = [(v, u, g) for g in res.gifts if hasattr(g, "slug") for v, u in [price_val(g)] if v is not None]
    if not lots:
        return []
    unit = lots[0][1]
    return sorted((x for x in lots if x[1] == unit), key=lambda x: x[0])


async def process(feed, lots):
    """Записывает историю и рассылает сигналы всем, кто следит за этим фидом."""
    now = time.time()
    if not lots:
        return
    unit = lots[0][1]
    prices = [x[0] for x in lots]
    floor, second = prices[0], (prices[1] if len(prices) > 1 else None)
    h = feed["hist"]
    if not h or abs(h[-1][1] - floor) > 1e-9 or now - h[-1][0] >= 300:  # пишем при изменении или раз в 5 минут
        h.append([now, floor, second, med(prices), unit]); del h[:-6000]
    seen, first = feed["seen"], not feed["seen"]
    subs = [w for w in watches if w["key"] == feed["key"]]
    cur = {g.slug: v for v, _, g in lots}
    if not first:
        for v, _, g in lots:
            old = seen.get(g.slug)
            if old is not None and v >= old:
                continue  # лот уже был и не подешевел
            others = [p for sl, p in cur.items() if sl != g.slug]
            ref = med(others) if len(others) >= 3 else (min(others) if others else None)
            disc = (1 - v / ref) * 100 if ref else 0
            prev_floor = min((p for sl, p in seen.items() if sl != g.slug), default=None)
            above = min((p for p in others if p > v), default=None)
            gapv = (above - v) if above else 0
            for w in subs:
                hit_tgt = w["target"] is not None and v <= w["target"]
                new_floor = w.get("newfloor", True) and prev_floor is not None and v < prev_floor
                gap_hit = unit == "★" and above is not None and gapv >= w.get("gap", 50)
                if (disc >= w["deal"] or hit_tgt or new_floor or gap_hit) and w["alerted"].get(g.slug, 1e18) > v:
                    w["alerted"][g.slug] = v
                    while len(w["alerted"]) > 200:
                        w["alerted"].pop(next(iter(w["alerted"])))
                    why = []
                    if new_floor:
                        why.append(f"ниже прежнего флора ({prev_floor:g} {unit}): новый самый дешёвый лот")
                    if gap_hit:
                        why.append(f"следующий лот дороже на {gapv:g} {unit} ({above:g})")
                    if disc >= w["deal"]:
                        why.append(f"ниже рынка на {disc:.0f}% (ориентир {ref:g}, медиана остальных)")
                    if hit_tgt:
                        why.append(f"не дороже цели {w['target']:g}")
                    cheaper = f"\nЦену снизили: было {old:g} {unit}" if old else ""
                    prof = f"\nПотенциал при продаже по ориентиру: +{ref - v:g} {unit} (+{(ref / v - 1) * 100:.0f}%), комиссию маркета учитывай сам" if ref and ref > v else ""
                    await send(w["chat"], f"Выгодный лот: {link(g.slug, f'{g.title} #{g.num}')} за {v:g} {unit}\n" + "\n".join(why) + cheaper + prof +
                               f"\n\n{html.escape(w['title'])} ({html.escape(fsum(w))})")
    feed["seen"] = cur
    # падение флора относительно медианы за последние 24 часа (без последнего часа)
    ref_pts = [x[1] for x in h if x[4] == unit and now - 86400 <= x[0] <= now - 3600]
    for w in subs:
        sent_now = False
        if len(ref_pts) >= 20:
            m = med(ref_pts)
            drop = (1 - floor / m) * 100
            lastf = w["last_drop"]
            if drop >= w["drop"] and (not lastf or now - lastf[0] > 1800 or floor < lastf[1] * 0.98):
                w["last_drop"] = [now, floor]; sent_now = True
                await send(w["chat"], f"Флор упал: {link(lots[0][2].slug, f'{floor:g} {unit}')} ({drop:.1f}% ниже медианы за 24ч: {m:g})\n\n" + stats(w))
            elif drop < w["drop"] / 2:
                w["last_drop"] = None
        # сравнение с флором первого дня слежения
        if w["day1"] and now - w["start"] >= 86400:
            d1 = [x[1] for x in h if x[4] == unit and w["start"] <= x[0] <= w["start"] + 86400]
            if d1 and floor < min(d1):
                if w["notified"] is None or floor < w["notified"]:
                    w["notified"] = floor
                    if not sent_now:  # если только что отправили сигнал о падении, второе сообщение не нужно
                        await send(w["chat"], f"Флор ниже, чем в первый день: {floor:g} {unit} (тогда {min(d1):g})\n\n" + stats(w))
            elif d1:
                w["notified"] = None


def stats(w):
    f = feeds.get(w["key"])
    if not f or not f["hist"]:
        return "Замеров пока нет."
    now, last = time.time(), f["hist"][-1]
    u, cur, sec = last[4], last[1], last[2]
    pts = [x for x in f["hist"] if x[4] == u and x[0] >= now - 7 * 86400]
    fl = [x[1] for x in pts]
    L = [f"<b>{html.escape(w['title'])}</b> ({html.escape(fsum(w))})",
         f"Сейчас: флор {cur:g} {u}" + (f", второй {sec:g} (разрыв {(sec / cur - 1) * 100:.1f}%)" if sec else "")]
    d24 = [x[1] for x in pts if x[0] >= now - 86400]
    if len(d24) >= 5:
        m = med(d24)
        L.append(f"За 24ч: медиана {m:g} · мин {min(d24):g} · макс {max(d24):g} · сейчас {(cur / m - 1) * 100:+.1f}% к медиане")
    if len(fl) >= 20:
        higher = sum(1 for x in fl if x > cur) / len(fl)
        sig = "ЗОНА ПОКУПКИ" if higher >= .9 else "дорого" if higher <= .1 else "нейтрально"
        L.append(f"Сигнал: {sig} (флор был выше текущего в {higher * 100:.0f}% замеров за неделю)")
    span = pts[-1][0] - pts[0][0] if pts else 0
    if span >= 172800 and fl:
        mean, byh = sum(fl) / len(fl), {}
        for x in pts:
            byh.setdefault(time.gmtime(x[0]).tm_hour, []).append(x[1] / mean)
        avg = {k: sum(v) / len(v) for k, v in byh.items() if len(v) >= 3}
        if len(avg) >= 6:
            o = sorted(avg, key=avg.get)
            L.append("Обычно дешевле всего (UTC): " + ", ".join(f"{k:02d}:00 ({(avg[k] - 1) * 100:+.1f}%)" for k in o[:3]))
            L.append("Обычно дороже всего (UTC): " + ", ".join(f"{k:02d}:00 ({(avg[k] - 1) * 100:+.1f}%)" for k in o[-3:]))
    else:
        L.append(f"Паттерн по часам: накоплено {span / 3600:.0f} ч из 48")
    d1 = [x[1] for x in f["hist"] if x[4] == u and w["start"] <= x[0] <= w["start"] + 86400]
    if d1:
        L.append(f"Флор первого дня слежения: {min(d1):g} ({(cur / min(d1) - 1) * 100:+.1f}% сейчас)")
    days = {}
    for x in pts:
        days.setdefault(time.strftime("%d.%m", time.gmtime(x[0])), []).append(x[1])
    L.append("\nПо дням:\n" + "\n".join(f"{d}: мин {min(v):g} · сред {sum(v) / len(v):.1f}" for d, v in list(days.items())[-7:]))
    return "\n".join(L)


async def watch_loop():
    backoff = 1
    while True:
        t0 = time.time()
        live = {w["key"] for w in watches}
        for k in [k for k in feeds if k not in live]:
            del feeds[k]
        for k in list(feeds):
            try:
                await process(feeds[k], await poll(feeds[k]))
            except FloodWaitError as e:
                backoff = min(backoff * 2, 8)
                await asyncio.sleep(e.seconds + 1)
            except Exception as e:
                print("watch error:", repr(e))
            await asyncio.sleep(1)
        save_watches()
        await asyncio.sleep(max(5, FAST_EVERY * backoff - (time.time() - t0)))
        backoff = max(1, backoff - 1)


async def add_watch(chat, v):
    key = fkey(v["gift"], v["filt"])
    mine = [w for w in watches if w["chat"] == chat]
    if any(w["key"] == key for w in mine):
        return "Уже слежу за этим."
    if len(mine) >= 5:
        return "Максимум 5 слежений. Удали лишнее в «Мои слежения»."
    w = {"id": uuid.uuid4().hex[:8], "chat": chat, "key": key, "gift": v["gift"], "title": v["title"],
         "filt": {k: list(x) for k, x in v["filt"].items()}, "start": time.time(), "deal": 10, "drop": 5,
         "target": None, "day1": True, "notified": None, "last_drop": None, "alerted": {}, "gap": 50, "newfloor": True}
    watches.append(w)
    feed = feeds.setdefault(key, {"key": key, "gift": v["gift"], "filt": w["filt"], "hist": [], "seen": {}})
    lots = await poll(feed)
    await process(feed, lots)
    save_watches()
    first = f"{lots[0][0]:g} {lots[0][1]}" if lots else "пока нет лотов"
    return (f"Слежу: {w['title']} ({fsum(w)})\nФлор сейчас: {first}\n"
            f"Опрашиваю рынок каждые {FAST_EVERY} сек. Напишу, если появится лот ниже первого (флора), "
            f"дешевле следующего лота на {w['gap']} ★+ или ниже рынка на {w['deal']}%+; а также если "
            f"флор упадёт на {w['drop']}%+ от медианы за сутки. Пороги настраиваются в «Мои слежения».")


def watch_menu(w):
    f = feeds.get(w["key"]); cur = f["hist"][-1] if f and f["hist"] else None
    b = lambda t, d: {"text": t, "callback_data": d}
    tgt = f"{w['target']:g}" if w["target"] is not None else "нет"
    rows = [[b(f"Выгодный лот: ниже рынка на {w['deal']}%+", f"wc:{w['id']}:deal")],
            [b(f"Падение флора: {w['drop']}%+ к медиане 24ч", f"wc:{w['id']}:drop")],
            [b("Новый лот ниже первого (флора): " + ("вкл" if w.get("newfloor", True) else "выкл"), f"wn:{w['id']}")],
            [b(f"Следующий лот дороже на {w.get('gap', 50)} ★+", f"wc:{w['id']}:gap")],
            [b(f"Цель по цене: {tgt}", f"wt:{w['id']}")],
            [b("Сравнение с первым днём: " + ("вкл" if w["day1"] else "выкл"), f"w1:{w['id']}")],
            [b("Статистика", f"ws:{w['id']}"), b("Удалить", f"wd:{w['id']}")],
            [b("← Мои слежения", "wl")]]
    now = f"Флор сейчас: {cur[1]:g} {cur[4]}" if cur else "Замеров пока нет"
    return f"<b>{html.escape(w['title'])}</b> ({html.escape(fsum(w))})\n{now}\nСлежу с {dt(w['start'])}", {"inline_keyboard": rows}


def watch_list(chat):
    mine = [w for w in watches if w["chat"] == chat]
    if not mine:
        return "Ты пока ни за чем не следишь. Открой коллекцию и нажми «Следить за флором».", {"inline_keyboard": [[{"text": "К коллекциям", "callback_data": "home"}]]}
    rows = [[{"text": f"{w['title']} · {fsum(w)}"[:60], "callback_data": f"wm:{w['id']}"}] for w in mine]
    return "Мои слежения (нажми, чтобы настроить):", {"inline_keyboard": rows + [[{"text": "К коллекциям", "callback_data": "home"}]]}


# ---------- подарки пользователя ----------
async def user_page(v, idx):
    res = await tele(GetSavedStarGiftsRequest(peer=v["peer"], offset=v["offsets"][idx], limit=PAGE))
    nxt = getattr(res, "next_offset", None)
    if nxt and len(v["offsets"]) == idx + 1:
        v["offsets"].append(nxt)
    lines = [f"<b>Подарки {html.escape(v['name'])}</b> · всего на профиле: {getattr(res, 'count', '?')} (показаны коллекционные)\n"]
    n = 0
    for sg in res.gifts:
        g = getattr(sg, "gift", None)
        if g is None or not hasattr(g, "slug"):
            continue
        n += 1
        extra = " · ".join(x for x in (attr_name(g, "StarGiftAttributeModel"), attr_name(g, "StarGiftAttributeBackdrop")) if x)
        lines.append(f'{idx * PAGE + n}. <a href="https://t.me/nft/{html.escape(g.slug)}">{html.escape(g.title)} #{g.num}</a>'
                     + (f" ({html.escape(extra)})" if extra else ""))
    if not n:
        lines.append("Коллекционных подарков не видно (профиль закрыт или их нет).")
    row = ([{"text": "← Назад", "callback_data": f"up:{v['id']}:{idx - 1}"}] if idx > 0 else []) + \
          ([{"text": "Дальше →", "callback_data": f"up:{v['id']}:{idx + 1}"}] if len(v["offsets"]) > idx + 1 else [])
    return "\n".join(lines), {"inline_keyboard": ([row] if row else []) + [[{"text": "К коллекциям", "callback_data": "home"}]]}


async def open_user(chat, name):
    if GetSavedStarGiftsRequest is None:
        return await bot("sendMessage", chat_id=chat, text="Нужен более новый Telethon: pip install -U telethon")
    name = name.strip().replace("https://t.me/", "").replace("@", "").split("/")[0]
    try:
        peer = users.get(name.lower()) or await tele.get_input_entity(name)
    except FloodWaitError:
        raise
    except Exception:
        return await bot("sendMessage", chat_id=chat, text="Не нашёл такого пользователя. Пришли ник вида @name.")
    users[name.lower()] = peer
    v = {"id": uuid.uuid4().hex[:8], "peer": peer, "name": "@" + name, "offsets": [""]}
    views[v["id"]] = v
    text, kb = await user_page(v, 0)
    await bot("sendMessage", chat_id=chat, text=text, reply_markup=kb, parse_mode="HTML", link_preview_options={"is_disabled": True})


async def home_view():
    cols = (await collections())[:60]
    if not cols:
        return "Сейчас нет коллекций на продаже.", {"inline_keyboard": []}
    btns = [{"text": t, "callback_data": f"c:{i}"} for i, t in cols]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([{"text": "Мои слежения", "callback_data": "wl"}, {"text": "Подарки пользователя", "callback_data": "u"}])
    return "Выбери коллекцию подарков:", {"inline_keyboard": rows}


KN = {"m": "Модель", "b": "Фон", "p": "Узор"}
SN = {"price": "Цена", "num": "Номер", "new": "Новые"}


def summary(v):
    f = " · ".join(f"{KN[k].lower()}: {n}" for k, (n, _) in v["filt"].items()) or "нет"
    return f"Фильтры: {html.escape(f)} · сортировка: {SN[v['sort']].lower()}"


async def render(v, idx):
    items, nxt, count = await page(v, idx)
    if nxt and len(v["offsets"]) == idx + 1:
        v["offsets"].append(nxt)
    lines = [f"<b>{html.escape(v['title'])}</b> · найдено: {count}", summary(v), ""]
    for i, (t, n, slug, pr, mdl, bg) in enumerate(items, idx * PAGE + 1):
        extra = " · ".join(x for x in (mdl, bg) if x)
        lines.append(f'{i}. <a href="https://t.me/nft/{html.escape(slug)}">{html.escape(t)} #{n}</a> — {pr}'
                     + (f" ({html.escape(extra)})" if extra else ""))
    if not items:
        lines.append("Ничего не найдено. Попробуй сбросить фильтры.")
    row = []
    if idx > 0:
        row.append({"text": "← Назад", "callback_data": f"p:{v['id']}:{idx - 1}"})
    if len(v["offsets"]) > idx + 1:
        row.append({"text": "Дальше →", "callback_data": f"p:{v['id']}:{idx + 1}"})
    kb = ([row] if row else []) + [[{"text": "Фильтры", "callback_data": f"f:{v['id']}"},
                                    {"text": "Следить за флором", "callback_data": f"w:{v['id']}"}],
                                   [{"text": "К коллекциям", "callback_data": "home"}]]
    return "\n".join(lines), {"inline_keyboard": kb}


async def filters_view(v):
    await page(v, 0) if not v.get("attrs") else None  # подгрузить списки моделей/фонов/узоров
    b = lambda t, d: {"text": t, "callback_data": d}
    rows = [[b(f"{KN[k]}: {v['filt'][k][0] if k in v['filt'] else 'любая'}", f"fl:{v['id']}:{k}:0")] for k in "mbp"]
    rows.append([b(("✓ " if v["sort"] == m else "") + SN[m], f"so:{v['id']}:{m}") for m in SN])
    rows.append([b("Сбросить", f"fr:{v['id']}"), b("Показать →", f"p:{v['id']}:0")])
    return f"<b>{html.escape(v['title'])}</b>\n" + summary(v) + "\n\nНастрой и жми «Показать».", {"inline_keyboard": rows}


def choices_view(v, k, pg):
    opts, per = v["attrs"].get(k, []), 20
    pg = max(0, min(pg, max(0, (len(opts) - 1) // per)))
    b = lambda t, d: {"text": t, "callback_data": d}
    btns = [b(n, f"fs:{v['id']}:{k}:{pg * per + i}") for i, (n, _) in enumerate(opts[pg * per:(pg + 1) * per])]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    nav = ([b("←", f"fl:{v['id']}:{k}:{pg - 1}")] if pg > 0 else []) + ([b("→", f"fl:{v['id']}:{k}:{pg + 1}")] if (pg + 1) * per < len(opts) else [])
    if nav:
        rows.append(nav)
    rows.append([b("Любая", f"fs:{v['id']}:{k}:-1"), b("← К фильтрам", f"f:{v['id']}")])
    return f"{KN[k]} ({len(opts)}), стр. {pg + 1}:", {"inline_keyboard": rows}


HELP = ("Что умеет бот:\n"
        "/start — выбрать коллекцию подарков: 25 ссылок на странице, фильтры по модели, фону и узору, сортировка\n"
        "В коллекции кнопка «Следить за флором»: бот сам ловит выгодные лоты и падение цены\n"
        "/watch — мои слежения и их настройки (пороги, цель по цене)\n"
        "/user @ник — коллекционные подарки пользователя\n"
        "Комиссию маркета в расчётах бот не учитывает.")
COMMANDS = [{"command": "start", "description": "Выбрать коллекцию подарков"},
            {"command": "watch", "description": "Мои слежения за ценами"},
            {"command": "user", "description": "Подарки пользователя"},
            {"command": "help", "description": "Как пользоваться"}]


async def setup_menu():  # меню команд рядом с клавиатурой (кнопка «/» или «Меню»)
    await bot("setMyCommands", commands=COMMANDS)
    await bot("setChatMenuButton", menu_button={"type": "commands"})


async def on_update(u):
    chat = None
    try:
        if "message" in u:
            m = u["message"]; chat = m["chat"]["id"]
            t = (m.get("text") or "").strip()
            if t.startswith(("/start", "/gifts")):
                waiting.pop(chat, None)
                text, kb = await home_view()
                await bot("sendMessage", chat_id=chat, text=text, reply_markup=kb)
            elif t.startswith("/watch"):
                text, kb = watch_list(chat)
                await bot("sendMessage", chat_id=chat, text=text, reply_markup=kb)
            elif t.startswith("/user"):
                if len(t.split()) > 1:
                    await open_user(chat, t.split()[1])
                else:
                    waiting[chat] = "user"
                    await bot("sendMessage", chat_id=chat, text="Пришли ник пользователя, например @durov")
            elif t.startswith("/help"):
                await bot("sendMessage", chat_id=chat, text=HELP)
            elif chat in waiting and t and not t.startswith("/"):
                st = waiting.pop(chat)
                if st == "user" or st is True:
                    await open_user(chat, t.split()[0])
                else:
                    w = next((x for x in watches if x["id"] == st[1] and x["chat"] == chat), None)
                    try:
                        val = float(t.replace(",", ".").split()[0])
                    except ValueError:
                        return await bot("sendMessage", chat_id=chat, text="Нужно число, например 120.")
                    if w:
                        w["target"] = val if val > 0 else None; save_watches()
                        text, kb = watch_menu(w)
                        await bot("sendMessage", chat_id=chat, text=text, reply_markup=kb, parse_mode="HTML")
        elif "callback_query" in u:
            q = u["callback_query"]; d = q["data"]; chat = q["message"]["chat"]["id"]; mid = q["message"]["message_id"]
            await bot("answerCallbackQuery", callback_query_id=q["id"])
            text = kb = None
            if d == "wl":
                text, kb = watch_list(chat)
            elif d == "u":
                waiting[chat] = "user"
                await bot("sendMessage", chat_id=chat, text="Пришли ник пользователя, например @durov")
                return
            elif d.startswith(("wm:", "wc:", "wt:", "ws:", "wd:", "w1:", "wn:")):
                parts = d.split(":")
                w = next((x for x in watches if x["id"] == parts[1] and x["chat"] == chat), None)
                if w:
                    c = parts[0]
                    if c == "ws":
                        await send(chat, stats(w)); return
                    if c == "wt":
                        waiting[chat] = ("t", w["id"])
                        await bot("sendMessage", chat_id=chat, text="Пришли целевую цену числом: напишу, когда появится лот не дороже. 0 — выключить.")
                        return
                    if c == "wd":
                        watches.remove(w); save_watches(); text, kb = watch_list(chat)
                    else:
                        if c == "wc":
                            k = parts[2]; opts = OPTS[k]; cur = w.get(k, DEF[k])
                            w[k] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
                        elif c == "wn":
                            w["newfloor"] = not w.get("newfloor", True)
                        elif c == "w1":
                            w["day1"] = not w["day1"]
                        save_watches(); text, kb = watch_menu(w)
            elif d.startswith("up:"):
                _, vid, idx = d.split(":")
                if vid in views:
                    text, kb = await user_page(views[vid], int(idx))
            elif d.startswith("w:") and d[2:] in views:
                await bot("sendMessage", chat_id=chat, text=await add_watch(chat, views[d[2:]]))
                return
            elif d.startswith("c:"):
                gid = int(d[2:])
                title = next((t for i, t in await collections() if i == gid), f"Gift {gid}")
                v = {"id": uuid.uuid4().hex[:8], "gift": gid, "title": title, "offsets": [""], "filt": {}, "sort": "price"}
                views[v["id"]] = v
                if len(views) > 500:
                    views.pop(next(iter(views)))
                text, kb = await render(v, 0)
            else:
                parts = d.split(":")
                v = views.get(parts[1]) if len(parts) > 1 else None
                if v:
                    cmd = parts[0]
                    if cmd == "p":
                        text, kb = await render(v, int(parts[2]))
                    elif cmd == "f":
                        text, kb = await filters_view(v)
                    elif cmd == "fl":
                        text, kb = choices_view(v, parts[2], int(parts[3]))
                    elif cmd == "fs":
                        k, i = parts[2], int(parts[3])
                        if i < 0:
                            v["filt"].pop(k, None)
                        else:
                            v["filt"][k] = v["attrs"][k][i]
                        v["offsets"] = [""]
                        text, kb = await filters_view(v)
                    elif cmd == "so":
                        v["sort"] = parts[2]; v["offsets"] = [""]
                        text, kb = await filters_view(v)
                    elif cmd == "fr":
                        v["filt"].clear(); v["sort"] = "price"; v["offsets"] = [""]
                        text, kb = await filters_view(v)
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
    load_watches()
    await tele.connect()
    if not await tele.is_user_authorized():
        raise SystemExit("STRING_SESSION не рабочая: создай заново через make_session.py")
    app = web.Application()
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    app.router.add_post("/webhook", webhook)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    await setup_menu()
    if PUBLIC_URL:
        await bot("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=SECRET,
                  allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        asyncio.create_task(keepalive())
    asyncio.create_task(watch_loop())
    print("gifts bot started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
