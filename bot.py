# bot.py — ANTY SOCIAL SHOP RPG v3.1 FINAL (полностью исправленная версия)
import asyncio, logging, os, random, re
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
from telegram.helpers import escape_markdown

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
RE_BALANCE = re.compile(r'^/баланс$')
RE_SMOKE = re.compile(r'^/дунуть$')
RE_STATUS = re.compile(r'^/статус$')
RE_TOP = re.compile(r'^/топ$')
RE_DAILY = re.compile(r'^/колесо$')
RE_PRIVILEGE = re.compile(r'^/привилегия$')
RE_CATALOG = re.compile(r'^/каталог$')
RE_CRAFT = re.compile(r'^/крафт$')
RE_GUILD = re.compile(r'^/guild$|^/вступить$')
RE_SHORTCUTS = re.compile(r'^(фарм|farm|дунуть|smoke|крафт|craft|баланс|balance|колесо|daily|топ|top|статус|status)$', re.IGNORECASE)

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
                last_berserk TIMESTAMP
            )
        """)
        cur = await db.execute("PRAGMA table_info(players)")
        columns = [row[1] for row in await cur.fetchall()]
        for col, def_type in [
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
            CREATE TABLE IF NOT EXISTS reservations (
                art TEXT PRIMARY KEY,
                user_id INTEGER,
                username TEXT,
                expires_at TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_weekly (
                guild TEXT PRIMARY KEY,
                total_farmed INTEGER DEFAULT 0,
                week_start DATE
            )
        """)
        # Инициализация гильдий для еженедельного рейтинга
        await db.execute("INSERT OR IGNORE INTO guild_weekly (guild, total_farmed, week_start) VALUES ('BLACK', 0, date('now'))")
        await db.execute("INSERT OR IGNORE INTO guild_weekly (guild, total_farmed, week_start) VALUES ('WHITE', 0, date('now'))")

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
            "ritual_count, referral_count, last_berserk FROM players WHERE user_id=?",
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
    ("💉 Рекрут", 0, 0),
    ("⚔️ Ветеран", 5000, 1500),
    ("👻 Призрак", 20000, 6000)
]

async def check_rank_up(context, user_id, username, old_balance, new_balance):
    for emoji, threshold, bonus in RANKS[1:]:
        if old_balance < threshold <= new_balance:
            if bonus:
                await update_balance(user_id, username, bonus)
            safe = escape_markdown(username, version=1)
            text = f"🎉 *_РАНГ ПОВЫШЕН!_*\n@{safe} теперь — {emoji} **{emoji_to_name(emoji)}**\n`+{bonus}` 🍬 закапало на баланс"
            try:
                await context.bot.send_message(chat_id="@guild_antysocial", text=text, parse_mode="Markdown")
            except Exception:
                pass

def emoji_to_name(emoji):
    for e, name, *_ in RANKS:
        if e == emoji:
            return name
    return ""

async def grant_title(user_id, emoji, name, context):
    await add_title(user_id, emoji)
    text = f"🎉 *_ТИТУЛ РАЗБЛОКИРОВАН!_*\n{emoji} Ты теперь — **{name}**"
    try:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
    except Exception:
        pass

# === НАСТРОЕНИЕ ТКАНИ ===
FABRIC_MOODS = [
    "🕸️ Ткань сегодня плотная и спокойная.",
    "🌀 Ткань дрожит от нетерпения.",
    "🌫️ Ткань едва видна в дыму.",
    "⚡ Ткань искрит электричеством.",
    "🕷️ Ткань липкая, как паутина."
]

# === ГЛАВНОЕ МЕНЮ ===
async def get_main_menu_keyboard(user_id):
    keyboard = [
        [InlineKeyboardButton("🍬 Фармить", callback_data="farm")],
        [InlineKeyboardButton("💰 Баланс", callback_data="balance"),
         InlineKeyboardButton("🌿 Крафт", callback_data="craft")],
        [InlineKeyboardButton("💨 Дунуть", callback_data="smoke")]
    ]
    player = await get_player_cached(user_id)
    if player:
        guild = await get_guild(user_id)
        if guild == "BLACK":
            keyboard.append([InlineKeyboardButton("🕯️ Ритуал", callback_data="ritual")])
        # Куст
        if player[0] >= 5000:
            keyboard.append([InlineKeyboardButton("🪴 Куст", callback_data="bush_menu")])
        else:
            keyboard.append([InlineKeyboardButton("🪴 Куст (⚔️ Ветеран)", callback_data="bush_preview")])
    keyboard.extend([
        [InlineKeyboardButton("📊 Статус", callback_data="status"),
         InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("🕋 Гильдии", callback_data="guild_info"),
         InlineKeyboardButton("📜 Законы", callback_data="rules")],
        [InlineKeyboardButton("🪪 Скидка", callback_data="privilege"),
         InlineKeyboardButton("📦 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🎡 Колесо", callback_data="daily")]
    ])
    return InlineKeyboardMarkup(keyboard)

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data="menu")]])

# === ВСПОМОГАТЕЛЬНЫЕ ===
def get_user_and_msg(update: Update):
    if update.callback_query:
        return update.callback_query.from_user, update.callback_query.message
    return update.effective_user, update.effective_message

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
async def send_whisper(context, chat_id, text, life_seconds=45):
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        context.job_queue.run_once(
            lambda c: c.bot.delete_message(chat_id, msg.message_id), when=life_seconds
        )
    except Exception as e:
        logger.error(f"Whisper error: {e}")

