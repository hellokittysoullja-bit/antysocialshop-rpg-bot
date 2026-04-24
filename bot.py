# bot.py — ANTY SOCIAL SHOP RPG v3.0 (финальный прод-код, исправлен)
import asyncio, logging, os, random, re, time
from datetime import datetime, timedelta, date
from threading import Thread
from functools import wraps

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
FARM_MIN, FARM_MAX = 45, 100          # оптимальный диапазон
HAPPY_HOUR_MULTIPLIER = 2
HAPPY_HOUR_DURATION_MIN = 30

# === КЭШ ИГРОКОВ ===
player_cache = TTLCache(maxsize=500, ttl=30)

def invalidate_cache(user_id):
    player_cache.pop(user_id, None)

# === КОМПИЛИРОВАННЫЕ РЕГУЛЯРКИ (РУССКИЕ КОМАНДЫ) ===
RE_RITUAL = re.compile(r'^/ритуал$')
RE_FARM = re.compile(r'^/фарм$')
RE_BALANCE = re.compile(r'^/баланс$')
RE_SMOKE = re.compile(r'^/дунуть$')
RE_STATUS = re.compile(r'^/статус$')
RE_TOP = re.compile(r'^/топ$')
RE_DAILY = re.compile(r'^/колесо$')
RE_PRIVILEGE = re.compile(r'^/привилегия$')
RE_CATALOG = re.compile(r'^/каталог$')
RE_CRAFT = re.compile(r'^/крафт$')  # <-- исправлено
RE_GUILD = re.compile(r'^/guild$|^/вступить$')  # чтобы работала команда вступления

