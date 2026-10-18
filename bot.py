"""Бот для маркета Telegram-подарков: каталог лотов, фильтры, слежение за флором,
подарки пользователя по нику.

Апдейты приходят на вебхук. Данные маркета читаются через пользовательскую
сессию Telethon: Bot API про перепродажу подарков ничего не знает.
"""
import asyncio
import hmac
import html
import inspect
import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import ClientSession, ClientTimeout, web
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

try:
    from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest
    from telethon.tl.types import (
        StarGiftAttributeIdBackdrop,
        StarGiftAttributeIdModel,
        StarGiftAttributeIdPattern,
    )
except ImportError:
    raise SystemExit("Telethon слишком старый: pip install -U telethon")

try:
    from telethon.tl.functions.payments import GetSavedStarGiftsRequest
except ImportError:
    GetSavedStarGiftsRequest = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gifts")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION = os.environ["STRING_SESSION"]
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
SECRET = os.environ.get("WEBHOOK_SECRET") or BOT_TOKEN.split(":")[1][:32]
API = f"{os.environ.get('TG_API', 'https://api.telegram.org')}/bot{BOT_TOKEN}"
WATCH_FILE = os.environ.get("WATCH_FILE", "watch.json")
POLL_EVERY = int(os.environ.get("FAST_EVERY", 60))
FEE = min(max(float(os.environ.get("MARKET_FEE", 20)) / 100, 0), 0.95)  # комиссия маркета при перепродаже

PAGE = int(os.environ.get("PAGE_SIZE", 20))  # лотов на странице
COLS_PER_PAGE = 30     # коллекций на странице главного меню
CHOICES_PER_PAGE = 20  # значений фильтра на странице
HIST_LIMIT = 6000
MAX_WATCHES = 5
MAX_PINS = 8
SAVED_BATCH = 100      # сколько подарков профиля просим за один запрос
SAVED_MAX_REQUESTS = 40

STARS, TON = "XTR", "TON"
SIGN = {STARS: "⭐", TON: "TON"}
LEGACY_UNITS = {"★": STARS}  # так хранились единицы в старых watch.json
NBSP = "\u00a0"
RULE = "━━━━━━━━━━━━━━"

tele = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
http = None
watches, feeds, pins = [], {}, {}
views, waiting, peers = {}, {}, {}
tasks = set()


class TTLCache:
    def __init__(self, size=300):
        self.size, self.data = size, {}

    def get(self, key):
        hit = self.data.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        self.data.pop(key, None)
        return None

    def put(self, key, value, ttl):
        if key not in self.data and len(self.data) >= self.size:
            self.data.pop(next(iter(self.data)))
        self.data[key] = (time.time() + ttl, value)
        return value


cache = TTLCache()


# ── форматирование ──────────────────────────────────────────

def esc(s):
    return html.escape(str(s))


def num(x):
    """Число без научной нотации и хвоста нулей: 1500000 -> «1 500 000»."""
    s = f"{round(x):,}" if abs(x - round(x)) < 1e-9 else f"{x:,.3f}".rstrip("0").rstrip(".")
    return s.replace(",", NBSP)


def signed(x):
    return ("+" if x >= 0 else "−") + num(abs(x))


def money(x, unit):
    return f"{num(x)}{NBSP}{SIGN.get(unit, unit)}"


def after_fee(price, unit):
    """Сколько получит продавец после комиссии маркета."""
    net = price * (1 - FEE)
    return math.floor(net) if unit == STARS else round(net, 3)


def breakeven(price, unit):
    gross = price / (1 - FEE)
    return math.ceil(gross) if unit == STARS else round(gross, 3)


FEE_PCT = num(FEE * 100)


def link(slug, text):
    return f'<a href="https://t.me/nft/{esc(slug)}">{esc(text)}</a>'


def med(values):
    values = sorted(values)
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def stamp(ts):
    return time.strftime("%d.%m %H:%M", time.gmtime(ts)) + " UTC"


def short(text, n=26):
    return text if len(text) <= n else text[:n - 1] + "…"


BARS = "▁▂▃▄▅▆▇█"


def spark(values, width=24):
    """Мини-график из блоков: быстро показывает, куда шёл флор."""
    if len(values) < 2:
        return ""
    n = min(width, len(values))
    size = len(values) / n
    pts = [min(values[int(i * size):max(int((i + 1) * size), int(i * size) + 1)]) for i in range(n)]
    lo, hi = min(pts), max(pts)
    if hi - lo < 1e-9:
        return BARS[3] * n
    return "".join(BARS[int((p - lo) / (hi - lo) * (len(BARS) - 1))] for p in pts)


def btn(text, data):
    return {"text": text, "callback_data": data}


def fit(lines, limit=3900):
    """Склеивает строки, не вылезая за лимит Telegram в 4096 символов."""
    out, size = [], 0
    for line in lines:
        size += len(line) + 1
        if size > limit:
            break
        out.append(line)
    return "\n".join(out)


# ── Bot API ─────────────────────────────────────────────────

async def api(method, **params):
    async with http.post(f"{API}/{method}", json=params) as resp:
        data = await resp.json()
    if not data.get("ok") and "not modified" not in str(data):
        log.warning("%s: %s", method, data)
    return data


async def say(chat, text, kb=None):
    params = dict(chat_id=chat, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True})
    if kb:
        params["reply_markup"] = kb
    return await api("sendMessage", **params)


async def edit(chat, mid, text, kb=None):
    params = dict(chat_id=chat, message_id=mid, text=text, parse_mode="HTML",
                  link_preview_options={"is_disabled": True})
    if kb:
        params["reply_markup"] = kb
    return await api("editMessageText", **params)


def background(coro):
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


# ── модели данных ───────────────────────────────────────────

ICON = {"m": "🧸", "b": "🎨", "p": "✨"}
KN = {"m": "Модель", "b": "Фон", "p": "Узор"}


def chips(filt):
    return " · ".join(f"{ICON[k]} {filt[k][0]}" for k in "mbp" if k in filt)


def fsum(w):
    return chips(w["filt"]) or "без фильтров"


@dataclass(frozen=True)
class Lot:
    title: str
    num: int
    slug: str
    price: Optional[float] = None
    unit: Optional[str] = None
    model: str = ""
    backdrop: str = ""

    @classmethod
    def of(cls, gift):
        price, unit = price_of(gift)
        return cls(gift.title, gift.num, gift.slug, price, unit,
                   attr_name(gift, "StarGiftAttributeModel"), attr_name(gift, "StarGiftAttributeBackdrop"))


@dataclass
class Catalog:
    gift: int
    title: str
    chat: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    offsets: list = field(default_factory=lambda: [""])
    filt: dict = field(default_factory=dict)
    sort: str = "price"
    page: int = 0
    attrs: Optional[dict] = None