async def send_whisper_dm(update, context, text, life_seconds=15):
    try:
        user_id = update.effective_user.id
        msg = await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
        context.job_queue.run_once(
            lambda c: c.bot.delete_message(chat_id=user_id, message_id=msg.message_id), when=life_seconds
        )
    except Exception as e:
        logger.error(f"Whisper DM error: {e}")

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = await get_player_cached(user_id)
    effective_msg = update.effective_message

    if context.args and context.args[0] == "activate":
        if not player:
            await update_balance(user_id, username, 800)
            bonus = "🎁 Смотритель дарует тебе `800` 🍬.\n\n"
        else:
            bonus = ""
        if await get_guild(user_id):
            welcome = "🎉 *_Добро пожаловать обратно в Гильдию Antysocialshop!_*\n▸ Твоя Ткань натянута, странник.\n▸ Возвращайся к ритуалам."
            await effective_msg.reply_text(bonus + welcome, reply_markup=await get_main_menu_keyboard(user_id), parse_mode="Markdown")
            return
        welcome = (
            "🎉 *_Добро пожаловать в Гильдию Antysocialshop!_*\n\n"
            "🕯️ **Чёрная Гильдия** — стабильность, ритуалы, тёмное благословение.\n"
            "⚜️ **Белая Гильдия** — азарт, удача, танец на лезвии.\n\n"
            "▸ _Выбери свой путь:_"
        )
        guild_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕯️ Чёрная Гильдия", callback_data="guild_join_BLACK"),
             InlineKeyboardButton("⚜️ Белая Гильдия", callback_data="guild_join_WHITE")]
        ])
        await effective_msg.reply_text(bonus + welcome, reply_markup=guild_kb, parse_mode="Markdown")
        return

    if not player:
        await update_balance(user_id, username, 0)  # создаём запись
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ АКТИВИРОВАТЬ ТЕРМИНАЛ", callback_data="activate_menu")]])
        await effective_msg.reply_text(
            "👁‍🗨 *Смотритель заметил тебя.*\n"
            "🪄 *Ткань реальности ждёт твоего шага.*\n"
            "▸ Здесь добываются редкие экземпляры, зарабатывают Очки Антисошл (🍬), курят бланты и вступают в гильдии.\n"
            "🎁 Нажми, чтобы получить `800` 🍬 и войти в 🔒 закрытый сектор.",
            reply_markup=kb, parse_mode="Markdown"
        )
        return

    guild = await get_guild(user_id)
    back = "⚔️ *С возвращением в Гильдию!*\n\n"
    if guild == "BLACK": back += "🕯️ Ты состоишь в *Чёрной Гильдии*.\n"
    elif guild == "WHITE": back += "⚜️ Ты состоишь в *Белой Гильдии*.\n"
    else: back += "Ты пока не в Гильдии. Нажми /guild чтобы вступить.\n"
    back += "\n🎮 *Твой терминал:*"
    await effective_msg.reply_text(back, reply_markup=await get_main_menu_keyboard(user_id), parse_mode="Markdown")

async def farm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if p and p[3]:
        last_farm = datetime.fromisoformat(p[3])
        if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
            remain = int((timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)).seconds / 60)
            await send_whisper_dm(update, context, f"⏳ Жди {remain} мин.", life_seconds=10)
            return

    earned = random.randint(FARM_MIN, FARM_MAX)
    if p and p[12]:  # Красные Глаза
        earned += int(earned * 0.05)
    if context.user_data.get("last_smoke_time") and \
       datetime.now() - context.user_data["last_smoke_time"] < timedelta(minutes=5):
        earned += random.randint(3, 5)

    # Happy Hour
    if context.bot_data.get("happy_hour") and context.bot_data.get("happy_hour_end") and datetime.now() < context.bot_data["happy_hour_end"]:
        earned *= HAPPY_HOUR_MULTIPLIER

    if random.randint(1, 100) == 1:
        earned *= 10
        await send_whisper(context, "@guild_antysocial",
                           f"🌟 @{escape_markdown(uname, version=1)} наткнулся на *Золотую жилу*! +{earned} 🍬",
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

    progress = (f"📈 до ⚔️ Ветерана {5000 - new_bal} 🍬" if new_bal < 5000
                else f"📈 до 👻 Призрака {20000 - new_bal} 🍬" if new_bal < 20000
                else "👑 Максимальный ранг")
    text = f"🍬 +{earned} → 💰 {new_bal}\n{progress}"
    await send_whisper_dm(update, context, text, life_seconds=15)
    await check_rank_up(context, uid, uname, old_bal, new_bal)

async def balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid)
    bal, bl = (p[0], p[1]) if p else (0, 0)
    progress = (f"📈 до ⚔️ Ветерана {5000 - bal} 🍬" if bal < 5000
                else f"📈 до 👻 Призрака {20000 - bal} 🍬" if bal < 20000
                else "👑 Максимальный ранг")
    text = f"💰 *БАЛАНС*\n`{bal}` 🍬\n🌿 `{bl}` Бланта\n{progress}"

    can_berserk = False
    if p and p[0] >= 300:
        last_berserk = p[18] if len(p) > 18 else None
        if not last_berserk or (datetime.now() - datetime.fromisoformat(last_berserk)) > timedelta(hours=24):
            can_berserk = True

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Испытать Бездну (300 🍬)", callback_data="berserk")],
        [InlineKeyboardButton("📋 Меню", callback_data="menu")]
    ]) if can_berserk else get_back_to_menu_keyboard()

    await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def craft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    bal = p[0] if p else 0
    if bal < 15:
        await send_whisper_dm(update, context, "🕳️ Пусто. Нужно `15` 🍬.", life_seconds=10)
        return
    await update_balance(uid, uname, -15)
    await update_blunts(uid, uname, 1)
    await increment_counter(uid, "craft_count")
    if random.random() < 0.05:
        await update_blunts(uid, uname, 1)
        await send_whisper(context, "@guild_antysocial",
                           f"⚡ @{escape_markdown(uname, version=1)} высек Искру Ткани из кремня рутины. +1 🌿",
                           life_seconds=45)
    new_p = await get_player_cached(uid)
    await send_whisper_dm(update, context,
                          f"🌿 Ты свернул Блант. → 💰 {new_p[0]} | 🌿 {new_p[1]}",
                          life_seconds=15)

