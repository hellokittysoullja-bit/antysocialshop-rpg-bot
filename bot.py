# bot.py — ANTY SOCIAL SHOP RPG v4.0 IDEAL (Искажение, именные бланты, Удача, Эхо)
import asyncio, logging, os, random, re, json
from datetime import datetime, timedelta, date
from threading import Thread

import aiosqlite
from cachetools import TTLCache
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# === ВЕБ-СЕРВЕР ДЛЯ RENDER ===
web_app = Flask(__name__)
@web_app.route("/")
def home():
    return "Antysocialshop RPG Bot is alive!"

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ===
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FARM_COOLDOWN_HOURS = 0.5
FARM_MIN, FARM_MAX = 45, 100
HAPPY_HOUR_MULTIPLIER = 2
HAPPY_HOUR_DURATION_MIN = 30

# === КЭШ ИГРОКОВ ===
player_cache = TTLCache(maxsize=500, ttl=30)

def invalidate_cache(user_id):
    player_cache.pop(user_id, None)

# === КОМПИЛИРОВАННЫЕ РЕГУЛЯРКИ ===
RE_RITUAL = re.compile(r'^/ритуал$')
RE_FARM = re.compile(r'^/фарм$')
RE_SMOKE = re.compile(r'^/дунуть$')
RE_CRAFT = re.compile(r'^/крафт$')
RE_TOP = re.compile(r'^/топ$')
RE_LUCK = re.compile(r'^/удача$|^/luck$')
RE_PROFILE = re.compile(r'^/профиль$|^/profile$')
RE_GUILD = re.compile(r'^/guild$|^/вступить$')
RE_SHORTCUTS = re.compile(r'^(фарм|farm|дунуть|smoke|крафт|craft|топ|top|удача|luck|профиль|profile)$', re.IGNORECASE)

# === ШЁПОТ ИСКАЖЕНИЯ ===
WHISPERS = [
    "🩸 Искажение наблюдает за твоими нитями...",
    "💠 Кристалл твоей судьбы пульсирует.",
    "🕯️ Смотритель помнит всех.",
    "🩸 Искажение шепчет твоё имя.",
    "🌀 Нити реальности натянуты до предела."
]

# === НЕЙРО-СТАТУСЫ ===
NEURO_STATUSES = [
    "Альфа-ритмы нестабильны",
    "Сенсорная депривация 80%",
    "Фаза быстрого сна",
    "Нейро-шунт активен",
    "Предел синаптической проводимости",
    "Резонанс с Искажением: 12%"
]

# === ИНИЦИАЛИЗАЦИЯ БД ===
async def init_db():
    async with aiosqlite.connect("players.db") as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                blunts INTEGER DEFAULT 0,
                guild TEXT DEFAULT NULL,
                last_farm TIMESTAMP,
                last_ritual TIMESTAMP,
                last_daily TIMESTAMP,
                titles TEXT DEFAULT '',
                last_farm_date DATE,
                passive_level INTEGER DEFAULT 0,
                passive_collected TIMESTAMP,
                karma INTEGER DEFAULT 0,
                achievements TEXT DEFAULT '',
                inhaled INTEGER DEFAULT 0,
                smoke_count INTEGER DEFAULT 0,
                farm_count INTEGER DEFAULT 0,
                craft_count INTEGER DEFAULT 0,
                ritual_count INTEGER DEFAULT 0,
                referral_count INTEGER DEFAULT 0,
                last_berserk TIMESTAMP,
                inventory TEXT DEFAULT '[]'
            )
        """)
        cur = await db.execute("PRAGMA table_info(players)")
        columns = [row[1] for row in await cur.fetchall()]
        for col, def_type in [
            ("inventory", "TEXT DEFAULT '[]'"),
            ("inhaled", "INTEGER DEFAULT 0"),
            ("smoke_count", "INTEGER DEFAULT 0"),
            ("farm_count", "INTEGER DEFAULT 0"),
            ("craft_count", "INTEGER DEFAULT 0"),
            ("ritual_count", "INTEGER DEFAULT 0"),
            ("referral_count", "INTEGER DEFAULT 0"),
            ("achievements", "TEXT DEFAULT ''"),
            ("passive_level", "INTEGER DEFAULT 0"),
            ("passive_collected", "TIMESTAMP"),
            ("karma", "INTEGER DEFAULT 0"),
            ("last_berserk", "TIMESTAMP")
        ]:
            if col not in columns:
                await db.execute(f"ALTER TABLE players ADD COLUMN {col} {def_type}")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_balance ON players(balance DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_last_farm ON players(last_farm)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_weekly (
                guild TEXT PRIMARY KEY,
                total_farmed INTEGER DEFAULT 0,
                week_start DATE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crystals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                description TEXT,
                amount_rub INTEGER,
                daily_oas INTEGER,
                total_earned INTEGER DEFAULT 0,
                start_date TIMESTAMP,
                cancelled INTEGER DEFAULT 0,
                completed INTEGER DEFAULT 0
            )
        """)
        await db.commit()

# === ПОЛУЧЕНИЕ ИГРОКА ===
async def get_player(user_id):
    async with aiosqlite.connect("players.db") as db:
        async with db.execute(
            "SELECT balance, blunts, guild, last_farm, last_ritual, last_daily, "
            "titles, last_farm_date, passive_level, passive_collected, karma, "
            "achievements, inhaled, smoke_count, farm_count, craft_count, "
            "ritual_count, referral_count, last_berserk, inventory FROM players WHERE user_id=?",
            (user_id,)
        ) as cursor:
            return await cursor.fetchone()

async def get_player_cached(user_id):
    if user_id in player_cache:
        return player_cache[user_id]
    p = await get_player(user_id)
    if p:
        player_cache[user_id] = p
    return p