# Быстрые сообщения без "/" (только в группах)
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
    for emoji, threshold, bonus in RANKS[1:]:  # пропускаем Рекрута
        if old_balance < threshold <= new_balance:
            if bonus:
                await update_balance(user_id, username, bonus)
            text = f"🎉 *_РАНГ ПОВЫШЕН!_*\n@{username} теперь — {emoji} **{emoji_to_name(emoji)}**\n`+{bonus}` 🍬 закапало на баланс"
            await context.bot.send_message(chat_id="@guild_antysocial", text=text, parse_mode="Markdown")

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
        # Куст для Ветеранов (>= 5000)
        if player[0] >= 5000:
            pc = player[9]
            if pc:
                last = datetime.fromisoformat(pc) if isinstance(pc, str) else pc
                if (datetime.now() - last).total_seconds() / 3600 >= 1:
                    keyboard.append([InlineKeyboardButton("🪴 Собрать урожай", callback_data="collect")])
            else:
                keyboard.append([InlineKeyboardButton("🪴 Куст", callback_data="bush_preview")])
        else:
            # Для новичков показываем кнопку-затравку
            keyboard.append([InlineKeyboardButton("🪴 Куст (⚔️ Ветеран)", callback_data="bush_preview")])
    keyboard.extend([
        [InlineKeyboardButton("📊 Статус", callback_data="status"),
         InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("🕋 Гильдии", callback_data="guild_info"),
         InlineKeyboardButton("📜 Законы", callback_data="rules")],
        [InlineKeyboardButton("🪪 Скидка", callback_data="privilege"),
         InlineKeyboardButton("📦 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🎡 Колесо", callback_data="daily")]  # ускорение удалено
    ])
    return InlineKeyboardMarkup(keyboard)

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

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = await get_player_cached(user_id)

    if context.args and context.args[0] == "activate":
        if not player:
            await update_balance(user_id, username, 0)
            await update_blunts(user_id, username, 0)
            await update_balance(user_id, username, 800)   # стартовый бонус 800
            bonus = "🎁 Смотритель дарует тебе `800` 🍬.\n\n"
        else:
            bonus = ""
        if await get_guild(user_id):
            welcome = "🎉 *_Добро пожаловать обратно в Гильдию Antysocialshop!_*\n▸ Твоя Ткань натянута, странник.\n▸ Возвращайся к ритуалам."
            await update.message.reply_text(bonus + welcome, reply_markup=await get_main_menu_keyboard(user_id), parse_mode="Markdown")
            return
        # Экран выбора гильдии
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
        await update.message.reply_text(bonus + welcome, reply_markup=guild_kb, parse_mode="Markdown")
        return

    if not player:
        await update_balance(user_id, username, 0)
        await update_blunts(user_id, username, 0)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ АКТИВИРОВАТЬ ТЕРМИНАЛ", callback_data="activate_menu")]])
        await update.message.reply_text(
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
    await update.message.reply_text(back, reply_markup=await get_main_menu_keyboard(user_id), parse_mode="Markdown")

async def farm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if p and p[3]:
        last_farm = datetime.fromisoformat(p[3])
        if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
            remain = int((timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)).seconds / 60)
            await msg.reply_text(f"⏳ Жди {remain} мин.", reply_markup=get_back_to_menu_keyboard())
            return

    earned = random.randint(FARM_MIN, FARM_MAX)
    blunts_bonus = 0
    if p and p[1] > 0:
        blunts_bonus = int(earned * 0.1 * min(p[1], 3))
        earned += blunts_bonus
    if p and p[12]:  # inhaled (Красные Глаза)
        smoke_bonus = int(earned * 0.05)
        earned += smoke_bonus
    if context.user_data.get("last_smoke_time") and \
       datetime.now() - context.user_data["last_smoke_time"] < timedelta(minutes=5):
        earned += random.randint(3, 5)

    if random.randint(1, 100) == 1:
        earned *= 10
        await context.bot.send_message(chat_id="@guild_antysocial",
                                       text=f"🌟 @{uname} наткнулся на *Золотую жилу*! +{earned} 🍬",
                                       parse_mode="Markdown")

    old_bal = p[0] if p else 0
    await update_balance(uid, uname, earned)
    await update_last_farm(uid)
    await increment_counter(uid, "farm_count")
    new_p = await get_player_cached(uid)
    new_bal = new_p[0]

    if new_p[14] == 1:  # farm_count
        await grant_title(uid, "🕯️", "Первый Шаг", context)
    if old_bal < 500 <= new_bal:
        await grant_title(uid, "✨", "Искра", context)

    progress = (f"📈 до ⚔️ Ветерана {5000 - new_bal} 🍬" if new_bal < 5000
                else f"📈 до 👻 Призрака {20000 - new_bal} 🍬" if new_bal < 20000
                else "👑 Максимальный ранг")
    text = f"🍬 +{earned} → 💰 {new_bal}\n{progress}"
    await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
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

    # Бросок в Бездну (раз в день, ставка 300)
    can_berserk = False
    if p and p[0] >= 300:
        last_berserk = p[18] if len(p) > 18 else None
        if not last_berserk or (datetime.now() - datetime.fromisoformat(last_berserk)) > timedelta(hours=24):
            can_berserk = True

    if can_berserk:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎲 Испытать Бездну (300 🍬)", callback_data="berserk")],
            [InlineKeyboardButton("📋 Меню", callback_data="menu")]
        ])
    else:
        kb = get_back_to_menu_keyboard()

    await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def craft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    bal = p[0] if p else 0
    if bal < 15:   # стоимость крафта 15 ОАС (было 5)
        await msg.reply_text("🕳️ Пусто. Нужно `15` 🍬.", reply_markup=get_back_to_menu_keyboard())
        return
    await update_balance(uid, uname, -15)
    await update_blunts(uid, uname, 1)
    await increment_counter(uid, "craft_count")
    if random.random() < 0.05:
        await update_blunts(uid, uname, 1)
        await context.bot.send_message(chat_id="@guild_antysocial",
                                       text=f"⚡ @{uname} высек Искру Ткани из кремня рутины. +1 🌿")
    new_p = await get_player_cached(uid)
    await msg.reply_text(f"🌿 Ты свернул Блант. → 💰 {new_p[0]} | 🌿 {new_p[1]}",
                         reply_markup=get_back_to_menu_keyboard())

async def smoke_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p or p[1] < 1:
        await msg.reply_text("🌿 У тебя нет Блантов. /craft", reply_markup=get_back_to_menu_keyboard())
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

    if p and not p[12]:  # inhaled
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
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())

async def ritual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start"); return
    if p[2] != "BLACK":
        await msg.reply_text("❌ Только Чёрная Гильдия."); return
    if p[4]:
        last = datetime.fromisoformat(p[4])
        if datetime.now() - last < timedelta(hours=24):
            await msg.reply_text(f"⏳ Жди {(timedelta(hours=24) - (datetime.now() - last)).seconds // 3600} ч.")
            return
    old_bal = p[0]
    await update_balance(uid, uname, 150)   # ритуал 150 ОАС (было 50)
    await update_last_ritual(uid)
    await increment_counter(uid, "ritual_count")
    extra = 15 if random.random() < 0.1 else 0
    if extra:
        await update_balance(uid, uname, extra)
    new_bal = (await get_player_cached(uid))[0]
    text = (f"🕯️ *РИТУАЛ ЗАВЕРШЁН*\n«Тьма одарила тебя стабильностью.»\n🍬 `+150` → 💰 {new_bal}"
            + ("\n🕯️ Ткань шепчет: «Ты избран»." if extra else ""))
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())
    await check_rank_up(context, uid, uname, old_bal, new_bal)

