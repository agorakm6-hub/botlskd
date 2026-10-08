// ============================================================
//  index.js — Telegram-бот Void Launcher
//  /start → описание + кнопки «Подписаться» / «Проверить»
//  Подписка на канал проверяется через пользовательский аккаунт
//  (SESSION_STRING), потому что бота в канал добавить нельзя.
//  После подписки бот отдаёт файл лаунчера из последнего релиза GitHub.
//  Хостинг: Render (webhook + анти-слип, как в chat.js).
// ============================================================
const TelegramBot = require('node-telegram-bot-api');
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { TelegramClient, Api } = require('telegram');
const { StringSession } = require('telegram/sessions');

// ====== КОНФИГ ======
const BOT_TOKEN = process.env.BOT_TOKEN || '';
const API_ID = parseInt(process.env.API_ID || '', 10);
const API_HASH = process.env.API_HASH || '';
const SESSION_STRING = process.env.SESSION_STRING || '';
if (!BOT_TOKEN) { console.error('❌ Не задан BOT_TOKEN.'); process.exit(1); }
if (!API_ID || !API_HASH) { console.error('❌ Не заданы API_ID / API_HASH (my.telegram.org → API development tools).'); process.exit(1); }
if (!SESSION_STRING) { console.error('❌ Не задан SESSION_STRING (получить: node gen-session.js).'); process.exit(1); }

const PORT = process.env.PORT || 10000;
const EXTERNAL_URL = process.env.RENDER_EXTERNAL_URL || process.env.WEBHOOK_URL;
const WEBHOOK_PATH = `/bot${BOT_TOKEN}`;
if (!EXTERNAL_URL) { console.error('❌ Не найден RENDER_EXTERNAL_URL или WEBHOOK_URL.'); process.exit(1); }

// Секрет вебхука: Telegram присылает его в заголовке X-Telegram-Bot-Api-Secret-Token,
// сервер сверяет его перед обработкой апдейта. Можно задать свой через WEBHOOK_SECRET,
// иначе генерируется случайный при каждом старте (вебхук переустанавливается автоматически).
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || crypto.randomBytes(24).toString('hex');

const CHANNEL_USERNAME = (process.env.CHANNEL_USERNAME || 'v0idlauncher').replace(/^@/, '');
const CHANNEL_URL = `https://t.me/${CHANNEL_USERNAME}`;

const GITHUB_REPO = process.env.GITHUB_REPO || 'pidorchain/VoidLauncher';
const GITHUB_TOKEN = process.env.GITHUB_TOKEN || ''; // необязательно, поднимает лимит GitHub API
const FALLBACK_TAG = process.env.FALLBACK_TAG || 'v1.6.1'; // если /releases/latest недоступен
// Если в релизе несколько файлов и бот выбирает не тот — задай регулярку имени файла, например: Setup.*\.exe
const ASSET_REGEX = process.env.ASSET_REGEX ? new RegExp(process.env.ASSET_REGEX, 'i') : null;

const AVA_PATH = path.join(__dirname, 'ava.png');
const TG_BOT_FILE_LIMIT = 49 * 1024 * 1024; // бот может отправить файл до 50 МБ

const ALERT_TEXT = 'Пожалуйста, подпишитесь на наш новостной канал, чтобы получить файл лаунчера ❤️';
const START_TEXT =
    '<b>VoidLauncher</b>: быстрый и красивый лаунчер Minecraft Java Edition для Windows. ' +
    'Сам скачивает Java и нужную версию игры, ставит Fabric и моды с Modrinth, ' +
    'поддерживает офлайн и Microsoft-аккаунты, настройку ОЗУ и флагов запуска, ' +
    'запуск сразу на сервер и автообновление. Установка в пару кликов, без лишних настроек.';