# === ОБНОВЛЕНИЕ ДАННЫХ ===
async def update_balance(user_id, username, amount):
    async with aiosqlite.connect("players.db") as db:
        await db.execute("INSERT OR IGNORE INTO players (user_id, username, balance, blunts) VALUES (?,?,0,0)", (user_id, username))
        await db.execute("UPDATE players SET balance=balance+?, username=? WHERE user_id=?", (amount, username, user_id))
        await db.commit()
    invalidate_cache(user_id)

async def update_blunts(user_id, username, amount):
    async with aiosqlite.connect("players.db") as db:
        await db.execute("INSERT OR IGNORE INTO players (user_id, username, balance, blunts) VALUES (?,?,0,0)", (user_id, username))
        await db.execute("UPDATE players SET blunts=blunts+?, username=? WHERE user_id=?", (amount, username, user_id))
        await db.commit()
    invalidate_cache(user_id)

async def update_last_farm(user_id):
    now = datetime.now()
    today = date.today()
    async with aiosqlite.connect("players.db") as db:
        await db.execute("UPDATE players SET last_farm=?, last_farm_date=? WHERE user_id=?", (now, today, user_id))
        await db.commit()
    invalidate_cache(user_id)

async def update_last_ritual(user_id):
    async with aiosqlite.connect("players.db") as db:
        await db.execute("UPDATE players SET last_ritual=? WHERE user_id=?", (datetime.now(), user_id))
        await db.commit()
    invalidate_cache(user_id)

async def update_last_daily(user_id):
    async with aiosqlite.connect("players.db") as db:
        await db.execute("UPDATE players SET last_daily=? WHERE user_id=?", (datetime.now(), user_id))
        await db.commit()
    invalidate_cache(user_id)

async def update_last_berserk(user_id):
    async with aiosqlite.connect("players.db") as db:
        await db.execute("UPDATE players SET last_berserk=? WHERE user_id=?", (datetime.now(), user_id))
        await db.commit()
    invalidate_cache(user_id)

async def increment_counter(user_id, field):
    async with aiosqlite.connect("players.db") as db:
        await db.execute(f"UPDATE players SET {field}=COALESCE({field},0)+1 WHERE user_id=?", (user_id,))
        await db.commit()
    invalidate_cache(user_id)

async def add_title(user_id, emoji):
    async with aiosqlite.connect("players.db") as db:
        cur = await db.execute("SELECT titles FROM players WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        titles = (row[0] or "") if row else ""
        if emoji not in titles:
            titles = (titles + " " + emoji).strip()
            await db.execute("UPDATE players SET titles=? WHERE user_id=?", (titles, user_id))
            await db.commit()
    invalidate_cache(user_id)

# === РАНГИ И БОНУСЫ ===
RANKS = [
    ("🪓 Рекрут", 0, 0),
    ("⚔️ Ветеран", 5000, 1500),
    ("🪦 Призрак", 20000, 6000)
]

async def check_rank_up(context, user_id, username, old_balance, new_balance):
    for emoji, threshold, bonus in RANKS[1:]:
        if old_balance < threshold <= new_balance:
            if bonus:
                await update_balance(user_id, username, bonus)
            text = (
                "<b><i>🎉 РАНГ ПОВЫШЕН!</i></b>\n"
                f"@{username} теперь — {emoji} <b>{emoji_to_name(emoji)}</b>\n"
                f"<code>+{bonus}</code> 🍬 закапало на баланс"
            )
            await context.bot.send_message(chat_id="@guild_antysocial", text=text, parse_mode='HTML')

def emoji_to_name(emoji):
    for e, name, *_ in RANKS:
        if e == emoji:
            return name
    return ""

async def grant_title(user_id, emoji, name, context):
    await add_title(user_id, emoji)
    text = f"<b><i>🎉 ТИТУЛ РАЗБЛОКИРОВАН!</i></b>\n{emoji} Ты теперь — <b>{name}</b>"
    try:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
    except Exception:
        pass

# === ГЛАВНОЕ МЕНЮ ===
async def get_main_menu_keyboard(user_id):
    whisper = random.choice(WHISPERS)
    keyboard = [
        [InlineKeyboardButton("🍬 Фармить", callback_data="farm")],
        [InlineKeyboardButton("🌿 Крафт", callback_data="craft"),
         InlineKeyboardButton("💨 Дунуть", callback_data="smoke")],
        [InlineKeyboardButton("⚜️ Профиль", callback_data="profile"),
         InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("🕋 Гильдии", callback_data="guild_info"),
         InlineKeyboardButton("📜 Законы", callback_data="rules")],
        [InlineKeyboardButton("🎲 Удача", callback_data="luck")],
    ]
    player = await get_player_cached(user_id)
    if player:
        guild = await get_guild(user_id)
        if guild == "BLACK":
            keyboard.append([InlineKeyboardButton("🕯️ Ритуал", callback_data="ritual")])
        if player[0] >= 5000:
            pc = player[9]
            if pc:
                last = datetime.fromisoformat(pc) if isinstance(pc, str) else pc
                if (datetime.now() - last).total_seconds() / 3600 >= 1:
                    keyboard.append([InlineKeyboardButton("🪴 Собрать урожай", callback_data="collect")])
            else:
                keyboard.append([InlineKeyboardButton("🪴 Куст", callback_data="collect")])
        else:
            keyboard.append([InlineKeyboardButton("🪴 Куст (⚔️ Ветеран)", callback_data="bush_preview")])
        keyboard.append([InlineKeyboardButton("🐾 Питомец (⚔️ Ветеран)", callback_data="pet_preview")])
    keyboard.append([
        InlineKeyboardButton("🪪 Скидка", callback_data="privilege"),
        InlineKeyboardButton("📦 Каталог", callback_data="catalog")
    ])
    return InlineKeyboardMarkup(keyboard), whisper

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data="menu")]])

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_user_and_msg(update: Update):
    if update.callback_query:
        return update.callback_query.from_user, update.callback_query.message
    return update.effective_user, update.message

