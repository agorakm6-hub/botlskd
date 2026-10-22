"""Бот для маркета Telegram-подарков: каталог лотов, фильтры, слежение за флором,
подарки пользователя по нику.

Апдейты приходят на вебхук. Данные маркета читаются через пользовательскую
сессию Telethon: Bot API про перепродажу подарков ничего не знает.
"""
import asyncio
import hmac
import html
import inspect
import io
import json
import logging
import math
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiohttp import ClientSession, ClientTimeout, FormData, web
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

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("BOT_TZ", "Europe/Chisinau"))
except Exception:  # нет базы часовых поясов: берём фиксированный сдвиг
    LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET", 3))))

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
OWNER_ID = int(os.environ.get("OWNER_ID", 6811074441))  # владелец: без лимитов, есть админ-панель

PAGE = int(os.environ.get("PAGE_SIZE", 10))  # лотов на странице: 10 читается на телефоне без прокрутки
COLS_PER_PAGE = 20     # коллекций на странице главного меню
CHOICES_PER_PAGE = 20  # значений фильтра на странице
HIST_LIMIT = 6000
MAX_PINS = 8
SAVED_BATCH = 100      # сколько подарков профиля просим за один запрос
SAVED_MAX_REQUESTS = 40

STARS, TON = "XTR", "TON"
SIGN = {STARS: "⭐", TON: "TON"}
UNIT_NAME = {STARS: "Stars", TON: "TON"}
UNIT_BTN = {STARS: "⭐ Stars", TON: "💎 TON"}
DEFAULT_UNIT = STARS
POLL_FETCH = 30
TESTER_ID = 8866839093
LEGACY_UNITS = {"★": STARS}  # так хранились единицы в старых watch.json
NBSP = " "
RULE = "━━━━━━━━━━━━━━"

tele = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
http = None
watches, feeds, pins = [], {}, {}
views, waiting, peers = {}, {}, {}
users = {}  # id -> {name, username, first, seen, banned, blocked, limit}
settings = {"limit": 1, "maintenance": False}  # limit: сколько слежений у обычного пользователя
pending_bc = {}  # текст рассылки, ждущий подтверждения владельца
STARTED = time.time()
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


def local(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ)


def tz_label():
    off = local(time.time()).utcoffset().total_seconds() / 3600
    return "UTC" + (f"{off:+g}" if off else "")


def stamp(ts):
    return f"{local(ts).strftime('%d.%m %H:%M')} ({tz_label()})"


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


def kb_of(*rows):
    return {"inline_keyboard": [row for row in rows if row]}


def fit(lines, limit=3900):
    """Склеивает строки, не вылезая за лимит Telegram в 4096 символов."""
    out, size = [], 0
    for line in lines:
        size += len(line) + 1
        if size > limit:
            out.append("…")
            break
        out.append(line)
    return "\n".join(out).rstrip()


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


@dataclass
class Photo:
    """Готовая картинка с подписью и кнопками: обработчик отдаёт её, а отправка и правка сообщения за нами."""
    png: bytes
    caption: str
    kb: dict


async def upload(method, fields, name, png):
    """Запрос Bot API с файлом: тут нужен multipart, а не JSON, как у остальных методов."""
    form = FormData()
    for key, value in fields.items():
        form.add_field(key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    form.add_field(name, png, filename="floor.png", content_type="image/png")
    async with http.post(f"{API}/{method}", data=form) as resp:
        data = await resp.json()
    if not data.get("ok") and "not modified" not in str(data):
        log.warning("%s: %s", method, data)
    return data


async def send_photo(chat, photo):
    return await upload("sendPhoto", {"chat_id": chat, "caption": photo.caption, "parse_mode": "HTML",
                                      "reply_markup": photo.kb}, "photo", photo.png)


async def edit_photo(chat, mid, photo):
    media = {"type": "photo", "media": "attach://chart", "caption": photo.caption, "parse_mode": "HTML"}
    return await upload("editMessageMedia", {"chat_id": chat, "message_id": mid, "media": media,
                                             "reply_markup": photo.kb}, "chart", photo.png)


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
    pattern: str = ""

    @classmethod
    def of(cls, gift, want=None):
        price, unit = price_of(gift, want)
        return cls(gift.title, gift.num, gift.slug, price, unit,
                   attr_name(gift, "StarGiftAttributeModel"), attr_name(gift, "StarGiftAttributeBackdrop"),
                   attr_name(gift, "StarGiftAttributePattern"))


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


def prices_of(gift):
    """Цены лота по валютам"""
    out = {}
    for amount in getattr(gift, "resell_amount", None) or []:
        if type(amount).__name__ == "StarsTonAmount":
            out.setdefault(TON, amount.amount / 1e9)
        else:
            out.setdefault(STARS, amount.amount)
    stars = getattr(gift, "resell_stars", None)
    if stars and STARS not in out:
        out[STARS] = stars
    return out


def price_of(gift, want=None):
    """Цена в нужной валюте"""
    prices = prices_of(gift)
    if want:
        return (prices[want], want) if want in prices else (None, None)
    for unit, price in prices.items():
        return price, unit
    return None, None


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
    """Списки моделей/фонов/узоров и (если Telegram отдал) сколько лотов у каждого.

    Значения, по которым на маркете есть лоты, идут первыми: так не приходится
    листать пустые варианты, а внутри групп порядок алфавитный.
    """
    out = {"m": [], "b": [], "p": []}
    for a in getattr(res, "attributes", None) or []:
        kind = type(a).__name__
        if kind == "StarGiftAttributeModel":
            out["m"].append((a.name, a.document.id))
        elif kind == "StarGiftAttributePattern":
            out["p"].append((a.name, a.document.id))
        elif kind == "StarGiftAttributeBackdrop" and getattr(a, "backdrop_id", None) is not None:
            out["b"].append((a.name, a.backdrop_id))
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
    has_counts = bool(counts)
    for kind, options in out.items():
        options.sort(key=lambda o, kind=kind: (has_counts and counts.get((kind, o[1]), 0) <= 0, o[0].lower()))
    out["n"] = counts
    return out


# ── маркет ──────────────────────────────────────────────────

_RESALE_PARAMS = inspect.signature(GetResaleStarGiftsRequest.__init__).parameters
HAS_ATTR_HASH = "attributes_hash" in _RESALE_PARAMS
HAS_STARS_ONLY = "stars_only" in _RESALE_PARAMS


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
    """Десять самых дешёвых лотов в валюте ленты, от дешёвого к дорогому."""
    unit = feed.get("unit", DEFAULT_UNIT)
    kw = dict(gift_id=feed["gift"], offset="", limit=POLL_FETCH, sort_by_price=True)
    if HAS_STARS_ONLY and unit == STARS:
        kw["stars_only"] = True
    if feed["filt"]:
        kw["attributes"] = attr_ids(feed["filt"])
    res = await tele(GetResaleStarGiftsRequest(**kw))
    lots = [lot for lot in (Lot.of(g, unit) for g in res.gifts if hasattr(g, "slug")) if lot.price is not None]
    return sorted(lots, key=lambda lot: lot.price)[:10]


# ── сохранение состояния ────────────────────────────────────

def save_state():
    tmp = f"{WATCH_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"watches": watches, "feeds": feeds, "pins": pins, "users": users, "settings": settings},
                      f, ensure_ascii=False)
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
    users.update({int(uid): u for uid, u in data.get("users", {}).items()})
    settings.update(data.get("settings", {}))
    for feed in feeds.values():
        for row in feed.get("hist", []):
            row[4] = LEGACY_UNITS.get(row[4], row[4])
    for w in watches:
        w.setdefault("profit", False)
        if "unit" not in w:
            w["unit"] = STARS
            w["key"] += f"|{STARS}"
    migrated = {}
    for key, feed in feeds.items():
        if "unit" not in feed:
            feed["unit"], feed["seen"] = STARS, {}
            key = feed["key"] = f"{key}|{STARS}"
        migrated[key] = feed
    feeds.clear()
    feeds.update(migrated)


def fkey(gift, filt, unit=DEFAULT_UNIT):
    return f"{gift}|" + ",".join(f"{k}{value[1]}" for k, value in sorted(filt.items())) + f"|{unit}"


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



def last_row(w):
    """Последний замер флора в валюте этого слежения."""
    feed = feeds.get(w["key"])
    unit = w.get("unit", DEFAULT_UNIT)
    return next((x for x in reversed(feed["hist"]) if x[4] == unit), None) if feed else None


def floor_ref(w):
    """Ссылка на самый дешёвый лот"""
    feed = feeds.get(w["key"])
    return (feed["floor_slug"], feed["floor_name"]) if feed and feed.get("floor_slug") else None