// Цвета кнопок: style "primary" — синяя, "success" — зелёная (Bot API 9.4+)
const GATE_KEYBOARD = {
    inline_keyboard: [
        [{ text: '📢 Подписаться', url: CHANNEL_URL, style: 'primary' }],
        [{ text: '✅ Проверить', callback_data: 'check', style: 'success' }],
    ],
};

// ====== ЮЗЕРБОТ (проверка подписки) ======
const tg = new TelegramClient(new StringSession(SESSION_STRING), API_ID, API_HASH, {
    connectionRetries: 10,
    autoReconnect: true,
});
tg.setLogLevel('error');

let channelEntity = null;
let userbotReady = false;
const peerCache = new Map();  // userId -> Api.InputPeerUser (нужен access_hash, без него Telegram не даёт найти пользователя)
const subscribedUntil = new Map(); // userId -> timestamp, до которого считаем «подписан» без нового запроса
const lastMiss = new Map();   // userId -> timestamp последней неудачной проверки (защита от флуда запросов)
const SUB_CACHE_MS = 20 * 1000;
const MISS_THROTTLE_MS = 3 * 1000;

function rememberUsers(users) {
    for (const u of users) {
        if (!u || u.id === undefined || u.accessHash === undefined || u.accessHash === null) continue;
        peerCache.set(String(u.id), new Api.InputPeerUser({ userId: u.id, accessHash: u.accessHash }));
    }
}

async function collectParticipants(filter, limit) {
    const list = [];
    for await (const u of tg.iterParticipants(channelEntity, { limit, ...filter })) list.push(u);
    rememberUsers(list);
}

async function initUserbot() {
    await tg.connect();
    const me = await tg.getMe();
    channelEntity = await tg.getEntity(CHANNEL_USERNAME);
    userbotReady = true;
    console.log(`✅ Юзербот подключён как ${me.firstName || ''} (@${me.username || 'без username'}), канал: ${CHANNEL_USERNAME}`);
    // Прогрев: подгружаем подписчиков заранее (нужны их access_hash)
    try {
        await collectParticipants({}, 20000);
        console.log(`✅ Загружено подписчиков в кэш: ${peerCache.size}`);
    } catch (e) {
        console.error(`❌ Не удалось получить список подписчиков (${e.errorMessage || e.message}). Аккаунт из SESSION_STRING должен быть админом канала!`);
    }
}

// Находим InputPeer пользователя: из кэша → среди последних подписчиков → поиск по имени
async function resolvePeer(userId, firstName) {
    const id = String(userId);
    if (peerCache.has(id)) return peerCache.get(id);
    await collectParticipants({}, 200);
    if (peerCache.has(id)) return peerCache.get(id);
    if (firstName) {
        await collectParticipants({ search: firstName }, 200);
        if (peerCache.has(id)) return peerCache.get(id);
    }
    return null;
}

async function isSubscribed(userId, firstName) {
    if (!userbotReady) throw new Error('userbot not ready');
    const id = String(userId);
    if ((subscribedUntil.get(id) || 0) > Date.now()) return true;
    if (Date.now() - (lastMiss.get(id) || 0) < MISS_THROTTLE_MS) return false;

    const peer = await resolvePeer(userId, firstName);
    if (!peer) { lastMiss.set(id, Date.now()); return false; }

    try {
        const res = await tg.invoke(new Api.channels.GetParticipant({ channel: channelEntity, participant: peer }));
        const p = res.participant;
        const ok = p instanceof Api.ChannelParticipant
            || p instanceof Api.ChannelParticipantSelf
            || p instanceof Api.ChannelParticipantCreator
            || p instanceof Api.ChannelParticipantAdmin;
        if (ok) { subscribedUntil.set(id, Date.now() + SUB_CACHE_MS); return true; }
        lastMiss.set(id, Date.now());
        return false;
    } catch (e) {
        if (e.errorMessage === 'USER_NOT_PARTICIPANT') { lastMiss.set(id, Date.now()); return false; }
        throw e;
    }
}