async def get_guild(user_id):
    p = await get_player_cached(user_id)
    return p[2] if p else None

async def set_guild(user_id, guild):
    async with aiosqlite.connect("players.db") as db:
        await db.execute("UPDATE players SET guild=? WHERE user_id=?", (guild, user_id))
        await db.commit()
    invalidate_cache(user_id)

async def get_top(limit=10):
    async with aiosqlite.connect("players.db") as db:
        async with db.execute("SELECT username, balance, guild FROM players ORDER BY balance DESC LIMIT ?", (limit,)) as cur:
            return await cur.fetchall()

async def count_guilds():
    async with aiosqlite.connect("players.db") as db:
        async with db.execute("SELECT guild, COUNT(*) FROM players WHERE guild IS NOT NULL GROUP BY guild") as cur:
            rows = await cur.fetchall()
    cnt = {"BLACK": 0, "WHITE": 0}
    for g, c in rows:
        if g in cnt:
            cnt[g] = c
    return cnt

# === ШЁПОТ ТКАНИ (автоудаление) ===
async def send_whisper(context: ContextTypes.DEFAULT_TYPE, chat_id: str, text: str, life_seconds: int = 45):
    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    context.job_queue.run_once(lambda c: c.bot.delete_message(chat_id, msg.message_id), when=life_seconds)

async def send_whisper_dm(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, life_seconds: int = 15):
    if update.callback_query:
        chat_id = update.callback_query.message.chat.id
    else:
        chat_id = update.effective_chat.id
    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    context.job_queue.run_once(lambda c: c.bot.delete_message(chat_id=chat_id, message_id=msg.message_id), when=life_seconds)

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    user_id = user.id
    username = user.username or user.first_name
    player = await get_player_cached(user_id)

    if context.args and context.args[0] == "activate":
        if not player:
            await update_balance(user_id, username, 0)
            await update_blunts(user_id, username, 0)
            await update_balance(user_id, username, 800)
            bonus = "🎁 Смотритель дарует тебе <code>800</code> 🍬.\n\n"
        else:
            bonus = ""
        if await get_guild(user_id):
            welcome = "<b><i>🎉 Добро пожаловать обратно в Гильдию Antysocialshop!</i></b>\n▸ Твоё Искажение натянуто, странник.\n▸ Возвращайся к ритуалам."
            kb, whisper = await get_main_menu_keyboard(user_id)
            await msg.reply_text(bonus + welcome + f"\n\n{whisper}", reply_markup=kb, parse_mode='HTML')
            return
        welcome = (
            "<b><i>🎉 Добро пожаловать в Гильдию Antysocialshop!</i></b>\n\n"
            "🕯️ <b>Тёмная Гильдия</b> — стабильность, ритуалы, тёмное благословение.\n"
            "⚜️ <b>Светлая Гильдия</b> — азарт, удача, танец на лезвии.\n\n"
            "▸ <i>Выбери свой путь:</i>"
        )
        guild_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕯️ Тёмная Гильдия", callback_data="guild_join_BLACK"),
             InlineKeyboardButton("⚜️ Светлая Гильдия", callback_data="guild_join_WHITE")]
        ])
        await msg.reply_text(bonus + welcome, reply_markup=guild_kb, parse_mode='HTML')
        return

    if not player:
        await update_balance(user_id, username, 0)
        await update_blunts(user_id, username, 0)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ АКТИВИРОВАТЬ ТЕРМИНАЛ", callback_data="activate_menu")]])
        await msg.reply_text(
            "<b>👁‍🗨 *Смотритель заметил тебя.*</b>\n"
            "<i>🪄 Искажение реальности ждёт твоего шага.</i>\n"
            "▸ Здесь добываются редкие экземпляры, зарабатывают Очки Антисошл (🍬), курят бланты и вступают в гильдии.\n"
            "🎁 Нажми, чтобы получить <code>800</code> 🍬 и войти в 🔒 закрытый сектор.",
            reply_markup=kb, parse_mode='HTML'
        )
        return

    guild = await get_guild(user_id)
    back = "<b>⚔️ *С возвращением в Гильдию!*</b>\n\n"
    if guild == "BLACK": back += "🕯️ Ты состоишь в <i>Тёмной Гильдии</i>.\n"
    elif guild == "WHITE": back += "⚜️ Ты состоишь в <i>Светлой Гильдии</i>.\n"
    else: back += "Ты пока не в Гильдии. Нажми /guild чтобы вступить.\n"
    kb, whisper = await get_main_menu_keyboard(user_id)
    text = f"<b><i>🎮 ГЛАВНОЕ МЕНЮ</i></b>\n{whisper}\n\n" + back
    await msg.reply_text(text, reply_markup=kb, parse_mode='HTML')