async def collect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start"); return
    bal = p[0]
    if bal < 5000:
        await msg.reply_text("❌ Доступно с ранга ⚔️ Ветеран (5000 ОАС)", reply_markup=get_back_to_menu_keyboard())
        return
    lvl = 3 if bal >= 20000 else 2   # Призрак / Ветеран
    pc = p[9]
    if pc:
        last = datetime.fromisoformat(pc) if isinstance(pc, str) else pc
        hrs = (datetime.now() - last).total_seconds() / 3600
        earned = int(hrs * 30 * lvl)  # базовый доход 30 ОАС/ч
        if earned >= 1:
            await update_balance(uid, uname, earned)
            async with aiosqlite.connect("players.db") as db:
                await db.execute("UPDATE players SET passive_collected=? WHERE user_id=?", (datetime.now(), uid))
                await db.commit()
            invalidate_cache(uid)
            new_bal = (await get_player_cached(uid))[0]
            await msg.reply_text(f"🪴 *УРОЖАЙ СОБРАН*\nТвой куст принёс `{earned}` 🍬.\n💰 *Баланс:* `{new_bal}` 🍬",
                                 parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())
        else:
            await msg.reply_text("⏳ Пока нечего собирать.")
    else:
        async with aiosqlite.connect("players.db") as db:
            await db.execute("UPDATE players SET passive_collected=? WHERE user_id=?", (datetime.now(), uid))
            await db.commit()
        invalidate_cache(uid)
        await msg.reply_text("⏳ Авто‑сборщик активирован. Заходи через час.")

async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("Сначала активируйся: /start"); return
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
    text = (f"👤 *{uname}*{g_emoji}\n"
            f"👻 *Ранг:* {rank_emoji} **{rank_name}**\n"
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
        await msg.reply_text("🏆 Топ пока пуст."); return
    text = "🏆 *ТОП-10 ИГРОКОВ*\n\n"
    for i, (name, bal, guild) in enumerate(top, 1):
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        g = "🕯️" if guild=="BLACK" else "⚜️" if guild=="WHITE" else ""
        text += f"{medal} {name} {g} — `{bal}` 🍬\n"
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
            "▸ /privilege — твоя скидка\n\n"
            "▸ _Ранг даёт власть._\n"
            "▸ _Гильдия даёт путь._\n"
            "▸ _Бланты усиливают фарм._")
    await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())

async def privilege_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start"); return
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
    await update.callback_query.message.reply_text("🕯️ *ANTYSOCIALSHOP · КАТАЛОГ*", parse_mode="Markdown", reply_markup=kb)

async def daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start"); return
    if p[5]:
        last = datetime.fromisoformat(p[5])
        if datetime.now() - last < timedelta(hours=24):
            await msg.reply_text(f"⏳ Колесо спит. Жди {(timedelta(hours=24) - (datetime.now() - last)).seconds // 3600} ч.")
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
        await context.bot.send_message(chat_id="@guild_antysocial",
                                       text=f"🌟 @{uname} сорвал Джекпот! {txt}", parse_mode="Markdown")
        await update_last_daily(uid)
        return
    await update_last_daily(uid)
    new_bal = (await get_player_cached(uid))[0]
    await msg.reply_text(f"🎡 *КОЛЕСО СМОТРИТЕЛЯ*\n{txt} → 💰 {new_bal} 🍬",
                         parse_mode="Markdown", reply_markup=get_back_to_menu_keyboard())

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        await update.message.reply_text(
            f"🕯️ @{member.username or member.first_name}, добро пожаловать в Гильдию. Твой первый /farm уже ждёт."
        )

# === БРОСОК В БЕЗДНУ ===
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
    if random.random() < 0.6:   # 60% успех
        await update_balance(uid, uname, 200)   # чистый выигрыш 200 (ставил 300, вернули 500)
        await query.message.edit_text("🎲 Ткань благосклонна! Ты получаешь +200 🍬.")
    else:
        await update_balance(uid, uname, -300)  # потеря ставки
        await query.message.edit_text("🕳️ Ткань промолчала. -300 🍬. Завтра она может заговорить.")

