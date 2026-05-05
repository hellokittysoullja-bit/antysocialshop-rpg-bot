# bot.py — ANTY SOCIAL SHOP RPG v7.15 FIXED
import asyncio, logging, os, random, re, json, hashlib, html
from datetime import datetime, timedelta, date, time
from threading import Thread

import asyncpg
from cachetools import TTLCache
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.error import BadRequest

# === ВЕБ-СЕРВЕР ===
web_app = Flask(__name__)
@web_app.route("/")
def home():
    return "Antysocialshop RPG Bot is alive!"

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FARM_COOLDOWN_HOURS = 0.5
FARM_MIN, FARM_MAX = 45, 100
HAPPY_HOUR_MULTIPLIER = 2
HAPPY_HOUR_DURATION_MIN = 30

player_cache = TTLCache(maxsize=1000, ttl=60)
top_cache = {"data": None, "timestamp": 0, "ttl": 60}

FARM_MEDALS = [
    (1, "🥉 Бронза", 10),
    (10, "🥈 Серебро", 30),
    (50, "🥇 Золото", 80),
    (250, "💎 Платина", 200)
]
CRAFT_MEDALS = [
    (1, "🥉 Бронза", 10),
    (10, "🥈 Серебро", 30),
    (50, "🥇 Золото", 80),
    (250, "💎 Платина", 200)
]
SMOKE_MEDALS = [
    (1, "🥉 Бронза", 10),
    (10, "🥈 Серебро", 30),
    (50, "🥇 Золото", 80),
    (250, "💎 Платина", 200)
]
RITUAL_MEDALS = [
    (1, "🥉 Бронза", 20),
    (10, "🥈 Серебро", 50),
    (50, "🥇 Золото", 120),
    (250, "💎 Платина", 300)
]

WHISPERS = [
    "🩸 Искажение наблюдает за твоими нитями",
    "💠 Кристалл твоей судьбы пульсирует",
    "🩸 Искажение шепчет твоё имя",
    "🌙 «Ночь опустилась на Гильдию. Смотритель пробудился.»"
]
NEURO_STATUSES = [
    "Альфа-ритмы нестабильны", "Сенсорная депривация 80%", "Фаза быстрого сна",
    "Нейро-шунт активен", "Предел синаптической проводимости", "Резонанс с Искажением: 12%"
]
FUNNY_REACTIONS = [
    "Выглядит как NFT, который никто не купит.", "Даже Бездна от такого закашлялась.",
    "Это не блант, это крик души.", "Искажение занесло это название в чёрный список.",
    "10/10, лучший блант для того чтобы спрятать его подальше.", "Пахнет так, будто его скрутил сам Ктулху.",
    "Этот блант вызывает желание помыть руки.", "С таким названием только в Бездну.",
    "Я бы такое не выкурил, но звучит гордо."
]
RANKS = [
    ("🪓 Рекрут", 0, 0),
    ("⚔️ Ветеран", 5000, 1500),
    ("🪦 Призрак", 20000, 6000),
    ("🪬 Некромант", 50000, 15000)
]
ACHIEVEMENTS = [
    {"id": "farm_1", "name": "Первый Шаг", "emoji": "🕯️", "desc": "Совершить 1 фарм очков (АнтиСошл)", "reward": "Титул 🕯️"},
    {"id": "craft_1", "name": "О! Росточек!", "emoji": "🌱", "desc": "Скрутить свой первый блант — главное средство успокоения от бед в этом мире", "reward": "Титул 🌱"},
    {"id": "smoke_1", "name": "Затяжка", "emoji": "🚬", "desc": "Выкурить свой первый блант – выдох за которым следует тишина. Глаза краснеют...", "reward": "Титул 🕶️"},
    {"id": "balance_1000", "name": "О-о-о! Блестяшки!", "emoji": "🍬", "desc": "Накопить 1000 OAC — главную валюту этого мира, за которую говорят даже тени.", "reward": "Титул 🍬"},
    {"id": "smoke_10", "name": "Дымный след", "emoji": "💨", "desc": "Выкурить 10 блантов. Ты перестал замечать пелену", "reward": ""},
    {"id": "craft_15", "name": "Скрученный", "emoji": "🌿", "desc": "Скрутить 15 блантов — каждый сгиб отточен, бумага больше не рвётся.", "reward": "+100 OAC"},
    {"id": "ritual_5", "name": "Прислужник тьмы", "emoji": "🕯️", "desc": "Совершить пять ритуалов — мрачных церемоний и тайных обрядов у алтарей", "reward": ""},
    {"id": "craft_50", "name": "Мастер Кручения", "emoji": "🗞️", "desc": "Скрутить 50 Блантов — довести ремесло до автоматизма, когда руки работают сами.", "reward": "+300 OAC, Рамка 🫧"},
    {"id": "smoke_25", "name": "Вечно Накуренный", "emoji": "🫩", "desc": "Выкурить 25 блантов — грань между воздухом и дымом начинает стираться окончательно.", "reward": "Титул 🫩"},
    {"id": "lab_first", "name": "Скрытое в тени", "emoji": "📿", "desc": "Найти в лабиринте свой первый сундук — драгоценности спрятанные в глубоких чертогах этого мира", "reward": "Титул 📿"},
    {"id": "referral_1", "name": "Пожиратель Душ", "emoji": "🩸", "desc": "Привести 1 друга — ещё одну душу в мир, где связи прочнее стали.", "reward": "Титул 🩸, Рамка 🩸"},
    {"id": "streak_7", "name": "Семь Шагов", "emoji": "🕊️", "desc": "Заходить 7 дней подряд — неделю неразрывного присутствия.", "reward": "Титул 🕊️"},
    {"id": "balance_20000", "name": "Груда блестяшек", "emoji": "🪦", "desc": "Накопить 20000 ОАС — богатство, от которого веет холодом и обещанием власти.", "reward": "Фон ⚰️"},
    {"id": "lab_chest_3", "name": "Ооо! Костяшки!!", "emoji": "🦴", "desc": "Открыть 3 Костяных сундука — первых три трофея из глубин, где покоятся останки.", "reward": "Титул 🦴"},
    {"id": "rank_phantom", "name": "Призрачный Гончий", "emoji": "👻", "desc": "Достигнуть ранга \"Призрак\" — стать частью тех, чьё присутствие ощущают только во мраке.", "reward": "Титул 👻"},
    {"id": "balance_50000", "name": "Повелитель Мёртвых", "emoji": "🩸", "desc": "Накопить 50 000 OAC — гора валюты, что заставляет всех о вас шептаться.", "reward": "+10 000 OAC, Рамка 🩸, Фон 💀"},
    {"id": "check_10", "name": "Всевидящий", "emoji": "👁️", "desc": "Проверить 10 блантов через /check", "reward": "Фон 👁️"},
    {"id": "lab_death_5", "name": "Похоронен заживо", "emoji": "🪦", "desc": "Умереть в Лабиринте 5 раз — возрождаться и вновь погружаться во тьму комнат.", "reward": "Титул 🪦"},
    {"id": "lab_chest_10", "name": "Костяной ключ", "emoji": "🗝️", "desc": "Открыть 10 Костяных сундуков — замков что отдают вам свои секреты.", "reward": "Титул 🗝️"},
    {"id": "craft_250", "name": "Поклонник Плантеры", "emoji": "🌿", "desc": "Скрутить 250 обычных блантов — урожай свитков достойный благословения джунглей.", "reward": "Титул 🌿"},
    {"id": "alchemy_15", "name": "Алхимик", "emoji": "🔮", "desc": "15 раз воспользоваться магией — навыком тайных жестов, доступным не каждому.", "reward": "Титул 🔮"},
    {"id": "lunar_lord", "name": "Лунный лорд", "emoji": "🌀", "desc": "Выполнить все остальные достижения", "reward": "Уникальный фон 🌀"}
]
ACHIEVEMENTS_DICT = {a["id"]: a for a in ACHIEVEMENTS}

LABYRINTH_ROOMS = [
    {
        "name": "👁️ Зал Наблюдателя",
        "desc": "📿 <i>Сотни глаз смотрят на тебя с потолка.</i> <b>Они ждут тебя.</b>",
        "options": [
            {"text": "⚔️ Уничтожить смотрящих (20 OAC)", "cost_oac": 20, "risk": 0.6, "reward_oac": (10,50), "fail": "life"},
            {"text": "🕯️ Отвести взгляд (1 блант)", "cost_blunt": 1, "risk": 0.8, "reward_fragment": True, "fail": "none"},
            {"text": "🏃 Бежать", "cost_none": True, "risk": 1.0, "reward_escape": True, "fail": "none"}
        ]
    },
    {
        "name": "⚗️ Алтарь Теней",
        "desc": "Густая кровь капает с алтаря. Тени шепчут о силе.",
        "options": [
            {"text": "🩸 Пожертвовать OAC (30 OAC)", "cost_oac": 30, "risk": 0.7, "reward_oac": (40,100), "fail": "life"},
            {"text": "📜 Прочесть руны (1 блант)", "cost_blunt": 1, "risk": 0.9, "reward_title": "Посвящённый", "fail": "none"},
            {"text": "🏃 Бежать", "cost_none": True, "risk": 1.0, "reward_escape": True, "fail": "none"}
        ]
    },
    {
        "name": "🌀 Водоворот Хаоса",
        "desc": "Воздух дрожит, затягивая в воронку. Прямо в центре — мерцающий сгусток.",
        "options": [
            {"text": "🌀 Схватить сгусток (25 OAC)", "cost_oac": 25, "risk": 0.5, "reward_dust": True, "fail": "life_big"},
            {"text": "🚪 Обойти (1 блант)", "cost_blunt": 1, "risk": 0.95, "reward_none": True, "fail": "life"},
            {"text": "🏃 Бежать", "cost_none": True, "risk": 1.0, "reward_escape": True, "fail": "none"}
        ]
    },
    {
        "name": "☠️ Склеп Короля",
        "desc": "Груды костей, трон из черепов. С них свисают драгоценные камни.",
        "options": [
            {"text": "💎 Сорвать камень (20 OAC)", "cost_oac": 20, "risk": 0.8, "reward_oac": (20,80), "fail": "life"},
            {"text": "🕯️ Зажечь свечу (1 блант)", "cost_blunt": 1, "risk": 1.0, "reward_dust": True, "fail": "none"},
            {"text": "🏃 Бежать", "cost_none": True, "risk": 1.0, "reward_escape": True, "fail": "none"}
        ]
    }
]

# ========== БАЗА ДАННЫХ NEON ==========
db_pool = None
BLUNTS_PER_PAGE = 3

BLUNT_IMAGES = {
    "common": "AgACAgIAAxkBAAIRu2n5Hi5JOKJANQjkJhqNgW8zcXfLAAKVFGsbxM_JS3cfFnraoo4lAQADAgADeQADOwQ",
    "rare": "AgACAgIAAxkBAAIRvWn5HpzDS9213DAVEDp5K6AEbco_AAKWFGsbxM_JS2MGaxcgGQ2jAQADAgADeQADOwQ",
    "epic": "AgACAgIAAxkBAAIRv2n5Hp89jxsNz8V4uZUDwE1xtC07AAKXFGsbxM_JS6170Njn8cLjAQADAgADeQADOwQ",
    "legendary": "AgACAgIAAxkBAAIRwWn5HqFzbds_ThN4Pogn9c-VdjsaAAKYFGsbxM_JS6GsI_So0AHxAQADAgADeQADOwQ"
}

async def init_db_pool():
    global db_pool
    database_url = os.getenv("NEON_DATABASE_URL")
    if not database_url:
        raise Exception("NEON_DATABASE_URL не установлена!")
    db_pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10, command_timeout=10)
    async with db_pool.acquire() as conn:
        await create_tables(conn)
    logger.info("База данных Neon инициализирована (пул 2-10, таймаут 10с).")

async def close_db_pool():
    global db_pool
    if db_pool:
        await db_pool.close()