async def farm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if p and p[3]:
        last_farm = datetime.fromisoformat(p[3])
        if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
            remain = int((timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)).seconds / 60)
            await send_whisper_dm(update, context, f"🍬 <i>OAC копятся</i> 🌿\n\n<b>Подожди {remain} мин.</b>", life_seconds=10)
            return

    earned = random.randint(FARM_MIN, FARM_MAX)
    if p and p[12]:
        earned += int(earned * 0.05)
    if context.user_data.get("last_smoke_time") and \
       datetime.now() - context.user_data["last_smoke_time"] < timedelta(minutes=5):
        earned += random.randint(3, 5)

    if context.bot_data.get("happy_hour"):
        earned *= HAPPY_HOUR_MULTIPLIER

    if random.randint(1, 100) == 1:
        earned *= 10
        await send_whisper(context, "@guild_antysocial",
                           f"🌟 @{uname} наткнулся на <i>Золотую жилу</i>! +{earned} 🍬",
                           life_seconds=45)

    old_bal = p[0] if p else 0
    await update_balance(uid, uname, earned)
    await update_last_farm(uid)
    await increment_counter(uid, "farm_count")
    new_p = await get_player_cached(uid)
    new_bal = new_p[0]

    if new_p[14] == 1:
        await grant_title(uid, "🕯️", "Первый Шаг", context)
    if old_bal < 500 <= new_bal:
        await grant_title(uid, "✨", "Искра", context)

    progress = (f"⚔️ <b>До Ветерана:</b> {5000 - new_bal} OAC" if new_bal < 5000
                else f"🪦 <b>До Призрака:</b> {20000 - new_bal} OAC" if new_bal < 20000
                else "👑 Максимальный ранг")
    text = (f"💎 <i>Ты нафармил OAC:</i> <b>+{earned}</b> 🍬\n"
            f"⚜️ <i>У тебя:</i> {new_bal} 🍬\n\n"
            f"{progress}\n"
            f"🕯️ <i>Гильдия ждёт твоего триумфа.</i> 🌿")
    await send_whisper_dm(update, context, text, life_seconds=15)
    await check_rank_up(context, uid, uname, old_bal, new_bal)

async def craft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    bal = p[0] if p else 0

    # подменю крафта
    text = (f"<b><i>🌿 КРАФТ БЛАНТА</i></b>\n\n"
            f"🛡️ <i>у тебя:</i> <code>{bal}</code> 🍬")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌿 Обычный блант (15 🍬)", callback_data="craft_normal")],
        [InlineKeyboardButton("💍 Именной блант (50 🍬)", callback_data="craft_named")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')

async def handle_craft_normal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id; uname = query.from_user.username or query.from_user.first_name
    p = await get_player_cached(uid)
    bal = p[0] if p else 0
    if bal < 15:
        await send_whisper_dm(update, context, "🕳️ Пусто. Нужно <code>15</code> 🍬.", life_seconds=10)
        return
    await update_balance(uid, uname, -15)
    await update_blunts(uid, uname, 1)
    await increment_counter(uid, "craft_count")
    if random.random() < 0.05:
        await update_blunts(uid, uname, 1)
        await send_whisper(context, "@guild_antysocial",
                           f"⚡ @{uname} высек Искру Искажения из рутины. +1 🌿",
                           life_seconds=45)
    new_p = await get_player_cached(uid)
    await send_whisper_dm(update, context,
                          f"🌿 Ты свернул Блант. → 💰 {new_p[0]} | 🌿 {new_p[1]}",
                          life_seconds=15)

async def handle_craft_named(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p or p[0] < 50:
        await send_whisper_dm(update, context, "🕳️ Недостаточно OAC. Нужно <code>50</code> 🍬.", life_seconds=10)
        return
    context.user_data['awaiting_named_blunt'] = True
    text = ("<b><i>💍 ИМЕННОЙ БЛАНТ</i></b>\n\n"
            "<i>Введи имя своего бланта (до 25 символов)</i>\n\n"
            "[❌ Отмена]")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_named")]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

async def handle_named_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_named_blunt'):
        return
    user = update.effective_user
    uid = user.id
    name = update.message.text.strip()[:25]
    if not name:
        msg = await update.message.reply_text("❌ Имя не может быть пустым. Придумай что-то особенное.")
        context.job_queue.run_once(lambda c: c.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id), when=10)
        return
    # экранирование спецсимволов HTML
    name = name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    # сохранение в инвентарь
    async with aiosqlite.connect("players.db") as db:
        cur = await db.execute("SELECT inventory FROM players WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        inv = json.loads(row[0]) if row and row[0] else []
        # проверка на количество именных блантов для титулов (заглушка)
        named_count = sum(1 for item in inv if item.get("type") == "named")
        # создание объекта
        rare = random.random()
        if rare < 0.02: rarity = "legendary"; color = "🟡"
        elif rare < 0.1: rarity = "epic"; color = "🟣"
        elif rare < 0.35: rarity = "rare"; color = "🔵"
        else: rarity = "common"; color = "🟢"
        new_blunt = {
            "id": f"blunt_{int(datetime.now().timestamp())}",
            "name": name,
            "type": "named",
            "created_at": datetime.now().isoformat(),
            "rarity": rarity
        }
        inv.append(new_blunt)
        await db.execute("UPDATE players SET inventory=? WHERE user_id=?", (json.dumps(inv), uid))
        await db.commit()
    await update_balance(uid, user.username or user.first_name, -50)
    invalidate_cache(uid)
    context.user_data['awaiting_named_blunt'] = False
    text = (f"<b><i>💍 БЛАНТ СОТКАН</i></b>\n\n"
            f"Ты вплёл в <b>Искажение</b> свой именной блант:\n"
            f"<b>«{name}»</b> {color}\n\n"
            f"<i>Он навсегда останется в твоей коллекции.</i>")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 В меню", callback_data="menu")]])
    await update.message.reply_text(text, reply_markup=kb, parse_mode='HTML')

async def cancel_named(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_named_blunt'] = False
    # вернуть в подменю крафта
    await craft_callback(update, context)

async def smoke_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p or p[1] < 1:
        # показать экран с нулевым свёртком
        text = (f"<b><i>💨 ДУНУТЬ</i></b>\n\n"
                f"🌿 <i>свёрток пуст</i>")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="menu")]])
        await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
        return

    # экран выбора
    text = (f"<b><i>💨 ДУНУТЬ</i></b>\n\n"
            f"🌿 <i>блантов в свёртке:</i> <b>{p[1]}</b>")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Дунуть", callback_data="do_smoke")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')