@dataclass
class Profile:
    peer: object
    name: str
    items: list
    total: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


def remember(view):
    views[view.id] = view
    while len(views) > 500:
        views.pop(next(iter(views)))
    return view


def price_of(gift):
    for amount in getattr(gift, "resell_amount", None) or []:
        if type(amount).__name__ == "StarsTonAmount":
            return amount.amount / 1e9, TON
        return amount.amount, STARS
    stars = getattr(gift, "resell_stars", None)
    return (stars, STARS) if stars else (None, None)


def attr_name(gift, cls_name):
    return next((a.name for a in getattr(gift, "attributes", []) if type(a).__name__ == cls_name), "")


ATTR_ID = {
    "m": lambda i: StarGiftAttributeIdModel(document_id=i),
    "p": lambda i: StarGiftAttributeIdPattern(document_id=i),
    "b": lambda i: StarGiftAttributeIdBackdrop(backdrop_id=i),
}
ID_KIND = {"StarGiftAttributeIdModel": "m", "StarGiftAttributeIdPattern": "p", "StarGiftAttributeIdBackdrop": "b"}


def attr_ids(filt):
    return [ATTR_ID[k](value[1]) for k, value in filt.items()]


def read_attrs(res):
    """Списки моделей/фонов/узоров и (если Telegram отдал) сколько лотов у каждого."""
    out = {"m": [], "b": [], "p": []}
    for a in getattr(res, "attributes", None) or []:
        kind = type(a).__name__
        if kind == "StarGiftAttributeModel":
            out["m"].append((a.name, a.document.id))
        elif kind == "StarGiftAttributePattern":
            out["p"].append((a.name, a.document.id))
        elif kind == "StarGiftAttributeBackdrop" and getattr(a, "backdrop_id", None) is not None:
            out["b"].append((a.name, a.backdrop_id))
    for options in out.values():
        options.sort(key=lambda o: o[0].lower())
    counts = {}
    for c in getattr(res, "counters", None) or []:
        try:
            ref = c.attribute
            ident = getattr(ref, "document_id", None)
            if ident is None:
                ident = getattr(ref, "backdrop_id", None)
            counts[(ID_KIND[type(ref).__name__], ident)] = c.count
        except (AttributeError, KeyError):
            continue
    out["n"] = counts
    return out


# ── маркет ──────────────────────────────────────────────────

HAS_ATTR_HASH = "attributes_hash" in inspect.signature(GetResaleStarGiftsRequest.__init__).parameters


async def collections():
    cols = cache.get("cols")
    if cols is not None:
        return cols
    res = await tele(GetStarGiftsRequest(hash=0))
    gifts = [g for g in getattr(res, "gifts", []) if getattr(g, "availability_resale", 0)]
    cols = sorted(((g.id, getattr(g, "title", None) or f"Gift {g.id}") for g in gifts), key=lambda c: c[1].lower())
    return cache.put("cols", cols, ttl=300)


async def gift_attrs(gift_id):
    """Модели, фоны и узоры коллекции. Кэшируем отдельно от страниц лотов."""
    attrs = cache.get(("attrs", gift_id))
    if attrs is not None:
        return attrs
    kw = dict(gift_id=gift_id, offset="", limit=1)
    if HAS_ATTR_HASH:
        kw["attributes_hash"] = 0
    res = await tele(GetResaleStarGiftsRequest(**kw))
    return cache.put(("attrs", gift_id), read_attrs(res), ttl=600)


async def lots_page(v, idx):
    offset = v.offsets[idx]
    key = ("lots", v.gift, offset, v.sort, tuple(sorted((k, x[1]) for k, x in v.filt.items())))
    hit = cache.get(key)
    if hit is not None:
        return hit
    kw = dict(gift_id=v.gift, offset=offset, limit=PAGE)
    if v.sort == "price":
        kw["sort_by_price"] = True
    elif v.sort == "num":
        kw["sort_by_num"] = True
    if v.filt:
        kw["attributes"] = attr_ids(v.filt)
    res = await tele(GetResaleStarGiftsRequest(**kw))
    lots = [Lot.of(g) for g in res.gifts if hasattr(g, "slug")]
    data = (lots, getattr(res, "next_offset", None), getattr(res, "count", len(lots)))
    return cache.put(key, data, ttl=60)


async def poll(feed):
    """Десять самых дешёвых лотов в одной валюте, от дешёвого к дорогому."""
    kw = dict(gift_id=feed["gift"], offset="", limit=10, sort_by_price=True)
    if feed["filt"]:
        kw["attributes"] = attr_ids(feed["filt"])
    res = await tele(GetResaleStarGiftsRequest(**kw))
    lots = [lot for lot in (Lot.of(g) for g in res.gifts if hasattr(g, "slug")) if lot.price is not None]
    if not lots:
        return []
    unit = lots[0].unit
    return sorted((lot for lot in lots if lot.unit == unit), key=lambda lot: lot.price)


# ── сохранение состояния ────────────────────────────────────

def save_state():
    tmp = f"{WATCH_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"watches": watches, "feeds": feeds, "pins": pins}, f, ensure_ascii=False)
        os.replace(tmp, WATCH_FILE)  # без этого оборванная запись убивала весь файл
    except OSError as e:
        log.error("не удалось сохранить %s: %s", WATCH_FILE, e)