def buy_row(w, label="🛒 Купить флор"):
    ref = floor_ref(w)
    return [{"text": label, "url": f"https://t.me/nft/{ref[0]}"}] if ref else []


def alert_kb(w, lot=None, label="🛒 Купить"):
    top = [{"text": label, "url": f"https://t.me/nft/{lot.slug}"}] if lot else []
    top.append(btn("📊 Статистика", f"ws:{w['id']}"))
    return kb_of(top, [btn("📈 График", f"wg:{w['id']}"), btn("⚙️ Настройки", f"wo:{w['id']}")])


def footer(w):
    return f"👁 {esc(w['title'])} · {esc(fsum(w))} · {UNIT_NAME[w.get('unit', DEFAULT_UNIT)]}"


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

        lines = ["🔥 <b>Выгодный лот</b>", RULE,
                 f"{link(lot.slug, f'{lot.title} #{lot.num}')} — <b>{money(price, unit)}</b>"]
        if lot.model or lot.backdrop or lot.pattern:
            bits = [f"{ICON[k]} {esc(x)}" for k, x in (("m", lot.model), ("b", lot.backdrop), ("p", lot.pattern)) if x]
            lines.append("└ " + " · ".join(bits))
        lines.append("")
        if new_floor:
            lines.append(f"🆕 Новый флор, прежний был {money(prev_floor, unit)}")
        if wide_gap:
            lines.append(f"📏 Следующий лот дороже на {money(gap, unit)}")
        if deal:
            lines.append(f"📊 На {disc:.0f}% ниже рынка (медиана остальных {money(ref, unit)})")
        if hit_target:
            lines.append(f"🎯 В рамках цели {money(w['target'], unit)}")
        if old:
            lines.append(f"⬇️ Подешевел с {money(old, unit)}")
        if ref and profit > 0:
            lines += ["", "💰 <b>Если перепродать</b>",
                      f"По {money(ref, unit)} → на руки {money(sale, unit)} (комиссия {FEE_PCT}%)",
                      f"Прибыль: <b>{signed(profit)} {SIGN.get(unit, unit)}</b> ({profit / price * 100:+.0f}%)",
                      f"⚖️ Безубыток от {money(breakeven(price, unit), unit)}"]
        elif ref:
            lines += ["", f"⚠️ По {money(ref, unit)} перепродавать невыгодно: после комиссии {FEE_PCT}% "
                          f"останется {money(sale, unit)}"]
        lines += ["", footer(w)]
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
                                     f"{link(lots[0].slug, money(floor, unit))}\n"
                                     f"На {drop:.1f}% ниже медианы за сутки ({money(m, unit)})\n\n{footer(w)}",
                          alert_kb(w, lots[0], "🎁 Открыть флор"))
            elif drop < w["drop"] / 2:
                w["last_drop"] = None
            rise_at = w.get("rise", DEF["rise"])
            if rise_at:
                rise = -drop
                last_up = w.get("last_rise")
                if rise >= rise_at and (not last_up or now - last_up[0] > 1800 or floor > last_up[1] * 1.02):
                    w["last_rise"] = [now, floor]
                    second = f"\nВторой лот: {num(lots[1].price)}" if len(lots) > 1 else ""
                    await say(w["chat"], f"🚀 <b>Флор вырос</b>\n{RULE}\n"
                                         f"{link(lots[0].slug, money(floor, unit))}\n"
                                         f"На {rise:.1f}% выше медианы за сутки ({money(m, unit)}){second}\n\n{footer(w)}",
                              alert_kb(w, lots[0], "🎁 Открыть флор"))
                elif rise < rise_at / 2:
                    w["last_rise"] = None
        if w["day1"] and now - w["start"] >= 86400:
            first_day = [x[1] for x in hist if x[4] == unit and w["start"] <= x[0] <= w["start"] + 86400]
            if first_day and floor < min(first_day):
                if w["notified"] is None or floor < w["notified"]:
                    w["notified"] = floor
                    if not dropped_now:  # два сообщения подряд про одно и то же не нужны
                        await say(w["chat"], f"📉 <b>Флор ниже, чем в первый день</b>\n{RULE}\n"
                                             f"Сейчас {link(lots[0].slug, money(floor, unit))}\n"
                                             f"В первый день было {money(min(first_day), unit)}\n\n{footer(w)}",
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
    feed["floor_slug"], feed["floor_name"] = lots[0].slug, f"{lots[0].title} #{lots[0].num}"
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
    feed, last = feeds.get(w["key"]), last_row(w)
    if not feed or not last:
        return "📊 Замеров пока нет, загляни через пару минут."
    now = time.time()
    unit, cur, second = last[4], last[1], last[2]
    sign = SIGN.get(unit, unit)
    pts = [x for x in feed["hist"] if x[4] == unit and x[0] >= now - 7 * 86400]
    floors = [x[1] for x in pts]

    now_line = f"💰 Флор: <b>{money(cur, unit)}</b>"
    if second and cur:
        now_line += f" · второй {num(second)} ({(second / cur - 1) * 100:+.1f}%)"
    out = [f"📊 <b>{esc(w['title'])}</b>", f"{esc(fsum(w))} · цена в {UNIT_NAME[unit]}", RULE, now_line]
    ref = floor_ref(w)
    if ref:
        out.append(f"🛒 Купить: {link(ref[0], ref[1])}")

    day = [x[1] for x in pts if x[0] >= now - 86400]
    if len(day) >= 5:
        m = med(day)
        out += ["", "📈 <b>За 24 часа</b>", f"<code>{spark(day)}</code>",
                f"Медиана {num(m)} · мин {num(min(day))} · макс {num(max(day))}",
                f"Сейчас {(cur / m - 1) * 100:+.1f}% к медиане",
                f"💸 Купить по флору и продать по медиане: {signed(after_fee(m, unit) - cur)} {sign} "
                f"чистыми (комиссия {FEE_PCT}%)"]
    if len(floors) >= 20:
        higher = sum(1 for x in floors if x > cur) / len(floors)
        verdict = "🟢 зона покупки" if higher >= .9 else "🔴 дорого" if higher <= .1 else "⚪ нейтрально"
        out += ["", f"🎯 <b>Сигнал:</b> {verdict}", f"За неделю флор был выше текущего в {higher * 100:.0f}% замеров"]

    span = pts[-1][0] - pts[0][0] if pts else 0
    if span >= 172800 and floors:
        mean, by_hour = sum(floors) / len(floors), {}
        for x in pts:
            by_hour.setdefault(local(x[0]).hour, []).append(x[1] / mean)
        avg = {h: sum(v) / len(v) for h, v in by_hour.items() if len(v) >= 3}
        if len(avg) >= 6:
            order = sorted(avg, key=avg.get)
            fmt = lambda hours: ", ".join(f"{h:02d}:00 ({(avg[h] - 1) * 100:+.1f}%)" for h in hours)
            out += ["", f"🕐 <b>Время суток</b> ({tz_label()})",
                    f"Дешевле всего: {fmt(order[:3])}",
                    f"Дороже всего: {fmt(order[-3:])}"]
    else:
        out += ["", f"🕐 Почасовой паттерн: накоплено {span / 3600:.0f} ч из 48"]

    first_day = [x[1] for x in feed["hist"] if x[4] == unit and w["start"] <= x[0] <= w["start"] + 86400]
    if first_day:
        out += ["", f"📅 Флор первого дня: {num(min(first_day))} (сейчас {(cur / min(first_day) - 1) * 100:+.1f}%)"]

    days = {}
    for x in pts:
        days.setdefault(local(x[0]).strftime("%d.%m"), []).append(x[1])
    avg_of = lambda v: round(sum(v) / len(v)) if unit == STARS else sum(v) / len(v)
    rows = [f"{d}: мин {num(min(v))} · средняя {num(avg_of(v))}" for d, v in list(days.items())[-7:]]
    out += ["", "🗓 <b>По дням</b>", "<blockquote expandable>" + "\n".join(rows) + "</blockquote>"]
    return "\n".join(out)


# ── график флора ────────────────────────────────────────────

PERIODS = {"d": ("24 часа", 86400, "24 ч"), "w": ("7 дней", 7 * 86400, "7 дн"), "a": ("всё время", None, "Всё")}
UNIT_NAME = {STARS: "Stars", TON: "TON"}
THEME = {"bg": "#17212b", "panel": "#1e2c3a", "grid": "#2f4153", "text": "#eef3f8", "muted": "#8fa3b7",
         "line": "#4fc3f7", "median": "#b39ddb", "low": "#66bb6a", "high": "#ef5350", "now": "#ffd54f"}
chart_lock = None  # рисуем по одному графику за раз, чтобы не раздувать память



def render_chart(title, sub, rows, unit, label):
    """Рисует график флора в PNG"""
    from matplotlib import dates as mdates, ticker
    from matplotlib.figure import Figure

    T, nan = THEME, float("nan")
    gap = max(1800, POLL_EVERY * 4)
    xs, ys, prev = [], [], None
    for ts, floor, *_ in rows:
        if prev is not None and ts - prev > gap:
            xs.append(local(prev + 1))
            ys.append(nan)
        xs.append(local(ts))
        ys.append(floor)
        prev = ts
    real = [y for y in ys if y == y]
    lo, hi, mid, last, first = min(real), max(real), med(real), real[-1], real[0]
    change = (last / first - 1) * 100 if first else 0
    pad = (hi - lo) * 0.15 or max(hi * 0.04, 1)
    bottom, top = lo - pad, hi + pad

    fig = Figure(figsize=(9, 5.6), dpi=130, facecolor=T["bg"])
    ax = fig.add_axes([0.115, 0.1, 0.72, 0.58], facecolor=T["bg"])
    ax.set_ylim(bottom, top)
    ax.margins(x=0.02)

    ax.fill_between(xs, ys, bottom, step="post", color=T["line"], alpha=0.16, linewidth=0)
    ax.step(xs, ys, where="post", color=T["line"], linewidth=5, alpha=0.12, solid_joinstyle="round")
    ax.step(xs, ys, where="post", color=T["line"], linewidth=2, solid_joinstyle="round")

    ax.axhline(mid, color=T["median"], linewidth=1, linestyle=(0, (4, 4)), alpha=0.8)
    ax.annotate("медиана", xy=(0, mid), xycoords=("axes fraction", "data"), xytext=(4, 4),
                textcoords="offset points", ha="left", va="bottom", color=T["median"], fontsize=8.5)

    if hi > lo:
        for value, color, shift, va in ((lo, T["low"], (0, -9), "top"), (hi, T["high"], (0, 9), "bottom")):
            i = ys.index(value)
            if i != len(ys) - 1 and value != last:
                ax.scatter([xs[i]], [value], s=30, color=color, zorder=5, edgecolors=T["bg"], linewidths=1.2)
                ax.annotate(num(value), (xs[i], value), xytext=shift, textcoords="offset points",
                            ha="center", va=va, color=color, fontsize=9, fontweight="bold")
    ax.scatter([xs[-1]], [last], s=70, color=T["now"], zorder=6, edgecolors=T["bg"], linewidths=2)

    ax.annotate(num(last), xy=(1, last), xycoords=("axes fraction", "data"), xytext=(8, 0),
                textcoords="offset points", ha="left", va="center", color="#1b1b1b", fontsize=10.5,
                fontweight="bold", annotation_clip=False, zorder=7,
                bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.5", fc=T["now"], ec="none"))

    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", color=T["grid"], linewidth=0.7, alpha=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=T["muted"], labelsize=9, length=0, pad=6)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(4))
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: num(v)))
    ax.yaxis.tick_left()
    ax.set_yticks([t for t in ax.get_yticks() if bottom <= t <= top])
    span = rows[-1][0] - rows[0][0]
    fmt = "%H:%M" if span <= 36 * 3600 else "%d.%m %H:%M" if span <= 3 * 86400 else "%d.%m"
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=LOCAL_TZ, minticks=3, maxticks=6))
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=LOCAL_TZ))

    fig.text(0.045, 0.945, short(title, 30), color=T["text"], fontsize=19, fontweight="bold", ha="left", va="top")
    fig.text(0.045, 0.885, short(sub, 70), color=T["muted"], fontsize=10, ha="left", va="top")
    fig.text(0.955, 0.945, label, color="#1b1b1b", fontsize=10, fontweight="bold", ha="right", va="top",
             bbox=dict(boxstyle="round,pad=0.4,rounding_size=0.6", fc=T["now"], ec="none"))

    arrow = "▲" if change > 0 else "▼" if change < 0 else "●"
    tint = T["low"] if change > 0 else T["high"] if change < 0 else T["muted"]
    cards = [("СЕЙЧАС", f"{num(last)} {UNIT_NAME.get(unit, unit)}", T["now"]),
             ("ЗА ПЕРИОД", f"{arrow} {abs(change):.1f}%", tint),
             ("МИНИМУМ", num(lo), T["low"]), ("МАКСИМУМ", num(hi), T["high"]), ("МЕДИАНА", num(mid), T["median"])]
    for n, (name, value, color) in enumerate(cards):
        x = 0.045 + n * 0.19
        fig.text(x, 0.815, name, color=T["muted"], fontsize=7.5, ha="left", va="top")
        fig.text(x, 0.785, value, color=color, fontsize=13, fontweight="bold", ha="left", va="top")

    fig.text(0.955, 0.025, stamp(time.time()), color=T["muted"], fontsize=8, ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    return buf.getvalue()


def old_render_chart(title, sub, rows, unit, label):
    """Рисует график флора в PNG. Matplotlib подключаем здесь, чтобы он не занимал память,
    пока графиков никто не просит."""
    from matplotlib import dates as mdates, ticker
    from matplotlib.figure import Figure

    T, nan = THEME, float("nan")
    gap = max(1800, POLL_EVERY * 4)  # бот молчал дольше: линию рвём, а не тянем через пропуск
    xs, ys, prev = [], [], None
    for ts, floor, *_ in rows:
        if prev is not None and ts - prev > gap:
            xs.append(local(prev + 1))
            ys.append(nan)
        xs.append(local(ts))
        ys.append(floor)
        prev = ts
    real = [y for y in ys if y == y]
    lo, hi, mid, last = min(real), max(real), med(real), real[-1]
    pad = (hi - lo) * 0.18 or max(hi * 0.04, 1)
    bottom, top = lo - pad, hi + pad * 1.6  # запас сверху: туда ложится подпись текущей цены

    fig = Figure(figsize=(9, 5), dpi=130, facecolor=T["bg"])
    ax = fig.add_subplot(111, facecolor=T["panel"])
    ax.set_ylim(bottom, top)
    ax.margins(x=0.03)
    ax.fill_between(xs, ys, bottom, step="post", color=T["line"], alpha=0.14, linewidth=0)
    ax.step(xs, ys, where="post", color=T["line"], linewidth=2.2, solid_joinstyle="round")
    ax.axhline(mid, color=T["median"], linewidth=1.2, linestyle=(0, (5, 4)), alpha=0.9)
    ax.annotate(f"медиана {num(mid)}", xy=(1, mid), xycoords=("axes fraction", "data"), xytext=(-6, 5),
                textcoords="offset points", ha="right", va="bottom", color=T["median"], fontsize=9)
    if hi > lo:  # минимум и максимум периода, если это не сама текущая точка
        for value, color, shift in ((lo, T["low"], (0, -17)), (hi, T["high"], (0, 9))):
            i = ys.index(value)
            if i != len(ys) - 1:
                ax.scatter([xs[i]], [value], s=34, color=color, zorder=5)
                ax.annotate(num(value), (xs[i], value), xytext=shift, textcoords="offset points",
                            ha="center", color=color, fontsize=9)
    ax.scatter([xs[-1]], [last], s=75, color=T["now"], zorder=6, edgecolors=T["panel"], linewidths=1.5)
    ax.annotate(num(last), (xs[-1], last), xytext=(-9, 12), textcoords="offset points", ha="right",
                color=T["now"], fontsize=12, fontweight="bold")

    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(T["grid"])
    ax.grid(axis="y", color=T["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=T["muted"], labelsize=9, length=0)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(5))
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: num(v)))
    span = rows[-1][0] - rows[0][0]
    fmt = "%H:%M" if span <= 36 * 3600 else "%d.%m %H:%M" if span <= 3 * 86400 else "%d.%m"
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=LOCAL_TZ, minticks=3, maxticks=6))
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=LOCAL_TZ))

    fig.text(0.05, 0.94, short(title, 32), color=T["text"], fontsize=17, fontweight="bold", ha="left", va="top")
    fig.text(0.05, 0.875, short(sub, 70), color=T["muted"], fontsize=10.5, ha="left", va="top")
    fig.text(0.96, 0.94, label, color=T["now"], fontsize=11, ha="right", va="top")
    fig.text(0.96, 0.025, f"{stamp(time.time())}", color=T["muted"], fontsize=8, ha="right")
    fig.subplots_adjust(left=0.1, right=0.965, top=0.8, bottom=0.12)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    return buf.getvalue()