async def do_smoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id; uname = query.from_user.username or query.from_user.first_name
    p = await get_player_cached(uid)
    if not p or p[1] < 1:
        await query.answer("Свёрток пуст.")
        return

    # сам процесс курения (старая логика)
    save = (p[2] == "WHITE" and random.randint(1, 100) <= 20)
    if not save:
        await update_blunts(uid, uname, -1)

    r = random.random()
    effect = ""
    if r <= 0.5:
        earned = random.randint(15, 40)
        if context.bot_data.get("happy_hour"):
            earned *= HAPPY_HOUR_MULTIPLIER
        await update_balance(uid, uname, earned)
        effect = f"💨 <i>Лёгкий приход</i>\n💡 «Станки Фабрики №9 работают в ритме твоего сердца...»\n\n🍬 <b>+{earned} OAC</b>"
    elif r <= 0.75:
        effect = "💨 <i>Паранойя...</i>\n💡 «Смотритель наблюдает...»\n✨ Никакого видимого эффекта."
    else:
        effect = "💨 <i>Плацебо</i>\n💡 «Дым рассеялся, ничего не изменилось...»"

    if p and not p[12]:
        await add_title(uid, "💨")
        async with aiosqlite.connect("players.db") as db:
            await db.execute("UPDATE players SET inhaled=1 WHERE user_id=?", (uid,))
            await db.commit()
        invalidate_cache(uid)
        effect += "\n\n<b><i>🎉 ТИТУЛ РАЗБЛОКИРОВАН!</i></b>\n💨 Ты теперь — <b>Красные Глаза</b>"

    context.user_data["last_smoke_time"] = datetime.now()
    await increment_counter(uid, "smoke_count")

    new_p = await get_player_cached(uid)
    bl_left = new_p[1] if new_p else 0
    text = (f"<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n"
            f"{effect}\n\n"
            f"🌿 <i>в свёртке осталось:</i> <b>{bl_left}</b>")
    if save:
        text += "\n⚜️ <i>Светлая Гильдия сохранила твой Блант!</i>"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Дунуть ещё", callback_data="do_smoke") if bl_left >= 1 else InlineKeyboardButton("🌿 Свёрток пуст", callback_data="noop")],
        [InlineKeyboardButton("🔙 В меню", callback_data="menu")]
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

async def ritual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await send_whisper_dm(update, context, "🕳️ Ты ещё не активирован. /start", life_seconds=10)
        return
    if p[2] != "BLACK":
        await send_whisper_dm(update, context, "❌ Только Тёмная Гильдия.", life_seconds=10)
        return
    if p[4]:
        last = datetime.fromisoformat(p[4])
        if datetime.now() - last < timedelta(hours=24):
            await send_whisper_dm(update, context, f"⏳ Жди {(timedelta(hours=24) - (datetime.now() - last)).seconds // 3600} ч.", life_seconds=10)
            return
    old_bal = p[0]
    reward = 150
    if context.bot_data.get("happy_hour"):
        reward *= HAPPY_HOUR_MULTIPLIER
    await update_balance(uid, uname, reward)
    await update_last_ritual(uid)
    await increment_counter(uid, "ritual_count")
    extra = 15 if random.random() < 0.1 else 0
    if extra:
        await update_balance(uid, uname, extra)
    new_bal = (await get_player_cached(uid))[0]
    text = (f"🕯️ <i>РИТУАЛ ЗАВЕРШЁН</i>\n"
            f"Ритуал принёс тебе <i>{reward} OAC</i> 🍬\n"
            f"⚜️ <i>У тебя: {new_bal}</i> 🍬\n\n"
            f"<i>«Тьма одарила тебя стабильностью»</i> 🌿")
    await send_whisper_dm(update, context, text, life_seconds=15)
    await check_rank_up(context, uid, uname, old_bal, new_bal)

async def collect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # оставлено без изменений (старая логика)
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await send_whisper_dm(update, context, "🕳️ Ты ещё не активирован. /start", life_seconds=10)
        return
    bal = p[0]
    if bal < 5000:
        await msg.reply_text(
            "🪴 <i>Выращивать кусты — привилегия Ветерана</i> 💎\n"
            f"⚔️ <i>До ранга Ветеран осталось:</i> {5000 - bal} / 5000 🍬",
            parse_mode='HTML'
        )
        return
    lvl = 3 if bal >= 20000 else 2
    pc = p[9]
    if pc:
        last = datetime.fromisoformat(pc) if isinstance(pc, str) else pc
        hrs = (datetime.now() - last).total_seconds() / 3600
        earned = int(hrs * 30 * lvl)
        if context.bot_data.get("happy_hour"):
            earned *= HAPPY_HOUR_MULTIPLIER
        if earned >= 1:
            await update_balance(uid, uname, earned)
            async with aiosqlite.connect("players.db") as db:
                await db.execute("UPDATE players SET passive_collected=? WHERE user_id=?", (datetime.now(), uid))
                await db.commit()
            invalidate_cache(uid)
            new_bal = (await get_player_cached(uid))[0]
            await send_whisper_dm(update, context,
                                  f"🪴 <i>УРОЖАЙ СОБРАН</i>\nТвой куст принёс <code>{earned}</code> 🍬.\n💰 <i>Баланс:</i> <code>{new_bal}</code> 🍬",
                                  life_seconds=15)
        else:
            await send_whisper_dm(update, context, "⏳ Пока нечего собирать.", life_seconds=10)
    else:
        async with aiosqlite.connect("players.db") as db:
            await db.execute("UPDATE players SET passive_collected=? WHERE user_id=?", (datetime.now(), uid))
            await db.commit()
        invalidate_cache(uid)
        await send_whisper_dm(update, context, "⏳ Авто‑сборщик активирован. Заходи через час.", life_seconds=10)