# === КОЛБЭК КНОПОК ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id
    try:
        if data == "menu":
            await q.message.edit_text("🎮 *ГЛАВНОЕ МЕНЮ*", reply_markup=await get_main_menu_keyboard(uid), parse_mode="Markdown")
        elif data == "farm": await farm_callback(update, context)
        elif data == "balance": await balance_callback(update, context)
        elif data == "craft": await craft_callback(update, context)
        elif data == "smoke": await smoke_callback(update, context)
        elif data == "ritual": await ritual_callback(update, context)
        elif data == "collect": await collect_callback(update, context)
        elif data == "status": await status_callback(update, context)
        elif data == "top": await top_callback(update, context)
        elif data == "guild_info": await guild_info_callback(update, context)
        elif data == "rules": await rules_callback(update, context)
        elif data == "privilege": await privilege_callback(update, context)
        elif data == "catalog": await catalog_callback(update, context)
        elif data == "daily": await daily_callback(update, context)
        elif data == "berserk": await berserk_callback(update, context)
        elif data == "bush_preview":
            await q.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 ОАС)", show_alert=True)
        elif data in ("guild_join_BLACK", "guild_join_WHITE"):
            guild = "BLACK" if data == "guild_join_BLACK" else "WHITE"
            await set_guild(uid, guild)
            g_emoji = "🕯️" if guild=="BLACK" else "⚜️"
            g_name = "Чёрная" if guild=="BLACK" else "Белая"
            uname = q.from_user.username or q.from_user.first_name
            await q.message.edit_text(
                f"🎉 *_ГИЛЬДИЯ ПРИНЯЛА_*\nТы теперь — {g_emoji} **{g_name} Гильдия** ·\n✅ Ткань стала плотнее...",
                parse_mode="Markdown"
            )
            await context.bot.send_message(
                chat_id="@guild_antysocial",
                text=f"🕋 @{uname} вплёл свою нить в {g_emoji} {g_name} Ткань. Реальность стала плотнее."
            )
        elif data == "activate_menu":
            await start(update, context)   # перенаправляем на активацию
        else:
            await q.message.edit_text("❓ Неизвестная команда.")
    except Exception as e:
        logger.error(f"Button error: {e}")

# === ОБРАБОТЧИК ВСТУПЛЕНИЯ ПО КОМАНДЕ ===
async def guild_join_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await get_guild(user_id):
        await update.message.reply_text("❌ Ты уже состоишь в Гильдии.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🕯️ Чёрная", callback_data="guild_join_BLACK"),
         InlineKeyboardButton("⚜️ Белая", callback_data="guild_join_WHITE")]
    ])
    await update.message.reply_text("🕋 Выбери свою Гильдию, Странник:", reply_markup=keyboard)

# === БЫСТРЫЕ СООБЩЕНИЯ БЕЗ СЛЕША (только в группах) ===
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
    try:
        await context.bot.set_chat_description(chat_id="@guild_antysocial", description=desc)
    except: pass

async def happy_hour_trigger(context):
    context.bot_data["happy_hour"] = True
    context.bot_data["happy_hour_end"] = datetime.now() + timedelta(minutes=HAPPY_HOUR_DURATION_MIN)
    await context.bot.send_message(chat_id="@guild_antysocial",
                                   text="🌟 *ЧАС УДАЧИ!* Все действия приносят x2 🍬 30 минут!",
                                   parse_mode="Markdown")
    context.job_queue.run_once(reset_happy_hour, HAPPY_HOUR_DURATION_MIN*60)

async def reset_happy_hour(context):
    context.bot_data["happy_hour"] = False
    await context.bot.send_message(chat_id="@guild_antysocial", text="⏳ Час Удачи завершён.")

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
        await context.bot.send_message(
            chat_id="@guild_antysocial",
            text=f"🎉 *_КРУГ СОБРАН_*\nТвоя гильдия под благословением: `+5%` к {wrd} на неделю",
            parse_mode="Markdown"
        )

# === ЗАПУСК ===
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())
    Thread(target=run_web_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    # Английские команды
    for cmd, cbk in [("start", start), ("farm", farm_callback), ("balance", balance_callback),
                     ("craft", craft_callback), ("smoke", smoke_callback), ("ritual", ritual_callback),
                     ("status", status_callback), ("top", top_callback), ("rules", rules_callback),
                     ("privilege", privilege_callback), ("catalog", catalog_callback), ("daily", daily_callback),
                     ("collect", collect_callback)]:
        app.add_handler(CommandHandler(cmd, cbk))

    # Русские команды (с прекомпилированными регулярками)
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

    # Быстрые сообщения без слеша (только в группах)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(RE_SHORTCUTS), handle_chat_shortcut))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Фоновые задачи
    job = app.job_queue
    job.run_repeating(update_pulse, interval=300, first=10)
    job.run_once(lambda c: job.run_repeating(happy_hour_trigger, interval=random.randint(14400, 28800),
                 first=random.randint(3600, 10800)), when=1)
    job.run_repeating(cleanup_expired_reservations, interval=3600, first=60)

    now = datetime.now()
    days_until_saturday = (5 - now.weekday()) % 7
    next_saturday = (now + timedelta(days=days_until_saturday)).replace(hour=12, minute=0, second=0, microsecond=0)
    first_seconds = max(1, (next_saturday - now).total_seconds())
    job.run_repeating(weekly_guild_rating, interval=7*24*3600, first=first_seconds)

    print("BOT READY")
    app.run_polling()
    loop.close()