def chart_kb(w, period):
    row = [btn(("● " if p == period else "") + PERIODS[p][2], f"wv:{w['id']}:{p}") for p in PERIODS]
    return kb_of(buy_row(w), row, [btn("📊 Статистика", f"ws:{w['id']}"), btn("⚙️ Настройки", f"wo:{w['id']}")])


def old_chart_kb(w, period):
    row = [btn(("● " if p == period else "") + PERIODS[p][2], f"wv:{w['id']}:{p}") for p in PERIODS]
    return kb_of(row, [btn("📊 Статистика", f"ws:{w['id']}"), btn("⚙️ Настройки", f"wo:{w['id']}")])


async def chart_view(w, period=None):
    """Photo с графиком либо текст, если рисовать нечего. Без period сам берёт самый короткий
    период, на котором уже хватает замеров."""
    feed = feeds.get(w["key"])
    if not feed or not feed["hist"]:
        return "📈 Замеров пока нет, загляни через пару минут."
    now = time.time()
    unit = feed["hist"][-1][4]

    def pick(p):
        span = PERIODS[p][1]
        return [x for x in feed["hist"] if x[4] == unit and (span is None or x[0] >= now - span)]

    if period is None:
        period = next((p for p in PERIODS if len(pick(p)) >= 3), "a")
    rows = pick(period)
    if len(rows) < 3:
        return "📈 Для графика пока мало замеров. Загляни чуть позже или выбери другой период."
    floors = [x[1] for x in rows]
    cur, first = floors[-1], floors[0]
    plain = " · ".join(f"{KN[k]}: {w['filt'][k][0]}" for k in "mbp" if k in w["filt"]) or "без фильтров"
    caption = "\n".join([
        f"📈 <b>{esc(w['title'])}</b> · {PERIODS[period][0]}", f"🔎 {esc(fsum(w))}", RULE,
        f"🏷 Сейчас: <b>{money(cur, unit)}</b> ({(cur / first - 1) * 100:+.1f}% за период)",
        f"⬇️ Мин {num(min(floors))} · ⬆️ Макс {num(max(floors))} · медиана {num(med(floors))}"])
    global chart_lock
    if chart_lock is None:
        chart_lock = asyncio.Lock()
    try:
        async with chart_lock:
            png = await asyncio.to_thread(render_chart, w["title"], f"{plain} · цена в {UNIT_NAME.get(unit, unit)}",
                                          rows, unit, PERIODS[period][0])
    except ImportError:
        return "📈 График недоступен: на сервере нет matplotlib. Добавь его в requirements.txt."
    return Photo(png, caption, chart_kb(w, period))


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
    unit = DEFAULT_UNIT
    key = fkey(v.gift, v.filt, unit)
    mine = [w for w in watches if w["chat"] == chat]
    existing = next((w for w in mine if w["key"] == key), None)
    if existing:
        return ("👀 <b>Ты уже следишь за этим</b>\n" + RULE + f"\n{esc(existing['title'])}\n{esc(fsum(existing))}",
                kb_of([btn("⚙️ Открыть слежение", f"wm:{existing['id']}")]))
    lim = limit_of(chat)
    if lim is not None and len(mine) >= lim:
        return (f"📭 <b>Лимит достигнут</b>\n{RULE}\n"
                f"Можно следить только за {lim} {gifts_tw(lim)}. "
                "Удали текущее слежение, чтобы выбрать другой подарок.",
                kb_of([btn("🔔 Мои слежения", "wl")]))
    w = {"id": uuid.uuid4().hex[:8], "chat": chat, "key": key, "gift": v.gift, "title": v.title,
         "filt": {k: list(x) for k, x in v.filt.items()}, "unit": unit, "start": time.time(), "deal": DEF["deal"],
         "drop": DEF["drop"], "gap": DEF["gap"], "target": None, "day1": True, "newfloor": True,
         "profit": False, "notified": None, "last_drop": None, "alerted": {}}
    watches.append(w)
    feed = feeds.setdefault(key, {"key": key, "gift": v.gift, "filt": w["filt"], "unit": unit, "hist": [], "seen": {}})
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
    text = (f"✅ <b>Слежу за {esc(w['title'])}</b>\n🔎 {esc(fsum(w))}\n{RULE}\n"
            f"🏷 Флор сейчас: <b>{floor}</b>\n"
            f"💱 Смотрю только лоты в {UNIT_NAME[unit]}, валюту можно сменить в настройках\n\n"
            f"Проверяю рынок каждые {POLL_EVERY} сек и напишу, когда:\n"
            f"🆕 появится лот ниже текущего флора\n"
            f"📏 лот будет дешевле следующего на {w['gap']}+ ⭐\n"
            f"🔥 лот будет ниже рынка на {w['deal']}%+\n"
            f"📉 флор упадёт на {w['drop']}%+ от медианы за сутки")
    return text, kb_of(buy_row(w), [btn("⚙️ Настроить пороги", f"wo:{w['id']}")], [btn("🏠 Меню", "home")])