async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("Сначала активируйся: /start")
        return
    bal, bl, guild = p[0], p[1], p[2]
    rank_emoji, rank_name = "🪓", "Рекрут"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold:
            rank_emoji = emoji
            rank_name = emoji_to_name(emoji)
    if guild == "BLACK":
        g_emoji = " 🕯️ Тёмная"
    elif guild == "WHITE":
        g_emoji = " ⚜️ Светлая"
    else:
        g_emoji = ""
    titles = p[6] if p[6] else "—"
    neuro = random.choice(NEURO_STATUSES)
    text = (f"<b><i>⚜️ ПРОФИЛЬ</i></b>\n\n"
            f"👤 <b>{uname}</b>{g_emoji}\n\n"
            f"⚜️ <i>ранг:</i> {rank_emoji} <b>{rank_name}</b>\n"
            f"🛡️ <i>ОАС:</i> <b>{bal}</b> 🍬\n"
            f"🌿 <i>блантов в свёртке:</i> <b>{bl}</b>\n"
            f"🪴 <i>куст:</i> <b>+{30 * (3 if bal >= 20000 else 2 if bal >= 5000 else 0)}</b> 🍬/ч")

    # именные бланты
    inv = json.loads(p[19]) if len(p) > 19 and p[19] else []
    named = [item for item in inv if item.get("type") == "named"]
    if named:
        text += "\n\n💍 <b>именные бланты:</b>"
        for item in named:
            name = item["name"]
            rarity = item.get("rarity", "common")
            color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
            text += f"\n   {color} «{name}»"
    text += f"\n\n🧬 <i>титулы:</i> {titles}"
    text += f"\n🧠 <i>нейро-статус:</i> {neuro}"

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 В меню", callback_data="menu")]])
    await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

async def top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id
    top = await get_top(10)
    if not top:
        await msg.reply_text("🏆 Топ пока пуст.")
        return
    text = "<b>🏆 ТОП-10 ИГРОКОВ</b>\n\n"
    for i, (name, bal, guild) in enumerate(top, 1):
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        g = "🕯️" if guild=="BLACK" else "⚜️" if guild=="WHITE" else ""
        text += f"{medal} {name} {g} — {bal} 🍬\n"
    async with aiosqlite.connect("players.db") as db:
        async with db.execute("SELECT COUNT(*) FROM players WHERE balance > (SELECT balance FROM players WHERE user_id=?)", (uid,)) as cur:
            pos = (await cur.fetchone())[0] + 1
    text += f"\n📊 <i>Твоя позиция:</i> {pos}"
    await msg.reply_text(text, parse_mode='HTML', reply_markup=get_back_to_menu_keyboard())

async def guild_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id
    counts = await count_guilds()
    guild = await get_guild(uid)
    text = (f"<b>🕋 ГИЛЬДИИ</b>\n\n"
            f"🕯️ Тёмная: <code>{counts['BLACK']}</code> странников\n"
            f"⚜️ Светлая: <code>{counts['WHITE']}</code> странников\n\n"
            f"🕯️ Ритуал: <code>+150</code> 🍬 раз в 24 ч.\n"
            f"⚜️ Удача: <code>20%</code> сохранить Блант при 💨.")
    if guild:
        g_emoji = "🕯️" if guild == "BLACK" else "⚜️"
        g_name = "Тёмная" if guild == "BLACK" else "Светлая"
        text += f"\n\n✅ Ты состоишь в {g_emoji} <b>{g_name} Гильдии</b>."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📋 В меню", callback_data="menu")]])
    else:
        text += "\n\nТы пока не в Гильдии."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕯️ Вступить в Тёмную", callback_data="guild_join_BLACK"),
             InlineKeyboardButton("⚜️ Вступить в Светлую", callback_data="guild_join_WHITE")],
            [InlineKeyboardButton("📋 В меню", callback_data="menu")]
        ])
    try:
        await msg.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        await msg.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

async def rules_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    text = (
        "<b><i>📜 КОДЕКС ГИЛЬДИИ</i></b>\n\n"
        "<b>⚙️ Основные действия</b>\n"
        "🍬 <code>/farm</code> — добыча ОАС\n"
        "🌿 <code>/craft</code> — создание блантов\n"
        "💨 <code>/smoke</code> — дунуть блант\n"
        "🎲 <code>/luck</code> — раздел Удачи\n\n"
        "<b>💍 Именные бланты</b>\n"
        "💎 Создай свой <b><i>вечный именной</i></b> блант через меню «Крафт». "
        "Он не курится, получает редкость и навсегда остаётся в твоей коллекции. "
        "Показать свой блант в чат — через Профиль.\n\n"
        "<b>🕋 Гильдии и развитие</b>\n"
        "🕯️ Тёмная: <code>/ritual</code> (+150 🍬 раз в 24 ч)\n"
        "⚜️ Светлая: 20% шанс сохранить блант при 💨\n"
        "🪴 Куст: пассивный доход с ранга ⚔️ Ветеран\n"
        "🐾 Питомец: доступен с ранга ⚔️ Ветеран\n\n"
        "<b>ℹ️ Информация</b>\n"
        "⚜️ <code>/profile</code> — твой профиль и коллекция\n"
        "🏆 <code>/top</code> — список сильнейших\n\n"
        "<b>🛡️ Магазин (будущее)</b>\n"
        "<code>/privilege</code> — твоя скидка\n"
        "<code>/catalog</code> — ссылка на каталог\n\n"
        "<b><i>Ранг даёт власть. Гильдия даёт путь. Искажение награждает верных.</i></b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💍 Создать именной блант", callback_data="craft_named")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