async def smoke_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p or p[1] < 1:
        await send_whisper_dm(update, context, "🌿 У тебя нет Блантов. /craft", life_seconds=10)
        return
    save = (p[2] == "WHITE" and random.randint(1, 100) <= 20)
    if not save:
        await update_blunts(uid, uname, -1)

    r = random.random()
    effect = ""
    if r <= 0.5:
        earned = random.randint(15, 40)
        await update_balance(uid, uname, earned)
        effect = f"💨 *Лёгкий приход*\n[Гул Фабрики №9] «Станки работают в ритме твоего сердца...»\n🍬 +{earned}"
    elif r <= 0.75:
        effect = "💨 *Паранойя...*\n[Зловещий шёпот] «Смотритель наблюдает...»\n✨ Никакого видимого эффекта."
    else:
        effect = "💨 *Плацебо*\n[Тишина] «Дым рассеялся, ничего не изменилось...»"

    if p and not p[12]:
        await add_title(uid, "💨")
        async with aiosqlite.connect("players.db") as db:
            await db.execute("UPDATE players SET inhaled=1 WHERE user_id=?", (uid,))
            await db.commit()
        invalidate_cache(uid)
        effect += "\n\n🎉 *_ТИТУЛ РАЗБЛОКИРОВАН!_*\n💨 Ты теперь — **Красные Глаза**"

    context.user_data["last_smoke_time"] = datetime.now()
    await increment_counter(uid, "smoke_count")

    new_bal = (await get_player_cached(uid))[0]
    text = effect + (f"\n💰 Баланс: `{new_bal}` 🍬" if r <= 0.5 else "")
    if save: text += "\n⚜️ *Белая Гильдия сохранила твой Блант!*"
    await send_whisper_dm(update, context, text, life_seconds=15)

async def ritual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await send_whisper_dm(update, context, "🕳️ Ты ещё не активирован. /start", life_seconds=10)
        return
    if p[2] != "BLACK":
        await send_whisper_dm(update, context, "❌ Только Чёрная Гильдия.", life_seconds=10)
        return
    if p[4]:
        last = datetime.fromisoformat(p[4])
        if datetime.now() - last < timedelta(hours=24):
            await send_whisper_dm(update, context, f"⏳ Жди {(timedelta(hours=24) - (datetime.now() - last)).seconds // 3600} ч.", life_seconds=10)
            return
    old_bal = p[0]
    await update_balance(uid, uname, 150)
    await update_last_ritual(uid)
    await increment_counter(uid, "ritual_count")
    extra = 15 if random.random() < 0.1 else 0
    if extra:
        await update_balance(uid, uname, extra)
    new_bal = (await get_player_cached(uid))[0]
    text = (f"🕯️ *РИТУАЛ ЗАВЕРШЁН*\n«Тьма одарила тебя стабильностью.»\n🍬 `+150` → 💰 {new_bal}"
            + ("\n🕯️ Ткань шепчет: «Ты избран»." if extra else ""))
    await send_whisper_dm(update, context, text, life_seconds=15)
    await check_rank_up(context, uid, uname, old_bal, new_bal)

async def collect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await send_whisper_dm(update, context, "🕳️ Ты ещё не активирован. /start", life_seconds=10)
        return
    bal = p[0]
    if bal < 5000:
        await send_whisper_dm(update, context,
            f"🪴 Выращивать кусты — привилегия Ветерана 💎\n⚔️ До ранга Ветеран осталось: {5000 - bal} / 5000 🍬",
            life_seconds=15)
        return
    lvl = 3 if bal >= 20000 else 2
    pc = p[9]
    if pc:
        last = datetime.fromisoformat(pc) if isinstance(pc, str) else pc
        hrs = (datetime.now() - last).total_seconds() / 3600
        earned = int(hrs * 30 * lvl)
        if earned >= 1:
            await update_balance(uid, uname, earned)
            async with aiosqlite.connect("players.db") as db:
                await db.execute("UPDATE players SET passive_collected=? WHERE user_id=?", (datetime.now(), uid))
                await db.commit()
            invalidate_cache(uid)
            new_bal = (await get_player_cached(uid))[0]
            await send_whisper_dm(update, context,
                                  f"🪴 *УРОЖАЙ СОБРАН*\nТвой куст принёс `{earned}` 🍬.\n💰 *Баланс:* `{new_bal}` 🍬",
                                  life_seconds=15)
        else:
            await send_whisper_dm(update, context, "⏳ Пока нечего собирать.", life_seconds=10)
    else:
        async with aiosqlite.connect("players.db") as db:
            await db.execute("UPDATE players SET passive_collected=? WHERE user_id=?", (datetime.now(), uid))
            await db.commit()
        invalidate_cache(uid)
        await send_whisper_dm(update, context, "⏳ Авто‑сборщик активирован. Заходи через час.", life_seconds=10)

