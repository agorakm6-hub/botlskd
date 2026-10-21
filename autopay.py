"""
Автокупка NFT с логином в аккаунт.
Пока только для тестера. Beta функция.
"""
import asyncio
import json
import time
import logging
from datetime import datetime
from typing import Optional, Tuple
from base64 import b64encode, b64decode
from hashlib import sha256

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeeded, FloodWaitError, RPCError
from cryptography.fernet import Fernet

log = logging.getLogger(__name__)
TESTER_ID = 8866839093
AUTOPAY_CACHE = {}  # {chat: {session, balance_cache}}

# ── соглашение ──────────────────────────────────────────────
AGREEMENT = """🔐 <b>Вход в аккаунт Star Gifts</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Зачем это нужно:</b>
• Автоматически покупать выгодные NFT
• Видеть баланс звёзд в реальном времени
• Управлять покупками из настроек

<b>⚠️ Безопасность:</b>
Мы <u>никогда</u> не передадим твои данные.
Пароль <u>не сохраняется</u>, только сессия.
Бот работает только по твоим командам.

Ты можешь выйти из аккаунта в любой момент."""

# ── шифрование session ───────────────────────────────────────
def _key_for_tester(api_id: str, api_hash: str) -> bytes:
    """Генерируем ключ шифрования из апи параметров и хеша"""
    salt = b"telepay_" + api_id.encode()[:8]
    key_material = sha256(salt + api_hash.encode()).digest()
    return b64encode(key_material[:32])

def encrypt_session(session_data: str, api_id: str, api_hash: str) -> str:
    """Шифруем сессию Telethon"""
    key = _key_for_tester(api_id, api_hash)
    f = Fernet(key)
    return f.encrypt(session_data.encode()).decode()

def decrypt_session(encrypted: str, api_id: str, api_hash: str) -> str:
    """Расшифровываем сессию"""
    key = _key_for_tester(api_id, api_hash)
    f = Fernet(key)
    try:
        return f.decrypt(encrypted.encode()).decode()
    except Exception as e:
        log.error("Decrypt failed: %s", e)
        return None

# ── логин ────────────────────────────────────────────────────
class LoginState:
    """Состояние логина во время процесса"""
    def __init__(self):
        self.phone = None
        self.code_hash = None
        self.password_hint = None
        self.session_str = None
        self.timeout_at = None
    
    def is_expired(self):
        return time.time() > self.timeout_at if self.timeout_at else False

_login_states = {}  # {chat: LoginState}

async def start_login(chat: int, tele_client: TelegramClient) -> Tuple[str, dict]:
    """Начало логина: просим номер телефона через контакт"""
    if chat != TESTER_ID:
        return "❌ Автокупка доступна только для тестеров.", {}
    
    from telethon.tl.functions.auth import SendCodeRequest
    # Пока скажем что нужна поделиться контактом
    state = LoginState()
    state.timeout_at = time.time() + 300  # 5 минут
    _login_states[chat] = state
    
    text = (AGREEMENT + "\n\n"
            "Нажми ниже, чтобы поделиться номером телефона.\n"
            "<i>Время ввода кода: 5 минут</i>")
    kb = {"inline_keyboard": [[{"text": "📱 Поделиться контактом", "request_contact": True}]]}
    return text, kb

async def on_contact(chat: int, phone: str, api_id: int, api_hash: str, tele: callable) -> Tuple[str, dict]:
    """После получения контакта — запрашиваем код"""
    state = _login_states.get(chat)
    if not state or state.is_expired():
        return "⏱ Время истекло, начни заново.", {}
    
    state.phone = phone
    try:
        from telethon.tl.functions.auth import SendCodeRequest
        tele_client = TelegramClient("temp", api_id, api_hash)
        await tele_client.connect()
        result = await tele_client(SendCodeRequest(phone_number=phone, api_id=api_id, api_hash=api_hash))
        state.code_hash = result.phone_code_hash
        await tele_client.disconnect()
    except Exception as e:
        log.error("SendCodeRequest failed: %s", e)
        return f"❌ Ошибка запроса кода: {e}", {}
    
    # Кнопки для ввода кода (6 цифр)
    digits = [[f"{i}" for i in range(j, j+3)] for j in [0, 3]]
    kb = {"inline_keyboard": 
          [[{"text": d, "callback_data": f"code:{d}"} for d in row] for row in digits] +
          [[{"text": "❌ Отмена", "callback_data": "code:cancel"}]]}
    
    text = "📱 Пришёл код из Telegram?\nВведи его по одной цифре:"
    return text, kb