async def privilege_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # оставлено без изменений (старый код)
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    bal = p[0]
    rank_emoji, rank_name = "🪓", "Рекрут"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold:
            rank_emoji = emoji
            rank_name = emoji_to_name(emoji)
    if bal >= 20000:
        percent = 100
        active = 10
    elif bal >= 5000:
        percent = min(100, int((bal - 5000) / (20000 - 5000) * 100))
        active = percent // 10
    else:
        percent = min(100, int(bal / 5000 * 100))
        active = percent // 10
    inactive = 10 - active
    progress_bar = "🟪" * active + "⬛️" * inactive
    quote = "🩸 <i>Кровь питает Искажение. Павшие дают скидку</i>"
    text = (f"<b>🪪 ТВОЯ СКИДКА</b>\n\n"
            f"⚜️ <b>РАНГ:</b> {rank_emoji} <b>{rank_name}</b>\n"
            f"💎 <b>OAC:</b> {bal}\n\n"
            f"🔮 <b><i>До след. уровня силы:</i></b>\n"
            f"{progress_bar} {percent}%\n\n"
            f"{quote}")
    await msg.reply_text(text, parse_mode='HTML', reply_markup=get_back_to_menu_keyboard())

async def catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Перейти", url="https://t.me/antysocialshop")]])
    await msg.reply_text("<b>🕯️ ANTYSOCIALSHOP · КАТАЛОГ</b>", parse_mode='HTML', reply_markup=kb)

async def luck_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str = None):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("Сначала активируйся: /start")
        return
    bal = p[0]
    now = datetime.now()

    # Колесо
    last_daily = p[5]
    wheel_available = not (last_daily and (now - datetime.fromisoformat(last_daily)) < timedelta(hours=24))
    # Бездна
    last_berserk = p[18] if len(p) > 18 else None
    berserk_available = (bal >= 300 and (not last_berserk or (now - datetime.fromisoformat(last_berserk)) > timedelta(hours=24)))

    text = f"<b><i>🎲 ИСПЫТАНИЕ СУДЬБЫ</i></b>\n\n🛡️ <i>ты держишь:</i> <code>{bal}</code> 🍬\n\n"
    kb_rows = []

    if wheel_available:
        kb_rows.append([InlineKeyboardButton("🎡 Крутить", callback_data="luck_wheel")])
    else:
        diff = timedelta(hours=24) - (now - datetime.fromisoformat(last_daily))
        hrs = int(diff.seconds // 3600)
        mins = int((diff.seconds % 3600) // 60)
        kb_rows.append([InlineKeyboardButton(f"🎡 {hrs} ч {mins} мин", callback_data="luck_wheel")])

    if berserk_available:
        kb_rows.append([InlineKeyboardButton("🎲 Рискнуть", callback_data="luck_berserk")])
    else:
        if bal < 300:
            kb_rows.append([InlineKeyboardButton(f"🎲 нужно ещё {300 - bal} 🍬", callback_data="luck_berserk")])
        else:
            diff = timedelta(hours=24) - (now - datetime.fromisoformat(last_berserk))
            hrs = int(diff.seconds // 3600)
            mins = int((diff.seconds % 3600) // 60)
            kb_rows.append([InlineKeyboardButton(f"🎲 {hrs} ч {mins} мин", callback_data="luck_berserk")])

    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)

    if action == "luck_wheel":
        if not wheel_available:
            await msg.edit_text("🎡 Колесо не готово. Возвращайся позже.", reply_markup=kb, parse_mode='HTML')
            return
        # сам розыгрыш (перенесено из daily)
        await update_last_daily(uid)
        r = random.random()
        if r <= 0.4:
            prize, txt = 30, f"+30 🍬"; await update_balance(uid, uname, prize)
        elif r <= 0.65:
            prize, txt = 75, f"+75 🍬"; await update_balance(uid, uname, prize)
        elif r <= 0.8:
            prize, txt = 1, "+1 🌿 Блант"; await update_blunts(uid, uname, prize)
        elif r <= 0.9:
            prize, txt = 150, f"+150 🍬"; await update_balance(uid, uname, prize)
        elif r <= 0.97:
            prize, txt = 2, "+2 🌿 Бланта"; await update_blunts(uid, uname, prize)
        else:
            prize = 1000; double = random.random() < 0.5
            if double:
                await update_balance(uid, uname, prize * 2)
                txt = f"🌟 <b><i>ДЖЕКПОТ!</i></b> <code>+2000</code> 🍬"
            else:
                await update_balance(uid, uname, prize)
                txt = f"🌟 <b><i>ДЖЕКПОТ!</i></b> <code>+1000</code> 🍬"
            await grant_title(uid, "🧛🏻‍♀️", "Призрачный Гончий", context)
            await context.bot.send_message(chat_id="@guild_antysocial",
                                           text=f"🌟 @{uname} сорвал Джекпот! {txt}", parse_mode='HTML')
        new_bal = (await get_player_cached(uid))[0]
        text = f"<b><i>🎲 КОЛЕСО СМОТРИТЕЛЯ</i></b>\n\n{txt} → 💰 {new_bal} 🍬"
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="luck")]]), parse_mode='HTML')
        return

    if action == "luck_berserk":
        if not berserk_available:
            await msg.edit_text("🎲 Бездна недоступна.", reply_markup=kb, parse_mode='HTML')
            return
        await update_last_berserk(uid)
        if random.random() < 0.6:
            await update_balance(uid, uname, 200)
            text = "🎲 Ткань благосклонна! Ты получаешь +200 🍬."
        else:
            await update_balance(uid, uname, -300)
            text = "🕳️ Ткань промолчала. -300 🍬. Завтра она может заговорить."
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="luck")]]), parse_mode='HTML')
        return

    await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = q.from_user.id
    try:
        if data == "menu":
            await q.answer()
            kb, whisper = await get_main_menu_keyboard(uid)
            text = f"<b><i>🎮 ГЛАВНОЕ МЕНЮ</i></b>\n{whisper}"
            await q.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
        elif data == "farm": await q.answer(); await farm_callback(update, context)
        elif data == "craft": await q.answer(); await craft_callback(update, context)
        elif data == "smoke": await q.answer(); await smoke_callback(update, context)
        elif data == "ritual": await q.answer(); await ritual_callback(update, context)
        elif data == "collect": await q.answer(); await collect_callback(update, context)
        elif data == "profile": await q.answer(); await profile_callback(update, context)
        elif data == "top": await q.answer(); await top_callback(update, context)
        elif data == "guild_info": await q.answer(); await guild_info_callback(update, context)
        elif data == "rules": await q.answer(); await rules_callback(update, context)
        elif data == "privilege": await q.answer(); await privilege_callback(update, context)
        elif data == "catalog": await q.answer(); await catalog_callback(update, context)
        elif data == "luck": await q.answer(); await luck_callback(update, context)
        elif data in ("luck_wheel", "luck_berserk"): await q.answer(); await luck_callback(update, context, action=data)
        elif data == "craft_normal": await q.answer(); await handle_craft_normal(update, context)
        elif data == "craft_named": await q.answer(); await handle_craft_named(update, context)
        elif data == "cancel_named": await q.answer(); await cancel_named(update, context)
        elif data == "do_smoke": await q.answer(); await do_smoke(update, context)
        elif data == "pet_preview": await q.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 ОАС)", show_alert=True)
        elif data == "bush_preview": await q.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 ОАС)", show_alert=True)
        elif data == "activate_menu": await q.answer(); context.args = ["activate"]; await start(update, context)
        elif data == "guild_join_BLACK" or data == "guild_join_WHITE":
            await q.answer()
            guild = "BLACK" if data == "guild_join_BLACK" else "WHITE"
            await set_guild(uid, guild)
            g_emoji = "🕯️" if guild=="BLACK" else "⚜️"
            g_name = "Тёмная" if guild=="BLACK" else "Светлая"
            uname = q.from_user.username or q.from_user.first_name
            await q.message.edit_text(
                f"<b><i>🎉 ГИЛЬДИЯ ПРИНЯЛА</i></b>\n"
                f"Ты теперь — {g_emoji} <b>{g_name} Гильдия</b> ·\n\n"
                f"<i>✅ Искажение стало плотнее...</i>",
                parse_mode='HTML'
            )
            await context.bot.send_message(
                chat_id="@guild_antysocial",
                text=f"🕋 @{uname} вплёл свою нить в {g_emoji} {g_name} Искажение."
            )
        else:
            await q.answer("Неизвестная команда.")
    except Exception as e:
        logger.error(f"Button error: {e}")