async def bush_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p or p[0] < 5000:
        await query.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 ОАС)", show_alert=True)
        return
    lvl = 3 if p[0] >= 20000 else 2
    income = 30 * lvl
    pc = p[9]
    next_collect = ""
    can_collect = False
    if pc:
        last = datetime.fromisoformat(pc) if isinstance(pc, str) else pc
        elapsed = datetime.now() - last
        if elapsed.total_seconds() / 3600 >= 1:
            can_collect = True
        else:
            remain = int((timedelta(hours=1) - elapsed).total_seconds() / 60)
            next_collect = f"\n⏳ Сбор через {remain} мин."
    else:
        next_collect = "\n⏳ Авто‑сборщик активирован. Заходи через час."

    text = (f"🪴 *ТВОЙ КУСТ*\n"
            f"Уровень: `{lvl}`\n"
            f"Доход: `+{income}` 🍬 / час"
            f"{next_collect}")
    buttons = []
    if can_collect:
        buttons.append([InlineKeyboardButton("🪴 Собрать урожай", callback_data="collect")])
    buttons.append([InlineKeyboardButton("📋 В меню", callback_data="menu")])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("Сначала активируйся: /start")
        return
    bal, bl, guild = p[0], p[1], p[2]
    rank_emoji, rank_name = "💉 Рекрут", ""
    for emoji, threshold, _ in RANKS:
        if bal >= threshold:
            rank_emoji = emoji
            rank_name = emoji_to_name(emoji)
    g_emoji = " 🕯️" if guild == "BLACK" else " ⚜️" if guild == "WHITE" else ""
    titles = p[6] if p[6] else "—"
    neuro = random.choice([
        "Альфа-ритмы нестабильны", "Сенсорная депривация 80%",
        "Фаза быстрого сна", "Нейро-шунт активен",
        "Предел синаптической проводимости"
    ])
    mood = random.choice(FABRIC_MOODS)
    text = (f"👤 *{escape_markdown(uname, version=1)}*{g_emoji}\n"
            f"👻 *Ранг:* {rank_emoji} **{escape_markdown(rank_name, version=1)}**\n"
            f"💰 *ОАС:* `{bal}` 🍬\n"
            f"🌿 *Бланты:* `{bl}`\n"
            f"🧬 *Титулы:* {titles}\n"
            f"🧠 *Нейро-статус:* _{neuro}_\n"
            f"🪴 *Куст:* `+{30 * (3 if bal >= 20000 else 2 if bal >= 5000 else 0)}` 🍬 / час | /collect чтобы собрать\n"
            f"{mood}")
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())

async def top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id
    top = await get_top(10)
    if not top:
        await msg.reply_text("🏆 Топ пока пуст.")
        return
    text = "🏆 *ТОП-10 ИГРОКОВ*\n\n"
    for i, (name, bal, guild) in enumerate(top, 1):
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        g = "🕯️" if guild=="BLACK" else "⚜️" if guild=="WHITE" else ""
        text += f"{medal} {escape_markdown(name or '---', version=1)} {g} — `{bal}` 🍬\n"
    async with aiosqlite.connect("players.db") as db:
        async with db.execute("SELECT COUNT(*) FROM players WHERE balance > (SELECT balance FROM players WHERE user_id=?)", (uid,)) as cur:
            pos = (await cur.fetchone())[0] + 1
    text += f"\n📊 *Твоя позиция:* `{pos}`"
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())

async def guild_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    counts = await count_guilds()
    guild = await get_guild(uid)
    text = (f"🕋 *ГИЛЬДИИ*\n\n"
            f"🕯️ Чёрная: `{counts['BLACK']}` странников\n"
            f"⚜️ Белая: `{counts['WHITE']}` странников\n\n"
            f"🕯️ Ритуал: `+150` 🍬 раз в 24 ч.\n"
            f"⚜️ Удача: `20%` сохранить Блант при 💨.")
    if guild:
        g_emoji = "🕯️" if guild == "BLACK" else "⚜️"
        g_name = "Чёрная" if guild == "BLACK" else "Белая"
        text += f"\n\n✅ Ты состоишь в {g_emoji} *{g_name} Гильдии*."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📋 В меню", callback_data="menu")]])
    else:
        text += "\n\nТы пока не в Гильдии."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕯️ Вступить в Чёрную", callback_data="guild_join_BLACK"),
             InlineKeyboardButton("⚜️ Вступить в Белую", callback_data="guild_join_WHITE")],
            [InlineKeyboardButton("📋 В меню", callback_data="menu")]
        ])
    await query.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def rules_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("📜 **ЗАКОНЫ ГИЛЬДИИ**\n\n"
            "🍬 Фарми.  💨 Дуй.  🪴 Расти.\n\n"
            "▸ /farm — добыча 🍬 (раз в 30 мин)\n"
            "▸ /craft — `15` 🍬 = 1 🌿 Блант\n"
            "▸ /smoke — активация 🌿\n"
            "▸ /daily — 🎡 Колесо\n"
            "▸ /privilege — твоя скидка\n"
            "▸ /collect — сбор урожая (⚔️ Ветеран+)\n\n"
            "▸ _Ранг даёт власть._\n"
            "▸ _Гильдия даёт путь._")
    await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())