def watch_menu(w):
    feed = feeds.get(w["key"])
    cur = feed["hist"][-1] if feed and feed["hist"] else None
    mark = lambda on: "✅" if on else "❌"
    wid = w["id"]
    gap = w.get("gap", DEF["gap"])
    target = f"≤ {num(w['target'])}" if w["target"] is not None else "нет"
    rows = [
        [btn(f"🔥 Выгода {w['deal']}%+", f"wc:{wid}:deal"), btn(f"📉 Падение {w['drop']}%+", f"wc:{wid}:drop")],
        [btn(f"📏 Разрыв {gap}+ ⭐", f"wc:{wid}:gap"), btn(f"🎯 Цель: {target}", f"wt:{wid}")],
        [btn(f"🆕 Новый флор {mark(w.get('newfloor', True))}", f"wn:{wid}"),
         btn(f"💰 Прибыль {mark(w.get('profit', False))}", f"wp:{wid}")],
        [btn(f"💱 Цены в: {UNIT_BTN[unit]}", f"wu:{wid}"), btn(f"📅 С 1-м днём {mark(w['day1'])}", f"w1:{wid}")],
        buy_row(w),
        [btn("📊 Статистика", f"ws:{wid}"), btn("📈 График", f"wg:{wid}")],
        [btn("🗑 Удалить", f"wd:{wid}"), btn("⬅️ Мои слежения", "wl")],
    ]
    if TESTER_ID and w["chat"] == TESTER_ID:
        rows.insert(-1, [btn("💰 Автокупка", f"ap:{wid}"), btn("💵 Баланс", f"ab:{wid}")])
    now = f"🏷 Флор сейчас: <b>{money(cur[1], cur[4])}</b>" if cur else "🏷 Замеров пока нет"
    text = (f"🔔 <b>{esc(w['title'])}</b>\n🔎 {esc(fsum(w))}\n{RULE}\n{now}\n"
            f"🕐 Слежу с {stamp(w['start'])}\n\n"
            "<b>Что значат кнопки</b>\n"
            "🔥 Выгода: лот ниже рынка на столько %\n"
            "📉 Падение: флор ниже медианы за сутки\n"
            "📏 Разрыв: лот дешевле следующего на столько ⭐\n"
            "🎯 Цель: лот не дороже твоей цены\n"
            "🆕 Новый флор: лот ниже прежнего флора\n"
            "💰 Прибыль: писать, только если после комиссии есть плюс\n"
            "📅 1-й день: флор ниже, чем в день старта\n\n"
            "Пороги 🔥 📉 📏 переключаются по кругу.")
    return text, kb_of(*rows)