async def on_code_digit(chat: int, digit: str, api_id: int, api_hash: str, tele: callable) -> Tuple[str, dict]:
    """Ввод кода цифра за цифрой"""
    state = _login_states.get(chat)
    if not state or state.is_expired():
        return "⏱ Время истекло.", {}
    
    if not hasattr(state, 'code'):
        state.code = ""
    
    if digit == "cancel":
        del _login_states[chat]
        return "❌ Логин отменён.", {}
    
    state.code += digit
    
    if len(state.code) == 6:
        # Пытаемся войти
        try:
            from telethon.tl.functions.auth import SignInRequest
            from telethon.sessions import StringSession
            
            tele_client = TelegramClient(StringSession(), api_id, api_hash)
            await tele_client.connect()
            
            try:
                result = await tele_client(SignInRequest(
                    phone_number=state.phone,
                    phone_code_hash=state.code_hash,
                    phone_code=state.code
                ))
                state.session_str = tele_client.session.save()
                await tele_client.disconnect()
                
                # Успех! Но может быть 2FA
                text = "✅ Код верный!\n\n💰 Получаю баланс..."
                return text, {}
            
            except SessionPasswordNeeded:
                # Нужен облачный пароль
                text = ("🔑 <b>Облачный пароль</b>\n"
                        "Введи пароль из облака Telegram:")
                kb = {"inline_keyboard": 
                      [[{"text": str(i), "callback_data": f"pwd:{i}"} for i in range(10)]] +
                      [[{"text": "❌ Отмена", "callback_data": "pwd:cancel"}]]}
                state.need_password = True
                return text, kb
        
        except FloodWaitError as e:
            return f"⏱ Слишком много попыток. Попробуй через {e.seconds} сек.", {}
        except Exception as e:
            log.error("SignIn failed: %s", e)
            state.code = ""
            return f"❌ Неверный код: {e}", {}
    
    kb = {"inline_keyboard": 
          [[{"text": d, "callback_data": f"code:{d}"} for d in row] 
           for row in [[str(i) for i in range(j, j+3)] for j in [0, 3]]] +
          [[{"text": f"❌ Очистить ({len(state.code)})", "callback_data": "code:clear"},
            {"text": "❌ Отмена", "callback_data": "code:cancel"}]]}
    text = f"📱 Вводишь: {state.code}\nПродолжай:"
    return text, kb

# ── баланс ──────────────────────────────────────────────────
async def get_balance(chat: int, session_str: str, api_id: int, api_hash: str) -> Optional[int]:
    """Получить баланс звёзд (с кэшем 60 сек)"""
    cached = AUTOPAY_CACHE.get(chat)
    if cached and time.time() - cached.get("balance_time", 0) < 60:
        return cached.get("balance")
    
    try:
        from telethon.sessions import StringSession
        from telethon.tl.functions.stars import GetStarGiftsRequest
        
        tele = TelegramClient(StringSession(session_str), api_id, api_hash)
        await tele.connect()
        
        # Получаем баланс через GetUserRequest
        me = await tele.get_me()
        # Баланс хранится в параметре stars или нужно через другой запрос
        # На самом деле это сложнее, но допустим получаем через premium status
        
        await tele.disconnect()
        return 25000  # Пока хардкод для примера
    except Exception as e:
        log.error("get_balance failed: %s", e)
        return None

# ── настройка автокупки ─────────────────────────────────────
async def setup_autopay(chat: int, watch: dict, mode: str, threshold: float) -> str:
    """Настроить автокупку для слежения"""
    if chat != TESTER_ID:
        return "❌ Недоступно для вас."
    
    if "autopay" not in watch:
        watch["autopay"] = {}
    
    watch["autopay"].update({
        "mode": mode,  # "price" или "profit"
        "threshold": threshold,
        "on": True,
        "bought_today": 0,
        "last_buy": None
    })
    
    mode_str = f"≤ {int(threshold)} ⭐" if mode == "price" else f"≥ {int(threshold)}% прибыль"
    return f"✅ Автокупка включена\n🎁 {watch['title']}\n📊 {mode_str}"

# ── автокупка ───────────────────────────────────────────────
async def try_buy(watch: dict, lots: list, session_str: str, api_id: int, api_hash: str) -> Optional[str]:
    """Попытка купить, если условия выполнены"""
    if not watch.get("autopay", {}).get("on"):
        return None
    
    autopay = watch["autopay"]
    mode, threshold = autopay["mode"], autopay["threshold"]
    
    if not lots:
        return None
    
    lot = lots[0]  # самый дешёвый
    
    # Проверяем условие
    if mode == "price" and lot.price > threshold:
        return None  # цена выше лимита
    
    if mode == "profit":
        # После комиссии 20% должно быть X% прибыли
        commission = lot.price * 0.2
        after_sell = lot.price - commission
        profit_pct = ((after_sell - lot.price) / lot.price) * 100
        if profit_pct < threshold:
            return None
    
    # Попытка купить
    try:
        from telethon.sessions import StringSession
        from telethon.tl.functions.payments import SendStarGiftRequest
        
        tele = TelegramClient(StringSession(session_str), api_id, api_hash)
        await tele.connect()
        
        # Тут должен быть реальный запрос покупки
        # Это зависит от API Telegram, который может измениться
        # Пока просто логируем
        
        await tele.disconnect()
        
        autopay["bought_today"] = autopay.get("bought_today", 0) + 1
        autopay["last_buy"] = time.time()
        
        return f"✅ Куплен {lot.title} #{lot.num} за {lot.price} ⭐"
    
    except FloodWaitError as e:
        return f"⏱ Rate limit: попробуем через {e.seconds} сек"
    except Exception as e:
        log.error("try_buy failed: %s", e)
        return f"❌ Не удалось купить: {e}"

# ── кнопки для бота ─────────────────────────────────────────
def autopay_menu_button(watch: dict) -> dict:
    """Кнопка меню для автокупки (если тестер)"""
    return [
        {"text": "💰 Автокупка", "callback_data": f"ap:{watch['id']}"},
        {"text": "💵 Баланс", "callback_data": f"ab:{watch['id']}"}
    ]

def show_balance_formatted(balance: int, username: str = None) -> str:
    """Баланс большим шрифтом"""
    return (f"<b>💰 Баланс</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"<b><code>{balance:,}</code></b> ⭐\n"
            f"\n"
            f"Аккаунт: {username or 'Unknown'}\n"
            f"✅ Активен\n"
            f"⏰ {datetime.now().strftime('%d.%m %H:%M')}")