async def privilege_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    bal, guild = p[0], p[2]
    if bal >= 20000: rank, div, maxp, target = "👻 Призрак", 30, 0.20, None
    elif bal >= 5000: rank, div, maxp, target = "⚔️ Ветеран", 45, 0.15, 20000
    else: rank, div, maxp, target = "💉 Рекрут", 60, 0.10, 5000
    note = "🎲 Шанс 20% не потратить ОАС" if guild == "WHITE" else "🔒 Стабильно"
    text = (f"🪪 *ТВОЯ СКИДКА*\n\n{rank} {guild or 'Нет'}\n💰 `{bal}` 🍬\n\n"
            f"💸 Каждые `{div}` 🍬 = `1` ₽ скидки\n"
            f"📉 Максимум: `{int(maxp*100)}%` от цены\n{note}\n")
    if target:
        perc = min(100, int(bal / target * 100))
        bar = "🟩" * (perc//10) + "⬛" * (10 - perc//10)
        text += f"\n⚔️ *Прогресс:* {bar} `{perc}%`\n"
        phrase = ("«Ты слышишь шёпот Фабрики...»" if perc<30
                  else "«Ткань реальности отзывается...»" if perc<70
                  else "«Смотритель чувствует твоё приближение...»")
        text += f"👁‍🗨 _{phrase}_"
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())

async def catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Перейти", url="https://t.me/antysocialshop")]])
    await update.effective_message.reply_text("🕯️ *ANTYSOCIALSHOP · КАТАЛОГ*", parse_mode="Markdown", reply_markup=kb)

async def daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await send_whisper_dm(update, context, "🕳️ Ты ещё не активирован. /start", life_seconds=10)
        return
    if p[5]:
        last = datetime.fromisoformat(p[5])
        if datetime.now() - last < timedelta(hours=24):
            await send_whisper_dm(update, context,
                                  f"⏳ Колесо спит. Жди {(timedelta(hours=24) - (datetime.now() - last)).seconds // 3600} ч.",
                                  life_seconds=10)
            return
    r = random.random()
    if r <= 0.4: prize, txt = 30, "+30 🍬"; await update_balance(uid, uname, prize)
    elif r <= 0.65: prize, txt = 75, "+75 🍬"; await update_balance(uid, uname, prize)
    elif r <= 0.8: prize, txt = 1, "+1 🌿 Блант"; await update_blunts(uid, uname, prize)
    elif r <= 0.9: prize, txt = 150, "+150 🍬"; await update_balance(uid, uname, prize)
    elif r <= 0.97: prize, txt = 2, "+2 🌿 Бланта"; await update_blunts(uid, uname, prize)
    else:
        prize = 1000
        double = random.random() < 0.5
        if double:
            await update_balance(uid, uname, prize * 2)
            txt = f"🌟 *_ДЖЕКПОТ!_* `+2000` 🍬 → 💰 {(await get_player_cached(uid))[0]} 🍬\n🧛🏻‍♀️ Титул: **Призрачный Гончий**"
        else:
            await update_balance(uid, uname, prize)
            txt = f"🌟 *_ДЖЕКПОТ!_* `+1000` 🍬 → 💰 {(await get_player_cached(uid))[0]} 🍬\n🧛🏻‍♀️ Титул: **Призрачный Гончий**"
        await grant_title(uid, "🧛🏻‍♀️", "Призрачный Гончий", context)
        try:
            await context.bot.send_message(chat_id="@guild_antysocial",
                                           text=f"🌟 @{escape_markdown(uname, version=1)} сорвал Джекпот! {txt}", parse_mode="Markdown")
        except Exception:
            pass
        await update_last_daily(uid)
        return
    await update_last_daily(uid)
    new_bal = (await get_player_cached(uid))[0]
    await send_whisper_dm(update, context,
                          f"🎡 *КОЛЕСО СМОТРИТЕЛЯ*\n{txt} → 💰 {new_bal} 🍬",
                          life_seconds=15)

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        safe_name = escape_markdown(member.username or member.first_name, version=1)
        await update.message.reply_text(
            f"🕯️ @{safe_name}, добро пожаловать в Гильдию. Твой первый /farm уже ждёт."
        )

async def berserk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    uname = query.from_user.username or query.from_user.first_name
    p = await get_player_cached(uid)
    if not p or p[0] < 300:
        await query.answer("❌ Недостаточно ОАС", show_alert=True)
        return
    last_berserk = p[18] if len(p) > 18 else None
    if last_berserk and (datetime.now() - datetime.fromisoformat(last_berserk)) < timedelta(hours=24):
        await query.answer("⏳ Ты уже испытывал Бездну сегодня. Возвращайся завтра.", show_alert=True)
        return

    await update_last_berserk(uid)
    if random.random() < 0.6:
        await update_balance(uid, uname, 200)
        await query.message.edit_text("🎲 Ткань благосклонна! Ты получаешь +200 🍬.")
    else:
        await update_balance(uid, uname, -300)
        await query.message.edit_text("🕳️ Ткань промолчала. -300 🍬. Завтра она может заговорить.")