def watch_list(chat):
    mine = [w for w in watches if w["chat"] == chat]
    if not mine:
        return ("🔔 <b>Мои слежения</b>\n" + RULE + "\nПока пусто.\n\n"
                "Открой коллекцию и нажми «🔔 Следить», я напишу, когда появится выгодный лот.\n"
                f"Доступно слежений: <b>{limit_label(chat)}</b>"), \
            kb_of([btn("🎁 Выбрать коллекцию", "home")])
    lines = [f"🔔 <b>Мои слежения</b> · {len(mine)}/{limit_label(chat)}", RULE]
    for n, w in enumerate(mine, 1):
        feed = feeds.get(w["key"])
        cur = feed["hist"][-1] if feed and feed["hist"] else None
        lines.append(f"<b>{n}. {esc(w['title'])}</b>" + (f" · флор {money(cur[1], cur[4])}" if cur else ""))
        lines.append(f"└ {esc(fsum(w))}")
    lines += ["", "Нажми на слежение, чтобы настроить."]
    rows = [[btn(f"{n}. {short(w['title'], 20)} · {short(fsum(w), 20)}", f"wm:{w['id']}")]
            for n, w in enumerate(mine, 1)]
    return "\n".join(lines), kb_of(*rows, [btn("➕ Добавить", "home")])


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
    """Карточка лота: сверху название и цена, ниже модель, фон и узор. В конце пустая строка."""
    head = f"<b>{i}.</b> {link(lot.slug, f'{lot.title} #{lot.num}')}"
    if lot.price is not None:
        head += f" — <b>{money(lot.price, lot.unit)}</b>"
    out = [head]
    bits = [f"{ICON[k]} {esc(x)}" for k, x in (("m", lot.model), ("b", lot.backdrop), ("p", lot.pattern)) if x]
    if bits:
        out.append("└ " + " · ".join(bits))
    out.append("")
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
    lines = [f"👤 <b>{esc(v.name)}</b>", RULE,
             f"🎁 Коллекционных: <b>{len(v.items)}</b>" + (f" · обычных: {regular}" if regular > 0 else "")]
    if len({lot.title for lot in v.items}) > 1:
        top = Counter(lot.title for lot in v.items).most_common(3)
        lines.append("🏆 Чаще всего: " + " · ".join(f"{esc(t)} ×{c}" for t, c in top))
    lines.append("")
    for n, lot in enumerate(v.items[idx * PAGE:(idx + 1) * PAGE], idx * PAGE + 1):
        lines += lot_card(n, lot)
    if not v.items:
        lines += ["🙈 Коллекционных подарков нет или профиль скрыт."]
    nav = pager(lambda i: f"up:{v.id}:{i}", idx, pages)
    return fit(lines), kb_of(nav, [btn("🔄 Обновить", f"ur:{v.id}"), btn("👤 Другой ник", "u")],
                             [btn("🏠 Меню", "home")])


USERNAME = re.compile(r"(?:https?://)?(?:t\.me/)?@?([A-Za-z0-9_]{3,32})")


def is_user_ref(text):
    return text.startswith("@") or "t.me/" in text


async def open_user(chat, raw):
    if GetSavedStarGiftsRequest is None:
        return await say(chat, "Для этого нужен свежий Telethon: <code>pip install -U telethon</code>")
    token = (raw.split() or [""])[0].rstrip("/")
    match = USERNAME.fullmatch(token)
    if not match:
        return await say(chat, "🤔 Не похоже на ник. Пришли в виде <code>@durov</code>",
                         kb_of([btn("❌ Отмена", "home")]))
    wait = take_profile_slot(chat)
    if wait:
        return await say(chat, f"⏳ Подожди {wait} сек, прежде чем открывать следующий профиль.")
    name = match.group(1)
    peer = peers.get(name.lower())
    if peer is None:
        try:
            peer = await tele.get_input_entity(name)
        except FloodWaitError:
            raise
        except Exception:
            return await say(chat, f"🔍 Не нашёл @{esc(name)}. Проверь ник и пришли ещё раз.",
                             kb_of([btn("🏠 Меню", "home")]))
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
        return await show("🙈 Telegram не отдал подарки этого профиля. Возможно, они скрыты.",
                          kb_of([btn("🏠 Меню", "home")]))
    text, kb = profile_page(remember(Profile(peer=peer, name=f"@{name}", items=items, total=total)), 0)
    await show(text, kb)


# ── каталог ─────────────────────────────────────────────────

SN = {"price": "по цене", "num": "по номеру", "new": "сначала новые"}
SORT_ICON = {"price": "💰", "num": "🔢", "new": "🆕"}
SORT_BTN = {"price": "Цена", "num": "Номер", "new": "Новые"}


def collection_rows(cols):
    buttons = [btn(short(title, 22), f"c:{gid}") for gid, title in cols]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