def load_state():
    try:
        with open(WATCH_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        log.error("%s не читается (%s), начинаю с чистого листа", WATCH_FILE, e)
        try:
            os.replace(WATCH_FILE, f"{WATCH_FILE}.bad")
        except OSError:
            pass
        return
    watches.extend(data.get("watches", []))
    feeds.update(data.get("feeds", {}))
    pins.update({int(chat): ids for chat, ids in data.get("pins", {}).items()})
    for feed in feeds.values():
        for row in feed.get("hist", []):
            row[4] = LEGACY_UNITS.get(row[4], row[4])
    for w in watches:
        w.setdefault("profit", False)


def fkey(gift, filt):
    return f"{gift}|" + ",".join(f"{k}{value[1]}" for k, value in sorted(filt.items()))


def own_watch(chat, wid):
    return next((w for w in watches if w["id"] == wid and w["chat"] == chat), None)


def toggle_pin(chat, gid):
    mine = pins.setdefault(chat, [])
    if gid in mine:
        mine.remove(gid)
        return False
    if len(mine) >= MAX_PINS:
        mine.pop(0)  # самую старую закреплённую вытесняем
    mine.append(gid)
    return True


# ── сигналы ─────────────────────────────────────────────────

DEAL, DROP, GAP = [5, 10, 15, 20, 30], [3, 5, 10, 15], [10, 25, 50, 100, 250, 500]
OPTS, DEF = {"deal": DEAL, "drop": DROP, "gap": GAP}, {"deal": 10, "drop": 5, "gap": 50}


def alert_kb(w, lot=None, label="🎁 Открыть лот"):
    top = [{"text": label, "url": f"https://t.me/nft/{lot.slug}"}] if lot else []
    top.append(btn("📊 Статистика", f"ws:{w['id']}"))
    return {"inline_keyboard": [top, [btn("⚙️ Настройки", f"wo:{w['id']}")]]}


async def check_lot(lot, cur, prev, subs):
    """Лот появился в топе или подешевел: смотрим, кому о нём написать."""
    price, unit = lot.price, lot.unit
    old = prev.get(lot.slug)
    if old is not None and price >= old:
        return
    others = [p for slug, p in cur.items() if slug != lot.slug]
    ref = med(others) if len(others) >= 3 else min(others, default=None)
    disc = (1 - price / ref) * 100 if ref else 0
    prev_floor = min((p for slug, p in prev.items() if slug != lot.slug), default=None)
    above = min((p for p in others if p > price), default=None)
    gap = above - price if above else 0
    is_floor = all(price <= p for p in others)
    sale = after_fee(ref, unit) if ref else None
    profit = sale - price if sale is not None else None

    for w in subs:
        hit_target = w["target"] is not None and price <= w["target"]
        new_floor = w.get("newfloor", True) and prev_floor is not None and price < prev_floor
        wide_gap = unit == STARS and is_floor and above is not None and gap >= w.get("gap", 50)
        deal = disc >= w["deal"]
        if not (deal or hit_target or new_floor or wide_gap):
            continue
        if w.get("profit") and not (profit and profit > 0):
            continue
        if w["alerted"].get(lot.slug, float("inf")) <= price:
            continue
        w["alerted"][lot.slug] = price
        while len(w["alerted"]) > 200:
            w["alerted"].pop(next(iter(w["alerted"])))

        lines = ["🔥 <b>Выгодный лот</b>", f"{link(lot.slug, f'{lot.title} #{lot.num}')} — <b>{money(price, unit)}</b>", RULE]
        if new_floor:
            lines.append(f"• новый флор, прежний был {money(prev_floor, unit)}")
        if wide_gap:
            lines.append(f"• следующий лот дороже на {money(gap, unit)}")
        if deal:
            lines.append(f"• на {disc:.0f}% ниже рынка (медиана остальных {money(ref, unit)})")
        if hit_target:
            lines.append(f"• укладывается в цель {money(w['target'], unit)}")
        if old:
            lines.append(f"• подешевел с {money(old, unit)}")
        if ref and profit > 0:
            lines += ["",
                      f"💰 Перепродажа по {money(ref, unit)}: на руки {money(sale, unit)} "
                      f"(комиссия {FEE_PCT}%), прибыль <b>{signed(profit)}</b> ({profit / price * 100:+.0f}%)",
                      f"⚖️ Безубыток: от {money(breakeven(price, unit), unit)}"]
        elif ref:
            lines += ["", f"⚠️ По {money(ref, unit)} перепродавать невыгодно: после комиссии {FEE_PCT}% "
                          f"останется {money(sale, unit)}"]
        lines += ["", f"👁 {esc(w['title'])} · {esc(fsum(w))}"]
        await say(w["chat"], "\n".join(lines), alert_kb(w, lot))


async def check_floor(feed, subs, lots, now):
    """Падение флора относительно суток и относительно первого дня слежения."""
    unit, floor = lots[0].unit, lots[0].price
    hist = feed["hist"]
    day = [x[1] for x in hist if x[4] == unit and now - 86400 <= x[0] <= now - 3600]
    for w in subs:
        dropped_now = False
        if len(day) >= 20:
            m = med(day)
            drop = (1 - floor / m) * 100
            last = w["last_drop"]
            if drop >= w["drop"] and (not last or now - last[0] > 1800 or floor < last[1] * 0.98):
                w["last_drop"], dropped_now = [now, floor], True
                await say(w["chat"], f"📉 <b>Флор упал</b>\n{RULE}\n"
                                     f"{link(lots[0].slug, money(floor, unit))}: на {drop:.1f}% ниже медианы "
                                     f"за сутки ({money(m, unit)})\n\n👁 {esc(w['title'])} · {esc(fsum(w))}",
                          alert_kb(w, lots[0], "🎁 Открыть флор"))
            elif drop < w["drop"] / 2:
                w["last_drop"] = None
        if w["day1"] and now - w["start"] >= 86400:
            first_day = [x[1] for x in hist if x[4] == unit and w["start"] <= x[0] <= w["start"] + 86400]
            if first_day and floor < min(first_day):
                if w["notified"] is None or floor < w["notified"]:
                    w["notified"] = floor
                    if not dropped_now:  # два сообщения подряд про одно и то же не нужны
                        await say(w["chat"], f"📉 <b>Флор ниже, чем в первый день</b>\n{RULE}\n"
                                             f"Сейчас {link(lots[0].slug, money(floor, unit))}, "
                                             f"тогда было {money(min(first_day), unit)}\n\n"
                                             f"👁 {esc(w['title'])} · {esc(fsum(w))}",
                                  alert_kb(w, lots[0], "🎁 Открыть флор"))
            elif first_day:
                w["notified"] = None


async def process(feed, lots):
    if not lots:
        return
    now = time.time()
    unit = lots[0].unit
    prices = [lot.price for lot in lots]
    floor = prices[0]
    hist = feed["hist"]
    if not hist or abs(hist[-1][1] - floor) > 1e-9 or now - hist[-1][0] >= 300:
        hist.append([now, floor, prices[1] if len(prices) > 1 else None, med(prices), unit])
        del hist[:-HIST_LIMIT]
    subs = [w for w in watches if w["key"] == feed["key"]]
    cur = {lot.slug: lot.price for lot in lots}
    if feed["seen"]:  # первый замер только запоминаем
        for lot in lots:
            await check_lot(lot, cur, feed["seen"], subs)
    feed["seen"] = cur
    await check_floor(feed, subs, lots, now)


def stats(w):
    feed = feeds.get(w["key"])
    if not feed or not feed["hist"]:
        return "📊 Замеров пока нет, загляни через пару минут."
    now, last = time.time(), feed["hist"][-1]
    unit, cur, second = last[4], last[1], last[2]
    pts = [x for x in feed["hist"] if x[4] == unit and x[0] >= now - 7 * 86400]
    floors = [x[1] for x in pts]

    now_line = f"💰 Флор: <b>{money(cur, unit)}</b>"
    if second and cur:
        now_line += f" · второй {num(second)} ({(second / cur - 1) * 100:+.1f}%)"
    out = [f"📊 <b>{esc(w['title'])}</b>", esc(fsum(w)), RULE, now_line]

    day = [x[1] for x in pts if x[0] >= now - 86400]
    if len(day) >= 5:
        m = med(day)
        out += ["", "📈 <b>За 24 часа</b>", f"<code>{spark(day)}</code>",
                f"медиана {num(m)} · мин {num(min(day))} · макс {num(max(day))}",
                f"сейчас {(cur / m - 1) * 100:+.1f}% к медиане",
                f"💸 Купить по флору, продать по медиане: {signed(after_fee(m, unit) - cur)} "
                f"{SIGN.get(unit, unit)} чистыми (комиссия {FEE_PCT}%)"]
    if len(floors) >= 20:
        higher = sum(1 for x in floors if x > cur) / len(floors)
        verdict = "🟢 зона покупки" if higher >= .9 else "🔴 дорого" if higher <= .1 else "⚪ нейтрально"
        out += ["", f"🎯 <b>Сигнал:</b> {verdict}", f"за неделю флор был выше текущего в {higher * 100:.0f}% замеров"]

    span = pts[-1][0] - pts[0][0] if pts else 0
    if span >= 172800 and floors:
        mean, by_hour = sum(floors) / len(floors), {}
        for x in pts:
            by_hour.setdefault(time.gmtime(x[0]).tm_hour, []).append(x[1] / mean)
        avg = {h: sum(v) / len(v) for h, v in by_hour.items() if len(v) >= 3}
        if len(avg) >= 6:
            order = sorted(avg, key=avg.get)
            fmt = lambda hours: ", ".join(f"{h:02d}:00 ({(avg[h] - 1) * 100:+.1f}%)" for h in hours)
            out += ["", f"🕐 Дешевле всего обычно (UTC): {fmt(order[:3])}",
                    f"🕐 Дороже всего обычно (UTC): {fmt(order[-3:])}"]
    else:
        out += ["", f"🕐 Почасовой паттерн: накоплено {span / 3600:.0f} ч из 48"]

    first_day = [x[1] for x in feed["hist"] if x[4] == unit and w["start"] <= x[0] <= w["start"] + 86400]
    if first_day:
        out.append(f"📅 Флор первого дня: {num(min(first_day))} (сейчас {(cur / min(first_day) - 1) * 100:+.1f}%)")

    days = {}
    for x in pts:
        days.setdefault(time.strftime("%d.%m", time.gmtime(x[0])), []).append(x[1])
    rows = [f"{d}: мин {num(min(v))} · средняя {num(sum(v) / len(v))}" for d, v in list(days.items())[-7:]]
    out += ["", "🗓 <b>По дням</b>", "<blockquote expandable>" + "\n".join(rows) + "</blockquote>"]
    return "\n".join(out)


async def watch_loop():
    slow = 1
    while True:
        started = time.time()
        live = {w["key"] for w in watches}
        for key in [k for k in feeds if k not in live]:
            del feeds[key]
        for key in list(feeds):
            feed = feeds.get(key)
            if feed is None:
                continue
            try:
                await process(feed, await poll(feed))
            except FloodWaitError as e:
                slow = min(slow * 2, 8)
                log.warning("flood wait %s сек", e.seconds)
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                log.exception("опрос %s", key)
            await asyncio.sleep(1)
        save_state()
        await asyncio.sleep(max(5, POLL_EVERY * slow - (time.time() - started)))
        slow = max(1, slow - 1)


async def add_watch(chat, v):
    """Возвращает (текст, клавиатура)."""
    key = fkey(v.gift, v.filt)
    mine = [w for w in watches if w["chat"] == chat]
    if any(w["key"] == key for w in mine):
        return "Ты уже следишь за этим. Настройки: /watch", None
    if len(mine) >= MAX_WATCHES:
        return f"Максимум {MAX_WATCHES} слежений. Лишнее можно удалить в /watch.", None
    w = {"id": uuid.uuid4().hex[:8], "chat": chat, "key": key, "gift": v.gift, "title": v.title,
         "filt": {k: list(x) for k, x in v.filt.items()}, "start": time.time(), "deal": DEF["deal"],
         "drop": DEF["drop"], "gap": DEF["gap"], "target": None, "day1": True, "newfloor": True,
         "profit": False, "notified": None, "last_drop": None, "alerted": {}}
    watches.append(w)
    feed = feeds.setdefault(key, {"key": key, "gift": v.gift, "filt": w["filt"], "hist": [], "seen": {}})
    lots = []
    try:
        lots = await poll(feed)
        await process(feed, lots)
    except FloodWaitError:
        raise
    except Exception:
        log.exception("первый замер %s", key)
    save_state()
    floor = money(lots[0].price, lots[0].unit) if lots else "пока нет лотов"
    text = (f"✅ <b>Слежу за {esc(w['title'])}</b>\n{esc(fsum(w))}\n{RULE}\n"
            f"💰 Флор сейчас: <b>{floor}</b>\n\n"
            f"Проверяю рынок раз в {POLL_EVERY} сек и напишу, если:\n"
            f"• появится лот ниже текущего флора\n"
            f"• лот будет дешевле следующего на {w['gap']}+ ⭐\n"
            f"• лот будет ниже рынка на {w['deal']}%+\n"
            f"• флор упадёт на {w['drop']}%+ от медианы за сутки")
    kb = {"inline_keyboard": [[btn("⚙️ Настроить пороги", f"wo:{w['id']}"), btn("🏠 Меню", "home")]]}
    return text, kb


def watch_menu(w):
    feed = feeds.get(w["key"])
    cur = feed["hist"][-1] if feed and feed["hist"] else None
    mark = lambda on: "✅" if on else "❌"
    target = num(w["target"]) if w["target"] is not None else "нет"
    wid = w["id"]
    rows = [
        [btn(f"🔥 Выгодный лот: ниже рынка на {w['deal']}%+", f"wc:{wid}:deal")],
        [btn(f"📉 Падение флора: {w['drop']}%+ к медиане 24ч", f"wc:{wid}:drop")],
        [btn(f"🆕 Новый лот ниже флора: {mark(w.get('newfloor', True))}", f"wn:{wid}")],
        [btn(f"📏 Разрыв со следующим лотом: {w.get('gap', DEF['gap'])}+ ⭐", f"wc:{wid}:gap")],
        [btn(f"🎯 Цель по цене: {target}", f"wt:{wid}")],
        [btn(f"💰 Только с прибылью после комиссии: {mark(w.get('profit', False))}", f"wp:{wid}")],
        [btn(f"📅 Сравнение с первым днём: {mark(w['day1'])}", f"w1:{wid}")],
        [btn("📊 Статистика", f"ws:{wid}"), btn("🗑 Удалить", f"wd:{wid}")],
        [btn("⬅️ Мои слежения", "wl")],
    ]
    now = f"💰 Флор сейчас: <b>{money(cur[1], cur[4])}</b>" if cur else "Замеров пока нет"
    text = (f"🔔 <b>{esc(w['title'])}</b>\n{esc(fsum(w))}\n{RULE}\n{now}\n"
            f"🕐 Слежу с {stamp(w['start'])}\n\nНажми на пункт, чтобы изменить порог.")
    return text, {"inline_keyboard": rows}


def watch_list(chat):
    mine = [w for w in watches if w["chat"] == chat]
    home = [btn("🏠 Меню", "home")]
    if not mine:
        return ("🔔 <b>Мои слежения</b>\n" + RULE + "\nПока пусто. Открой коллекцию и нажми «Следить за флором», "
                "бот сам напишет о выгодных лотах."), {"inline_keyboard": [home]}
    lines = [f"🔔 <b>Мои слежения</b> ({len(mine)}/{MAX_WATCHES})", RULE]
    for n, w in enumerate(mine, 1):
        feed = feeds.get(w["key"])
        cur = feed["hist"][-1] if feed and feed["hist"] else None
        lines.append(f"{n}. <b>{esc(w['title'])}</b>" + (f" · флор {money(cur[1], cur[4])}" if cur else ""))
        lines.append(f"└ {esc(fsum(w))}")
    lines += ["", "Нажми на слежение, чтобы настроить."]
    rows = [[btn(f"{n}. {short(w['title'], 22)} · {short(fsum(w), 22)}", f"wm:{w['id']}")] for n, w in enumerate(mine, 1)]
    return "\n".join(lines), {"inline_keyboard": rows + [home]}


# ── подарки пользователя ────────────────────────────────────

SAVED_FILTERS = ("exclude_unlimited", "exclude_limited", "exclude_upgradable", "exclude_unupgradable")
SAVED_PARAMS = set(inspect.signature(GetSavedStarGiftsRequest.__init__).parameters) if GetSavedStarGiftsRequest else set()


async def fetch_saved(peer, server_filter):
    """Вытаскивает коллекционные подарки профиля целиком.

    Выдача Telegram смешивает обычные и коллекционные подарки, поэтому раньше
    страница из 25 штук после фильтрации могла сжаться до пары ссылок.
    Теперь читаем всё и режем на страницы уже у себя.
    """
    kw = dict(peer=peer, limit=SAVED_BATCH)
    if server_filter:
        kw.update({name: True for name in SAVED_FILTERS if name in SAVED_PARAMS})
    found, seen, offset = [], set(), ""
    for _ in range(SAVED_MAX_REQUESTS):
        res = await tele(GetSavedStarGiftsRequest(offset=offset, **kw))
        for saved in res.gifts:
            gift = getattr(saved, "gift", None)
            if gift is not None and hasattr(gift, "slug") and gift.slug not in seen:
                seen.add(gift.slug)
                found.append(Lot.of(gift))
        offset = getattr(res, "next_offset", None)
        if not offset:
            break
        await asyncio.sleep(0.2)
    return found


async def load_profile(peer):
    head = await tele(GetSavedStarGiftsRequest(peer=peer, offset="", limit=1))
    total = getattr(head, "count", 0) or 0
    items = await fetch_saved(peer, True)
    if not items and total:  # серверный фильтр ничего не вернул: перечитываем без него
        items = await fetch_saved(peer, False)
    return items, total


def lot_card(i, lot):
    """Карточка лота: ссылка, а под ней цена и атрибуты."""
    out = [f"{i}. {link(lot.slug, f'{lot.title} #{lot.num}')}"]
    bits = []
    if lot.price is not None:
        bits.append(f"<b>{money(lot.price, lot.unit)}</b>")
    if lot.model:
        bits.append(f"{ICON['m']} {esc(lot.model)}")
    if lot.backdrop:
        bits.append(f"{ICON['b']} {esc(lot.backdrop)}")
    if bits:
        out.append("└ " + " · ".join(bits))
    return out


def pager(data, idx, pages, has_next=None):
    """Строка навигации: ⬅️ 2/5 ➡️. data(i) собирает callback_data страницы i."""
    has_next = idx < pages - 1 if has_next is None else has_next
    row = [btn("⬅️", data(idx - 1))] if idx > 0 else []
    if pages > 1:
        row.append(btn(f"{idx + 1}/{pages}", "noop"))
    if has_next:
        row.append(btn("➡️", data(idx + 1)))
    return row


def profile_page(v, idx):
    pages = max(1, math.ceil(len(v.items) / PAGE))
    idx = max(0, min(idx, pages - 1))
    regular = v.total - len(v.items)
    stat = f"🎁 Коллекционных: <b>{len(v.items)}</b>" + (f" · обычных: {regular}" if regular > 0 else "")
    lines = [f"👤 <b>{esc(v.name)}</b>", RULE, stat, ""]
    for n, lot in enumerate(v.items[idx * PAGE:(idx + 1) * PAGE], idx * PAGE + 1):
        lines += lot_card(n, lot)
    if not v.items:
        lines.append("Коллекционных подарков нет или профиль скрыт.")
    nav = pager(lambda i: f"up:{v.id}:{i}", idx, pages)
    rows = ([nav] if nav else []) + [[btn("🔄 Обновить", f"ur:{v.id}"), btn("🏠 Меню", "home")]]
    return fit(lines), {"inline_keyboard": rows}


USERNAME = re.compile(r"(?:https?://)?(?:t\.me/)?@?([A-Za-z0-9_]{3,32})")


def is_user_ref(text):
    return text.startswith("@") or "t.me/" in text


async def open_user(chat, raw):
    if GetSavedStarGiftsRequest is None:
        return await say(chat, "Для этого нужен свежий Telethon: <code>pip install -U telethon</code>")
    token = (raw.split() or [""])[0].rstrip("/")
    match = USERNAME.fullmatch(token)
    if not match:
        return await say(chat, "Пришли ник вида @durov")
    name = match.group(1)
    peer = peers.get(name.lower())
    if peer is None:
        try:
            peer = await tele.get_input_entity(name)
        except FloodWaitError:
            raise
        except Exception:
            return await say(chat, f"Не нашёл пользователя @{esc(name)}. Проверь ник.")
        peers[name.lower()] = peer
    sent = await say(chat, f"⏳ Загружаю подарки @{esc(name)}…")
    mid = (sent.get("result") or {}).get("message_id")

    async def show(text, kb=None):
        return await (edit(chat, mid, text, kb) if mid else say(chat, text, kb))

    try:
        items, total = await load_profile(peer)
    except FloodWaitError as e:
        return await show(f"⏳ Telegram просит подождать {e.seconds} сек. Попробуй чуть позже.")
    except RPCError as e:
        log.warning("подарки %s: %s", name, e)
        return await show("Telegram не отдал подарки этого профиля. Возможно, они скрыты.")
    text, kb = profile_page(remember(Profile(peer=peer, name=f"@{name}", items=items, total=total)), 0)
    await show(text, kb)


# ── каталог ─────────────────────────────────────────────────

SN = {"price": "по цене", "num": "по номеру", "new": "сначала новые"}
SORT_ICON = {"price": "💰", "num": "🔢", "new": "🆕"}
SORT_BTN = {"price": "Цена", "num": "Номер", "new": "Новые"}


def collection_rows(cols):
    buttons = [btn(short(title), f"c:{gid}") for gid, title in cols]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


async def home_view(chat, page=0, note=""):
    cols = await collections()
    if not cols:
        return f"{note}Сейчас на маркете ничего нет.", {"inline_keyboard": [[btn("🔔 Слежения", "wl")]]}
    pages = math.ceil(len(cols) / COLS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    titles = dict(cols)
    pinned = [(gid, f"📌 {titles[gid]}") for gid in pins.get(chat, []) if gid in titles]
    rows = collection_rows(pinned) if page == 0 else []
    rows += collection_rows(cols[page * COLS_PER_PAGE:(page + 1) * COLS_PER_PAGE])
    nav = pager(lambda i: f"home:{i}", page, pages)
    if nav:
        rows.append(nav)
    rows.append([btn("🔔 Слежения", "wl"), btn("👤 Подарки по нику", "u")])
    text = (f"{note}🎁 <b>Маркет подарков</b>\n{RULE}\n"
            "Выбери коллекцию или напиши её название.\n"
            "Подарки пользователя: пришли @ник.\n\n"
            f"На маркете: {len(cols)}" + (f" · закреплено: {len(pinned)}" if pinned else ""))
    return text, {"inline_keyboard": rows}


async def render(v, idx):
    idx = max(0, min(idx, len(v.offsets) - 1))
    lots, nxt, count = await lots_page(v, idx)
    v.page = idx
    if nxt and len(v.offsets) == idx + 1:
        v.offsets.append(nxt)
    pages = max(1, math.ceil(count / PAGE))
    lines = [f"🎁 <b>{esc(v.title)}</b>", RULE, f"🔎 {esc(chips(v.filt) or 'без фильтров')}",
             f"{SORT_ICON[v.sort]} {SN[v.sort]} · лотов: {num(count)}"]
    if v.sort == "price" and idx == 0 and lots and lots[0].price is not None:
        lines.append(f"🏷 Флор: <b>{money(lots[0].price, lots[0].unit)}</b>")
    lines.append("")
    for n, lot in enumerate(lots, idx * PAGE + 1):
        lines += lot_card(n, lot)
    if not lots:
        lines.append("Ничего не найдено. Открой «Фильтры» и сбрось их.")
    nav = pager(lambda i: f"p:{v.id}:{i}", idx, pages, has_next=len(v.offsets) > idx + 1)
    pinned = v.gift in pins.get(v.chat, [])
    active = f" · {len(v.filt)}" if v.filt else ""
    rows = ([nav] if nav else []) + [
        [btn(f"🔎 Фильтры{active}", f"f:{v.id}"), btn("🔔 Следить за флором", f"w:{v.id}")],
        [btn("📌 Закреплено ✅" if pinned else "📌 Закрепить", f"pin:{v.id}"), btn("🏠 Меню", "home")],
    ]
    return fit(lines), {"inline_keyboard": rows}


async def filters_view(v):
    if v.attrs is None:
        v.attrs = await gift_attrs(v.gift)
    try:
        _, _, count = await lots_page(v, 0)
    except FloodWaitError:
        raise
    except Exception:
        log.exception("счётчик лотов")
        count = None
    rows = []
    for k in "mbp":
        if k in v.filt:
            rows.append([btn(f"{ICON[k]} {KN[k]}: {short(v.filt[k][0], 22)}", f"fl:{v.id}:{k}:0"),
                         btn("✕", f"fs:{v.id}:{k}:-1")])
        else:
            rows.append([btn(f"{ICON[k]} {KN[k]}: любая  ›", f"fl:{v.id}:{k}:0")])
    rows.append([btn(("✅ " if v.sort == s else SORT_ICON[s] + " ") + SORT_BTN[s], f"so:{v.id}:{s}") for s in SN])
    show = f"👁 Показать ({num(count)})" if count is not None else "👁 Показать"
    rows.append([btn("♻️ Сбросить", f"fr:{v.id}"), btn(show, f"p:{v.id}:0")])
    sel = lambda k: esc(v.filt[k][0]) if k in v.filt else "любая"
    text = (f"🔎 <b>Фильтры</b> · {esc(v.title)}\n{RULE}\n"
            + "\n".join(f"{ICON[k]} {KN[k]}: <b>{sel(k)}</b>" for k in "mbp")
            + f"\n{SORT_ICON[v.sort]} Сортировка: <b>{SN[v.sort]}</b>\n\n"
            + (f"Подходит лотов: <b>{num(count)}</b>\n" if count is not None else "")
            + "Нажми на пункт, чтобы выбрать значение.")
    return text, {"inline_keyboard": rows}


def option_rows(v, kind, indices):
    """Кнопки значений фильтра по индексам в полном списке (по две в ряд)."""
    options, counts = v.attrs.get(kind, []), v.attrs.get("n", {})
    chosen = v.filt.get(kind, (None, None))[1]
    buttons = []
    for i in indices:
        name, ident = options[i]
        label = short(name, 20) + (f" · {counts[(kind, ident)]}" if (kind, ident) in counts else "")
        buttons.append(btn(("✅ " if ident == chosen else "") + label, f"fs:{v.id}:{kind}:{i}"))
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def choices_view(v, kind, pg):
    options = v.attrs.get(kind, [])
    pages = max(1, math.ceil(len(options) / CHOICES_PER_PAGE))
    pg = max(0, min(pg, pages - 1))
    start = pg * CHOICES_PER_PAGE
    rows = option_rows(v, kind, range(start, min(start + CHOICES_PER_PAGE, len(options))))
    nav = pager(lambda i: f"fl:{v.id}:{kind}:{i}", pg, pages)
    if nav:
        rows.append(nav)
    rows.append([btn("♻️ Любая", f"fs:{v.id}:{kind}:-1"), btn("⬅️ К фильтрам", f"f:{v.id}")])
    now = v.filt[kind][0] if kind in v.filt else "любая"
    hint = "Выбери из списка или просто напиши название." if len(options) > 8 else "Выбери из списка."
    counts = " Цифра рядом с названием — сколько лотов." if v.attrs.get("n") else ""
    text = (f"{ICON[kind]} <b>{KN[kind]}</b> · {esc(v.title)}\n{RULE}\n{hint}{counts}\n\n"
            f"Сейчас: <b>{esc(now)}</b>\nВсего значений: {len(options)}")
    return text, {"inline_keyboard": rows}


# ── обработка апдейтов ──────────────────────────────────────

BTN_HOME, BTN_WATCH, BTN_USER = "🎁 Коллекции", "🔔 Слежения", "👤 Подарки по нику"
MAIN_KB = {"keyboard": [[{"text": BTN_HOME}, {"text": BTN_WATCH}], [{"text": BTN_USER}]],
           "resize_keyboard": True, "is_persistent": True,
           "input_field_placeholder": "Название коллекции или @ник"}
WELCOME = ("👋 Привет! Здесь можно смотреть лоты Telegram-подарков, фильтровать их по модели, фону и узору "
           "и следить за ценой.\n\nМеню всегда под рукой внизу.")
HELP = ("🎁 <b>Маркет подарков</b>\n" + RULE + "\n"
        "<b>Что умеет</b>\n"
        "🎁 Каталог: лоты любой коллекции с фильтрами по модели, фону и узору\n"
        "🔔 Слежение: бот сам пишет о выгодных лотах и падении флора\n"
        "👤 Подарки по нику: все коллекционные подарки пользователя\n\n"
        "<b>Команды</b>\n"
        "/start: меню\n"
        "/watch: мои слежения\n"
        "/user @ник: подарки пользователя\n\n"
        "<b>Подсказки</b>\n"
        "• напиши название коллекции, и я её найду\n"
        "• пришли @ник, и покажу подарки\n"
        "• в фильтре можно написать название модели прямо в чат\n\n"
        f"💰 Комиссия маркета {FEE_PCT}% учитывается в прибыли.")
COMMANDS = [{"command": "start", "description": "Меню"},
            {"command": "watch", "description": "Мои слежения за ценами"},
            {"command": "user", "description": "Подарки пользователя"},
            {"command": "help", "description": "Как пользоваться"}]
STALE = "Это меню устарело, открой коллекцию заново.\n\n"


def get_view(args, kind):
    v = views.get(args[0]) if args else None
    return v if isinstance(v, kind) else None


async def show_home(chat, page=0, note=""):
    text, kb = await home_view(chat, page, note)
    await say(chat, text, kb)


async def ask_user(chat):
    waiting[chat] = "user"
    await say(chat, "👤 Пришли ник пользователя, например @durov")


async def cb_noop(chat, a):
    return None


async def cb_home(chat, a):
    return await home_view(chat, int(a[0]) if a and a[0] else 0)


async def cb_watches(chat, a):
    return watch_list(chat)


async def cb_ask_user(chat, a):
    await ask_user(chat)


async def cb_user_page(chat, a):
    v = get_view(a, Profile)
    return profile_page(v, int(a[1])) if v else await home_view(chat, note=STALE)


async def cb_user_refresh(chat, a):
    v = get_view(a, Profile)
    if not v:
        return await home_view(chat, note=STALE)
    v.items, v.total = await load_profile(v.peer)
    return profile_page(v, 0)


async def cb_collection(chat, a):
    gid = int(a[0])
    title = next((t for i, t in await collections() if i == gid), f"Gift {gid}")
    return await render(remember(Catalog(gift=gid, title=title, chat=chat)), 0)


async def cb_page(chat, a):
    v = get_view(a, Catalog)
    return await render(v, int(a[1])) if v else await home_view(chat, note=STALE)


async def cb_pin(chat, a):
    v = get_view(a, Catalog)
    if not v:
        return await home_view(chat, note=STALE)
    toggle_pin(chat, v.gift)
    save_state()
    return await render(v, v.page)


async def cb_filters(chat, a):
    v = get_view(a, Catalog)
    return await filters_view(v) if v else await home_view(chat, note=STALE)


async def cb_choices(chat, a):
    v = get_view(a, Catalog)
    if not v:
        return await home_view(chat, note=STALE)
    if v.attrs is None:
        v.attrs = await gift_attrs(v.gift)
    waiting[chat] = ("find", v.id, a[1])  # следующий текст пользователя ищем среди значений
    return choices_view(v, a[1], int(a[2]))


async def cb_pick(chat, a):
    v = get_view(a, Catalog)
    if not v:
        return await home_view(chat, note=STALE)
    kind, i = a[1], int(a[2])
    options = (v.attrs or {}).get(kind, [])
    if 0 <= i < len(options):
        v.filt[kind] = options[i]
    else:
        v.filt.pop(kind, None)
    v.offsets = [""]
    return await filters_view(v)


async def cb_sort(chat, a):
    v = get_view(a, Catalog)
    if not v:
        return await home_view(chat, note=STALE)
    if a[1] in SN:
        v.sort = a[1]
        v.offsets = [""]
    return await filters_view(v)


async def cb_reset(chat, a):
    v = get_view(a, Catalog)
    if not v:
        return await home_view(chat, note=STALE)
    v.filt.clear()
    v.sort, v.offsets = "price", [""]
    return await filters_view(v)


async def cb_track(chat, a):
    v = get_view(a, Catalog)
    if not v:
        return await home_view(chat, note=STALE)
    text, kb = await add_watch(chat, v)
    await say(chat, text, kb)


async def cb_watch(chat, a):
    w = own_watch(chat, a[0])
    return watch_menu(w) if w else watch_list(chat)


async def cb_open_watch(chat, a):
    """Настройки из уведомления открываем новым сообщением, чтобы не затирать сигнал."""
    w = own_watch(chat, a[0])
    if w:
        await say(chat, *watch_menu(w))


async def cb_tune(chat, a):
    w = own_watch(chat, a[0])
    if w and len(a) > 1 and a[1] in OPTS:
        opts = OPTS[a[1]]
        cur = w.get(a[1], DEF[a[1]])
        w[a[1]] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
        save_state()
    return watch_menu(w) if w else watch_list(chat)


def toggler(name, default):
    async def handler(chat, a):
        w = own_watch(chat, a[0])
        if w:
            w[name] = not w.get(name, default)
            save_state()
        return watch_menu(w) if w else watch_list(chat)
    return handler


async def cb_target(chat, a):
    w = own_watch(chat, a[0])
    if w:
        waiting[chat] = ("target", w["id"])
        await say(chat, "🎯 Пришли целевую цену числом, напишу, когда появится лот не дороже. 0 — выключить.")


async def cb_stats(chat, a):
    w = own_watch(chat, a[0])
    if w:
        await say(chat, stats(w))


async def cb_delete(chat, a):
    w = own_watch(chat, a[0])
    if w:
        watches.remove(w)
        save_state()
    return watch_list(chat)


CALLBACKS = {
    "noop": cb_noop, "home": cb_home, "wl": cb_watches, "u": cb_ask_user, "up": cb_user_page, "ur": cb_user_refresh,
    "c": cb_collection, "p": cb_page, "pin": cb_pin, "f": cb_filters, "fl": cb_choices, "fs": cb_pick,
    "so": cb_sort, "fr": cb_reset, "w": cb_track,
    "wm": cb_watch, "wo": cb_open_watch, "wc": cb_tune, "wt": cb_target, "ws": cb_stats, "wd": cb_delete,
    "wn": toggler("newfloor", True), "w1": toggler("day1", True), "wp": toggler("profit", False),
}


async def on_callback(q):
    await api("answerCallbackQuery", callback_query_id=q["id"])
    msg = q.get("message")
    action, *args = q["data"].split(":")
    handler = CALLBACKS.get(action)
    if not msg or handler is None:
        return
    chat, mid = msg["chat"]["id"], msg["message_id"]
    if action != "noop":
        waiting.pop(chat, None)  # любое нажатие отменяет «жду текст»; нужный обработчик выставит его заново
    result = await handler(chat, args)
    if result:
        await edit(chat, mid, *result)


async def on_find(chat, state, text):
    """Пользователь пишет название модели/фона/узора вместо листания списка."""
    _, vid, kind = state
    v = views.get(vid)
    if not isinstance(v, Catalog) or v.attrs is None:
        waiting.pop(chat, None)
        return await show_home(chat, note=STALE)
    options = v.attrs.get(kind, [])
    q = text.lower().strip()
    hits = [i for i, (name, _) in enumerate(options) if q in name.lower()]
    exact = [i for i in hits if options[i][0].lower() == q]
    if len(exact) == 1:
        hits = exact
    if not hits:
        return await say(chat, f"По запросу «{esc(text)}» ничего нет. Попробуй часть слова или выбери из списка.")
    if len(hits) == 1:
        waiting.pop(chat, None)
        v.filt[kind] = options[hits[0]]
        v.offsets = [""]
        out, kb = await filters_view(v)
        return await say(chat, out, kb)
    rows = option_rows(v, kind, hits[:CHOICES_PER_PAGE]) + [[btn("⬅️ К фильтрам", f"f:{v.id}")]]
    more = f"\nПоказал первые {CHOICES_PER_PAGE}, уточни запрос." if len(hits) > CHOICES_PER_PAGE else ""
    await say(chat, f"Нашёл {len(hits)}, выбери нужное.{more}", {"inline_keyboard": rows})


async def on_reply(chat, state, text):
    """Ответ на вопрос бота: ник пользователя, целевая цена или поиск значения фильтра."""
    if state == "user":
        waiting.pop(chat, None)
        return await open_user(chat, text)
    if state[0] == "find":
        return await on_find(chat, state, text)
    try:
        value = float(text.replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return await say(chat, "Нужно число, например 120.")
    waiting.pop(chat, None)
    w = own_watch(chat, state[1])
    if w:
        w["target"] = value if value > 0 else None
        save_state()
        await say(chat, *watch_menu(w))


async def find_collection(chat, query):
    if len(query) > 64:
        return
    found = [c for c in await collections() if query.lower() in c[1].lower()][:COLS_PER_PAGE]
    if not found:
        return await say(chat, f"По запросу «{esc(query)}» ничего нет. Все коллекции: {BTN_HOME}")
    rows = collection_rows(found) + [[btn("🏠 Все коллекции", "home")]]
    await say(chat, f"🔎 Нашёл: <b>{len(found)}</b>", {"inline_keyboard": rows})


async def on_message(m):
    chat = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/"):
        waiting.pop(chat, None)
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        if cmd in ("/start", "/gifts"):
            await say(chat, WELCOME, MAIN_KB)
            await show_home(chat)
        elif cmd == "/watch":
            await say(chat, *watch_list(chat))
        elif cmd == "/user":
            if arg:
                await open_user(chat, arg)
            else:
                await ask_user(chat)
        elif cmd == "/help":
            await say(chat, HELP)
        return
    if text in (BTN_HOME, BTN_WATCH, BTN_USER):
        waiting.pop(chat, None)
        if text == BTN_HOME:
            return await show_home(chat)
        if text == BTN_WATCH:
            return await say(chat, *watch_list(chat))
        return await ask_user(chat)
    state = waiting.get(chat)
    if is_user_ref(text) and not (isinstance(state, tuple) and state[0] == "target"):
        waiting.pop(chat, None)
        return await open_user(chat, text)
    if state is not None:
        return await on_reply(chat, state, text)
    await find_collection(chat, text)


async def handle(update):
    chat = None
    try:
        if "message" in update:
            chat = update["message"]["chat"]["id"]
            await on_message(update["message"])
        elif "callback_query" in update:
            msg = update["callback_query"].get("message")
            chat = msg["chat"]["id"] if msg else None
            await on_callback(update["callback_query"])
    except FloodWaitError as e:
        if chat:
            await say(chat, f"⏳ Telegram просит подождать {e.seconds} сек. Попробуй чуть позже.")
    except Exception:
        log.exception("апдейт %s", update.get("update_id"))
        if chat:
            await say(chat, "Что-то сломалось, попробуй ещё раз.")


# ── сервер ──────────────────────────────────────────────────

async def webhook(req):
    token = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token.encode(), SECRET.encode()):
        return web.Response(status=403)
    try:
        update = await req.json()
    except ValueError:
        return web.Response(status=400)
    background(handle(update))
    return web.Response(text="ok")


async def health(req):
    return web.Response(text="ok")


async def keepalive():
    """Бесплатный Render засыпает без трафика, так что пингуем себя раз в 10 минут."""
    while True:
        await asyncio.sleep(600)
        try:
            async with http.get(f"{PUBLIC_URL}/health") as resp:
                await resp.read()
        except Exception as e:
            log.warning("keepalive: %s", e)


async def main():
    global http
    http = ClientSession(timeout=ClientTimeout(total=30))
    load_state()
    await tele.connect()
    if not await tele.is_user_authorized():
        raise SystemExit("STRING_SESSION не работает, сгенерируй новую (make_session.py)")
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/webhook", webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    await api("setMyCommands", commands=COMMANDS)
    await api("setChatMenuButton", menu_button={"type": "commands"})
    if PUBLIC_URL:
        await api("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=SECRET,
                  allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        background(keepalive())
    else:
        log.warning("PUBLIC_URL не задан, вебхук не установлен и апдейты не придут")
    background(watch_loop())
    log.info("бот запущен")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