# === КРИСТАЛЛ ===
async def crystal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text("Формат: /crystal @username сумма описание")
        return
    username_arg = context.args[0]
    try:
        amount_rub = int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text("Сумма должна быть числом (рубли).")
        return
    description = " ".join(context.args[2:]) if len(context.args) > 2 else "без описания"
    if username_arg.startswith("@"):
        username_arg = username_arg[1:]
    target_user_id = None
    target_username = None
    async with aiosqlite.connect("players.db") as db:
        cur = await db.execute("SELECT user_id, username FROM players WHERE username=?", (username_arg,))
        row = await cur.fetchone()
        if row:
            target_user_id = row[0]
            target_username = row[1]
        else:
            await update.effective_message.reply_text("❌ Пользователь не найден или не активировал бота.")
            return

    daily_oas = int(amount_rub * 0.01 * 10)
    async with aiosqlite.connect("players.db") as db:
        await db.execute(
            "INSERT INTO crystals (user_id, username, description, amount_rub, daily_oas, start_date) VALUES (?, ?, ?, ?, ?, ?)",
            (target_user_id, target_username, description, amount_rub, daily_oas, datetime.now())
        )
        await db.commit()
    safe_username = escape_markdown(target_username, version=1)
    safe_desc = escape_markdown(description, version=1)
    try:
        await context.bot.send_message(chat_id=target_user_id, text=(
            f"💎 *КРИСТАЛЛ АКТИВИРОВАН*\n\n"
            f"Заказ «{safe_desc}» на {amount_rub} ₽\n"
            f"Каждый день ты будешь получать `+{daily_oas}` ОАС, пока товар в пути.\n\n"
            f"🕯️ _Если отменишь заказ, Кристалл разобьётся, и все накопленные ОАС сгорят._"
        ), parse_mode="Markdown")
    except Exception:
        pass
    await update.effective_message.reply_text(f"✅ Кристалл для @{safe_username} запущен.")

async def crystal_complete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not update.message or not update.message.reply_to_message:
        return
    target_user = update.message.reply_to_message.from_user
    async with aiosqlite.connect("players.db") as db:
        cur = await db.execute(
            "SELECT id, description, total_earned FROM crystals WHERE user_id=? AND cancelled=0 AND completed=0 ORDER BY start_date DESC LIMIT 1",
            (target_user.id,)
        )
        row = await cur.fetchone()
        if not row:
            await update.effective_message.reply_text("❌ Нет активного Кристалла.")
            return
        crystal_id, desc, earned = row
        await db.execute("UPDATE crystals SET completed=1 WHERE id=?", (crystal_id,))
        await db.commit()
    await grant_title(target_user.id, "💎", "Кристальный", context)
    try:
        await context.bot.send_message(chat_id=target_user.id, text=(
            f"💎 *КРИСТАЛЛ РАСКРЫЛСЯ*\n"
            f"Заказ «{desc}» получен.\n"
            f"Ты сохранил `{earned}` ОАС.\n"
            f"🧬 Титул: **Кристальный**"
        ), parse_mode="Markdown")
    except Exception:
        pass
    await update.effective_message.reply_text("✅ Кристалл завершён.")