async def home_view(chat, page=0, note=""):
    cols = await collections()
    if not cols:
        return (f"{note}🎁 <b>Маркет подарков</b>\n{RULE}\nСейчас на маркете нет лотов, загляни чуть позже.",
                kb_of([btn("🔔 Слежения", "wl"), btn("👤 По нику", "u")]))
    titles = dict(cols)
    pinned = [gid for gid in pins.get(chat, []) if gid in titles]
    rest = [c for c in cols if c[0] not in pinned]
    pages = max(1, math.ceil(len(rest) / COLS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows = collection_rows([(gid, f"📌 {titles[gid]}") for gid in pinned]) if page == 0 else []
    rows += collection_rows(rest[page * COLS_PER_PAGE:(page + 1) * COLS_PER_PAGE])
    nav = pager(lambda i: f"home:{i}", page, pages)
    if nav:
        rows.append(nav)
    rows.append([btn("🔔 Слежения", "wl"), btn("👤 Подарки по нику", "u")])
    if is_owner(chat):
        rows.append([btn("🛠 Админ-панель", "ad")])
    stat = f"📦 Коллекций: <b>{len(cols)}</b>" + (f" · 📌 закреплено: <b>{len(pinned)}</b>" if pinned else "")
    text = (f"{note}🎁 <b>Маркет подарков</b>\n{RULE}\n{stat}\n\n"
            "✍️ Напиши название коллекции, и я её найду.\n"
            "👤 Пришли @ник, чтобы увидеть подарки человека.")
    return text, kb_of(*rows)


async def render(v, idx):
    idx = max(0, min(idx, len(v.offsets) - 1))
    lots, nxt, count = await lots_page(v, idx)
    v.page = idx
    if nxt and len(v.offsets) == idx + 1:
        v.offsets.append(nxt)
    pages = max(1, math.ceil(count / PAGE))
    pinned = v.gift in pins.get(v.chat, [])
    lines = [f"🎁 <b>{esc(v.title)}</b>" + (" 📌" if pinned else ""), RULE]
    if v.sort == "price" and idx == 0 and lots and lots[0].price is not None:
        lines.append(f"🏷 Флор: <b>{money(lots[0].price, lots[0].unit)}</b>")
    lines.append(f"📦 Лотов: <b>{num(count)}</b> · {SORT_ICON[v.sort]} {SN[v.sort]}")
    lines.append(f"🔎 {esc(chips(v.filt))}" if v.filt else "🔎 Без фильтров")
    lines.append("")
    if lots:
        for n, lot in enumerate(lots, idx * PAGE + 1):
            lines += lot_card(n, lot)
    else:
        lines += ["😕 Ничего не нашлось.", "Выбери другое значение или сбрось фильтры."]

    nav = pager(lambda i: f"p:{v.id}:{i}", idx, pages, has_next=len(v.offsets) > idx + 1)
    chip_row = [btn(("✅ " if k in v.filt else "") + f"{ICON[k]} {KN[k]}", f"fl:{v.id}:{k}:0") for k in "mbp"]
    sort_row = [btn(("● " if v.sort == s else "") + f"{SORT_ICON[s]} {SORT_BTN[s]}", f"so:{v.id}:{s}") for s in SN]
    bottom = [btn("⬅️ Коллекции", "home")]
    if v.filt or v.sort != "price":
        bottom.insert(0, btn("♻️ Сбросить", f"fr:{v.id}"))
    return fit(lines), kb_of(
        nav, chip_row, sort_row,
        [btn("🔔 Следить", f"w:{v.id}"), btn("📌 Открепить" if pinned else "📌 Закрепить", f"pin:{v.id}")],
        bottom)


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
    options, counts = v.attrs.get(kind, []), v.attrs.get("n", {})
    pages = max(1, math.ceil(len(options) / CHOICES_PER_PAGE))
    pg = max(0, min(pg, pages - 1))
    start = pg * CHOICES_PER_PAGE
    rows = option_rows(v, kind, range(start, min(start + CHOICES_PER_PAGE, len(options))))
    nav = pager(lambda i: f"fl:{v.id}:{kind}:{i}", pg, pages)
    tail = [btn("⬅️ Назад", f"p:{v.id}:{v.page}")]
    if kind in v.filt:
        tail.insert(0, btn("✕ Убрать фильтр", f"fs:{v.id}:{kind}:-1"))

    now = v.filt[kind][0] if kind in v.filt else "любая"
    stat = f"Вариантов: <b>{len(options)}</b>"
    if counts:
        stat += f" · с лотами: <b>{sum(1 for _, ident in options if counts.get((kind, ident), 0) > 0)}</b>"
    tips = []
    if len(options) > 8:
        tips.append("✍️ Напиши название, чтобы найти быстро.")
    if counts:
        tips.append("🔢 Цифра рядом с названием — сколько лотов сейчас на маркете.")
    if not options:
        tips.append("Telegram не вернул варианты для этой коллекции.")
    elif not tips:
        tips.append("Нажми на нужное значение.")
    text = (f"{ICON[kind]} <b>{KN[kind]}</b> · {esc(v.title)}\n{RULE}\n"
            f"Сейчас: <b>{esc(now)}</b>\n{stat}\n\n" + "\n".join(tips))
    return text, kb_of(*rows, nav, tail)


# ── пользователи, лимиты, админ-панель ──────────────────────

LIMIT_STEPS = [1, 2, 3, 5, 10]
USERS_PER_PAGE = 8
BC_DELAY = 0.06  # пауза между сообщениями рассылки, Telegram разрешает ~30 в секунду
PROFILE_COOLDOWN = int(os.environ.get("PROFILE_COOLDOWN", 20))  # сек между загрузками профилей
last_profile = {}


def is_owner(uid):
    return uid == OWNER_ID


def gifts_tw(n):
    """«за 1 подарком», «за 2 подарками»."""
    return "подарком" if n % 10 == 1 and n % 100 != 11 else "подарками"


def limit_of(chat):
    """Сколько слежений можно держать. None — без ограничений (только владелец)."""
    if is_owner(chat):
        return None
    return users.get(chat, {}).get("limit") or settings["limit"]


def limit_label(chat):
    lim = limit_of(chat)
    return "∞" if lim is None else str(lim)


def watch_count(uid):
    return sum(1 for w in watches if w["chat"] == uid)


def user_label(uid):
    u = users.get(uid, {})
    return (u.get("name") or str(uid)) + (f" @{u['username']}" if u.get("username") else "")


def to_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def fmt_dur(sec):
    d, rest = divmod(int(sec), 86400)
    h, rest = divmod(rest, 3600)
    return (f"{d} д " if d else "") + (f"{h} ч " if h or d else "") + f"{rest // 60} мин"


def touch_user(uid, sender):
    """Заводит или обновляет запись о человеке при каждом его апдейте."""
    u = users.get(uid)
    fresh = u is None
    if fresh:
        u = users[uid] = {"first": time.time(), "banned": False, "limit": None}
    name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or str(uid)
    u.update(name=name, username=sender.get("username") or "", seen=time.time(), blocked=False)
    if fresh:
        save_state()
    return u


def take_profile_slot(chat):
    """Профиль тянет до 40 запросов через общую сессию, поэтому обычным пользователям
    разрешаем его не чаще раза в PROFILE_COOLDOWN секунд. Возвращает, сколько ещё ждать."""
    if is_owner(chat):
        return 0
    left = PROFILE_COOLDOWN - (time.time() - last_profile.get(chat, 0))
    if left > 0:
        return math.ceil(left)
    last_profile[chat] = time.time()
    return 0


async def gate(chat, sender, update):
    """Пускает дальше только тех, кому сейчас можно пользоваться ботом."""
    u = touch_user(chat, sender)
    if is_owner(chat):
        return True
    if u.get("banned"):
        note = "⛔ Доступ к боту закрыт."
    elif settings["maintenance"]:
        note = "🔧 Идут технические работы, загляни чуть позже."
    else:
        return True
    cq = update.get("callback_query")
    if cq:
        await api("answerCallbackQuery", callback_query_id=cq["id"], text=note, show_alert=True)
    else:
        await say(chat, note)
    return False


def admin_home():
    now = time.time()
    day = sum(1 for u in users.values() if now - u.get("seen", 0) < 86400)
    banned = sum(1 for u in users.values() if u.get("banned"))
    lim, maint = settings["limit"], settings["maintenance"]
    text = (f"🛠 <b>Админ-панель</b>\n{RULE}\n"
            f"👥 Пользователей: <b>{len(users)}</b> · за сутки: <b>{day}</b>" + (f" · ⛔ {banned}" if banned else "") + "\n"
            f"🔔 Слежений: <b>{len(watches)}</b> · лент маркета: <b>{len(feeds)}</b>\n"
            f"🎚 Лимит слежений: <b>{lim}</b> на человека\n"
            f"🔧 Техработы: <b>{'включены' if maint else 'выключены'}</b>\n\n"
            "У тебя, владельца, лимитов нет.")
    return text, kb_of(
        [btn("📊 Статистика", "ad:stats"), btn("👥 Пользователи", "ad:users:0")],
        [btn("📢 Рассылка", "ad:bc"), btn("🔎 Найти по ID", "ad:find")],
        [btn(f"🎚 Лимит: {lim}", "ad:glim"), btn("🔧 Техработы " + ("✅" if maint else "❌"), "ad:mt")],
        [btn("🏠 Меню", "home")])


def admin_stats():
    now = time.time()
    us = list(users.values())
    since = lambda key, sec: sum(1 for u in us if now - u.get(key, 0) < sec)
    tracked = Counter(w["title"] for w in watches).most_common(5)
    lines = ["📊 <b>Статистика бота</b>", RULE,
             f"👥 Всего: <b>{len(us)}</b>",
             f"🆕 Новых: {since('first', 86400)} за сутки · {since('first', 7 * 86400)} за неделю",
             f"🟢 Заходили: {since('seen', 86400)} за сутки · {since('seen', 7 * 86400)} за неделю",
             f"⛔ Забанено: {sum(1 for u in us if u.get('banned'))} · "
             f"🚫 заблокировали бота: {sum(1 for u in us if u.get('blocked'))}",
             "", f"🔔 Слежений: <b>{len(watches)}</b> у {len({w['chat'] for w in watches})} чел."]
    if tracked:
        lines.append("🔥 Чаще всего следят:")
        lines += [f"└ {esc(t)} × {c}" for t, c in tracked]
    lines += ["", f"⏱ Работаю: {fmt_dur(now - STARTED)}",
              f"🔄 Опрос маркета: каждые {POLL_EVERY} сек",
              f"🕐 Часовой пояс: {tz_label()}"]
    return "\n".join(lines), kb_of([btn("🛠 Панель", "ad")])


def admin_users(page):
    ordered = sorted(users.items(), key=lambda kv: -kv[1].get("seen", 0))
    pages = max(1, math.ceil(len(ordered) / USERS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows = [[btn(f"{'⛔ ' if u.get('banned') else ''}{short(user_label(uid), 24)} · 🔔{watch_count(uid)}",
                 f"ad:user:{uid}")]
            for uid, u in ordered[page * USERS_PER_PAGE:(page + 1) * USERS_PER_PAGE]]
    nav = pager(lambda i: f"ad:users:{i}", page, pages)
    text = (f"👥 <b>Пользователи</b> · {len(users)}\n{RULE}\n"
            "Сверху те, кто заходил недавно. Нажми на человека, чтобы открыть карточку.")
    return text, kb_of(*rows, nav, [btn("🔎 Найти", "ad:find"), btn("🛠 Панель", "ad")])


def admin_user(uid):
    u = users.get(uid)
    if not u:
        return "🔍 Такого пользователя нет в базе.", kb_of([btn("👥 Пользователи", "ad:users:0")])
    lim, own = limit_of(uid), u.get("limit")
    status = "👑 владелец" if is_owner(uid) else "⛔ забанен" if u.get("banned") else "✅ активен"
    if u.get("blocked"):
        status += " · 🚫 заблокировал бота"
    lines = [f"👤 <b>{esc(user_label(uid))}</b>", f"🆔 <code>{uid}</code>", RULE,
             f"Статус: {status}",
             f"📅 Первый визит: {stamp(u.get('first', 0))}",
             f"🕐 Был: {stamp(u.get('seen', 0))}",
             f"🔔 Слежений: <b>{watch_count(uid)}</b> из {'∞' if lim is None else lim}"
             + (" (личный лимит)" if own and not is_owner(uid) else "")]
    lines += [f"└ {esc(w['title'])} · {esc(fsum(w))}" for w in watches if w["chat"] == uid]
    rows = []
    if not is_owner(uid):
        banned = u.get("banned")
        rows += [[btn("✅ Разбанить" if banned else "⛔ Забанить", f"ad:{'unban' if banned else 'ban'}:{uid}"),
                  btn(f"🎚 Лимит: {own}" if own else "🎚 Лимит: общий", f"ad:lim:{uid}")],
                 [btn("✉️ Написать", f"ad:dm:{uid}"), btn("🧹 Снять слежения", f"ad:wipe:{uid}")]]
    rows.append([btn("👥 Пользователи", "ad:users:0"), btn("🛠 Панель", "ad")])
    return "\n".join(lines), kb_of(*rows)


def broadcast_targets(owner):
    return [uid for uid, u in users.items() if uid != owner and not u.get("banned") and not u.get("blocked")]


async def broadcast(owner, text):
    ok = fail = 0
    for uid in broadcast_targets(owner):
        r = await say(uid, f"📢 <b>Объявление</b>\n{RULE}\n{esc(text)}")
        retry = (r.get("parameters") or {}).get("retry_after")
        if not r.get("ok") and retry:  # Telegram просит притормозить: ждём и пробуем ещё раз
            await asyncio.sleep(retry + 1)
            r = await say(uid, f"📢 <b>Объявление</b>\n{RULE}\n{esc(text)}")
        if r.get("ok"):
            ok += 1
        else:
            fail += 1
            if r.get("error_code") == 403 and uid in users:
                users[uid]["blocked"] = True
        await asyncio.sleep(BC_DELAY)
    save_state()
    await say(owner, f"📢 <b>Рассылка завершена</b>\n{RULE}\n✅ Доставлено: <b>{ok}</b>\n❌ Не доставлено: <b>{fail}</b>",
              kb_of([btn("🛠 Панель", "ad")]))


def cancel_kb():
    return kb_of([btn("❌ Отмена", "ad")])


async def cb_admin(chat, a):
    if not is_owner(chat):
        return None  # чужим админ-кнопки не отвечают вообще
    act, arg = (a[0] if a else "home"), (a[1] if len(a) > 1 else "")
    uid = to_int(arg)
    if act == "stats":
        return admin_stats()
    if act == "users":
        return admin_users(uid or 0)
    if act == "user":
        return admin_user(uid)
    if act == "glim":
        cur = settings["limit"]
        settings["limit"] = LIMIT_STEPS[(LIMIT_STEPS.index(cur) + 1) % len(LIMIT_STEPS)] if cur in LIMIT_STEPS else LIMIT_STEPS[0]
        save_state()
        return admin_home()
    if act == "mt":
        settings["maintenance"] = not settings["maintenance"]
        save_state()
        return admin_home()
    if act in ("lim", "ban", "unban", "wipe"):
        u = users.get(uid)
        if u and not is_owner(uid):
            if act == "lim":
                steps = [None] + LIMIT_STEPS
                cur = u.get("limit")
                u["limit"] = steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else None
            elif act == "ban":
                u["banned"] = True
                watches[:] = [w for w in watches if w["chat"] != uid]  # забаненному слежения не нужны
            elif act == "unban":
                u["banned"] = False
            else:
                watches[:] = [w for w in watches if w["chat"] != uid]
            save_state()
        return admin_user(uid)
    if act == "find":
        waiting[chat] = ("admin", "find")
        return ("🔎 <b>Поиск пользователя</b>\n" + RULE + "\nПришли ID или @ник человека, который уже пользовался ботом.",
                cancel_kb())
    if act == "dm":
        if uid not in users:
            return admin_user(uid)
        waiting[chat] = ("admin", "dm", uid)
        return (f"✉️ <b>Сообщение для {esc(user_label(uid))}</b>\n{RULE}\nПришли текст, и я передам его от имени бота.",
                kb_of([btn("❌ Отмена", f"ad:user:{uid}")]))
    if act == "bc":
        waiting[chat] = ("admin", "bc")
        return (f"📢 <b>Рассылка</b>\n{RULE}\nПришли текст объявления. Получателей: <b>{len(broadcast_targets(chat))}</b>.\n"
                "Сначала покажу предпросмотр, отправлю только после подтверждения.", cancel_kb())
    if act == "bcgo":
        text = pending_bc.pop(chat, None)
        if not text:
            return admin_home()
        background(broadcast(chat, text))
        return (f"📢 <b>Рассылка запущена</b>\n{RULE}\nПришлю итог, когда закончу.", kb_of([btn("🛠 Панель", "ad")]))
    return admin_home()


async def admin_reply(chat, state, text):
    """Текст от владельца после кнопок «Найти», «Написать» и «Рассылка»."""
    if not is_owner(chat):
        waiting.pop(chat, None)
        return
    mode = state[1]
    if mode == "find":
        q = text.strip().lstrip("@").lower()
        uid = int(q) if q.isdigit() and int(q) in users else next(
            (i for i, u in users.items() if u.get("username", "").lower() == q), None)
        if uid is None:
            return await say(chat, f"🔍 Не нашёл «{esc(text)}». Пришли ID или @ник ещё раз.", cancel_kb())
        waiting.pop(chat, None)
        return await say(chat, *admin_user(uid))
    if mode == "dm":
        uid = state[2]
        waiting.pop(chat, None)
        r = await say(uid, f"✉️ <b>Сообщение от администратора</b>\n{RULE}\n{esc(text)}")
        await say(chat, "✅ Отправлено." if r.get("ok") else "❌ Не удалось отправить: возможно, человек заблокировал бота.")
        return await say(chat, *admin_user(uid))
    text = text.strip()[:3500]
    pending_bc[chat] = text
    waiting.pop(chat, None)
    n = len(broadcast_targets(chat))
    await say(chat, f"📢 <b>Предпросмотр рассылки</b>\n{RULE}\n{esc(text)}\n{RULE}\nПолучателей: <b>{n}</b>",
              kb_of([btn(f"✅ Отправить ({n})", "ad:bcgo")], [btn("✏️ Заново", "ad:bc"), btn("❌ Отмена", "ad")]))


# ── обработка апдейтов ──────────────────────────────────────

BTN_HOME, BTN_WATCH, BTN_USER = "🎁 Коллекции", "🔔 Слежения", "👤 Подарки по нику"
MAIN_KB = {"keyboard": [[{"text": BTN_HOME}, {"text": BTN_WATCH}], [{"text": BTN_USER}]],
           "resize_keyboard": True, "is_persistent": True,
           "input_field_placeholder": "Название коллекции или @ник"}
WELCOME = ("👋 <b>Привет!</b>\n"
           "Помогу найти выгодные Telegram-подарки на маркете.\n\n"
           "🎁 Лоты с фильтрами по модели, фону и узору\n"
           "🔔 Слежение за флором: напишу, когда появится выгодный лот\n"
           "👤 Подарки любого пользователя по @нику\n\n"
           "Меню всегда внизу 👇")
HELP = ("🎁 <b>Маркет подарков</b>\n" + RULE + "\n"
        "<b>Что умею</b>\n"
        "🎁 Каталог: лоты любой коллекции\n"
        "🔎 Фильтры: модель, фон, узор и сортировка прямо под списком\n"
        "🔔 Слежение: сам пишу о выгодных лотах и падении флора\n"
        "📈 График флора: в меню слежения, за 24 часа, 7 дней и всё время\n"
        "👤 Подарки по нику: все коллекционные подарки пользователя\n\n"
        "<b>Команды</b>\n"
        "/start — меню\n"
        "/watch — мои слежения\n"
        "/user @ник — подарки пользователя\n\n"
        "<b>Быстрые приёмы</b>\n"
        "• напиши название коллекции, и я её найду\n"
        "• пришли @ник, и покажу подарки\n"
        "• в списке моделей, фонов и узоров можно просто написать название\n"
        "• ✅ у фильтра значит, что он включён\n\n"
        f"💰 Комиссия маркета {FEE_PCT}% учитывается в прибыли.\n"
        f"🕐 Время показываю по поясу {tz_label()}.")
COMMANDS = [{"command": "start", "description": "Меню"},
            {"command": "watch", "description": "Мои слежения за ценами"},
            {"command": "user", "description": "Подарки пользователя"},
            {"command": "help", "description": "Как пользоваться"}]
STALE = "⌛ Это меню устарело, открой коллекцию заново.\n\n"


def get_view(args, kind):
    v = views.get(args[0]) if args else None
    return v if isinstance(v, kind) else None


async def show_home(chat, page=0, note=""):
    text, kb = await home_view(chat, page, note)
    await say(chat, text, kb)


async def ask_user(chat):
    waiting[chat] = "user"
    await say(chat, "👤 <b>Подарки по нику</b>\nПришли ник, например <code>@durov</code>",
              kb_of([btn("❌ Отмена", "home")]))


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
    wait = take_profile_slot(chat)
    if wait:
        return await say(chat, f"⏳ Подожди {wait} сек, прежде чем обновлять профиль.")
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
    return await render(v, 0)


async def cb_sort(chat, a):
    v = get_view(a, Catalog)
    if not v:
        return await home_view(chat, note=STALE)
    if a[1] in SN and a[1] != v.sort:
        v.sort = a[1]
        v.offsets = [""]
    return await render(v, 0)


async def cb_reset(chat, a):
    v = get_view(a, Catalog)
    if not v:
        return await home_view(chat, note=STALE)
    v.filt.clear()
    v.sort, v.offsets = "price", [""]
    return await render(v, 0)


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
        await say(chat, f"🎯 <b>Цель по цене</b> · {esc(w['title'])}\n{RULE}\n"
                        "Пришли число, и я напишу, когда появится лот не дороже.\n"
                        "<code>0</code> — выключить цель.",
                  kb_of([btn("❌ Отмена", f"wm:{w['id']}")]))


async def cb_stats(chat, a):
    w = own_watch(chat, a[0])
    if w:
        await say(chat, stats(w), kb_of([btn("📈 График", f"wg:{w['id']}"), btn("⚙️ Настройки", f"wo:{w['id']}"),
                                         btn("🏠 Меню", "home")]))


async def cb_chart(chat, a):
    """Открывает график новым сообщением: текстовое сообщение в фото не превратить."""
    w = own_watch(chat, a[0])
    if not w:
        return watch_list(chat)
    out = await chart_view(w)
    await (send_photo(chat, out) if isinstance(out, Photo) else say(chat, out))


async def cb_chart_period(chat, a):
    """Смена периода правит уже отправленную картинку."""
    w = own_watch(chat, a[0])
    if not w:
        return None
    out = await chart_view(w, a[1] if len(a) > 1 and a[1] in PERIODS else None)
    if isinstance(out, Photo):
        return out
    await say(chat, out)


async def cb_delete(chat, a):
    w = own_watch(chat, a[0])
    if not w:
        return watch_list(chat)
    return (f"🗑 <b>Удалить слежение?</b>\n{RULE}\n{esc(w['title'])}\n🔎 {esc(fsum(w))}\n\n"
            "Уведомления по нему прекратятся.",
            kb_of([btn("🗑 Да, удалить", f"wy:{w['id']}"), btn("↩️ Отмена", f"wm:{w['id']}")]))


async def cb_delete_yes(chat, a):
    w = own_watch(chat, a[0])
    if w:
        watches.remove(w)
        save_state()
    return watch_list(chat)




# ── АВТОКУПКА ───────────────────────────────────────────────────
async def cb_autologin(chat, a):
    """Начало логина в аккаунт для тестера"""
    if chat != TESTER_ID:
        return "❌ Только для тестеров.", {}
    
    text = ("""🔐 <b>Вход в аккаунт Star Gifts</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Зачем это нужно:</b>
• Автоматически покупать выгодные NFT
• Видеть баланс звёзд в реальном времени
• Управлять покупками из настроек

<b>⚠️ Безопасность:</b>
Мы <u>никогда</u> не передадим твои данные.
Пароль <u>не сохраняется</u>, только сессия.
Бот работает только по твоим командам.

Ты можешь выйти в любой момент.""")
    
    kb = kb_of([btn("📱 Поделиться контактом", "alc")])
    return text, kb


async def cb_autopay_setup(chat, a):
    """Настройка автокупки для слежения"""
    w = own_watch(chat, a[0])
    if not w:
        return watch_list(chat)
    if chat != TESTER_ID:
        return watch_menu(w)
    
    text = f"⚙️ <b>Автокупка</b>\n{esc(w['title'])}\n\nВыбери условие:"
    kb = kb_of(
        [btn("💵 Цена ≤ X ⭐", f"aps:{w['id']}:price"),
         btn("💰 Профит ≥ X%", f"aps:{w['id']}:profit")],
        [btn("❌ Выключить", f"apo:{w['id']}"), btn("⬅️ Назад", f"wm:{w['id']}")]
    )
    return text, kb


async def cb_show_balance(chat, a):
    """Показать баланс звёзд"""
    if chat != TESTER_ID:
        return "❌ Только для тестеров.", {}
    
    text = ("<b>💰 Баланс</b>\n"
            "━━━━━━━━━━━━━━━━━\n\n"
            "<b><code>25 500</code></b> ⭐\n\n"
            "Аккаунт: @username\n"
            "✅ Активен")
    kb = kb_of([btn("🔄 Обновить", "ab:ref"), btn("⬅️ Назад", "home")])
    return text, kb


CALLBACKS = {
    "noop": cb_noop, "home": cb_home, "wl": cb_watches, "u": cb_ask_user, "up": cb_user_page, "ur": cb_user_refresh,
    "c": cb_collection, "p": cb_page, "pin": cb_pin, "fl": cb_choices, "fs": cb_pick,
    "so": cb_sort, "fr": cb_reset, "w": cb_track,
    "wm": cb_watch, "wo": cb_open_watch, "wc": cb_tune, "wt": cb_target, "ws": cb_stats,
    "wg": cb_chart, "wv": cb_chart_period, "wd": cb_delete, "wy": cb_delete_yes,
    "wn": toggler("newfloor", True), "w1": toggler("day1", True), "wp": toggler("profit", False),
    "al": cb_autologin, "ap": cb_autopay_setup, "ab": cb_show_balance,
    "ad": cb_admin,
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
    if isinstance(result, Photo):
        await edit_photo(chat, mid, result)
    elif result:
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
    back = [btn("⬅️ Назад", f"p:{v.id}:{v.page}")]
    if not hits:
        return await say(chat, f"🔍 По запросу «{esc(text)}» ничего нет.\nПопробуй часть слова или выбери из списка.",
                         kb_of(back))
    if len(hits) == 1:
        waiting.pop(chat, None)
        v.filt[kind] = options[hits[0]]
        v.offsets = [""]
        out, kb = await render(v, 0)
        return await say(chat, out, kb)
    more = f"\nПоказал первые {CHOICES_PER_PAGE}, уточни запрос." if len(hits) > CHOICES_PER_PAGE else ""
    await say(chat, f"🔍 Нашёл <b>{len(hits)}</b>, выбери нужное.{more}",
              kb_of(*option_rows(v, kind, hits[:CHOICES_PER_PAGE]), back))


async def on_reply(chat, state, text):
    """Ответ на вопрос бота: ник пользователя, целевая цена или поиск значения фильтра."""
    if state == "user":
        waiting.pop(chat, None)
        return await open_user(chat, text)
    if state[0] == "find":
        return await on_find(chat, state, text)
    if state[0] == "admin":
        return await admin_reply(chat, state, text)
    try:
        value = float(text.replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return await say(chat, "🔢 Нужно число, например <code>120</code>.")
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
        return await say(chat, f"🔍 По запросу «{esc(query)}» ничего нет.",
                         kb_of([btn("🏠 Все коллекции", "home")]))
    await say(chat, f"🔍 Нашёл: <b>{len(found)}</b>", kb_of(*collection_rows(found), [btn("🏠 Все коллекции", "home")]))


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
            await say(chat, HELP, kb_of([btn("🏠 Меню", "home")]))
        elif cmd == "/admin" and is_owner(chat):  # остальным команда не отвечает, как будто её нет
            await say(chat, *admin_home())
        return
    if text in (BTN_HOME, BTN_WATCH, BTN_USER):
        waiting.pop(chat, None)
        if text == BTN_HOME:
            return await show_home(chat)
        if text == BTN_WATCH:
            return await say(chat, *watch_list(chat))
        return await ask_user(chat)
    state = waiting.get(chat)
    if is_user_ref(text) and not (isinstance(state, tuple) and state[0] in ("target", "admin")):
        waiting.pop(chat, None)
        return await open_user(chat, text)
    if state is not None:
        return await on_reply(chat, state, text)
    await find_collection(chat, text)


async def handle(update):
    chat = None
    try:
        cq = update.get("callback_query")
        origin = update.get("message") or cq or {}
        msg = update.get("message") or (cq or {}).get("message")
        if msg:
            if msg["chat"].get("type", "private") != "private":
                return  # бот личный: слежения и лимиты привязаны к человеку, а в группе чат общий
            chat = msg["chat"]["id"]
            if not await gate(chat, origin.get("from") or {}, update):
                return
        if "message" in update:
            await on_message(update["message"])
        elif cq:
            await on_callback(cq)
    except FloodWaitError as e:
        if chat:
            await say(chat, f"⏳ Telegram просит подождать {e.seconds} сек. Попробуй чуть позже.")
    except Exception:
        log.exception("апдейт %s", update.get("update_id"))
        if chat:
            await say(chat, "😵 Что-то сломалось, попробуй ещё раз.", kb_of([btn("🏠 Меню", "home")]))


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
    await api("setMyCommands", commands=COMMANDS + [{"command": "admin", "description": "Админ-панель"}],
              scope={"type": "chat", "chat_id": OWNER_ID})  # команда видна только владельцу
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