async def create_tables(conn):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            balance BIGINT DEFAULT 0,
            blunts BIGINT DEFAULT 0,
            guild TEXT DEFAULT NULL,
            last_farm TIMESTAMP,
            last_ritual TIMESTAMP,
            last_daily TIMESTAMP,
            titles TEXT DEFAULT '',
            last_farm_date DATE,
            passive_level INTEGER DEFAULT 0,
            passive_collected TIMESTAMP,
            karma INTEGER DEFAULT 0,
            inhaled INTEGER DEFAULT 0,
            smoke_count BIGINT DEFAULT 0,
            farm_count BIGINT DEFAULT 0,
            craft_count BIGINT DEFAULT 0,
            ritual_count BIGINT DEFAULT 0,
            referral_count BIGINT DEFAULT 0,
            last_berserk TIMESTAMP,
            inventory JSONB DEFAULT '[]',
            invited_by BIGINT DEFAULT NULL,
            profile_skins JSONB DEFAULT '{}',
            login_streak INTEGER DEFAULT 0,
            last_login_date DATE,
            oath TEXT DEFAULT '',
            keys BIGINT DEFAULT 0,
            check_count BIGINT DEFAULT 0,
            m_essence BIGINT DEFAULT 0,
            lab_chests BIGINT DEFAULT 0,
            lab_deaths BIGINT DEFAULT 0,
            alchemy_count BIGINT DEFAULT 0,
            last_lab_attempt TIMESTAMP,
            donated BIGINT DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS achievements_awarded (
            user_id BIGINT,
            ach_id TEXT,
            awarded_at TIMESTAMP,
            PRIMARY KEY (user_id, ach_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_weekly (
            guild TEXT PRIMARY KEY,
            total_farmed BIGINT DEFAULT 0,
            total_donated BIGINT DEFAULT 0,
            week_start DATE,
            war_active BOOLEAN DEFAULT FALSE
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS nft_registry (
            serial SERIAL PRIMARY KEY,
            blunt_id TEXT UNIQUE,
            created_by BIGINT,
            rarity TEXT DEFAULT 'common',
            rare_number TEXT UNIQUE,
            created_at TIMESTAMP
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS crystals (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            username TEXT,
            description TEXT,
            amount_rub INTEGER,
            daily_oas INTEGER,
            total_earned BIGINT DEFAULT 0,
            start_date TIMESTAMP,
            cancelled INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0
        )
    """)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def get_player_cached(user_id):
    if user_id in player_cache:
        return player_cache[user_id]
    if db_pool is None:
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM players WHERE user_id=$1", user_id)
    if row:
        p = dict(row)
        p["inventory"] = json.loads(p.get("inventory", "[]"))
        player_cache[user_id] = p
        return p
    return None

def invalidate_cache(user_id):
    player_cache.pop(user_id, None)

async def update_balance(user_id, username, amount):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            if username:
                await conn.execute("""
                    INSERT INTO players(user_id, username, balance, blunts)
                    VALUES($1, $2, 0, 0)
                    ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
                """, user_id, username)
            await conn.execute("UPDATE players SET balance = balance + $1 WHERE user_id = $2", amount, user_id)
    invalidate_cache(user_id)

async def update_blunts(user_id, username, amount):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            if username:
                await conn.execute("""
                    INSERT INTO players(user_id, username, balance, blunts)
                    VALUES($1, $2, 0, 0)
                    ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
                """, user_id, username)
            await conn.execute("UPDATE players SET blunts = blunts + $1 WHERE user_id = $2", amount, user_id)
    invalidate_cache(user_id)

async def update_essence(user_id, amount):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO players(user_id, username, balance, blunts)
                VALUES($1, '', 0, 0)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id)
            await conn.execute("UPDATE players SET m_essence = m_essence + $1 WHERE user_id = $2", amount, user_id)
    invalidate_cache(user_id)

ALLOWED_COUNTERS = {"farm_count","craft_count","smoke_count","ritual_count","referral_count","check_count","lab_chests","lab_deaths","alchemy_count"}
async def increment_counter(user_id, field):
    if field not in ALLOWED_COUNTERS:
        return
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"UPDATE players SET {field} = COALESCE({field}, 0) + 1 WHERE user_id = $1", user_id)
    invalidate_cache(user_id)

async def update_last_farm(user_id):
    now = datetime.now(); today = date.today()
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET last_farm=$1, last_farm_date=$2 WHERE user_id=$3", now, today, user_id)
    invalidate_cache(user_id)

async def update_last_ritual(user_id):
    now = datetime.now()
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET last_ritual=$1 WHERE user_id=$2", now, user_id)
    invalidate_cache(user_id)

async def update_last_daily(user_id):
    now = datetime.now()
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET last_daily=$1 WHERE user_id=$2", now, user_id)
    invalidate_cache(user_id)

async def update_last_berserk(user_id):
    now = datetime.now()
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET last_berserk=$1 WHERE user_id=$2", now, user_id)
    invalidate_cache(user_id)

async def add_title(user_id, emoji):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT titles FROM players WHERE user_id=$1", user_id)
        titles = row["titles"] if row else ""
        if emoji not in titles:
            titles = (titles + " " + emoji).strip()
            await conn.execute("UPDATE players SET titles=$1 WHERE user_id=$2", titles, user_id)
    invalidate_cache(user_id)

def emoji_to_name(e):
    for m, threshold, bonus in RANKS:
        if m.startswith(e):
            return m.split(' ',1)[1]
    return ""

async def check_rank_up(context, user_id, username, old_balance, new_balance):
    for emoji, threshold, bonus in RANKS[1:]:
        if old_balance < threshold <= new_balance:
            if bonus:
                await update_balance(user_id, username, bonus)
            try:
                await context.bot.send_message(chat_id="@guild_antysocial",
                    text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ @{html.escape(username)} теперь — {emoji} <b>{emoji_to_name(emoji)}</b>\n\n<b>+{bonus} OAC</b> 🍬 закапало на баланс", parse_mode='HTML')
            except Exception as e:
                logger.error(f"Ошибка отправки в канал: {e}")
            if threshold == 20000:
                await award_achievement(user_id, "rank_phantom", context)

async def unlock_border(user_id, emoji):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT profile_skins FROM players WHERE user_id=$1", user_id)
        skins = json.loads(row["profile_skins"]) if row and row["profile_skins"] else {}
        borders = skins.get("unlocked_borders", [])
        if emoji not in borders:
            borders.append(emoji)
        skins["unlocked_borders"] = borders
        if not skins.get("active_border"):
            skins["active_border"] = emoji
        await conn.execute("UPDATE players SET profile_skins=$1 WHERE user_id=$2", json.dumps(skins), user_id)
    invalidate_cache(user_id)

async def unlock_bg(user_id, emoji):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT profile_skins FROM players WHERE user_id=$1", user_id)
        skins = json.loads(row["profile_skins"]) if row and row["profile_skins"] else {}
        backs = skins.get("unlocked_backgrounds", [])
        if emoji not in backs:
            backs.append(emoji)
        skins["unlocked_backgrounds"] = backs
        if not skins.get("active_background"):
            skins["active_background"] = emoji
        await conn.execute("UPDATE players SET profile_skins=$1 WHERE user_id=$2", json.dumps(skins), user_id)
    invalidate_cache(user_id)

async def award_achievement(user_id, ach_id, context):
    ach = ACHIEVEMENTS_DICT.get(ach_id)
    if not ach:
        return
    p = await get_player_cached(user_id)
    if not p:
        return
    reward = ach["reward"]
    username = p["username"]
    if ach_id == "craft_15":
        await update_balance(user_id, username, 100)
        await unlock_border(user_id, "🫧")
    elif ach_id == "craft_50":
        await update_balance(user_id, username, 300)
        await unlock_border(user_id, "🫧")
    elif ach_id == "referral_1":
        await add_title(user_id, "🩸")
        await unlock_border(user_id, "🩸")
    elif ach_id == "balance_20000":
        await unlock_bg(user_id, "⚰️")
    elif ach_id == "balance_50000":
        await update_balance(user_id, username, 10000)
        await unlock_border(user_id, "🩸")
        await unlock_bg(user_id, "💀")
    elif ach_id == "check_10":
        await unlock_bg(user_id, "👁️")
    elif ach_id == "lunar_lord":
        await unlock_bg(user_id, "🌀")
    if "Титул" in reward:
        await add_title(user_id, ach["emoji"])
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO achievements_awarded(user_id,ach_id,awarded_at) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                           user_id, ach_id, datetime.now())
    text = (
        f"<b>🕊️ СВИТОК ДОСТИЖЕНИЙ 🏆</b>\n\n"
        f"<b>🎉 Достижение разблокировано!</b>\n"
        f"<b>{ach['name']} {ach['emoji']}</b>\n\n"
        f"<i>📜 Запись добавлена</i>"
    )
    if reward and ach_id != "balance_1000":
        text += f"\n<i>{reward}</i>"
    await context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')

async def check_achievements(user_id, context):
    p = await get_player_cached(user_id)
    if not p:
        return
    stats = {
        "farm_1": p["farm_count"] >= 1,
        "craft_1": p["craft_count"] >= 1,
        "smoke_1": p["smoke_count"] >= 1,
        "balance_1000": p["balance"] >= 1000,
        "smoke_10": p["smoke_count"] >= 10,
        "craft_15": p["craft_count"] >= 15,
        "ritual_5": p["ritual_count"] >= 5,
        "craft_50": p["craft_count"] >= 50,
        "smoke_25": p["smoke_count"] >= 25,
        "lab_first": p["lab_chests"] >= 1,
        "referral_1": p["referral_count"] >= 1,
        "streak_7": p["login_streak"] >= 7,
        "balance_20000": p["balance"] >= 20000,
        "lab_chest_3": p["lab_chests"] >= 3,
        "rank_phantom": p["balance"] >= 20000,
        "balance_50000": p["balance"] >= 50000,
        "check_10": p["check_count"] >= 10,
        "lab_death_5": p["lab_deaths"] >= 5,
        "lab_chest_10": p["lab_chests"] >= 10,
        "craft_250": p["craft_count"] >= 250,
        "alchemy_15": p["alchemy_count"] >= 15,
    }
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", user_id)
    awarded = [r["ach_id"] for r in rows]
    for ach in ACHIEVEMENTS:
        ach_id = ach["id"]
        if ach_id not in awarded and stats.get(ach_id, False):
            await award_achievement(user_id, ach_id, context)
    all_ids = {a["id"] for a in ACHIEVEMENTS if a["id"] != "lunar_lord"}
    if all_ids.issubset(awarded) and "lunar_lord" not in awarded:
        await award_achievement(user_id, "lunar_lord", context)

async def create_named_blunt(uid, name, rarity=None, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            return await _create_named_blunt_inner(uid, name, rarity, conn)
    else:
        return await _create_named_blunt_inner(uid, name, rarity, conn)

async def _create_named_blunt_inner(uid, name, rarity, conn):
    blunt_id = f"blunt_{uid}_{int(datetime.now().timestamp()*1000)}_{random.randint(1000,9999)}"
    await conn.execute("""
        INSERT INTO players(user_id, username, balance, blunts)
        VALUES($1, '', 0, 0)
        ON CONFLICT (user_id) DO NOTHING
    """, uid)
    row = await conn.fetchrow("SELECT inventory FROM players WHERE user_id=$1", uid)
    inv = json.loads(row["inventory"]) if row and row["inventory"] else []
    if not rarity:
        rare = random.random()
        if rare < 0.02: rarity = "legendary"
        elif rare < 0.11: rarity = "epic"
        elif rare < 0.37: rarity = "rare"
        else: rarity = "common"
    color_map = {"legendary":"🟡","epic":"🟣","rare":"🔵","common":"🟢"}
    prefix_map = {"legendary":"L","epic":"E","rare":"R","common":"C"}
    color = color_map.get(rarity, "🟢")
    prefix = prefix_map.get(rarity, "C")
    async with conn.transaction():
        serial_row = await conn.fetchrow(
            "INSERT INTO nft_registry(blunt_id,created_by,rarity,created_at) VALUES($1,$2,$3,$4) RETURNING serial",
            blunt_id, uid, rarity, datetime.now()
        )
        serial = serial_row["serial"]
        count_row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM nft_registry WHERE rarity=$1", rarity)
        rare_count = count_row["cnt"]
        rare_number = f"{prefix}-{rare_count:04d}"
        await conn.execute("UPDATE nft_registry SET rare_number=$1 WHERE blunt_id=$2", rare_number, blunt_id)
    hash_hex = hashlib.sha256(f"{name}{serial}{datetime.now().timestamp()}".encode()).hexdigest()[:12].upper()
    short_hash = f"0x{hash_hex[:6]}...{hash_hex[-4:]}"
    reaction = random.choice(FUNNY_REACTIONS)
    item = {
        "id": blunt_id, "serial": serial, "name": name, "type": "named",
        "created_at": datetime.now().isoformat(), "rarity": rarity,
        "rare_number": rare_number, "hash": short_hash, "reaction": reaction,
        "owner_history": [{"user_id": uid, "since": datetime.now().isoformat()}]
    }
    inv.append(item)
    await conn.execute("UPDATE players SET inventory=$1 WHERE user_id=$2", json.dumps(inv), uid)
    invalidate_cache(uid)
    return item

async def get_guild(user_id):
    p = await get_player_cached(user_id)
    return p["guild"] if p else None

async def set_guild(user_id, guild):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET guild=$1 WHERE user_id=$2", guild, user_id)
    invalidate_cache(user_id)

async def get_top(limit=10):
    now = datetime.now().timestamp()
    if top_cache["data"] and (now - top_cache["timestamp"]) < top_cache["ttl"]:
        return top_cache["data"]
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, balance, guild FROM players ORDER BY balance DESC LIMIT $1", limit)
    top_cache["data"] = rows
    top_cache["timestamp"] = now
    return rows

async def count_guilds():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT guild, COUNT(*) as cnt FROM players WHERE guild IS NOT NULL GROUP BY guild")
    cnt = {"BLACK":0, "WHITE":0}
    for r in rows:
        if r["guild"] in cnt:
            cnt[r["guild"]] = r["cnt"]
    return cnt

async def send_whisper(context, chat_id, text):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Whisper error: {e}")

async def send_whisper_dm(update, context, text, reply_markup=None):
    if update.callback_query:
        msg = update.callback_query.message
    elif update.message:
        msg = update.message
    else:
        msg = None
    if msg:
        try:
            await msg.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"HTML send failed: {e}. Sending without formatting.")
            try:
                await msg.reply_text(text, reply_markup=reply_markup)
            except Exception as e2:
                logger.error(f"Plain text send also failed: {e2}")
    else:
        logger.error("No message to reply to.")

def format_date(iso_string):
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%d.%m.%Y в %H:%M")
    except:
        return iso_string

def get_user_and_msg(update: Update):
    if update.callback_query:
        return update.callback_query.from_user, update.callback_query.message
    return update.effective_user, update.message

def get_medal_text_and_reward(old_count, new_count, medals_list):
    bonus = 0
    text = ""
    for threshold, medal_name, reward in medals_list:
        if old_count < threshold <= new_count:
            bonus += reward
            text += f"🎉 <b>Твой ранг повышен до {medal_name}!</b> (+{reward} OAC)\n"
    return text, bonus

def get_medal_progress(new_count, medals_list):
    cur_medal = medals_list[0][1]
    cur_th = medals_list[0][0]
    next_th = medals_list[1][0] if len(medals_list) > 1 else None
    next_medal = medals_list[1][1] if len(medals_list) > 1 else ""
    for th, name, _ in medals_list:
        if new_count >= th:
            cur_medal = name
            cur_th = th
        else:
            next_th = th
            next_medal = name
            break
    max_rank = new_count >= medals_list[-1][0]
    if max_rank:
        goal_str = f"{cur_medal} (Максимум)"
        progress = 100
        bar = "▓" * 10
    else:
        progress = int((new_count - cur_th) / (next_th - cur_th) * 100) if next_th > cur_th else 100
        bar = "▓" * (progress // 10) + "░" * (10 - progress // 10)
        goal_str = f"{cur_medal} → {next_medal}"
    return f"{bar} {progress}%\n{goal_str}"

def progress_bar(percent):
    filled = int(percent / 10)
    empty = 10 - filled
    return "▓" * filled + "░" * empty

def get_rank_progress(balance):
    if balance >= RANKS[-1][1]:
        emoji = RANKS[-1][0]
        name = emoji.split(' ',1)[1]
        return f"⚜️ <b>Ранг:</b> {emoji} <b>{name}</b> (Максимум)\n<b>▓▓▓▓▓▓▓▓▓▓ 100%</b>"
    for i in range(len(RANKS)-1):
        curr_emoji, curr_th, _ = RANKS[i]
        next_emoji, next_th, _ = RANKS[i+1]
        if balance < next_th:
            curr_name = curr_emoji.split(' ',1)[1] if ' ' in curr_emoji else curr_emoji
            progress = int((balance - curr_th) / (next_th - curr_th) * 100)
            bar = "▓" * (progress // 10) + "░" * (10 - progress // 10)
            return (
                f"⚜️ <b>Ранг:</b> {curr_emoji} <b>{curr_name}</b>\n"
                f"{bar} <b>{progress}%</b>\n"
                f"<b>{balance} / {next_th} OAC</b>"
            )
    return ""
    if balance >= RANKS[-1][1]:
        emoji = RANKS[-1][0]
        name = emoji.split(' ',1)[1]
        return f"⚜️ Ранг: {emoji} {name} (Максимум)\n▓▓▓▓▓▓▓▓▓▓ 100%"
    for i in range(len(RANKS)-1):
        curr_emoji, curr_th, _ = RANKS[i]
        next_emoji, next_th, _ = RANKS[i+1]
        if balance < next_th:
            curr_name = curr_emoji.split(' ',1)[1] if ' ' in curr_emoji else curr_emoji
            progress = int((balance - curr_th) / (next_th - curr_th) * 100)
            bar = "▓" * (progress // 10) + "░" * (10 - progress // 10)
            return (
                f"⚜️ Ранг: {curr_emoji} → {next_emoji}\n"
                f"{bar} {progress}%\n"
                f"{balance} / {next_th} OAC"
            )
    return ""

async def add_war_score(user_id, points):
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        war = await conn.fetchrow("SELECT war_active FROM guild_weekly WHERE war_active = TRUE LIMIT 1")
        if not war:
            return
        row = await conn.fetchrow("SELECT guild FROM players WHERE user_id = $1", user_id)
        guild = row["guild"] if row else None
        if not guild or guild not in ("BLACK", "WHITE"):
            return
        await conn.execute("""
            INSERT INTO guild_weekly (guild, week_start, total_farmed)
            VALUES ($1, CURRENT_DATE, $2)
            ON CONFLICT (guild) DO UPDATE SET total_farmed = guild_weekly.total_farmed + $2
        """, guild, points)

async def send_blunt_image(context, chat_id, rarity):
    file_id = BLUNT_IMAGES.get(rarity)
    if file_id:
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=file_id)
        except Exception as e:
            logger.error(f"Blunt image error: {e}")

async def get_main_menu_keyboard(user_id):
    whisper = random.choice(WHISPERS)
    p = await get_player_cached(user_id)
    balance = p["balance"] if p else 0

    bush_btn = InlineKeyboardButton("🪴 Куст", callback_data="collect") if balance >= 5000 else InlineKeyboardButton("🔒 Куст (⚔️ Ветеран)", callback_data="bush_preview")
    pet_btn = InlineKeyboardButton("🐾 Питомец", callback_data="pet_preview") if balance >= 5000 else InlineKeyboardButton("🔒 Питомец (⚔️ Ветеран)", callback_data="pet_preview")

    keyboard = [
        [InlineKeyboardButton("🍬 Фармить", callback_data="farm")],
        [InlineKeyboardButton("🌿 Крафт", callback_data="craft"), InlineKeyboardButton("💨 Дунуть", callback_data="smoke")],
        [bush_btn],
        [InlineKeyboardButton("⚜️ Профиль", callback_data="profile"), InlineKeyboardButton("🏆 Лидеры", callback_data="top")],
        [InlineKeyboardButton("🕋 Гильдия", callback_data="guild_info"), pet_btn],
        [InlineKeyboardButton("🎲 Удача", callback_data="luck"), InlineKeyboardButton("🏛️ Лабиринт", callback_data="lab_start")],
        [InlineKeyboardButton("🛒 Магазин", callback_data="shop")],
    ]
    return InlineKeyboardMarkup(keyboard), whisper

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])

# ========== ОБРАБОТЧИКИ КОМАНД ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    user_id = user.id
    username = user.username or user.first_name
    username_escaped = html.escape(username)
    player = await get_player_cached(user_id)

    # Реферальная система – исправлена
    if context.args and len(context.args[0]) > 0:
        deep_link = context.args[0]
        # Полный deep_link вида blunt_<blunt_id> или что-то ещё
        if deep_link.startswith("blunt_"):
            blunt_id = deep_link  # не убираем префикс, это полный ID
            async with db_pool.acquire() as conn:
                creator_row = await conn.fetchrow("SELECT created_by FROM nft_registry WHERE blunt_id=$1", blunt_id)
            if creator_row:
                creator_id = creator_row["created_by"]
                if creator_id != user_id:
                    async with db_pool.acquire() as conn:
                        ref_row = await conn.fetchrow("SELECT username FROM players WHERE user_id=$1", creator_id)
                        creator_username = ref_row["username"] if ref_row else str(creator_id)
                        invited = await conn.fetchval("SELECT invited_by FROM players WHERE user_id=$1", user_id)
                        if not invited:
                            await conn.execute("UPDATE players SET invited_by=$1 WHERE user_id=$2", creator_id, user_id)
                            await update_balance(creator_id, creator_username, 50)
                            await increment_counter(creator_id, "referral_count")
                            new_name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
                            await create_named_blunt(creator_id, new_name, rarity="legendary")
                            await add_title(creator_id, "🩸")
                            await award_achievement(creator_id, "referral_1", context)
                            try:
                                await context.bot.send_message(chat_id="@guild_antysocial",
                                    text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{username_escaped}</b> был призван нитью @{html.escape(creator_username)}.\n🕸️ Искажение становится плотнее...", parse_mode='HTML')
                            except Exception as e:
                                logger.error(f"Ошибка отправки в канал: {e}")

    if not player:
        await update_balance(user_id, username, 0)
        await update_blunts(user_id, username, 0)
        await update_balance(user_id, username, 800)
        new_name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
        await create_named_blunt(user_id, new_name)
        bonus = "🎁 Смотритель дарует тебе <code>800</code> 🍬 и твой первый именной блант!\n\n"
        welcome = ("<b><i>🎉 Добро пожаловать в Гильдию Antysocialshop!</i></b>\n\n"
                   "🕯️ <b>Тёмная Гильдия</b> — стабильность, ритуалы, тёмное благословение.\n"
                   "⚜️ <b>Светлая Гильдия</b> — азарт, удача, танец на лезвии.\n\n"
                   "▸ <i>Выбери свой путь:</i>")
        guild_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕯️ Тёмная Гильдия", callback_data="guild_join_BLACK"),
             InlineKeyboardButton("⚜️ Светлая Гильдия", callback_data="guild_join_WHITE")]
        ])
        await msg.reply_text(bonus + welcome, reply_markup=guild_kb, parse_mode='HTML')
        return

    await process_daily_login(user_id, context)
    guild = await get_guild(user_id)

    p = await get_player_cached(user_id)
    bal = p["balance"] if p else 0
    rank_emoji, rank_name = "🪓", "Рекрут"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold:
            parts = emoji.split(' ', 1)
            rank_emoji = parts[0]
            rank_name = parts[1] if len(parts) > 1 else ""
            break
    display_name = user.first_name or user.username or "Странник"
    rank_display = f"{rank_emoji} {rank_name}" if rank_name else rank_emoji

    whisper = random.choice(WHISPERS)
    back = f"⚔️ С возвращением в Гильдию, <b>{rank_display} {html.escape(display_name)}</b>.\n\n"
    if guild == "BLACK":
        back += "🔮 <b>Ты — часть Тёмной Гильдии. 🕯️Ритуалы ждут тебя</b>\n"
    elif guild == "WHITE":
        back += "🔮 <b>Ты — часть Светлой Гильдии. ⚜️ Исповедь очищает душу и ждёт тебя</b>\n"
    else:
        back += "🔮 <b>Ты пока не в Гильдии. Нажми /guild чтобы вступить</b>\n"

    menu_text = f"<b>🎮 ГЛАВНОЕ МЕНЮ</b>\n\n<i>{whisper}</i>\n\n" + back

    kb, _ = await get_main_menu_keyboard(user_id)
    await msg.reply_text(menu_text, reply_markup=kb, parse_mode='HTML')

async def process_daily_login(user_id, context):
    p = await get_player_cached(user_id)
    if not p:
        return
    today = date.today()
    last = p["last_login_date"]
    streak = p["login_streak"]
    if last != today:
        if last and (today - last).days == 1:
            streak += 1
        else:
            streak = 1
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE players SET login_streak=$1, last_login_date=$2 WHERE user_id=$3", streak, today, user_id)
        invalidate_cache(user_id)
        reward = {1:10,2:20,3:30,4:40,5:50,6:60,7:70}.get(streak, 10)
        await update_balance(user_id, p["username"], reward)
        if streak == 7:
            await check_achievements(user_id, context)
        bar = "▓" * streak + "░" * (7 - streak)
        msg = (
            f"🎁 Серия входов: {streak}/7\n"
            f"{bar} {int(streak/7*100)}%\n\n"
            f"<b>+{reward} OAC</b>"
        )
        await context.bot.send_message(chat_id=user_id, text=msg, parse_mode='HTML')
    await check_achievements(user_id, context)

async def grant_title(user_id, emoji, name, context):
    await add_title(user_id, emoji)

# ========== ОСНОВНЫЕ ДЕЙСТВИЯ ==========
async def farm_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    uname_escaped = html.escape(uname)
    p = await get_player_cached(uid)
    if p and p.get("last_farm"):
        last = p["last_farm"]
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        if datetime.now() - last < timedelta(hours=FARM_COOLDOWN_HOURS):
            remain = int((timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now()-last)).seconds/60)
            await send_whisper_dm(update, context, f"🍬 <i>OAC копятся</i> 🌿\n\n<b>Подожди {remain} мин.</b>")
            return

    earned = random.randint(FARM_MIN, FARM_MAX)
    crit = False
    if p and (p.get("smoke_count") or 0) > 0:
        earned += int(earned*0.05)
    if context.user_data.get("last_smoke_time") and datetime.now() - context.user_data["last_smoke_time"] < timedelta(minutes=5):
        earned += random.randint(3,5)
    happy = context.bot_data.get("happy_hour", False)
    if happy:
        earned *= HAPPY_HOUR_MULTIPLIER
    if random.randint(1,100) == 1:
        earned *= 10
        crit = True
        await send_whisper(context, "@guild_antysocial", f"🌟 @{uname_escaped} наткнулся на <i>Золотую жилу</i>! +{earned} 🍬")

    old_bal = p["balance"] if p else 0
    old_count = p["farm_count"] if p else 0
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                UPDATE players SET
                    balance = balance + $2,
                    farm_count = farm_count + 1,
                    last_farm = NOW(),
                    last_farm_date = CURRENT_DATE
                WHERE user_id = $1
                RETURNING *
            """, uid, earned)
            if row:
                p_new = dict(row)
                p_new["inventory"] = json.loads(p_new.get("inventory", "[]"))
                player_cache[uid] = p_new
            else:
                # Игрок не существовал, создаём запись через стандартную функцию
                await update_balance(uid, uname, earned)
                await increment_counter(uid, "farm_count")
                await update_last_farm(uid)
                p_new = await get_player_cached(uid)

    await add_war_score(uid, earned)

    new_count = p_new["farm_count"]
    medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, FARM_MEDALS)
    if medal_bonus:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE players SET balance = balance + $1 WHERE user_id = $2", medal_bonus, uid)
                invalidate_cache(uid)
        p_new = await get_player_cached(uid)
    new_balance = p_new["balance"]
    progress_bar_str = get_medal_progress(new_count, FARM_MEDALS)
    rank_progress = get_rank_progress(new_balance)

    crit_str = " (крит x10!)" if crit else ""
    happy_str = " 🌟x2" if happy else ""
    text = (
        f"<b>💎 Ты нафармил:</b> <i>+{earned} OAC</i> 🍬{crit_str}{happy_str}\n"
        f"⚜️ У тебя: <i>{new_balance} OAC</i>\n"
        + (f"\n{medal_text}" if medal_text else "") +
        f"\n🎯 <b>Фарминг:</b> {new_count}\n{progress_bar_str}\n\n"
        f"{rank_progress}"
    )
    await send_whisper_dm(update, context, text)
    await check_rank_up(context, uid, uname, old_bal, new_balance)
    await check_achievements(uid, context)

async def craft_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    bal = p["balance"] if p else 0
    text = f"<b><i>🌿 КРАФТ БЛАНТА</i></b>\n\n🛡️ <i>у тебя:</i> <code>{bal}</code> 🍬"
    kb_rows = [
        [InlineKeyboardButton("🌿 Обычный блант (15 🍬)", callback_data="craft_normal")],
        [InlineKeyboardButton("💍 Именной блант (50 🍬)", callback_data="craft_named")],
    ]
    if p and p.get("m_essence", 0) > 0:
        kb_rows.append([InlineKeyboardButton(f"💠 Использовать Пыль (1 доза)", callback_data="use_dust")])
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
    if update.callback_query:
            await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')
    else:
            await msg.reply_text(text,
    reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')

async def handle_craft_normal(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id; uname = query.from_user.username or query.from_user.first_name
    p = await get_player_cached(uid)
    if not p or p["balance"] < 15:
        await send_whisper_dm(update, context, "<b><i>🕳️ ИСКАЖЕНИЕ МОЛЧИТ</i></b>\n\n<i>🛡️ Недостаточно OAC.</i>\n🕯️ Требуется <b>15 OAC</b> 🍬.")
        return
    old_count = p["craft_count"]
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                UPDATE players SET
                    balance = balance - 15,
                    blunts = blunts + 1,
                    craft_count = craft_count + 1
                WHERE user_id = $1
                RETURNING *
            """, uid)
            if row:
                p_new = dict(row)
                p_new["inventory"] = json.loads(p_new.get("inventory", "[]"))
                player_cache[uid] = p_new
            else:
                await update_balance(uid, uname, -15)
                await update_blunts(uid, uname, 1)
                await increment_counter(uid, "craft_count")
                invalidate_cache(uid)
                p_new = await get_player_cached(uid)

    await add_war_score(uid, 10)
    if random.random() < 0.05:
        await update_blunts(uid, uname, 1)
        await send_whisper(context, "@guild_antysocial", f"⚡ @{html.escape(uname)} высек Искру Искажения из рутины. +1 🌿")

    new_count = p_new["craft_count"]
    medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, CRAFT_MEDALS)
    if medal_bonus:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE players SET balance = balance + $1 WHERE user_id = $2", medal_bonus, uid)
                invalidate_cache(uid)
        p_new = await get_player_cached(uid)
    new_balance = p_new["balance"]
    progress_bar_str = get_medal_progress(new_count, CRAFT_MEDALS)

    text = (
        f"<b><i>🌿 БЛАНТ СКРУЧЕН</i></b>\n\n"
        f"🛡️ <i>Потрачено:</i> <b>15 OAC</b>\n"
        f"⚜️ <i>У тебя:</i> <b>{new_balance} OAC</b> 🍬\n"
        + (f"\n{medal_text}" if medal_text else "") +
        f"\n🎯 <b>Крафтинг:</b> {new_count}\n{progress_bar_str}\n\n"
        f"🚬 <i>Блантов в свёртке:</i> <b>{p_new['blunts']}</b>"
    )
    # Отправляем результат в то же сообщение, если была кнопка, иначе новым
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode='HTML')
    else:
        await update.message.reply_text(text, parse_mode='HTML')
    await check_achievements(uid, context)

async def handle_craft_named(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p or p["balance"] < 50:
        await send_whisper_dm(update, context, "<b><i>🕳️ ИСКАЖЕНИЕ МОЛЧИТ</i></b>\n\n<i>🛡️ Недостаточно OAC.</i>\n🕯️ Требуется <b>50 OAC</b> 🍬.")
        return
    context.user_data['awaiting_named_blunt'] = True
    context.job_queue.run_once(lambda c: context.user_data.update({'awaiting_named_blunt': False}), 300)
    await query.message.delete()
    sent_msg = await context.bot.send_message(
        chat_id=query.message.chat.id,
        text="<b><i>💍 ИМЕННОЙ БЛАНТ</i></b>\n\n<i>Введи имя своего бланта (до 25 символов)</i>\n\n[❌ Отмена]",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_named")]]),
        parse_mode='HTML'
    )
    context.user_data['awaiting_named_blunt_msg_id'] = sent_msg.message_id

async def handle_named_name(update, context):
    if not context.user_data.get('awaiting_named_blunt'):
        return
    user = update.effective_user
    uid = user.id
    name = update.message.text.strip()[:25]
    if not name:
        await update.message.reply_text("❌ Имя не может быть пустым.")
        return
    context.user_data['awaiting_named_blunt'] = False
    msg_id = context.user_data.pop('awaiting_named_blunt_msg_id', None)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except:
            pass
    uname = user.username or user.first_name
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await update_balance(uid, uname, -50)
            await increment_counter(uid, "craft_count")
            item = await create_named_blunt(uid, name, conn=conn)
    await add_war_score(uid, 25)
    blunt_id = item["id"]
    name_escaped = html.escape(name)
    uname_escaped = html.escape(uname)
    color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(item["rarity"], "🟢")
    reaction = item["reaction"]
    await send_blunt_image(context, update.effective_chat.id, item["rarity"])
    text = (
        f"<b><i>💍 БЛАНТ СОТКАН</i></b>\n\n"
        f"🩸 <i>Ты вплёл в <b>Искажение</b> свой именной блант:</i>\n"
        f"{color} <b><i>«{name_escaped}»</i></b> <i>Редкость:</i> <b>{item['rarity']}</b>\n\n"
        f"💎 <i>Он навсегда останется в твоей коллекции.</i>\n\n"
        f"🩸 <i>{reaction}</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Поделиться", callback_data=f"share_blunt_{blunt_id}")],
        [InlineKeyboardButton("🔙 В Крафт", callback_data="craft"), InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await update.message.reply_text(text, reply_markup=kb, parse_mode='HTML')
    try:
        await context.bot.send_message(chat_id="@guild_antysocial",
            text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{uname_escaped}</b> создал свой блант {color} <b><i>«{name_escaped}»</i></b> 🌿\n<i>Редкость: {item['rarity']}</i>\n🩸 <i>{reaction}</i>", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Ошибка отправки в канал: {e}")
    await check_achievements(uid, context)

async def handle_use_dust(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p or p.get("m_essence", 0) < 1:
        await query.answer("Нет Кристальной Пыли.")
        return
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await update_essence(uid, -1)
            name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа","Коготь Хаоса","Вздох Пожирателя"])
            item = await create_named_blunt(uid, name, rarity="legendary", conn=conn)
    await add_war_score(uid, 50)
    reaction = item["reaction"]
    await send_blunt_image(context, query.message.chat.id, "legendary")
    text = (
        f"<b><i>💠 ПЫЛЬ ИСПОЛЬЗОВАНА</i></b>\n\n"
        f"🟡 <b><i>«{name}»</i></b> (Легендарный) 🌿\n"
        f"📜 Реакция: <i>{reaction}</i>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    try:
        await context.bot.send_message(chat_id="@guild_antysocial",
            text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{html.escape(p['username'])}</b> использовал 💠 Пыль и получил легендарный блант <b><i>«{name}»</i></b>!", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Ошибка отправки в канал: {e}")
    await check_achievements(uid, context)

async def cancel_named(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_named_blunt'] = False
    msg_id = context.user_data.pop('awaiting_named_blunt_msg_id', None)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=msg_id)
        except:
            pass
    await craft_callback(update, context)

async def smoke_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p or p["blunts"] < 1:
        await msg.edit_text(
            "<b>💨 ДУНУТЬ</b>\n\n"
            "<b>🌿 Твой свёрток пуст</b>\n"
            "\n"
            "<i>🎈 Скрути новый блант</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌿 Крафт", callback_data="craft")],
                [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
            ]),
            parse_mode='HTML')
        return
    await msg.edit_text(
        f"<b><i>💨 ДУНУТЬ</i></b>\n\n🌿 <i>блантов в свёртке:</i> <b>{p['blunts']}</b>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💨 Дунуть", callback_data="do_smoke")],
            [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
        ]), parse_mode='HTML')

async def do_smoke(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id; uname = html.escape(query.from_user.username or query.from_user.first_name)
    p = await get_player_cached(uid)
    if not p or p["blunts"] < 1:
        await query.answer("Свёрток пуст."); return
    save = (p["guild"]=="WHITE" and random.randint(1,100)<=20)
    r = random.random()
    earned = 0
    if r < 0.18:
        earned = random.randint(15,40)
        if context.bot_data.get("happy_hour"): earned *= HAPPY_HOUR_MULTIPLIER
    elif r < 0.36:
        pass
    elif r < 0.53:
        pass
    elif r < 0.70:
        earned = -5
    elif r < 0.85:
        pass
    else:
        pass

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                UPDATE players SET
                    blunts = blunts + CASE WHEN $2 THEN 0 ELSE -1 END,
                    balance = balance + $3,
                    smoke_count = smoke_count + 1
                WHERE user_id = $1
                RETURNING *
            """, uid, save, earned)
            if row:
                p_new = dict(row)
                p_new["inventory"] = json.loads(p_new.get("inventory", "[]"))
                player_cache[uid] = p_new
            else:
                if not save: await update_blunts(uid, uname, -1)
                if earned: await update_balance(uid, uname, earned)
                await increment_counter(uid, "smoke_count")
                invalidate_cache(uid)
                p_new = await get_player_cached(uid)

    if earned:
        await add_war_score(uid, earned)
    if p and not p.get("inhaled"):
        await add_title(uid, "💨")
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE players SET inhaled=1 WHERE user_id=$1", uid)
        invalidate_cache(uid)
        p_new = await get_player_cached(uid)

    context.user_data["last_smoke_time"] = datetime.now()
    new_count = p_new["smoke_count"]
    medal_text, medal_bonus = get_medal_text_and_reward(p["smoke_count"], new_count, SMOKE_MEDALS)
    if medal_bonus:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE players SET balance = balance + $1 WHERE user_id = $2", medal_bonus, uid)
                invalidate_cache(uid)
        p_new = await get_player_cached(uid)
    bl_left = p_new["blunts"]
    progress_bar_str = get_medal_progress(new_count, SMOKE_MEDALS)

    if r < 0.18:
        effect = f"<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n💨 <b>Лёгкий приход</b>\n💡 «Станки Фабрики №9 работают в ритме твоего сердца»\n\n🍬 <b>+{earned} OAC</b>"
    elif r < 0.36:
        effect = "<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n💤 <b>Полный Штиль</b>\n🚬 «Дым рассеялся, оставив лишь лёгкий шлейф»"
    elif r < 0.53:
        effect = "<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n😵‍💫 <b>Паранойя</b>\n<i>Всё идёт не так. Тени сгущаются…</i>"
    elif r < 0.70:
        effect = f"<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n💨 <b>Кашель</b>\n💊 «Первая тяга была слишком жёсткой, пробило на кашель»\n\n📉 <b>{earned} OAC</b>"
    elif r < 0.85:
        effect = "<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n🛋️ <b>Паралич</b>\n📺 «Тело стало ватным, смотришь в одну точку и не можешь пошевелиться»"
    else:
        effect = "<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n🧘 <b>Глубокое Озарение</b>\n🕯️ «Ты понял, что блант — это ключ к разгадке бытия»"

    if p and not p.get("inhaled"):
        effect += "\n\n<b><i>🎉 ТИТУЛ РАЗБЛОКИРОВАН!</i></b>\n💨 Ты теперь — <b>Красные Глаза</b>"

    text = f"{effect}\n\n🍃 В свёртке: <b>{bl_left}</b>\n{medal_text}💨 <b>Дым:</b> {new_count}\n{progress_bar_str}"
    if save: text += "\n⚜️ <i>Светлая Гильдия сохранила твой Блант!</i>"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Дунуть ещё", callback_data="do_smoke") if bl_left>=1 else InlineKeyboardButton("🌿 Крафтить ещё", callback_data="craft")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    await check_achievements(uid, context)

async def ritual_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p or p.get("guild") != "BLACK":
        await send_whisper_dm(update, context, "❌ Только Тёмная Гильдия.")
        return
    if p.get("last_ritual"):
        last = p["last_ritual"]
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        if datetime.now() - last < timedelta(hours=24):
            remain = int((timedelta(hours=24) - (datetime.now()-last)).seconds/3600)
            await send_whisper_dm(update, context, f"⏳ Жди {remain} ч."); return
    reward = 150
    if context.bot_data.get("happy_hour"): reward *= HAPPY_HOUR_MULTIPLIER
    extra = 15 if random.random() < 0.1 else 0
    old_count = p["ritual_count"]
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                UPDATE players SET
                    balance = balance + $2 + $3,
                    ritual_count = ritual_count + 1,
                    last_ritual = NOW()
                WHERE user_id = $1
                RETURNING *
            """, uid, reward, extra)
            if row:
                p_new = dict(row)
                p_new["inventory"] = json.loads(p_new.get("inventory", "[]"))
                player_cache[uid] = p_new
            else:
                await update_balance(uid, uname, reward + extra)
                await update_last_ritual(uid)
                await increment_counter(uid, "ritual_count")
                invalidate_cache(uid)
                p_new = await get_player_cached(uid)

    await add_war_score(uid, reward + extra)
    new_count = p_new["ritual_count"]
    medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, RITUAL_MEDALS)
    if medal_bonus:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE players SET balance = balance + $1 WHERE user_id = $2", medal_bonus, uid)
                invalidate_cache(uid)
        p_new = await get_player_cached(uid)
    new_balance = p_new["balance"]
    progress_bar_str = get_medal_progress(new_count, RITUAL_MEDALS)

    text = (
        f"<b><i>🕯️ РИТУАЛ ЗАВЕРШЁН</i></b>\n\n"
        f"Ритуал принёс тебе <b>{reward} OAC</b> 🍬\n\n"
        f"⚜️ У тебя: <b>{new_balance} OAC</b>\n"
        + (f"{medal_text}" if medal_text else "") +
        f"🕯️ <b>Ритуалы:</b> {new_count}\n{progress_bar_str}"
    )
    await send_whisper_dm(update, context, text)
    await check_rank_up(context, uid, uname, p["balance"], new_balance)
    await check_achievements(uid, context)

async def collect_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p: await send_whisper_dm(update, context, "🕳️ Ты ещё не активирован. /start"); return
    bal = p["balance"]
    if bal < 5000:
        await send_whisper_dm(update, context, "❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)")
        return
    lvl = 3 if bal >= 20000 else 2
    pc = p.get("passive_collected")
    if pc:
        last = pc if isinstance(pc, datetime) else datetime.fromisoformat(str(pc))
        hrs = (datetime.now() - last).total_seconds()/3600
        earned = int(hrs * 30 * lvl)
        if context.bot_data.get("happy_hour"): earned *= HAPPY_HOUR_MULTIPLIER
        if earned >= 1:
            await update_balance(uid, uname, earned)
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE players SET passive_collected=$1 WHERE user_id=$2", datetime.now(), uid)
            invalidate_cache(uid)
            new_bal = (await get_player_cached(uid))["balance"]
            await send_whisper_dm(update, context, f"<b><i>🪴 УРОЖАЙ СОБРАН</i></b>\n\nТвой куст принёс <b>{earned} OAC</b> 🍬.\n\n💎 <i>У тебя:</i> <b>{new_bal} OAC</b> 🍬")
        else: await send_whisper_dm(update, context, "⏳ Пока нечего собирать.")
    else:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE players SET passive_collected=$1 WHERE user_id=$2", datetime.now(), uid)
        invalidate_cache(uid)
        await send_whisper_dm(update, context, "⏳ Авто‑сборщик активирован. Заходи через час.")

async def profile_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("Сначала активируйся: /start")
        return
    bal, bl, guild = p["balance"], p["blunts"], p["guild"]
    rank_emoji, rank_name = "🪓", "Рекрут"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold:
            parts = emoji.split(' ', 1)
            rank_emoji = parts[0]
            rank_name = parts[1] if len(parts) > 1 else ""
    if guild == "BLACK": g_emoji = " 🕯️ Тёмная Гильдия"
    elif guild == "WHITE": g_emoji = " ⚜️ Светлая Гильдия"
    else: g_emoji = ""
    neuro = random.choice(NEURO_STATUSES)
    skins = p.get("profile_skins", {})
    bg = skins.get("active_background", "") if isinstance(skins, dict) else ""
    active_title = skins.get("active_title", "—") if isinstance(skins, dict) else "—"
    inv_data = p.get("inventory", [])
    badges = []
    if any(it.get("rarity")=="legendary" for it in inv_data): badges.append("🟡")
    if p["referral_count"]>0: badges.append("🩸")
    if p["login_streak"]>=7: badges.append("🔥")
    if p["check_count"]>=10: badges.append("👁️")
    badge_str = ' '.join(badges) if badges else "—"
    try:
        photos = await context.bot.get_user_profile_photos(uid, limit=1)
        if photos.photos:
            await context.bot.send_photo(chat_id=msg.chat.id, photo=photos.photos[0][0].file_id)
    except:
        pass

    rank_progress = get_rank_progress(bal)
    text = (
        f"<b>⚜️ ПРОФИЛЬ</b>\n"
        f"👤 <b>{uname}</b>{g_emoji}\n"
        f"🫧 Фон: {bg}\n\n"
        f"{rank_progress}\n"
        f"💎 <b>ОАС:</b> <b>{bal} OAC</b> 🍬\n"
        f"🌿 <b>Блантов в свёртке:</b> <b>{bl}</b>\n"
        f"🪴 <b>Куст:</b> <b>+{30 * (3 if bal>=20000 else 2 if bal>=5000 else 0)} OAC/ч</b>\n"
        f"🧬 <b>Титул:</b> {active_title}\n"
        f"🧠 <b>Нейро-статус:</b> {neuro}\n\n"
        f"🎖️ <b>Заслуги:</b> {badge_str}"
    )

    named = [it for it in inv_data if it.get("type") == "named"]
    rarity_order = {"legendary": 0, "epic": 1, "rare": 2, "common": 3}
    named.sort(key=lambda x: (rarity_order.get(x.get("rarity", "common"), 3),
                               x.get("serial", 999999)))

    if named:
        text += "\n\n<b>💍 Именные бланты (NFT):</b>"
        for item in named[:2]:
            name = item["name"]
            rarity = item.get("rarity", "common")
            color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
            rare_number = item.get("rare_number", "?-????")
            hash_code = item.get("hash", "0x????...????")
            #await send_blunt_image(context, msg.chat.id, rarity)
            text += (
                f"\n   {color} <b>💍 Имя Бланта:</b> <b>{name}</b>\n"
                f"   🩸 Серийный номер: <b>#{rare_number}</b> · <i>{hash_code}</i>\n"
            )

    kb_rows = []
    if len(named) > 2:
        kb_rows.append([InlineKeyboardButton(f"💍 Все именные бланты ({len(named)})", callback_data="my_blunts")])
    kb_rows.append([InlineKeyboardButton("📜 Кодекс", callback_data="rules")])
    kb_rows.append([InlineKeyboardButton("🎨 Кастомизация", callback_data="skins_menu"),
                    InlineKeyboardButton("🏆 Достижения", callback_data="achievements")])
    kb_rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
    await msg.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb_rows))

async def my_blunts_callback(update, context, page=0):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p: return
    inv_data = p.get("inventory", [])
    named = [it for it in inv_data if it.get("type") == "named"]
    rarity_order = {"legendary": 0, "epic": 1, "rare": 2, "common": 3}
    named.sort(key=lambda x: (rarity_order.get(x.get("rarity", "common"), 3), x.get("serial", 999999)))

    if not named:
        await query.message.edit_text("🏆 У тебя пока нет именных блантов.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В профиль", callback_data="profile")]]))
        return

    total_pages = (len(named) + BLUNTS_PER_PAGE - 1) // BLUNTS_PER_PAGE
    start = page * BLUNTS_PER_PAGE
    end = start + BLUNTS_PER_PAGE
    page_blunts = named[start:end]

    rarity_names = {"legendary": "Легендарный", "epic": "Эпический", "rare": "Редкий", "common": "Обычный"}
    text = f"<b>💎 ТВОИ ИМЕННЫЕ БЛАНТЫ ({len(named)})</b>\n\n"

    kb_rows = []
    for i, item in enumerate(page_blunts, start=1):
        name = item["name"]
        rarity = item.get("rarity", "common")
        color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
        rare_number = item.get("rare_number", "?-????")
        hash_code = item.get("hash", "0x????...????")
        rarity_name = rarity_names.get(rarity, rarity)

        text += (
            f"{start + i}) {color} <b>«{name}»</b> <i>({rarity_name})</i>\n"
            f"<b>🩸Серийный номер и хеш:</b>\n<i>#{rare_number} · {hash_code}</i>\n\n"
        )
        kb_rows.append([
            InlineKeyboardButton(f"{color} Детали ({start + i})", callback_data=f"blunt_details_{item['id']}"),
            InlineKeyboardButton("🔗 Поделиться", callback_data=f"share_blunt_{item['id']}")
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀ Назад", callback_data=f"blunts_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("▶ Далее", callback_data=f"blunts_page_{page+1}"))
    if nav_buttons:
        kb_rows.append(nav_buttons)
    kb_rows.append([InlineKeyboardButton("🔙 В профиль", callback_data="profile")])

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')

ACHIEVEMENTS_PER_PAGE = 5
async def achievements_callback(update, context, page=0):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p: return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", uid)
    awarded = [r["ach_id"] for r in rows]
    completed_count = len(awarded)
    total_achievements = len(ACHIEVEMENTS)
    total_pages = (total_achievements + ACHIEVEMENTS_PER_PAGE - 1) // ACHIEVEMENTS_PER_PAGE
    start = page * ACHIEVEMENTS_PER_PAGE
    end = start + ACHIEVEMENTS_PER_PAGE
    page_ach = ACHIEVEMENTS[start:end]
    text = f"<b>🏆 ДОСТИЖЕНИЯ ({completed_count}/{total_achievements})</b>\n\n"
    for ach in page_ach:
        done = ach["id"] in awarded
        icon = "✅" if done else "⬜"
        text += f"{icon} {ach['emoji']} <b>{ach['name']}</b>\n"
        text += f"<i>{ach['desc']}</i>\n"
        if ach["reward"]:
            text += f"Награда: {ach['reward']}\n"
        text += "\n"
    kb_rows = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"ach_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("▶️ Далее", callback_data=f"ach_page_{page+1}"))
    if nav_buttons:
        kb_rows.append(nav_buttons)
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="profile")])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')

async def top_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    top = await get_top(10)
    if not top:
        await msg.reply_text("🏆 Топ-10 пока пуст.")
        return
    text = "<b>🏆 ТОП-10 ИГРОКОВ</b>\n\n"
    for i, row in enumerate(top, 1):
        name = html.escape(row["username"]); bal = row["balance"]; guild = row["guild"]
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        g = "🕯️" if guild=="BLACK" else "⚜️" if guild=="WHITE" else ""
        text += f"{medal} <b>{name}</b> {g} — <b>{bal} OAC</b> 🍬\n"

    # Исправлен расчёт позиции и десятого места
    async with db_pool.acquire() as conn:
        user_row = await conn.fetchrow("SELECT balance FROM players WHERE user_id=$1", uid)
        if user_row:
            my_bal = user_row["balance"]
            pos = await conn.fetchval("SELECT COUNT(*) FROM players WHERE balance > $1", my_bal) + 1
            if pos > 10:
                tenth = await conn.fetchrow("SELECT balance FROM players ORDER BY balance DESC LIMIT 1 OFFSET 9")
                if tenth and tenth["balance"] is not None:
                    tenth_balance = tenth["balance"]
                    gap = tenth_balance - my_bal
                    if gap > 0:
                        perc = int((1 - gap / tenth_balance) * 100) if tenth_balance else 100
                        text += f"\n📊 <b>Твоя позиция:</b> {pos}\n🎯 <b>До Топ-10:</b> {gap} OAC\n{progress_bar(perc)} <b>{perc}%</b>"
                    else:
                        text += f"\n📊 <b>Твоя позиция:</b> {pos}\n🎯 <b>До Топ-10:</b> 0 OAC\n{progress_bar(100)} <b>100%</b>"
                else:
                    text += f"\n📊 <b>Твоя позиция:</b> {pos}\n🎯 <b>До Топ-10:</b> ? OAC"
            else:
                text += f"\n📊 <b>Твоя позиция:</b> {pos} (ты в топе!)"
        else:
            text += "\n📊 <b>Твоя позиция:</b> не найдена"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Разведка", callback_data="top_scout")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

async def top_scout_callback(update, context):
    query = update.callback_query
    await query.answer()
    top = await get_top(3)
    if not top:
        await query.answer("Топ пуст.")
        return
    text = "<b>🔍 РАЗВЕДКА: ТОП-3</b>\n\n"
    for i, row in enumerate(top):
        name = html.escape(row["username"])
        bal = row["balance"]
        guild = row["guild"]
        g = "🕯️" if guild=="BLACK" else "⚜️" if guild=="WHITE" else ""
        text += f"{'🥇' if i==0 else '🥈' if i==1 else '🥉'} <b>{name}</b> {g}\n💰 {bal} OAC\n\n"
    await send_whisper_dm(update, context, text)

async def guild_info_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    counts = await count_guilds()
    guild = await get_guild(uid)
    p = await get_player_cached(uid)

    async with db_pool.acquire() as conn:
        black_donated = await conn.fetchval("SELECT COALESCE(SUM(donated),0) FROM players WHERE guild='BLACK'")
        white_donated = await conn.fetchval("SELECT COALESCE(SUM(donated),0) FROM players WHERE guild='WHITE'")
    target = 50000
    black_perc = min(100, int(black_donated / target * 100))
    white_perc = min(100, int(white_donated / target * 100))

    text = (
        f"<b>🕋 ГИЛЬДИИ</b>\n\n"
        f"🕯️ <b>Тёмная: {counts['BLACK']}</b> странников | {progress_bar(black_perc)} <b>{black_perc}%</b>\n"
        f"⚜️ <b>Светлая: {counts['WHITE']}</b> странников | {progress_bar(white_perc)} <b>{white_perc}%</b>\n\n"
    )

    async with db_pool.acquire() as conn:
        war_row = await conn.fetchrow("SELECT war_active FROM guild_weekly WHERE war_active = TRUE LIMIT 1")
        if war_row:
            scores = await conn.fetch("SELECT guild, total_farmed FROM guild_weekly")
            black_score = next((r["total_farmed"] for r in scores if r["guild"] == "BLACK"), 0)
            white_score = next((r["total_farmed"] for r in scores if r["guild"] == "WHITE"), 0)
            total_war = max(black_score + white_score, 1)
            black_perc_war = int(black_score / total_war * 100)
            white_perc_war = int(white_score / total_war * 100)
            black_bar = "▓" * (black_perc_war // 10) + "░" * (10 - black_perc_war // 10)
            white_bar = "▓" * (white_perc_war // 10) + "░" * (10 - white_perc_war // 10)
            text += (
                f"\n⚔️ <b>Война гильдий</b>\n\n"
                f"🕯️ Тёмные: {black_score} очков\n{black_bar} {black_perc_war}%\n\n"
                f"⚜️ Светлые: {white_score} очков\n{white_bar} {white_perc_war}%\n"
            )

    kb_rows = []
    if guild:
        g_emoji = "🕯️" if guild == "BLACK" else "⚜️"
        g_name = "Тёмная" if guild == "BLACK" else "Светлая"
        text += f"Ты состоишь в {g_emoji} <b>{g_name} Гильдии</b>.\n"
        if guild == "BLACK" and p:
            last_ritual = p.get("last_ritual")
            if last_ritual:
                if isinstance(last_ritual, str):
                    last_ritual = datetime.fromisoformat(last_ritual)
                if datetime.now() - last_ritual < timedelta(hours=24):
                    diff = timedelta(hours=24) - (datetime.now() - last_ritual)
                    hrs = int(diff.seconds // 3600)
                    mins = int((diff.seconds % 3600) // 60)
                    kb_rows.append([InlineKeyboardButton(f"🕯️ Ритуал ({hrs} ч {mins} мин)", callback_data="ritual")])
                else:
                    kb_rows.append([InlineKeyboardButton("🕯️ Ритуал", callback_data="ritual")])
            else:
                kb_rows.append([InlineKeyboardButton("🕯️ Ритуал", callback_data="ritual")])
        if guild == "WHITE" and p:
            kb_rows.append([InlineKeyboardButton("⚜️ Исповедь", callback_data="confess")])
        kb_rows.append([InlineKeyboardButton("🏛️ Храм Гильдии", callback_data="guild_shrine")])
    else:
        text += "<i>Ты пока не в Гильдии.</i>\n"
        kb_rows.append([InlineKeyboardButton("🕯️ Вступить в Тёмную", callback_data="guild_join_BLACK"),
                        InlineKeyboardButton("⚜️ Вступить в Светлую", callback_data="guild_join_WHITE")])
    kb_rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)
    if update.callback_query:
        if update.callback_query:
        await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
    else:
        await msg.reply_text(text, reply_markup=kb, parse_mode='HTML')
    else:
        await msg.reply_text(text, reply_markup=kb, parse_mode='HTML')
async def guild_shrine_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p or not p["guild"]:
        await query.answer("Ты не в гильдии.")
        return
    total = 50000
    donated = p.get("donated", 0)
    perc = int(donated / total * 100)
    bar = progress_bar(perc)
    text = (
        f"<b>🏛️ ХРАМ ГИЛЬДИИ</b>\n\n"
        f"🔹 {p['guild']} Гильдия\n"
        f"Прогресс строительства: {donated} / {total} OAC\n"
        f"{bar} {perc}%\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Пожертвовать 100 OAC", callback_data="shrine_donate_100")],
        [InlineKeyboardButton("💎 Пожертвовать 500 OAC", callback_data="shrine_donate_500")],
        [InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

async def confess_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p or p["guild"] != "WHITE":
        await query.answer("Только для Светлой Гильдии.")
        return
    if p["blunts"] < 1:
        await query.answer("Нужен 1 блант.")
        return
    await update_blunts(uid, p["username"], -1)
    r = random.random()
    if r < 0.70:
        reward = random.randint(100, 200)
        await update_balance(uid, p["username"], reward)
        text = f"<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\nБлагословение! +{reward} OAC."
    elif r < 0.95:
        await update_essence(uid, 1)
        text = "<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\nТы получил 💠 Кристальную Пыль."
    else:
        name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
        text = f"<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\n🌟 Чудо! Легендарный блант «{name}»!"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]]), parse_mode='HTML')

async def rules_callback(update, context):
    user, msg = get_user_and_msg(update)
    text = (
        "<b>📜 КОДЕКС ГИЛЬДИИ</b>\n\n"
        "<i>«Странник, познай законы этого мира…»</i>\n\n"
        "<b>⚙️ ОСНОВНЫЕ ДЕЙСТВИЯ</b>\n"
        "🍬 <code>/farm</code> — <i>добыча OAC</i>\n"
        "🌿 <code>/craft</code> — <i>создать блант</i>\n"
        "💨 <code>/smoke</code> — <i>выкурить блант</i>\n"
        "🎲 <code>/luck</code> — <i>испытать удачу</i> 🔮\n\n"
        "<b>💍 ИМЕННЫЕ БЛАНТЫ</b>\n"
        "💎 Создай свой <b>вечный именной Блант</b> через меню «Крафт».\n"
        "<i>Он не курится, получает редкость и навсегда остаётся в твоей коллекции.</i>\n\n"
        "<b>🕋 ГИЛЬДИИ И РАЗВИТИЕ</b>\n"
        "🕯️ <b>Тёмная Гильдия:</b> <code>/ritual</code> (+150 OAC раз в 24 ч) — <i>«Ритуалы укрепляют нити»</i>\n"
        "⚜️ <b>Светлая Гильдия:</b> 20% шанс сохранить блант при 💨, <code>/repent</code> — <i>исповедь</i>\n"
        "🪴 <b>Куст:</b> пассивный доход с ранга ⚔️ Ветеран\n"
        "🐾 <b>Питомец:</b> доступен с ранга ⚔️ Ветеран\n\n"
        "<b>ℹ️ ИНФОРМАЦИЯ</b>\n"
        "⚜️ <code>/profile</code> — твой профиль и коллекция\n"
        "🏆 <code>/top</code> — список сильнейших\n\n"
        "<b>🛡️ МАГАЗИН</b>\n"
        "<code>/privilege</code> — твоя скидка\n"
        "<code>/catalog</code> — ссылка на каталог\n\n"
        "<i>🏆 Ранг даёт власть. Гильдия даёт путь. Искажение награждает верных.</i> 🩸"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💍 Создать именной блант", callback_data="craft_named")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

async def privilege_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid)
    if not p: await msg.reply_text("🕳️ Ты ещё не активирован. /start"); return
    bal = p["balance"]
    rank_emoji, rank_name = "🪓", "Рекрут"
    next_rank_name = "Ветеран"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold:
            rank_emoji, rank_name = emoji, emoji_to_name(emoji)
    if bal >= 50000: percent = 100; active = 10; next_rank_name = "Максимум"
    elif bal >= 20000: percent = min(100, int((bal - 20000) / (50000 - 20000) * 100)); active = percent // 10; next_rank_name = "Некромант"
    elif bal >= 5000: percent = min(100, int((bal - 5000) / (20000 - 5000) * 100)); active = percent // 10; next_rank_name = "Призрак"
    else: percent = min(100, int(bal / 5000 * 100)); active = percent // 10; next_rank_name = "Ветеран"
    inactive = 10 - active
    progress_bar_str = "🟪" * active + "⬛️" * inactive
    text = (
        f"<b>🛡️ ПРИВИЛЕГИЯ</b>\n\n"
        f"⚜️ <b>Ранг:</b> {rank_name} 🕯️\n"
        f"🔮 <b>Текущая сила:</b> <b>{percent}%</b>\n"
        f"{progress_bar_str} {percent}%\n\n"
        f"🎯 <b>До след. уровня:</b> {next_rank_name}"
    )
    await msg.reply_text(text, parse_mode='HTML', reply_markup=get_back_to_menu_keyboard())

async def catalog_callback(update, context):
    user, msg = get_user_and_msg(update)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Перейти", url="https://t.me/antysocialshop")]])
    await msg.reply_text("<b>🕯️ ANTYSOCIALSHOP · КАТАЛОГ</b>", parse_mode='HTML', reply_markup=kb)

async def luck_callback(update, context, action=None):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p: await msg.reply_text("Сначала активируйся: /start"); return
    bal = p["balance"]; now = datetime.now()
    last_daily = p.get("last_daily")
    if isinstance(last_daily, str):
        last_daily = datetime.fromisoformat(last_daily)
    wheel_available = not (last_daily and (now - last_daily) < timedelta(hours=24))
    last_berserk = p.get("last_berserk")
    if isinstance(last_berserk, str):
        last_berserk = datetime.fromisoformat(last_berserk)
    berserk_available = (bal >= 300 and (not last_berserk or (now - last_berserk) > timedelta(hours=24)))
    veteran_alchemy = (bal >= 5000)
    text = f"<b><i>🎲 ИСПЫТАНИЕ СУДЬБЫ</i></b>\n\n🛡️ <i>у тебя:</i> <code>{bal}</code> 🍬\n\n"
    kb_rows = []
    if wheel_available:
        kb_rows.append([InlineKeyboardButton("🎡 Крутить", callback_data="luck_wheel")])
    else:
        diff = timedelta(hours=24) - (now - last_daily)
        hrs = int(diff.seconds // 3600); mins = int((diff.seconds % 3600) // 60)
        kb_rows.append([InlineKeyboardButton(f"🎡 Колесо набирает силу. Ещё {hrs} ч {mins} мин", callback_data="luck_wheel")])
    if berserk_available:
        kb_rows.append([InlineKeyboardButton("🎲 Рискнуть", callback_data="luck_berserk")])
    else:
        if bal < 300: kb_rows.append([InlineKeyboardButton(f"🎲 нужно ещё {300 - bal} 🍬", callback_data="luck_berserk")])
        else:
            diff = timedelta(hours=24) - (now - last_berserk)
            hrs = int(diff.seconds // 3600); mins = int((diff.seconds % 3600) // 60)
            kb_rows.append([InlineKeyboardButton(f"🎲 Бездна шепчет всё громче. Жди {hrs} ч {mins} мин", callback_data="luck_berserk")])
    if veteran_alchemy:
        kb_rows.append([InlineKeyboardButton("🧪 Запустить реакцию", callback_data="alchemy_start")])
    else:
        kb_rows.append([InlineKeyboardButton("🔮 Алхимия (⚔️ Ветеран)", callback_data="alchemy_start")])
    kb_rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)
    if action == "luck_wheel":
        if not wheel_available:
            diff = timedelta(hours=24) - (now - last_daily)
            hrs = int(diff.seconds // 3600); mins = int((diff.seconds % 3600) // 60)
            await send_whisper_dm(update, context, f"<b><i>🎡 Колесо не готово</i></b>\n\n💎 Испытаешь через <b>{hrs} ч {mins} мин</b>.")
            return
        await update_last_daily(uid)
        r = random.random()
        if r <= 0.4: prize = 30; prize_type = "oac"
        elif r <= 0.65: prize = 75; prize_type = "oac"
        elif r <= 0.8: prize = 1; prize_type = "blunt"
        elif r <= 0.9: prize = 150; prize_type = "oac"
        elif r <= 0.97: prize = 2; prize_type = "blunt"
        else:
            prize_type = "jackpot"; prize = 1000
            if random.random() < 0.5: prize *= 2
        final_prize = prize
        if context.bot_data.get("happy_hour") and prize_type in ("oac","jackpot"):
            final_prize = prize * HAPPY_HOUR_MULTIPLIER
        if prize_type == "jackpot":
            await update_balance(uid, uname, final_prize)
            await grant_title(uid, "🧛🏻‍♀️", "Призрачный Гончий", context)
            try:
                await context.bot.send_message(chat_id="@guild_antysocial", text=f"🌟 @{uname} сорвал Джекпот! +{final_prize} OAC", parse_mode='HTML')
            except: pass
        elif prize_type == "oac":
            await update_balance(uid, uname, final_prize)
            await add_war_score(uid, final_prize)
            new_p = await get_player_cached(uid)
            new_bal = new_p["balance"]
            next_rank_name = ""
            next_threshold = 0
            for emoji, threshold, _ in RANKS:
                if new_bal < threshold:
                    next_rank_name = emoji; next_threshold = threshold; break
            progress_text = f"<b>🎯 До ранга {next_rank_name}: <i>{next_threshold - new_bal} OAC</i></b>" if next_threshold else "<b>🏆 Максимальный ранг!</b>"
            text = f"<b>🩸 ДАР ИСКАЖЕНИЯ</b>\n\n<b>💎 Ты нафармил +{final_prize} OAC 🍬!</b>\n⚜️ <b>У тебя:</b> <i>{new_bal} OAC</i>\n\n{progress_text}"
            try:
                await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]), parse_mode='HTML')
            except BadRequest as e:
                if "message is not modified" not in str(e).lower(): raise
            return
        else:
            await update_blunts(uid, uname, prize)
            new_bal = (await get_player_cached(uid))["balance"]
            txt = f"+{prize} 🌿 Блант"
            text = f"<b><i>🎲 КОЛЕСО СМОТРИТЕЛЯ</i></b>\n\n{txt} → 💰 <b>{new_bal} OAC</b> 🍬"
        try:
            await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]), parse_mode='HTML')
        except BadRequest as e:
            if "message is not modified" not in str(e).lower(): raise
        return
    if action == "luck_berserk":
        if not berserk_available:
            if bal < 300: await send_whisper_dm(update, context, f"<b><i>🎲 Бездна требует жертву</i></b>\n\n⚠️ Недостаточно OAC (нужно ещё <b>{300-bal}</b>).")
            else:
                diff = timedelta(hours=24) - (now - last_berserk)
                hrs = int(diff.seconds // 3600); mins = int((diff.seconds % 3600) // 60)
                await send_whisper_dm(update, context, f"<b><i>🎲 Бездна молчит</i></b>\n\n🕳️ Примет тебя через <b>{hrs} ч {mins} мин</b>.")
            return
        await update_last_berserk(uid)
        if random.random() < 0.6: await update_balance(uid, uname, 200); res_text = f"<b><i>🎲 БЕЗДНА ОТВЕТИЛА</i></b>\n\nИскажение благосклонно! +<b>200 OAC</b> 🍬."
        else: await update_balance(uid, uname, -300); res_text = f"<b><i>🕳️ БЕЗДНА МОЛЧИТ</i></b>\n\nИскажение промолчало. –<b>300 OAC</b>."
        await add_war_score(uid, 200 if "200" in res_text else -300)
        try:
            await msg.edit_text(res_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]), parse_mode='HTML')
        except BadRequest as e:
            if "message is not modified" not in str(e).lower(): raise
        return
    if action == "alchemy_start":
        query = update.callback_query
        if not veteran_alchemy:
            await query.answer("🔮 Доступ к магии откроется на ранге ⚔️ Ветеран (5000 OAC).", show_alert=True)
            return
        if p["blunts"] < 5 or bal < 50:
            await send_whisper_dm(update, context, "🔮 Нужно 5 блантов и 50 OAC для запуска Котла.")
            return
        text = f"<b><i>🔮 АЛХИМИЧЕСКИЙ КОТЁЛ</i></b>\n\nУ тебя есть <b>5 блантов</b> и <b>50 OAC</b>.\nБросить их в Котёл?\n\n🌀 <i>Искажение шепчет: «Только тот, кто стал <b>ветераном</b> и не боится потерь – обретёт право использовать магию и истинную силу»</i> 🔮"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧪 Запустить реакцию", callback_data="alchemy_confirm")],
            [InlineKeyboardButton("🔙 Назад", callback_data="luck")]
        ])
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
        except BadRequest as e:
            if "message is not modified" not in str(e).lower(): raise
        return
    if action == "alchemy_confirm":
        if p["blunts"] < 5 or bal < 50:
            await send_whisper_dm(update, context, "🔮 Недостаточно ресурсов.")
            return
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await update_blunts(uid, uname, -5)
                await update_balance(uid, uname, -50)
                r = random.random()
                if r < 0.40:
                    await update_essence(uid, 1)
                    result_text = "<b><i>🔮 РЕЗУЛЬТАТ АЛХИМИИ</i></b>\n\n💠 <b>Чистая Пыльца!</b>\nТы получаешь 1 дозу Кристальной Пыли.\n\n🌀 <i>Искажение принимает твою жертву</i>"
                elif r < 0.75:
                    result_text = "<b><i>🔮 РЕЗУЛЬТАТ АЛХИМИИ</i></b>\n\n🌫️ <b>Грязный Выхлоп...</b>\nБланты сгорели без следа.\n\n🌀 <i>Искажение осталось голодным</i>"
                elif r < 0.90:
                    await update_essence(uid, 2)
                    result_text = "<b><i>🔮 РЕЗУЛЬТАТ АЛХИМИИ</i></b>\n\n✨ <b>Мерцающая Пыльца!</b>\nТы получаешь 2 дозы Кристальной Пыли.\n\n🌀 <i>Искажение щедро сегодня</i>"
                else:
                    name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа","Коготь Хаоса","Вздох Пожирателя"])
                    item = await create_named_blunt(uid, name, rarity="legendary", conn=conn)
                    result_text = f"<b><i>🔮 РЕЗУЛЬТАТ АЛХИМИИ</i></b>\n\n🌟 <b>Философский Камень!</b>\nТы получаешь легендарный блант <b><i>«{name}»</i></b>!\n\n🌀 <i>Искажение дарует тебе силу</i>"
                    try:
                        await context.bot.send_message(chat_id="@guild_antysocial",
                            text=f"🌟 @{uname} провёл Алхимический Ритуал и получил легендарный блант <b><i>«{name}»</i></b>!", parse_mode='HTML')
                    except Exception as e:
                        logger.error(f"Ошибка отправки в канал: {e}")
        await add_war_score(uid, 30)
        try:
            await msg.edit_text(result_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]), parse_mode='HTML')
        except BadRequest as e:
            if "message is not modified" not in str(e).lower(): raise
        return
    # Основной показ меню удачи
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
    except BadRequest as e:
        if "message is not modified" not in str(e).lower(): raise

async def check_blunt(update, context):
    if not context.args:
        await update.message.reply_text("Укажи серийный номер: /check R-0001")
        return
    nft_id = context.args[0].strip().upper()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT blunt_id, created_by, serial, rare_number FROM nft_registry WHERE rare_number = $1", nft_id)
        if not row:
            await update.message.reply_text("🕳️ Блант с таким серийным номером не найден.")
            return
        blunt_id, creator_id, serial, rare_number = row["blunt_id"], row["created_by"], row["serial"], row["rare_number"]
        # Поиск владельца через JSONB (владелец = создатель, т.к. нет передачи)
        # Но на всякий случай поищем в inventory (без LIKE, через contains)
        owner_row = await conn.fetchrow("SELECT user_id, inventory FROM players WHERE inventory @> $1::jsonb", json.dumps([{"id": blunt_id}]))
        if owner_row:
            owner_id = owner_row["user_id"]
            inv = json.loads(owner_row["inventory"])
            item = next((it for it in inv if it.get("id") == blunt_id), None)
        else:
            item = None
    if not item:
        await update.message.reply_text("Блант есть в реестре, но владелец не найден.")
        return
    name = item["name"]; rarity = item.get("rarity","common")
    color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
    reaction = item.get("reaction","")
    hash_code = item.get("hash","0x????...????")
    await send_blunt_image(context, update.effective_chat.id, rarity)
    details = f"<b>ДЕТАЛИ NFT БЛАНТА 💎</b>\n\n{color} <b>{name}</b>\n\n<b>Редкость:</b> <i>{rarity}</i> {color}\n\n🩸 <b>Серийный номер:</b> <b>#{rare_number}</b>\n🔗 <b>Хеш:</b> <b>{hash_code}</b>\n📜 <b>Реакция:</b> <i>{reaction}</i>\n"
    if "owner_history" in item:
        details += "\n🔄 История владения:\n"
        for entry in item["owner_history"]:
            date_str = format_date(entry.get('since',''))
            details += f"   @{entry.get('user_id','?')} — {date_str}\n"
    await update.message.reply_text(details, parse_mode='HTML')
    await increment_counter(update.effective_user.id, "check_count")

# Лабиринт (без изменений в логике, только мелкие правки)
async def lab_enter(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid)
    if not p: return
    now = datetime.now()
    last = p.get("last_lab_attempt")
    if last and isinstance(last, str):
        last = datetime.fromisoformat(last)
    if last and (now - last).total_seconds() < 12*3600:
        remain = 12*3600 - (now - last).total_seconds()
        hrs = int(remain // 3600); mins = int((remain % 3600) // 60)
        text = f"<b>🏛️ ЛАБИРИНТ ИСКАЖЕНИЯ 🔮</b>\n\n🎚️ Сегодня осталось <b>0 попыток</b>.\n⛓️‍💥 Жизни: <b>2</b>\n\n<i>– Портал откроется через <b>{hrs} ч {mins} мин</b>.</i>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="menu")]])
        await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
        return
    text = f"<b>🏛️ ЛАБИРИНТ ИСКАЖЕНИЯ 🔮</b>\n\n🎚️ Сегодня осталась <b>1 попытка</b>.\n⛓️‍💥 Жизни: <b>2</b>\n\n<i>– Ты стоишь у входа. Портал отвечает.</i>\n\nВыбери действие:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚪 Войти в Лабиринт", callback_data="lab_enter_confirm")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')

async def lab_enter_confirm(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    now = datetime.now()
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET last_lab_attempt=$1 WHERE user_id=$2", now, uid)
    invalidate_cache(uid)
    context.user_data["lab_room"] = 0
    context.user_data["lab_lives"] = 2
    context.user_data["lab_rewards"] = []
    room = random.choice(LABYRINTH_ROOMS)
    context.user_data["lab_current_room"] = room
    lives = context.user_data.get("lab_lives", 2)
    text = f"<b><i>{room['name']}</i></b>\n\n{room['desc']}\n\n⛓️‍💥 <b>Жизни: {lives}</b>"
    kb_rows = [[InlineKeyboardButton(opt["text"], callback_data=f"lab_option_{i}")] for i, opt in enumerate(room["options"])]
    kb_rows.append([InlineKeyboardButton("🏃 Бежать", callback_data="lab_escape")])
    lab_msg = await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')
    context.user_data["lab_msg_id"] = lab_msg.message_id
    context.user_data["lab_chat_id"] = lab_msg.chat.id

async def show_lab_room(update, context):
    room_index = context.user_data.get("lab_room", 0)
    lives = context.user_data.get("lab_lives", 2)
    if room_index >= 5:
        await show_lab_final(update, context); return
    room = random.choice(LABYRINTH_ROOMS)
    context.user_data["lab_current_room"] = room
    text = f"<b><i>{room['name']}</i></b>\n\n{room['desc']}\n\n⛓️‍💥 <b>Жизни: {lives}</b>"
    kb_rows = [[InlineKeyboardButton(opt["text"], callback_data=f"lab_option_{i}")] for i, opt in enumerate(room["options"])]
    kb_rows.append([InlineKeyboardButton("🏃 Бежать", callback_data="lab_escape")])
    chat_id = context.user_data.get("lab_chat_id")
    msg_id = context.user_data.get("lab_msg_id")
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')
    except Exception:
        try:
            query = update.callback_query
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')
        except:
            pass

async def handle_lab_option(update, context, option_index):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    room = context.user_data.get("lab_current_room")
    if not room: return
    opt = room["options"][option_index]
    p = await get_player_cached(uid)
    if not p: return
    if "cost_oac" in opt and p["balance"] < opt["cost_oac"]:
        await query.answer("Недостаточно OAC."); return
    if "cost_blunt" in opt and p["blunts"] < opt["cost_blunt"]:
        await query.answer("Недостаточно блантов."); return
    if "cost_oac" in opt: await update_balance(uid, p["username"], -opt["cost_oac"])
    elif "cost_blunt" in opt: await update_blunts(uid, p["username"], -opt["cost_blunt"])
    success = random.random() < opt["risk"]
    if success:
        if "reward_oac" in opt:
            earned = random.randint(*opt["reward_oac"])
            await update_balance(uid, p["username"], earned)
            await add_war_score(uid, earned)
            context.user_data.setdefault("lab_rewards", []).append(f"+{earned} OAC")
        elif "reward_fragment" in opt:
            context.user_data.setdefault("lab_rewards", []).append("💠 Кристальная Пыль")
        elif "reward_title" in opt:
            await add_title(uid, opt["reward_title"])
            context.user_data.setdefault("lab_rewards", []).append(f"Титул: {opt['reward_title']}")
        elif "reward_dust" in opt:
            context.user_data.setdefault("lab_rewards", []).append("💠 Кристальная Пыль")
        elif "reward_escape" in opt:
            await show_lab_final(update, context); return
    else:
        if opt["fail"] == "life": context.user_data["lab_lives"] -= 1
        elif opt["fail"] == "life_big": context.user_data["lab_lives"] -= 2
    if context.user_data.get("lab_lives", 2) <= 0:
        await increment_counter(uid, "lab_deaths")
        await show_lab_death(update, context); return
    context.user_data["lab_room"] += 1
    await asyncio.sleep(1.5)
    await show_lab_room(update, context)

async def show_lab_final(update, context):
    query = update.callback_query
    uid = query.from_user.id
    p = await get_player_cached(uid)
    rewards = context.user_data.get("lab_rewards", [])
    text = "<b><i>🎁 СУНДУК ИСКАЖЕНИЯ</i></b>\n\n<b>Ты достиг сердца Лабиринта. Сундук открывается... 💎</b>"
    if rewards: text += "\n\nСобрано: " + ", ".join(rewards)
    text += "\n+50 OAC 🍬\n💠 Кристальная Пыль: 1 доза"
    await update_balance(uid, p["username"], 50)
    await update_essence(uid, 1)
    await increment_counter(uid, "lab_chests")
    await add_war_score(uid, 80)
    for key in ("lab_room","lab_lives","lab_rewards","lab_msg_id","lab_chat_id"):
        context.user_data.pop(key, None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К Лабиринту", callback_data="lab_start")], [InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    await check_achievements(uid, context)

async def show_lab_death(update, context):
    query = update.callback_query
    uid = query.from_user.id
    p = await get_player_cached(uid)
    await update_balance(uid, p["username"], 50)
    for key in ("lab_room","lab_lives","lab_rewards","lab_msg_id","lab_chat_id"):
        context.user_data.pop(key, None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К Лабиринту", callback_data="lab_start")], [InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await query.message.edit_text("<b><i>🪦 ЛАБИРИНТ ПОГЛОТИЛ ТЕБЯ</i></b>\n\n<i>Твои жизни иссякли. Искажение выбросило тебя обратно.</i>\n\n+50 OAC 🍬", reply_markup=kb, parse_mode='HTML')

async def welcome_new_member(update, context):
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        await update.message.reply_text(
            f"<b><i>🕯️ ДОБРО ПОЖАЛОВАТЬ</i></b>\n\n⚜️ <b>{html.escape(member.username or member.first_name)}</b>, добро пожаловать в <b><i>Гильдию</i></b>\n<i>Твой первый /farm уже ждёт</i>"
        )

async def handle_chat_shortcut(update, context):
    text = update.message.text.strip().lower()
    mapping = {
        "фарм": farm_callback, "farm": farm_callback,
        "дунуть": smoke_callback, "smoke": smoke_callback,
        "крафт": craft_callback, "craft": craft_callback,
        "топ": top_callback, "top": top_callback,
        "удача": luck_callback, "luck": luck_callback,
        "профиль": profile_callback, "profile": profile_callback,
        "сбор": collect_callback,
        "правила": rules_callback,
        "исповедь": confess_callback, "repent": confess_callback,
        "гильдия": guild_info_callback,
        "привилегия": privilege_callback,
        "каталог": catalog_callback,
        "проверка": check_blunt,
        "ритуал": ritual_callback,
        "лабиринт": lab_enter,
        "питомец": pet_preview,
        "магазин": shop_callback
    }
    if text in mapping:
        await mapping[text](update, context)

async def pet_preview(update, context):
    await update.effective_message.reply_text("🐾 Питомцы пока не реализованы.")

async def shop_callback(update, context):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🪪 Скидка", callback_data="privilege")],
        [InlineKeyboardButton("📦 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await query.message.edit_text("<b>🛒 МАГАЗИН</b>", reply_markup=kb, parse_mode='HTML')

async def setbluntpic(update, context):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("⛔ Только для админа.")
    if not context.args:
        return await update.message.reply_text("Используй: /setbluntpic common (rare, epic, legendary) и прикрепи фото.")
    rarity = context.args[0].lower()
    if rarity not in BLUNT_IMAGES:
        return await update.message.reply_text("Редкость должна быть: common, rare, epic, legendary.")
    if not update.message.photo:
        return await update.message.reply_text("Пришли фото вместе с командой.")
    BLUNT_IMAGES[rarity] = update.message.photo[-1].file_id
    names = {"common":"⚪ Обычный","rare":"🔵 Редкий","epic":"🟣 Эпический","legendary":"🟡 Легендарный"}
    await update.message.reply_text(f"✅ Изображение для {names[rarity]} обновлено!", parse_mode='HTML')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = q.from_user.id
    try:
        if data == "menu":
            await q.answer()
            kb, whisper = await get_main_menu_keyboard(uid)
            menu_text = f"<b>🎮 ГЛАВНОЕ МЕНЮ</b>\n\n<i>{whisper}</i>"
            try: await q.message.edit_text(menu_text, reply_markup=kb, parse_mode='HTML')
            except Exception: await q.message.reply_text(menu_text, reply_markup=kb, parse_mode='HTML')
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
        elif data in ("luck_wheel", "luck_berserk", "alchemy_start", "alchemy_confirm"): await q.answer(); await luck_callback(update, context, action=data)
        elif data == "craft_normal": await q.answer(); await handle_craft_normal(update, context)
        elif data == "craft_named": await q.answer(); await handle_craft_named(update, context)
        elif data == "cancel_named": await q.answer(); await cancel_named(update, context)
        elif data == "do_smoke": await q.answer(); await do_smoke(update, context)
        elif data == "use_dust": await q.answer(); await handle_use_dust(update, context)
        elif data == "top_scout": await q.answer(); await top_scout_callback(update, context)
        elif data == "achievements": await q.answer(); await achievements_callback(update, context, page=0)
        elif data.startswith("ach_page_"): await q.answer(); await achievements_callback(update, context, page=int(data.split("_")[-1]))
        elif data == "my_blunts": await q.answer(); await my_blunts_callback(update, context, page=0)
        elif data.startswith("blunts_page_"): await q.answer(); await my_blunts_callback(update, context, page=int(data.split("_")[-1]))
        elif data.startswith("share_blunt_"):
            await q.answer()
            blunt_id = data.replace("share_blunt_", "")
            p = await get_player_cached(uid)
            if not p: return
            bot_username = (await context.bot.get_me()).username
            ref_link = f"https://t.me/{bot_username}?start={blunt_id}"
            inv = p.get("inventory", [])
            item = next((it for it in inv if it.get("id")==blunt_id), None)
            username = html.escape(p["username"])
            if item:
                name = item["name"]; rarity = item.get("rarity","common")
                color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
                text = f"<b>{username}</b>\n\n{color} <b>Имя NFT бланта: «{name}»</b>\n🧬 <b>Редкость:</b> {rarity} {color}\n🩸 <b>Серийный номер:</b> #{item.get('rare_number','?-????')}\n📜 <b>Реакция:</b> <i>{item.get('reaction','')}</i>\n\n<i>Присоединяйся к Искажению:</i>\n{ref_link}"
            else: text = f"Блант не найден.\n{ref_link}"
            await send_whisper_dm(update, context, text)
        elif data.startswith("blunt_details_"):
            await q.answer()
            blunt_id = data.replace("blunt_details_", "")
            p = await get_player_cached(uid)
            if not p: return
            inv = p.get("inventory", [])
            item = next((it for it in inv if it.get("id")==blunt_id), None)
            if not item: await q.answer("Блант не найден."); return
            name = item["name"]; rarity = item.get("rarity","common")
            color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
            rare_number = item.get("rare_number","?-????")
            hash_code = item.get("hash","0x????...????")
            reaction = item.get("reaction","")
            text = f"<b>💎 ДЕТАЛИ NFT БЛАНТА</b>\n\n{color} <b>«{name}»</b>\n<b>Редкость:</b> <i>{rarity}</i> {color}\n\n🩸 <b>Серийный номер:</b> <i>#{rare_number}</i>\n\n🔗 <b>Хеш:</b> <i>{hash_code}</i>\n\n📜 <b>Реакция:</b> <i>{reaction}</i>\n\n"
            if "owner_history" in item:
                text += "🕊️ <b>История владения:</b>\n"
                for entry in item["owner_history"]:
                    date_str = format_date(entry.get('since',''))
                    text += f"   <b>@{entry.get('user_id','?')}</b> — {date_str}\n"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Поделиться", callback_data=f"share_blunt_{blunt_id}")],
                [InlineKeyboardButton("🏆 К списку", callback_data="my_blunts")]
            ])
            file_id = BLUNT_IMAGES.get(rarity)
            if file_id:
                await q.message.reply_photo(photo=file_id, caption=text, reply_markup=kb, parse_mode='HTML')
            else:
                await q.message.edit_text(text=text, reply_markup=kb, parse_mode='HTML')
        elif data == "lab_start": await q.answer(); await lab_enter(update, context)
        elif data == "lab_enter_confirm": await q.answer(); await lab_enter_confirm(update, context)
        elif data.startswith("lab_option_"): await q.answer(); await handle_lab_option(update, context, int(data.split("_")[-1]))
        elif data == "lab_escape": await q.answer(); await show_lab_final(update, context)
        elif data == "guild_shrine": await q.answer(); await guild_shrine_callback(update, context)
        elif data in ("shrine_donate_100", "shrine_donate_500"):
            amount = 100 if data == "shrine_donate_100" else 500
            p = await get_player_cached(uid)
            if p["balance"] < amount: await q.answer("Недостаточно OAC."); return
            await update_balance(uid, p["username"], -amount)
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE players SET donated = COALESCE(donated,0) + $1 WHERE user_id = $2", amount, uid)
            invalidate_cache(uid)
            await send_whisper_dm(update, context, f"💎 Ты внёс {amount} OAC в Храм. Спасибо, Странник!")
        elif data == "confess": await q.answer(); await confess_callback(update, context)
        elif data == "pet_preview": await q.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)", show_alert=True)
        elif data == "bush_preview": await q.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)", show_alert=True)
        elif data == "activate_menu": await q.answer(); # устаревшая кнопка, не используется
        elif data == "skins_menu":
            await q.answer()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Выбрать титул", callback_data="choose_title")],
                [InlineKeyboardButton("🖼️ Выбрать фон", callback_data="choose_bg")],
                [InlineKeyboardButton("🔙 Назад", callback_data="profile")]
            ])
            await q.message.edit_text("<b>🫧 СКИНЫ</b>\n\nВыбери, что хочешь изменить.", reply_markup=kb, parse_mode='HTML')
        elif data.startswith("set_title_") or data.startswith("set_bg_"):
            # базовые реализации (не изменялись)
            pass
        elif data == "shop": await shop_callback(update, context)
        else: await q.answer("Неизвестная команда.")
    except Exception as e: logger.error(f"Button error: {e}")

# ========== ДЖОБЫ ==========
async def update_pulse(context):
    async with db_pool.acquire() as conn:
        black = await conn.fetchval("SELECT COUNT(*) FROM players WHERE guild='BLACK'")
        white = await conn.fetchval("SELECT COUNT(*) FROM players WHERE guild='WHITE'")
        online = await conn.fetchval("SELECT COUNT(DISTINCT user_id) FROM players WHERE last_farm > $1", datetime.now()-timedelta(hours=1))
    desc = f"🕯️{black} ▰▱⚜️{white} | 👥{online}"
    try: await context.bot.set_chat_description(chat_id="@guild_antysocial", description=desc)
    except: pass

async def happy_hour_trigger(context):
    context.bot_data["happy_hour"] = True
    context.bot_data["happy_hour_end"] = datetime.now() + timedelta(minutes=HAPPY_HOUR_DURATION_MIN)
    try:
        await context.bot.send_message(chat_id="@guild_antysocial", text="🌟 <b>ЧАС УДАЧИ!</b> Все действия приносят x2 🍬 30 минут!", parse_mode='HTML')
    except Exception as e: logger.error(f"Happy hour announce error: {e}")
    context.job_queue.run_once(reset_happy_hour, HAPPY_HOUR_DURATION_MIN*60)
    # Планируем следующий случайный happy hour
    next_delay = random.randint(4*3600, 8*3600)  # 4-8 часов
    context.job_queue.run_once(happy_hour_trigger, next_delay, name="happy_hour")

async def reset_happy_hour(context):
    context.bot_data["happy_hour"] = False
    try: await context.bot.send_message(chat_id="@guild_antysocial", text="⏳ Час Удачи завершён.")
    except: pass

async def echo_of_distortion(context):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username, inventory FROM players WHERE inventory IS NOT NULL AND inventory != '[]' ORDER BY random() LIMIT 100")
    all_named = []
    for row in rows:
        try:
            inv = json.loads(row["inventory"])
            for item in inv:
                if item.get("type")=="named": all_named.append((row["user_id"], row["username"], item))
        except: continue
    if not all_named: return
    sample = random.sample(all_named, min(3, len(all_named)))
    text = "<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n"
    for uid, uname, item in sample:
        name = item["name"]; rarity = item.get("rarity","common")
        color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
        reaction = item.get("reaction","")
        text += f"⚜️ <b>@{html.escape(uname)}</b> создал свой блант {color} <b><i>«{html.escape(name)}»</i></b> 🌿\n<i>Редкость: {rarity}</i>\n🩸 <i>{reaction}</i>\n\n"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💍 Создать свой блант", callback_data="craft_named")]])
    try: await context.bot.send_message(chat_id="@guild_antysocial", text=text, parse_mode='HTML', reply_markup=kb)
    except: pass

async def weekly_guild_rating(context):
    async with db_pool.acquire() as conn:
        war = await conn.fetchrow("SELECT war_active FROM guild_weekly WHERE war_active = TRUE LIMIT 1")
        if not war:
            await conn.execute("UPDATE guild_weekly SET total_farmed = 0, war_active = TRUE")
            try:
                await context.bot.send_message(chat_id="@guild_antysocial",
                    text="⚔️ <b>ВОЙНА ГИЛЬДИЙ НАЧАЛАСЬ!</b>\n🕯️ Тёмные vs ⚜️ Светлые\nЗарабатывай OAC, крафти, проходи лабиринт — всё идёт в зачёт гильдии!\nПобедители получат сундук с ресурсами! 🎁", parse_mode='HTML')
            except: pass
        else:
            await conn.execute("UPDATE guild_weekly SET war_active = FALSE")
            rows = await conn.fetch("SELECT guild, total_farmed FROM guild_weekly")
            black = next((r["total_farmed"] for r in rows if r["guild"] == "BLACK"), 0)
            white = next((r["total_farmed"] for r in rows if r["guild"] == "WHITE"), 0)
            if black != white:
                winner = "BLACK" if black > white else "WHITE"
                oac = random.randint(200, 500)
                blunts = random.randint(3, 7)
                dust = random.randint(1, 3)
                async with db_pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE players SET
                            balance = balance + $1,
                            blunts = blunts + $2,
                            m_essence = m_essence + $3
                        WHERE guild = $4
                    """, oac, blunts, dust, winner)
                try:
                    await context.bot.send_message(chat_id="@guild_antysocial",
                        text=f"🎉 <b>ВОЙНА ГИЛЬДИЙ ЗАВЕРШЕНА!</b>\n{('🕯️' if winner == 'BLACK' else '⚜️')} <b>Победитель: {winner} гильдия</b> ({black if winner == 'BLACK' else white} очков)\nКаждый участник получает сундук с ресурсами!", parse_mode='HTML')
                except: pass

async def keep_db_alive(context):
    if db_pool:
        try:
            async with db_pool.acquire() as conn: await conn.execute("SELECT 1")
        except Exception as e: logger.error(f"Keep-alive error: {e}")

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db_pool())
    Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()

    async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        command = update.message.text.split()[0].split('@')[0][1:].lower()
        mapping = {
            "start": start,
            "farm": farm_callback,
            "craft": craft_callback,
            "smoke": smoke_callback,
            "ritual": ritual_callback,
            "profile": profile_callback,
            "top": top_callback,
            "rules": rules_callback,
            "privilege": privilege_callback,
            "catalog": catalog_callback,
            "luck": luck_callback,
            "collect": collect_callback,
            "check": check_blunt,
            "guild": guild_info_callback,
            "repent": confess_callback,
            "lab": lab_enter,
            "pet": pet_preview,
            "shop": shop_callback,
            "setbluntpic": setbluntpic,
        }
        if command in mapping:
            await mapping[command](update, context)

    app.add_handler(MessageHandler(filters.COMMAND, handle_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_shortcut))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_named_name))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(button_handler))

    job = app.job_queue
    job.run_repeating(update_pulse, interval=900, first=10)
    # Первый happy hour случайным образом через 1-3 часа, затем каждые 4-8 часов динамически
    first_hh = random.randint(3600, 10800)
    job.run_once(happy_hour_trigger, first_hh, name="happy_hour")
    job.run_daily(echo_of_distortion, time=time(hour=18, minute=0))
    now = datetime.now()
    days_until_saturday = (5 - now.weekday()) % 7
    next_saturday = (now + timedelta(days=days_until_saturday)).replace(hour=12, minute=0, second=0, microsecond=0)
    if next_saturday <= now: next_saturday += timedelta(days=7)
    job.run_repeating(weekly_guild_rating, interval=7*24*3600, first=max(1, (next_saturday - now).total_seconds()))
    job.run_repeating(keep_db_alive, interval=180, first=10)

    print("BOT READY – all critical fixes applied")
    app.run_polling()
    loop.run_until_complete(close_db_pool())
    loop.close()