async def crystal_void(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not update.message or not update.message.reply_to_message:
        return
    target_user = update.message.reply_to_message.from_user
    async with aiosqlite.connect("players.db") as db:
        cur = await db.execute(
            "SELECT id, description, total_earned FROM crystals WHERE user_id=? AND cancelled=0 AND completed=0 ORDER BY start_date DESC LIMIT 1",
            (target_user.id,)
        )
        row = await cur.fetchone()
        if not row:
            await update.effective_message.reply_text("❌ Нет активного Кристалла.")
            return
        crystal_id, desc, earned = row
        await db.execute("UPDATE players SET balance=MAX(0, balance-?) WHERE user_id=?", (earned, target_user.id))
        await db.execute("UPDATE crystals SET cancelled=1 WHERE id=?", (crystal_id,))
        await db.commit()
    try:
        await context.bot.send_message(chat_id=target_user.id, text=(
            f"💎 *КРИСТАЛЛ РАЗБИТ*\n"
            f"Заказ «{desc}» отменён.\n"
            f"`{earned}` ОАС сгорело."
        ), parse_mode="Markdown")
    except Exception:
        pass
    await update.effective_message.reply_text("💔 Кристалл отменён.")

async def process_crystals(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect("players.db") as db:
        cur = await db.execute(
            "SELECT id, user_id, username, daily_oas, total_earned FROM crystals WHERE cancelled=0 AND completed=0"
        )
        rows = await cur.fetchall()
        for crystal_id, uid, uname, daily, earned in rows:
            new_earned = earned + daily
            await db.execute("UPDATE crystals SET total_earned=? WHERE id=?", (new_earned, crystal_id))
            await db.execute("INSERT OR IGNORE INTO players (user_id, username, balance, blunts) VALUES (?, ?, 0, 0)", (uid, uname))
            await db.execute("UPDATE players SET balance=balance+? WHERE user_id=?", (daily, uid))
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"💎 Кристалл вырос на `+{daily}` ОАС. Накоплено: `{new_earned}` ОАС."
                )
            except Exception:
                pass
        await db.commit()

# === КОЛБЭК КНОПОК ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = q.from_user.id
    try:
        if data == "menu":
            await q.answer()
            await q.message.edit_text("🎮 *ГЛАВНОЕ МЕНЮ*", reply_markup=await get_main_menu_keyboard(uid), parse_mode="Markdown")
        elif data == "farm":
            await q.answer()
            await farm_callback(update, context)
        elif data == "balance":
            await q.answer()
            await balance_callback(update, context)
        elif data == "craft":
            await q.answer()
            await craft_callback(update, context)
        elif data == "smoke":
            await q.answer()
            await smoke_callback(update, context)
        elif data == "ritual":
            await q.answer()
            await ritual_callback(update, context)
        elif data == "collect":
            await q.answer()
            await collect_callback(update, context)
        elif data == "status":
            await q.answer()
            await status_callback(update, context)
        elif data == "top":
            await q.answer()
            await top_callback(update, context)
        elif data == "guild_info":
            await q.answer()
            await guild_info_callback(update, context)
        elif data == "rules":
            await q.answer()
            await rules_callback(update, context)
        elif data == "privilege":
            await q.answer()
            await privilege_callback(update, context)
        elif data == "catalog":
            await q.answer()
            await catalog_callback(update, context)
        elif data == "daily":
            await q.answer()
            await daily_callback(update, context)
        elif data == "berserk":
            await q.answer()
            await berserk_callback(update, context)
        elif data == "bush_preview":
            await q.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 ОАС)", show_alert=True)
        elif data == "bush_menu":
            await q.answer()
            await bush_menu_callback(update, context)
        elif data in ("guild_join_BLACK", "guild_join_WHITE"):
            await q.answer()
            guild = "BLACK" if data == "guild_join_BLACK" else "WHITE"
            await set_guild(uid, guild)
            g_emoji = "🕯️" if guild=="BLACK" else "⚜️"
            g_name = "Чёрная" if guild=="BLACK" else "Белая"
            safe_uname = escape_markdown(q.from_user.username or q.from_user.first_name, version=1)
            await q.message.edit_text(
                f"🎉 *_ГИЛЬДИЯ ПРИНЯЛА_*\nТы теперь — {g_emoji} **{g_name} Гильдия** ·\n✅ Ткань стала плотнее...",
                parse_mode="Markdown"
            )
            try:
                await context.bot.send_message(
                    chat_id="@guild_antysocial",
                    text=f"🕋 @{safe_uname} вплёл свою нить в {g_emoji} {g_name} Ткань. Реальность стала плотнее."
                )
            except Exception:
                pass
        elif data == "activate_menu":
            await q.answer()
            user = q.from_user
            uid_local = user.id
            username_local = user.username or user.first_name
            player_local = await get_player_cached(uid_local)
            if not player_local:
                await update_balance(uid_local, username_local, 800)
            bonus = "🎁 Смотритель дарует тебе `800` 🍬.\n\n" if not player_local else ""
            welcome = (
                "🎉 *_Добро пожаловать в Гильдию Antysocialshop!_*\n\n"
                "🕯️ **Чёрная Гильдия** — стабильность, ритуалы, тёмное благословение.\n"
                "⚜️ **Белая Гильдия** — азарт, удача, танец на лезвии.\n\n"
                "▸ _Выбери свой путь:_"
            )
            guild_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🕯️ Чёрная Гильдия", callback_data="guild_join_BLACK"),
                 InlineKeyboardButton("⚜️ Белая Гильдия", callback_data="guild_join_WHITE")]
            ])
            await q.message.edit_text(bonus + welcome, reply_markup=guild_kb, parse_mode="Markdown")
        elif data == "activate_terminal":
            await q.answer()
            user = q.from_user
            uid_local = user.id
            username_local = user.username or user.first_name
            player_local = await get_player_cached(uid_local)
            if not player_local:
                await update_balance(uid_local, username_local, 800)
            bonus = "🎁 Смотритель дарует тебе `800` 🍬.\n\n" if not player_local else ""
            welcome = (
                "🎉 *_Добро пожаловать в Гильдию Antysocialshop!_*\n\n"
                "🕯️ **Чёрная Гильдия** — стабильность, ритуалы, тёмное благословение.\n"
                "⚜️ **Белая Гильдия** — азарт, удача, танец на лезвии.\n\n"
                "▸ _Выбери свой путь:_"
            )
            guild_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🕯️ Чёрная Гильдия", callback_data="guild_join_BLACK"),
                 InlineKeyboardButton("⚜️ Белая Гильдия", callback_data="guild_join_WHITE")]
            ])
            await q.message.edit_text(bonus + welcome, reply_markup=guild_kb, parse_mode="Markdown")
        else:
            await q.answer()
            await q.message.edit_text("❓ Неизвестная команда.")
    except Exception as e:
        logger.error(f"Button error: {e}")

async def guild_join_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await get_guild(user_id):
        await update.effective_message.reply_text("❌ Ты уже состоишь в Гильдии.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🕯️ Чёрная", callback_data="guild_join_BLACK"),
         InlineKeyboardButton("⚜️ Белая", callback_data="guild_join_WHITE")]
    ])
    await update.effective_message.reply_text("🕋 Выбери свою Гильдию, Странник:", reply_markup=keyboard)