// ====== GITHUB: ПОСЛЕДНИЙ РЕЛИЗ ======
// Релиз запрашивается у GitHub и кэшируется на 5 минут — когда build.yml
// выпустит новый релиз, бот сам начнёт отдавать новый файл.
let releaseCache = null; // { tag, asset, htmlUrl, fetchedAt }
let fileIdCache = null;  // { key, fileId } — чтобы не качать и не заливать файл заново
let downloadInflight = null;
const RELEASE_TTL_MS = 5 * 60 * 1000;

async function ghJson(apiPath) {
    const headers = { Accept: 'application/vnd.github+json', 'User-Agent': 'void-launcher-bot' };
    if (GITHUB_TOKEN) headers.Authorization = `Bearer ${GITHUB_TOKEN}`;
    const res = await fetch(`https://api.github.com${apiPath}`, { headers, signal: AbortSignal.timeout(15000) });
    if (!res.ok) throw new Error(`GitHub API ${res.status}`);
    return res.json();
}

function pickAsset(assets) {
    const usable = (assets || []).filter((a) => a.state === 'uploaded' && !/\.(blockmap|ya?ml|sha\d*|sig|asc|txt|json)$/i.test(a.name));
    if (ASSET_REGEX) {
        const forced = usable.find((a) => ASSET_REGEX.test(a.name));
        if (forced) return forced;
    }
    const rank = (a) => {
        const n = a.name.toLowerCase();
        if (n.endsWith('.exe') && /setup|install/.test(n)) return 0;
        if (n.endsWith('.exe')) return 1;
        if (n.endsWith('.msi')) return 2;
        if (n.endsWith('.zip')) return 3;
        return 4;
    };
    return usable.sort((a, b) => rank(a) - rank(b))[0] || null;
}

async function getRelease() {
    if (releaseCache && Date.now() - releaseCache.fetchedAt < RELEASE_TTL_MS) return releaseCache;
    try {
        let data;
        try { data = await ghJson(`/repos/${GITHUB_REPO}/releases/latest`); }
        catch (e) { data = await ghJson(`/repos/${GITHUB_REPO}/releases/tags/${FALLBACK_TAG}`); }
        const asset = pickAsset(data.assets);
        if (!asset) throw new Error('В релизе нет подходящего файла');
        releaseCache = { tag: data.tag_name, asset, htmlUrl: data.html_url, fetchedAt: Date.now() };
        return releaseCache;
    } catch (e) {
        if (releaseCache) { console.warn(`⚠️ GitHub недоступен, отдаю закэшированный релиз: ${e.message}`); return releaseCache; }
        throw e;
    }
}

const sendCooldown = new Map(); // userId -> ts, чтобы не спамили файлом
async function sendLauncher(chatId, userId) {
    if (Date.now() - (sendCooldown.get(String(userId)) || 0) < 8000) return;
    sendCooldown.set(String(userId), Date.now());

    let rel;
    try { rel = await getRelease(); }
    catch (e) {
        console.error('release error:', e.message);
        await bot.sendMessage(chatId, '⚠️ Не получилось получить файл лаунчера, попробуй чуть позже.');
        return;
    }

    const caption = `✅ Подписка подтверждена!\n\nVoidLauncher ${rel.tag}`;
    const linkKeyboard = { inline_keyboard: [[{ text: '⬇️ Скачать с GitHub', url: rel.asset.browser_download_url, style: 'primary' }]] };

    // Файл больше лимита бота — отдаём ссылкой
    if (rel.asset.size > TG_BOT_FILE_LIMIT) {
        await bot.sendMessage(chatId, `${caption}\n\nФайл слишком большой для отправки в Telegram, скачай по кнопке ниже.`, { reply_markup: linkKeyboard });
        return;
    }

    const key = `${rel.asset.id}:${rel.asset.updated_at}`;
    try {
        if (fileIdCache && fileIdCache.key === key) {
            try { await bot.sendDocument(chatId, fileIdCache.fileId, { caption }); return; }
            catch (e) { fileIdCache = null; } // file_id протух — зальём заново
        }
        if (!downloadInflight) {
            downloadInflight = (async () => {
                const res = await fetch(rel.asset.browser_download_url, { redirect: 'follow', headers: { 'User-Agent': 'void-launcher-bot' }, signal: AbortSignal.timeout(180000) });
                if (!res.ok) throw new Error(`download ${res.status}`);
                return Buffer.from(await res.arrayBuffer());
            })().finally(() => { downloadInflight = null; });
        }
        const buf = await downloadInflight;
        const sent = await bot.sendDocument(chatId, buf, { caption }, { filename: rel.asset.name, contentType: 'application/octet-stream' });
        if (sent && sent.document) fileIdCache = { key, fileId: sent.document.file_id };
    } catch (e) {
        console.error('sendLauncher error:', e.message);
        await bot.sendMessage(chatId, `${caption}\n\nНе получилось отправить файл напрямую — скачай по кнопке ниже.`, { reply_markup: linkKeyboard });
    }
}