async def echo_of_distortion(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect("players.db") as db:
        cur = await db.execute("SELECT user_id, inventory FROM players WHERE inventory IS NOT NULL AND inventory != '[]'")
        rows = await cur.fetchall()
    all_named = []
    for user_id, inv_json in rows:
        try:
            inv = json.loads(inv_json)
            for item in inv:
                if item.get("type") == "named":
                    all_named.append((user_id, item))
        except:
            continue
    if not all_named:
        return
    sample = random.sample(all_named, min(3, len(all_named)))
    text = "🌀 <b>Эхо Искажения:</b>\n"
    for uid, item in sample:
        text += f"@{uid} создал блант <b>«{item['name']}»</b>\n"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💍 Создать свой блант", callback_data="craft_named")]])
    await context.bot.send_message(chat_id="@guild_antysocial", text=text, parse_mode='HTML', reply_markup=kb)

# === ЗАПУСК ===
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())
    Thread(target=run_web_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    for cmd, cbk in [
        ("start", start), ("farm", farm_callback), ("craft", craft_callback),
        ("smoke", smoke_callback), ("ritual", ritual_callback),
        ("profile", profile_callback), ("top", top_callback), ("rules", rules_callback),
        ("privilege", privilege_callback), ("catalog", catalog_callback),
        ("luck", luck_callback), ("collect", collect_callback),
        ("crystal", crystal_start), ("complete", crystal_complete), ("void", crystal_void)
    ]:
        app.add_handler(CommandHandler(cmd, cbk))

    app.add_handler(MessageHandler(filters.Regex(RE_FARM), farm_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_CRAFT), craft_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_SMOKE), smoke_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_RITUAL), ritual_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_TOP), top_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_LUCK), luck_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_PROFILE), profile_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_GUILD), guild_join_ru))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(RE_SHORTCUTS), handle_chat_shortcut))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_named_name))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(button_handler))

    job = app.job_queue
    job.run_repeating(update_pulse, interval=300, first=10)
    job.run_once(lambda c: job.run_repeating(happy_hour_trigger, interval=random.randint(14400, 28800),
                 first=random.randint(3600, 10800)), when=1)
    job.run_repeating(process_crystals, interval=86400, first=3600)
    job.run_daily(echo_of_distortion, time=datetime.time(hour=18, minute=0))

    now = datetime.now()
    days_until_saturday = (5 - now.weekday()) % 7
    next_saturday = (now + timedelta(days=days_until_saturday)).replace(hour=12, minute=0, second=0, microsecond=0)
    if next_saturday <= now:
        next_saturday += timedelta(days=7)
    first_seconds = max(1, (next_saturday - now).total_seconds())
    job.run_repeating(weekly_guild_rating, interval=7*24*3600, first=first_seconds)

    print("BOT READY")
    app.run_polling()
    loop.close()