async def handle_chat_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    mapping = {
        "фарм": farm_callback, "farm": farm_callback,
        "дунуть": smoke_callback, "smoke": smoke_callback,
        "крафт": craft_callback, "craft": craft_callback,
        "баланс": balance_callback, "balance": balance_callback,
        "колесо": daily_callback, "daily": daily_callback,
        "топ": top_callback, "top": top_callback,
        "статус": status_callback, "status": status_callback
    }
    if text in mapping:
        await mapping[text](update, context)

# === СЛУЖЕБНЫЕ ЗАДАЧИ ===
async def update_pulse(context):
    try:
        async with aiosqlite.connect("players.db") as db:
            cur = await db.execute("SELECT COUNT(*), SUM(balance) FROM players")
            total_players, total_oas = await cur.fetchone()
            cur = await db.execute("SELECT COUNT(*) FROM players WHERE guild='BLACK'")
            black = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM players WHERE guild='WHITE'")
            white = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM players WHERE last_farm > ?",
                                   (datetime.now() - timedelta(hours=1),))
            online = (await cur.fetchone())[0]
        desc = f"🕯️{black} ▰▱⚜️{white} | 👥{online}"
        await context.bot.set_chat_description(chat_id="@guild_antysocial", description=desc)
    except Exception as e:
        logger.error(f"Pulse error: {e}")

async def happy_hour_trigger(context):
    context.bot_data["happy_hour"] = True
    context.bot_data["happy_hour_end"] = datetime.now() + timedelta(minutes=HAPPY_HOUR_DURATION_MIN)
    try:
        await context.bot.send_message(chat_id="@guild_antysocial",
                                       text="🌟 *ЧАС УДАЧИ!* Все действия приносят x2 🍬 30 минут!",
                                       parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Happy hour message error: {e}")
    context.job_queue.run_once(reset_happy_hour, HAPPY_HOUR_DURATION_MIN*60)

async def reset_happy_hour(context):
    context.bot_data["happy_hour"] = False
    try:
        await context.bot.send_message(chat_id="@guild_antysocial", text="⏳ Час Удачи завершён.")
    except Exception:
        pass

async def cleanup_expired_reservations(context):
    async with aiosqlite.connect("players.db") as db:
        await db.execute("DELETE FROM reservations WHERE expires_at < ?", (datetime.now(),))
        await db.commit()

# === КРУГ СМОТРИТЕЛЯ ===
async def weekly_guild_rating(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect("players.db") as db:
        await db.execute("UPDATE guild_weekly SET total_farmed=0")
        await db.execute("""
            UPDATE guild_weekly SET total_farmed = (
                SELECT COALESCE(SUM(balance),0) FROM players WHERE guild = guild_weekly.guild
            )
        """)
        await db.execute("UPDATE guild_weekly SET week_start=?", (date.today(),))
        await db.commit()
        cur = await db.execute("SELECT guild, total_farmed FROM guild_weekly")
        rows = await cur.fetchall()
    if len(rows) >= 2:
        black = next((r[1] for r in rows if r[0]=="BLACK"), 0)
        white = next((r[1] for r in rows if r[0]=="WHITE"), 0)
        winner = "BLACK" if black > white else "WHITE"
        wrd = "ритуалу" if winner=="BLACK" else "сохранению бланта"
        try:
            await context.bot.send_message(
                chat_id="@guild_antysocial",
                text=f"🎉 *_КРУГ СОБРАН_*\nТвоя гильдия под благословением: `+5%` к {wrd} на неделю",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Weekly rating message error: {e}")

# === ЗАПУСК ===
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())
    Thread(target=run_web_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    for cmd, cbk in [("start", start), ("farm", farm_callback), ("balance", balance_callback),
                     ("craft", craft_callback), ("smoke", smoke_callback), ("ritual", ritual_callback),
                     ("status", status_callback), ("top", top_callback), ("rules", rules_callback),
                     ("privilege", privilege_callback), ("catalog", catalog_callback), ("daily", daily_callback),
                     ("collect", collect_callback), ("crystal", crystal_start), ("complete", crystal_complete),
                     ("void", crystal_void)]:
        app.add_handler(CommandHandler(cmd, cbk))

    app.add_handler(MessageHandler(filters.Regex(RE_FARM), farm_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_BALANCE), balance_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_CRAFT), craft_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_SMOKE), smoke_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_RITUAL), ritual_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_STATUS), status_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_TOP), top_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_DAILY), daily_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_PRIVILEGE), privilege_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_CATALOG), catalog_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_GUILD), guild_join_ru))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(RE_SHORTCUTS), handle_chat_shortcut))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(button_handler))

    job = app.job_queue
    job.run_repeating(update_pulse, interval=300, first=10)
    job.run_once(lambda c: job.run_repeating(happy_hour_trigger, interval=random.randint(14400, 28800),
                 first=random.randint(3600, 10800)), when=1)
    job.run_repeating(cleanup_expired_reservations, interval=3600, first=60)
    job.run_repeating(process_crystals, interval=86400, first=3600)

    now = datetime.now()
    days_until_saturday = (5 - now.weekday()) % 7
    next_saturday = (now + timedelta(days=days_until_saturday)).replace(hour=12, minute=0, second=0, microsecond=0)
    first_seconds = max(1, (next_saturday - now).total_seconds())
    job.run_repeating(weekly_guild_rating, interval=7*24*3600, first=first_seconds)

    print("BOT READY")
    app.run_polling()
    loop.close()