// ====== БОТ / СЕРВЕР ======
const bot = new TelegramBot(BOT_TOKEN, { webHook: false });
console.log('🚀 Void Launcher бот запущен (webhook)');

process.on('uncaughtException', (err) => console.error('Uncaught Exception:', err));
process.on('unhandledRejection', (reason) => console.error('Unhandled Rejection:', reason));

let avaFileId = null;
async function sendGate(chatId) {
    const opts = { caption: START_TEXT, parse_mode: 'HTML', reply_markup: GATE_KEYBOARD };
    if (fs.existsSync(AVA_PATH) || avaFileId) {
        try {
            const sent = await bot.sendPhoto(chatId, avaFileId || fs.createReadStream(AVA_PATH), opts);
            if (!avaFileId && sent.photo) avaFileId = sent.photo[sent.photo.length - 1].file_id;
            return;
        } catch (e) { console.error('sendPhoto error:', e.message); avaFileId = null; }
    }
    await bot.sendMessage(chatId, START_TEXT, { parse_mode: 'HTML', reply_markup: GATE_KEYBOARD });
}

async function checkOrFail(user) {
    try { return { ok: await isSubscribed(user.id, user.first_name), error: false }; }
    catch (e) {
        console.error('subscription check error:', e.errorMessage || e.message);
        return { ok: false, error: true };
    }
}

bot.on('message', async (msg) => {
    if (msg.chat.type !== 'private' || !msg.from || msg.from.is_bot) return;
    const chatId = msg.chat.id;
    const cmd = msg.text && msg.text.startsWith('/') ? msg.text.trim().split(/[\s@]/)[0].toLowerCase() : null;

    if (cmd === '/help') { await sendGate(chatId); return; }

    const { ok, error } = await checkOrFail(msg.from);
    if (error) { await bot.sendMessage(chatId, '⚠️ Не удалось проверить подписку, попробуй чуть позже.'); return; }
    if (!ok) {
        if (cmd === '/start') await sendGate(chatId);
        else await bot.sendMessage(chatId, ALERT_TEXT, { reply_markup: GATE_KEYBOARD });
        return;
    }
    await sendLauncher(chatId, msg.from.id);
});

bot.on('callback_query', async (q) => {
    try {
        if (q.data !== 'check' || !q.message) { await bot.answerCallbackQuery(q.id); return; }
        const { ok, error } = await checkOrFail(q.from);
        if (error) { await bot.answerCallbackQuery(q.id, { text: 'Не удалось проверить подписку, попробуй чуть позже.', show_alert: true }); return; }
        if (!ok) { await bot.answerCallbackQuery(q.id, { text: ALERT_TEXT, show_alert: true }); return; }
        await bot.answerCallbackQuery(q.id);
        await sendLauncher(q.message.chat.id, q.from.id);
    } catch (e) { console.error('callback error:', e.message); }
});

const MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024;

const server = http.createServer((req, res) => {
    if (req.method === 'POST' && req.url === WEBHOOK_PATH) {
        if (req.headers['x-telegram-bot-api-secret-token'] !== WEBHOOK_SECRET) {
            console.warn('⚠️ Webhook: неверный или отсутствующий secret token — запрос отклонён');
            res.writeHead(401, { 'Content-Type': 'application/json' });
            res.end('{"ok":false}');
            req.destroy();
            return;
        }

        let body = '';
        let tooBig = false;
        req.on('data', (c) => {
            if (tooBig) return;
            body += c;
            if (body.length > MAX_WEBHOOK_BODY_BYTES) {
                tooBig = true;
                res.writeHead(413, { 'Content-Type': 'application/json' });
                res.end('{"ok":false}');
                req.destroy();
            }
        });
        req.on('end', () => {
            if (tooBig) return;
            try { bot.processUpdate(JSON.parse(body)); } catch (e) { console.error('parse error:', e); }
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end('{"ok":true}');
        });
        return;
    }
    if (req.method === 'GET' && req.url === '/health') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', bot: 'running', userbot: userbotReady, uptime: process.uptime() }));
        return;
    }
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end('OK');
});

server.listen(PORT, async () => {
    console.log(`✅ Сервер на порту ${PORT}`);
    try {
        await bot.setWebHook(`${EXTERNAL_URL}${WEBHOOK_PATH}`, { secret_token: WEBHOOK_SECRET });
        console.log('✅ Webhook установлен');
    } catch (e) { console.error('❌ Webhook error:', e); }
    try {
        // Меню команд (кнопка "Menu" слева от поля ввода)
        await bot.setMyCommands([
            { command: 'help', description: 'Что такое VoidLauncher' },
        ]);
        console.log('✅ Меню команд установлено');
    } catch (e) { console.error('❌ setMyCommands error:', e); }
    keepAliveLoop();
    heartbeatLoop();
    initUserbot().catch((e) => console.error('❌ Юзербот не запустился:', e.errorMessage || e.message));
});

// ====== АНТИ-СЛИП ======
// Метод 1: периодический self-ping через fetch на собственный /health.
async function keepAliveLoop() {
    const url = `${EXTERNAL_URL}/health`;
    await new Promise((r) => setTimeout(r, 10000));
    while (true) {
        let success = false;
        for (let attempt = 1; attempt <= 3 && !success; attempt++) {
            try {
                const res = await fetch(url, { signal: AbortSignal.timeout(10000) });
                console.log(`🔄 Keep-alive пинг: ${res.status}`);
                success = true;
            } catch (e) {
                console.warn(`⚠️ Keep-alive пинг не удался (попытка ${attempt}/3): ${e.message}`);
                await new Promise((r) => setTimeout(r, 5000));
            }
        }
        if (!success) console.error('❌ Keep-alive: все попытки пинга провалились в этом цикле');
        await new Promise((r) => setTimeout(r, 150000)); // раз в 2.5 минуты
    }
}

// Метод 2: независимый пинг через http/https напрямую — резервный канал.
function heartbeatLoop() {
    setInterval(() => {
        try {
            const mod = EXTERNAL_URL.startsWith('https') ? https : http;
            const req = mod.get(`${EXTERNAL_URL}/health`, { timeout: 10000 }, (res) => {
                console.log(`💓 Heartbeat пинг: ${res.statusCode}`);
                res.resume();
            });
            req.on('timeout', () => req.destroy());
            req.on('error', (e) => console.warn(`⚠️ Heartbeat пинг не удался: ${e.message}`));
        } catch (e) {
            console.warn(`⚠️ Heartbeat ошибка: ${e.message}`);
        }
    }, 240000); // раз в 4 минуты
}
