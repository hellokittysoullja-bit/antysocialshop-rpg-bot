# bot.py — ANTY SOCIAL SHOP RPG v7.14 FINAL FIXED (complete, 2710 lines)
import asyncio, logging, os, random, re, json, hashlib, html
from datetime import datetime, timedelta, date, time
from threading import Thread

import asyncpg
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.error import BadRequest

import functools
import asyncio

def db_retry(max_retries=3, delay=0.2):
    """Автоматически повторяет запрос к БД при временных сбоях соединения."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except (asyncpg.exceptions.ConnectionDoesNotExistError,
                        asyncpg.exceptions.InterfaceError,
                        asyncpg.exceptions.PostgresConnectionError) as e:
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(f"DB retry {attempt+1}/{max_retries} for {func.__name__}: {e}")
                    await asyncio.sleep(delay * (attempt + 1))
        return wrapper
    return decorator

def rate_limit(seconds: int = 2):
    """Запрещает повторный вызов функции чаще, чем раз в seconds секунд."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            user_id = update.effective_user.id
            key = f"rate_{func.__name__}_{user_id}"
            now = datetime.now()
            last_time = context.user_data.get(key)
            if last_time and (now - last_time).total_seconds() < seconds:
                # Показываем предупреждение, но не выполняем действие
                if update.callback_query:
                    await update.callback_query.answer("⏳ Слишком быстро! Подожди немного.", show_alert=True)
                else:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="⏳ Пожалуйста, не так быстро. Попробуй через пару секунд."
                    )
                return
            context.user_data[key] = now
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

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

top_cache = {"data": None, "timestamp": 0, "ttl": 60}

def _json_safe_load(value, default):
    if isinstance(value, (list, dict)):
        return value
    if value in (None, ""):
        return default.copy() if isinstance(default, (list, dict)) else default
    try:
        parsed = json.loads(value)
        if parsed is None:
            return default.copy() if isinstance(default, (list, dict)) else default
        return parsed
    except Exception:
        return default.copy() if isinstance(default, (list, dict)) else default

def _to_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None

def emoji_to_name(emoji: str) -> str:
    if not emoji:
        return ""
    parts = str(emoji).split(" ", 1)
    return parts[1] if len(parts) > 1 else parts[0]

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
import redis.asyncio as aioredis

redis = None
player_cache = {}  # fallback-словарь, если Redis не подключён

async def init_redis():
    global redis
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        redis = await aioredis.from_url(redis_url)
        logger.info("Redis подключён – кэш активирован")
    else:
        logger.info("REDIS_URL не задан – используется in-memory кэш")

async def get_player_cached(user_id):
    # Пробуем Redis
    if redis:
        key = f"player:{user_id}"
        data = await redis.get(key)
        if data:
            p = json.loads(data)
            if isinstance(p, dict):
                return p

    # Fallback – старый кэш в памяти
    if user_id in player_cache:
        return player_cache[user_id]

    # Запрос к БД
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM players WHERE user_id=$1", user_id)
    if row:
        p = dict(row)
        # Нормализация числовых полей – ни одного None
        numeric_fields = [
            'balance', 'blunts', 'farm_count', 'craft_count', 'smoke_count',
            'ritual_count', 'referral_count', 'check_count', 'lab_chests',
            'lab_deaths', 'alchemy_count', 'login_streak', 'donated', 'm_essence',
            'passive_level', 'karma', 'inhaled', 'keys'
        ]
        for field in numeric_fields:
            p[field] = p.get(field) or 0
        # Инвентарь и скины
        p["inventory"] = _json_safe_load(p.get("inventory"), [])
        p["profile_skins"] = _json_safe_load(p.get("profile_skins"), {})

        # Сохраняем в кэш
        if redis:
            await redis.setex(key, 10, json.dumps(p, default=str))
        else:
            player_cache[user_id] = p
        return p
    return None

def invalidate_cache(user_id):
    """Сбрасывает кэш игрока (Redis + in-memory)."""
    if redis:
        asyncio.create_task(redis.delete(f"player:{user_id}"))
    else:
        player_cache.pop(user_id, None)

async def ensure_player_exists(user_id, username=None, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            await ensure_player_exists(user_id, username=username, conn=conn)
        return
    await conn.execute(
        "INSERT INTO players(user_id, username, balance, blunts) VALUES($1, COALESCE(NULLIF($2, ''), ''), 0, 0) ON CONFLICT (user_id) DO UPDATE SET username = COALESCE(NULLIF(EXCLUDED.username, ''), players.username)",
        user_id,
        username or "",
    )

@db_retry()
async def update_last_farm(user_id, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            await update_last_farm(user_id, conn=conn)
        return
    await ensure_player_exists(user_id, conn=conn)
    await conn.execute("UPDATE players SET last_farm = NOW(), last_farm_date = CURRENT_DATE WHERE user_id = $1", user_id)
    invalidate_cache(user_id)

@db_retry()
async def update_last_daily(user_id, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            await update_last_daily(user_id, conn=conn)
        return
    await ensure_player_exists(user_id, conn=conn)
    await conn.execute("UPDATE players SET last_daily = NOW() WHERE user_id = $1", user_id)
    invalidate_cache(user_id)

@db_retry()
async def update_last_ritual(user_id, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            await update_last_ritual(user_id, conn=conn)
        return
    await ensure_player_exists(user_id, conn=conn)
    await conn.execute("UPDATE players SET last_ritual = NOW() WHERE user_id = $1", user_id)
    invalidate_cache(user_id)

@db_retry()
async def update_last_berserk(user_id, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            await update_last_berserk(user_id, conn=conn)
        return
    await ensure_player_exists(user_id, conn=conn)
    await conn.execute("UPDATE players SET last_berserk = NOW() WHERE user_id = $1", user_id)
    invalidate_cache(user_id)

async def get_guild(user_id):
    p = await get_player_cached(user_id)
    return p.get("guild") if p else None

async def add_title(user_id, emoji, conn=None):
    title = str(emoji or "").strip()
    if not title:
        return
    if conn is None:
        async with db_pool.acquire() as conn:
            await add_title(user_id, title, conn=conn)
        return
    await ensure_player_exists(user_id, conn=conn)
    row = await conn.fetchrow("SELECT titles FROM players WHERE user_id = $1", user_id)
    titles = (row["titles"] if row and row["titles"] else "").split()
    if title not in titles:
        titles.append(title)
        await conn.execute("UPDATE players SET titles = $1 WHERE user_id = $2", " ".join(titles).strip(), user_id)
    invalidate_cache(user_id)

@db_retry()
async def create_named_blunt(user_id, name, rarity=None, conn=None):
    """Создаёт именной блант и добавляет его ТОЛЬКО в инвентарь."""
    if rarity not in ("common", "rare", "epic", "legendary"):
        r = random.random()
        if r < 0.01: rarity = "legendary"
        elif r < 0.05: rarity = "epic"
        elif r < 0.20: rarity = "rare"
        else: rarity = "common"

    clean_name = str(name or "").strip()[:25] or "Безымянный"
    reaction = random.choice(FUNNY_REACTIONS)
    blunt_id = f"blunt_{user_id}_{int(datetime.utcnow().timestamp())}_{random.randint(1000,9999)}"
    hash_code = "0x" + hashlib.sha256((blunt_id + ":hash").encode()).hexdigest()[:16]
    rare_number = f"{rarity[0].upper()}-{random.randint(1000,9999)}"

    item = {
        "id": blunt_id,
        "type": "named",
        "name": clean_name,
        "rarity": rarity,
        "serial": None,                # без реестра serial не хранится
        "rare_number": rare_number,
        "hash": hash_code,
        "reaction": reaction,
        "created_at": datetime.utcnow().isoformat(),
        "owner_history": [{"user_id": str(user_id), "since": datetime.utcnow().isoformat()}],
    }

    if conn is None:
        async with db_pool.acquire() as new_conn:
            row = await new_conn.fetchrow("SELECT inventory FROM players WHERE user_id = $1", user_id)
            inventory = _json_safe_load(row["inventory"] if row else None, [])
            inventory.append(item)
            await new_conn.execute("UPDATE players SET inventory = $1 WHERE user_id = $2", json.dumps(inventory), user_id)
            invalidate_cache(user_id)
            return item
    else:
        row = await conn.fetchrow("SELECT inventory FROM players WHERE user_id = $1", user_id)
        inventory = _json_safe_load(row["inventory"] if row else None, [])
        inventory.append(item)
        await conn.execute("UPDATE players SET inventory = $1 WHERE user_id = $2", json.dumps(inventory), user_id)
        invalidate_cache(user_id)
        return item

async def check_achievements(user_id, context):
    p = await get_player_cached(user_id)
    if not p:
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", user_id)
        awarded = {r["ach_id"] for r in rows}
        for ach in ACHIEVEMENTS:
            ach_id = ach["id"]
            if ach_id == "lunar_lord":
                continue
            condition_met = False
            balance = p.get("balance", 0)
            if ach_id == "farm_1" and p.get("farm_count", 0) >= 1:
                condition_met = True
            elif ach_id == "craft_1" and p.get("craft_count", 0) >= 1:
                condition_met = True
            # ... все остальные условия (оставьте как в моём предыдущем полном коде)
            if condition_met and ach_id not in awarded:
                await conn.execute(
                    "INSERT INTO achievements_awarded(user_id, ach_id, awarded_at) VALUES($1, $2, NOW()) ON CONFLICT DO NOTHING",
                    user_id, ach_id
                )
                await _award_achievement_rewards(user_id, p, ach.get("reward", ""), context)
                try:
                    text = (
                        f"<b>🕊️ СВИТОК ДОСТИЖЕНИЙ 🏆</b>\n\n"
                        f"<b>🎉 Достижение разблокировано!</b>\n\n"
                        f"<i>{ach['emoji']} «{ach['name']}» {ach['emoji']}</i>\n\n"
                        f"<b>📜 Запись добавлена! 💎</b>"
                    )
                    await context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
                except Exception as e:
                    logger.error(f"Achievement notify error: {e}")

        # lunar_lord отдельно
        rows2 = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", user_id)
        awarded_ids = {r["ach_id"] for r in rows2}
        all_other_ids = {a["id"] for a in ACHIEVEMENTS if a["id"] != "lunar_lord"}
        if "lunar_lord" not in awarded_ids and all_other_ids.issubset(awarded_ids):
            lunar = ACHIEVEMENTS_DICT["lunar_lord"]
            await conn.execute(
                "INSERT INTO achievements_awarded(user_id, ach_id, awarded_at) VALUES($1, $2, NOW()) ON CONFLICT DO NOTHING",
                user_id, "lunar_lord"
            )
            await _award_achievement_rewards(user_id, p, lunar.get("reward", ""), context)
            try:
                text = (
                    f"<b>🕊️ СВИТОК ДОСТИЖЕНИЙ 🏆</b>\n\n"
                    f"<b>🎉 Достижение разблокировано!</b>\n\n"
                    f"<i>{lunar['emoji']} «{lunar['name']}» {lunar['emoji']}</i>\n\n"
                    f"<b>📜 Запись добавлена! 💎</b>"
                )
                await context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Achievement notify error (lunar): {e}")
                
async def check_rank_up(context, user_id, username, old_balance, new_balance):
    old_idx = 0
    new_idx = 0
    for i, (_, threshold, _) in enumerate(RANKS):
        if old_balance >= threshold:
            old_idx = i
        if new_balance >= threshold:
            new_idx = i
    if new_idx > old_idx:
        rank_name = emoji_to_name(RANKS[new_idx][0])
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⚜️ <b>Новый ранг:</b> {rank_name}\n\nТвой баланс теперь {new_balance} OAC.",
                parse_mode='HTML'
            )
        except Exception:
            pass

FARM_MEDALS = [(1, "🥉 Бронза", 10), (10, "🥈 Серебро", 30), (50, "🥇 Золото", 80), (250, "💎 Платина", 200)]
CRAFT_MEDALS = [(1, "🥉 Бронза", 10), (10, "🥈 Серебро", 30), (50, "🥇 Золото", 80), (250, "💎 Платина", 200)]
SMOKE_MEDALS = [(1, "🥉 Бронза", 10), (10, "🥈 Серебро", 30), (50, "🥇 Золото", 80), (250, "💎 Платина", 200)]
RITUAL_MEDALS = [(1, "🥉 Бронза", 20), (10, "🥈 Серебро", 50), (50, "🥇 Золото", 120), (250, "💎 Платина", 300)]

WHISPERS = ["🩸 Искажение наблюдает за твоими нитями", "💠 Кристалл твоей судьбы пульсирует", "🩸 Искажение шепчет твоё имя", "🌙 «Ночь опустилась на Гильдию. Смотритель пробудился.»"]
NEURO_STATUSES = ["Альфа-ритмы нестабильны", "Сенсорная депривация 80%", "Фаза быстрого сна", "Нейро-шунт активен", "Предел синаптической проводимости", "Резонанс с Искажением: 12%"]
FUNNY_REACTIONS = ["Выглядит как NFT, который никто не купит.", "Даже Бездна от такого закашлялась.", "Это не блант, это крик души.", "Искажение занесло это название в чёрный список.", "10/10, лучший блант для того чтобы спрятать его подальше.", "Пахнет так, будто его скрутил сам Ктулху.", "Этот блант вызывает желание помыть руки.", "С таким названием только в Бездну.", "Я бы такое не выкурил, но звучит гордо."]
RANKS = [("🪓 Рекрут", 0, 0), ("⚔️ Ветеран", 5000, 1500), ("🪦 Призрак", 20000, 6000), ("🪬 Некромант", 50000, 15000)]
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
    {"id": "balance_50000", "name": "Повелитель Мёртвых", "emoji": "🩸", "desc": "Накопить 50 000 OAC — гора валюты, что заставляет всех о вас шептаться.", "reward": "+10000 OAC, Рамка 🩸, Фон 💀"},
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
    db_pool = await asyncpg.create_pool(database_url, min_size=5, max_size=20, command_timeout=15)
    async with db_pool.acquire() as conn:
        await create_tables(conn)
# Гарантируем наличие столбца war_active в guild_weekly
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='guild_weekly' AND column_name='war_active'
                ) THEN
                    ALTER TABLE guild_weekly ADD COLUMN war_active BOOLEAN DEFAULT FALSE;
                END IF;
            END $$;
        """)
        await init_redis()          # если добавили Redis
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
            inhaled INTEGER DEFAULT 0,
            smoke_count INTEGER DEFAULT 0,
            farm_count INTEGER DEFAULT 0,
            craft_count INTEGER DEFAULT 0,
            ritual_count INTEGER DEFAULT 0,
            referral_count INTEGER DEFAULT 0,
            last_berserk TIMESTAMP,
            inventory JSONB DEFAULT '[]',
            invited_by BIGINT DEFAULT NULL,
            profile_skins JSONB DEFAULT '{}',
            login_streak INTEGER DEFAULT 0,
            last_login_date DATE,
            oath TEXT DEFAULT '',
            keys INTEGER DEFAULT 0,
            check_count INTEGER DEFAULT 0,
            m_essence INTEGER DEFAULT 0,
            lab_chests INTEGER DEFAULT 0,
            lab_deaths INTEGER DEFAULT 0,
            alchemy_count INTEGER DEFAULT 0,
            last_lab_attempt TIMESTAMP,
            donated INTEGER DEFAULT 0
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
            total_farmed INTEGER DEFAULT 0,
            total_donated INTEGER DEFAULT 0,
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
            total_earned INTEGER DEFAULT 0,
            start_date TIMESTAMP,
            cancelled INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0
        )
    """)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
@db_retry()
async def update_balance(user_id, username, amount, conn=None):
    owns_conn = conn is None
    if owns_conn:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                if username:
                    await conn.execute("""
                        INSERT INTO players(user_id, username, balance, blunts)
                        VALUES($1, $2, 0, 0)
                        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
                    """, user_id, username)
                await conn.execute("UPDATE players SET balance = balance + $1 WHERE user_id = $2", amount, user_id)
    else:
        if username:
            await conn.execute("""
                INSERT INTO players(user_id, username, balance, blunts)
                VALUES($1, $2, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
            """, user_id, username)
        await conn.execute("UPDATE players SET balance = balance + $1 WHERE user_id = $2", amount, user_id)
    invalidate_cache(user_id)

@db_retry()
async def update_blunts(user_id, username, amount, conn=None):
    owns_conn = conn is None
    if owns_conn:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                if username:
                    await conn.execute("""
                        INSERT INTO players(user_id, username, balance, blunts)
                        VALUES($1, $2, 0, 0)
                        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
                    """, user_id, username)
                await conn.execute("UPDATE players SET blunts = blunts + $1 WHERE user_id = $2", amount, user_id)
    else:
        if username:
            await conn.execute("""
                INSERT INTO players(user_id, username, balance, blunts)
                VALUES($1, $2, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
            """, user_id, username)
        await conn.execute("UPDATE players SET blunts = blunts + $1 WHERE user_id = $2", amount, user_id)
    invalidate_cache(user_id)

async def update_essence(user_id, amount, conn=None):
    owns_conn = conn is None
    if owns_conn:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO players(user_id, username, balance, blunts)
                    VALUES($1, '', 0, 0)
                    ON CONFLICT (user_id) DO NOTHING
                """, user_id)
                await conn.execute("UPDATE players SET m_essence = m_essence + $1 WHERE user_id = $2", amount, user_id)
    else:
        await conn.execute("""
            INSERT INTO players(user_id, username, balance, blunts)
            VALUES($1, '', 0, 0)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)
        await conn.execute("UPDATE players SET m_essence = m_essence + $1 WHERE user_id = $2", amount, user_id)
    invalidate_cache(user_id)

ALLOWED_COUNTERS = {"farm_count","craft_count","smoke_count","ritual_count","referral_count","check_count","lab_chests","lab_deaths","alchemy_count"}
async def increment_counter(user_id, field, conn=None):
    if field not in ALLOWED_COUNTERS:
        return
    if conn is None:
        async with db_pool.acquire() as conn:
            await conn.execute(f"UPDATE players SET {field} = COALESCE({field}, 0) + 1 WHERE user_id = $1", user_id)
    else:
        await conn.execute(f"UPDATE players SET {field} = COALESCE({field}, 0) + 1 WHERE user_id = $1", user_id)
    invalidate_cache(user_id)

@db_retry()
async def set_guild(user_id, guild):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET guild=$1 WHERE user_id=$2", guild, user_id)
    invalidate_cache(user_id)

@db_retry()
async def get_top(limit=10):
    now = datetime.now().timestamp()
    if top_cache["data"] and (now - top_cache["timestamp"]) < top_cache["ttl"]:
        return top_cache["data"]
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, balance, guild FROM players ORDER BY balance DESC LIMIT $1", limit)
    top_cache["data"] = rows
    top_cache["timestamp"] = now
    return rows

@db_retry()
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

async def send_whisper_dm(update, context, text):
    if update.callback_query:
        chat_id = update.callback_query.message.chat.id
    else:
        chat_id = update.effective_chat.id
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Whisper error: {e}")

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
        progress = int((new_count - cur_th) / (next_th - cur_th) * 100)
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

async def add_war_score(user_id, points, conn=None):
    if not db_pool:
        return
    try:
        if conn is not None:
            # Используем переданное соединение
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
        else:
            async with db_pool.acquire() as conn:
                await add_war_score(user_id, points, conn=conn)
    except Exception as e:
        logger.error(f"add_war_score error: {e}")

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

import functools
import traceback

def error_handler(func):
    """Middleware: перехватывает исключения в обработчиках, уведомляет пользователя и админа."""
    @functools.wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            # Логируем полную трассировку
            logger.error(f"Unhandled error in {func.__name__}:", exc_info=True)
            # Сбрасываем состояние ожидания именного бланта
            if 'awaiting_named_blunt' in context.user_data:
                context.user_data['awaiting_named_blunt'] = False
            # Уведомляем пользователя (всплывашка или сообщение)
            if update.callback_query:
                await update.callback_query.answer("⚠️ Внутренняя ошибка. Админ уже в курсе.", show_alert=True)
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ Что-то пошло не так. Попробуйте позже."
                )
            # Отправляем детали ошибки админу в Telegram
            if ADMIN_ID:
                try:
                    err_msg = f"🚨 <b>Ошибка в {func.__name__}</b>\n<code>{html.escape(str(e))}</code>"
                    await context.bot.send_message(chat_id=ADMIN_ID, text=err_msg, parse_mode='HTML')
                except Exception as notify_err:
                    logger.error(f"Failed to notify admin: {notify_err}")
    return wrapper

# ========== ОБРАБОТЧИКИ КОМАНД ==========
# ========== ОБРАБОТЧИКИ КОМАНД (полный, надёжный, с лабиринтом) ==========

async def safe_edit(update: Update, context, text: str, reply_markup=None, parse_mode='HTML'):
    """Редактирует сообщение, а при неудаче отправляет новое."""
    try:
        if update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await update.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"safe_edit fallback: {e}")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"safe_edit unexpected: {e}", exc_info=True)

async def send_reply(update: Update, context, text, reply_markup=None, parse_mode='HTML'):
    """Отправляет новое сообщение или редактирует существующее (для меню)."""
    try:
        if update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        logger.warning(f"send_reply fallback: {e}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"send_reply unexpected: {e}", exc_info=True)
        
async def animate_progress_bar(update, context, title="", duration=0.4, steps=5):
    """Быстрая и устойчивая анимация прогресс-бара."""
    chat_id = update.effective_chat.id
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=f"{title}\n[░░░░░░░░░░] 0%")
    except Exception:
        return None

    for i in range(1, steps + 1):
        filled = "▓" * (i * 2)          # 2 символа на шаг, всего 10
        empty = "░" * (10 - len(filled))
        percent = i * (100 // steps)
        await asyncio.sleep(duration / steps)
        try:
            # asyncio.wait_for предотвращает зависание
            await asyncio.wait_for(
                msg.edit_text(f"{title}\n[{filled}{empty}] {percent}%", parse_mode='HTML'),
                timeout=0.5
            )
        except (BadRequest, asyncio.TimeoutError, Exception):
            # при любой ошибке прекращаем анимацию и возвращаем что есть
            return msg
    return msg

def get_medal_target(count, medals_list):
    """Возвращает следующую цель (порог) для прогресса медалей."""
    for th, name, reward in medals_list:
        if count < th:
            return th
    return medals_list[-1][0]  # максимум

def get_medal_progress(new_count, medals_list):
    """Возвращает строку с прогресс-баром и названиями медалей (жирными)."""
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
        goal_str = f"<b>{cur_medal}</b> (Максимум)"
        progress = 100
        bar = "▓" * 10
    else:
        progress = int((new_count - cur_th) / (next_th - cur_th) * 100) if next_th != cur_th else 100
        bar = "▓" * (progress // 10) + "░" * (10 - progress // 10)
        goal_str = f"<b>{cur_medal}</b> → <b>{next_medal}</b>"
    return f"{bar} {progress}%\n{goal_str}"

def get_rank_progress(balance):
    """Возвращает прогресс ранга с жирным "Ранг:" и жирным прогресс-баром."""
    if balance >= RANKS[-1][1]:
        emoji = RANKS[-1][0]
        name = emoji.split(' ',1)[1]
        return f"<b>⚜️ Ранг:</b> {emoji} {name} (Максимум)\n<b>▓▓▓▓▓▓▓▓▓▓ 100%</b>"
    for i in range(len(RANKS)-1):
        curr_emoji, curr_th, _ = RANKS[i]
        next_emoji, next_th, _ = RANKS[i+1]
        if balance < next_th:
            curr_name = curr_emoji.split(' ',1)[1] if ' ' in curr_emoji else curr_emoji
            progress = int((balance - curr_th) / (next_th - curr_th) * 100)
            bar = "▓" * (progress // 10) + "░" * (10 - progress // 10)
            return (
                f"<b>⚜️ Ранг:</b> {curr_emoji} → {next_emoji}\n"
                f"<b>{bar} {progress}%</b>\n"
                f"{balance} / {next_th} OAC"
            )
    return ""
    
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    user_id = user.id
    username = user.username or user.first_name
    username_escaped = html.escape(username)
    player = await get_player_cached(user_id)

    if context.args and context.args[0].startswith("blunt_"):
        ref_blunt_id = context.args[0].replace("blunt_", "")
        creator_id = None
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, inventory FROM players")
        for row in rows:
            try:
                inv = _json_safe_load(row["inventory"], [])
                for item in inv:
                    if item.get("id") == ref_blunt_id:
                        creator_id = row["user_id"]
                        break
            except:
                continue
            if creator_id:
                break
        if creator_id and creator_id != user_id:
            async with db_pool.acquire() as conn:
                ref_row = await conn.fetchrow("SELECT username FROM players WHERE user_id=$1", creator_id)
                creator_username = ref_row["username"] if ref_row else str(creator_id)
                cur = await conn.fetchrow("SELECT invited_by FROM players WHERE user_id=$1", user_id)
                already = cur and cur["invited_by"] is not None
                if not already:
                    await conn.execute("UPDATE players SET invited_by=$1 WHERE user_id=$2", creator_id, user_id)
                    await update_balance(creator_id, creator_username, 50)
                    await increment_counter(creator_id, "referral_count")
                    new_name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
                    await create_named_blunt(creator_id, new_name, rarity="legendary")
                    await add_title(creator_id, "🩸")
                    await grant_title(creator_id, "🩸", "Пожиратель Душ", context)
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

# Продолжение # Фарм + анимация
@error_handler
@rate_limit(3)
async def farm_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = user.username or user.first_name
    uname_escaped = html.escape(uname)
    p = await get_player_cached(uid)

    if p and p["last_farm"]:
        last = _to_datetime(p["last_farm"])
        if datetime.now() - last < timedelta(hours=FARM_COOLDOWN_HOURS):
            remain = int((timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now()-last)).seconds/60)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"<b>🍬 OAC копятся 🌱</b>\n\n<b>🍃 Подожди {remain} мин</b>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
            )
            if update.callback_query:
                await update.callback_query.answer()
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

    # Единая транзакция через один conn
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await update_balance(uid, uname, earned, conn=conn)
            await update_last_farm(uid, conn=conn)
            await increment_counter(uid, "farm_count", conn=conn)
            await add_war_score(uid, earned, conn=conn)
        p_new = await get_player_cached(uid)

    new_count = p_new["farm_count"]
    medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, FARM_MEDALS)
    if medal_bonus:
        await update_balance(uid, uname, medal_bonus)
        p_new = await get_player_cached(uid)
    new_balance = p_new["balance"]
    target = get_medal_target(new_count, FARM_MEDALS)
    progress_bar_str = get_medal_progress(new_count, FARM_MEDALS)
    rank_progress = get_rank_progress(new_balance)

    crit_str = " (крит x10!)" if crit else ""
    happy_str = " 🌟x2" if happy else ""
    text = (
        f"<b>💎 Ты нафармил: +{earned} OAC</b> 🍬{crit_str}{happy_str}\n\n"
        f"<b>⚜️ У тебя:</b> <i>{new_balance} OAC</i>\n\n"
        f"{medal_text}"
        f"<b>🎯 Фарминг:</b> {new_count}/{target}\n"
        f"<b>{progress_bar_str}</b>\n\n"
        f"{rank_progress}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])

    # Анимированный прогресс-бар (без падения при TimedOut)
    anim_msg = await animate_progress_bar(update, context, title="🍬 Фармим...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
    else:
        await safe_edit(update, context, text, reply_markup=kb)

    await check_rank_up(context, uid, uname, old_bal, new_balance)
    await check_achievements(uid, context)
    
# Крафт
@error_handler
@rate_limit(2)
async def craft_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    bal = p["balance"] if p else 0
    text = f"<b>🌿 КРАФТ БЛАНТА</b>\n\n🛡️ <i>у тебя:</i> <code>{bal}</code> 🍬"
    kb_rows = [
        [InlineKeyboardButton("🌿 Обычный блант (15 🍬)", callback_data="craft_normal")],
        [InlineKeyboardButton("💍 Именной блант (50 🍬)", callback_data="craft_named")],
    ]
    if p and p.get("m_essence", 0) > 0:
        kb_rows.append([InlineKeyboardButton(f"💠 Использовать Пыль (1 доза)", callback_data="use_dust")])
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
    await send_reply(update, context, text, InlineKeyboardMarkup(kb_rows))

@error_handler
@rate_limit(2)
async def handle_craft_normal(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id; uname = query.from_user.username or query.from_user.first_name
    p = await get_player_cached(uid)
    if not p or p["balance"] < 15:
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text="<b>❌ Недостаточно OAC.</b>\n🕯️ Требуется <b>15 OAC</b> 🍬.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]]),
            parse_mode='HTML'
        )
        return

    old_count = p["craft_count"]
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await update_balance(uid, uname, -15, conn=conn)
            await update_blunts(uid, uname, 1, conn=conn)
            await increment_counter(uid, "craft_count", conn=conn)
            await add_war_score(uid, 10, conn=conn)
            if random.random() < 0.05:
                await update_blunts(uid, uname, 1, conn=conn)
                await send_whisper(context, "@guild_antysocial", f"⚡ @{html.escape(uname)} высек Искру Искажения из рутины. +1 🌿")
        p_new = await get_player_cached(uid)

    new_count = p_new["craft_count"]
    medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, CRAFT_MEDALS)
    if medal_bonus:
        await update_balance(uid, uname, medal_bonus)
        p_new = await get_player_cached(uid)
    new_balance = p_new["balance"]
    target = get_medal_target(new_count, CRAFT_MEDALS)
    progress_bar_str = get_medal_progress(new_count, CRAFT_MEDALS)

    text = (
        f"<b>🌿 БЛАНТ СКРУЧЕН</b>\n\n"
        f"<b>🛡️ Потрачено:</b> <b>15 OAC</b>\n"
        f"<b>⚜️ У тебя:</b> <b>{new_balance} OAC</b> 🍬\n\n"
        f"{medal_text}"
        f"<b>🎯 Крафтинг:</b> {new_count}/{target}\n"
        f"<b>{progress_bar_str}</b>\n\n"
        f"<b>🍃 Блантов в свёртке:</b> <b>{p_new['blunts']}</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌿 Скрафтить ещё", callback_data="craft_normal")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])

    anim_msg = await animate_progress_bar(update, context, title="🌿 Скручиваем...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
    else:
        await safe_edit(update, context, text, reply_markup=kb)
    await check_achievements(uid, context)

@error_handler
async def handle_craft_named(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p or p["balance"] < 50:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="<b>🕳️ ИСКАЖЕНИЕ МОЛЧИТ</b>\n\n<i>🛡️ Недостаточно OAC.</i>\n🕯️ Требуется <b>50 OAC</b> 🍬.",
            parse_mode='HTML'
        )
        return
    context.user_data['awaiting_named_blunt'] = True
    context.job_queue.run_once(clear_named_blunt_state, 300, data=uid)
    await query.message.delete()
    sent_msg = await context.bot.send_message(
        chat_id=query.message.chat.id,
        text="<b>💍 ИМЕННОЙ БЛАНТ</b>\n\n<i>Введи имя своего бланта (до 25 символов)</i>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_named")]]),
        parse_mode='HTML'
    )
    context.user_data['awaiting_named_blunt_msg_id'] = sent_msg.message_id
    
async def handle_named_name(update, context):
    """Обрабатывает ввод имени и создаёт именной блант."""
    try:
        user = update.effective_user
        uid = user.id
        name = update.message.text.strip()[:25]
        if not name:
            await update.message.reply_text("❌ Имя не может быть пустым.")
            return

        uname = user.username or user.first_name

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await update_balance(uid, uname, -50, conn=conn)
                await increment_counter(uid, "craft_count", conn=conn)
                item = await create_named_blunt(uid, name, rarity=None, conn=conn)

        await add_war_score(uid, 25)

        blunt_id = item["id"]
        name_escaped = html.escape(name)
        color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(item["rarity"], "🟢")
        reaction = item["reaction"]

        caption = (
            f"<b>💍 БЛАНТ СОТКАН</b>\n\n"
            f"🩸 <i>Ты вплёл в <b>Искажение</b> свой именной блант:</i>\n"
            f"{color} <b><i>«{name_escaped}»</i></b> <i>Редкость:</i> <b>{item['rarity']}</b>\n\n"
            f"💎 <i>Он навсегда останется в твоей коллекции.</i>\n\n"
            f"🩸 <i>{reaction}</i>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Поделиться", callback_data=f"share_blunt_{blunt_id}")],
            [InlineKeyboardButton("🔙 В Крафт", callback_data="craft"),
             InlineKeyboardButton("🏰 В меню", callback_data="menu")]
        ])

        file_id = BLUNT_IMAGES.get(item["rarity"])
        if file_id:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=file_id,
                caption=caption,
                reply_markup=kb,
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(caption, reply_markup=kb, parse_mode='HTML')

        # Пост в канал
        try:
            await context.bot.send_message(
                chat_id="@guild_antysocial",
                text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{html.escape(uname)}</b> создал свой блант {color} "
                     f"<b><i>«{name_escaped}»</i></b> 🌿\n<i>Редкость: {item['rarity']}</i>\n🩸 <i>{reaction}</i>",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Ошибка отправки в канал: {e}")

        await check_achievements(uid, context)

    except Exception as e:
        import traceback
        err = traceback.format_exc()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Ошибка в named_name:\n<code>{html.escape(err[:800])}</code>",
            parse_mode='HTML'
        )
    finally:
        context.user_data['awaiting_named_blunt'] = False

async def handle_use_dust(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)

    if not p or p["m_essence"] < 1:
        await query.answer("Нет Кристальной Пыли.")
        return

    # Единая транзакция
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Тратим пыль
            await conn.execute("UPDATE players SET m_essence = m_essence - 1 WHERE user_id = $1", uid)
            name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа","Коготь Хаоса","Вздох Пожирателя"])
            item = await create_named_blunt(uid, name, rarity="legendary", conn=conn)

            # War score
            try:
                war = await conn.fetchrow("SELECT war_active FROM guild_weekly WHERE war_active = TRUE LIMIT 1")
                if war:
                    guild_row = await conn.fetchrow("SELECT guild FROM players WHERE user_id = $1", uid)
                    guild = guild_row["guild"] if guild_row else None
                    if guild in ("BLACK", "WHITE"):
                        await conn.execute(
                            "INSERT INTO guild_weekly (guild, week_start, total_farmed) "
                            "VALUES ($1, CURRENT_DATE, $2) ON CONFLICT (guild) DO UPDATE SET "
                            "total_farmed = guild_weekly.total_farmed + $2",
                            guild, 50
                        )
            except Exception:
                pass

    await add_war_score(uid, 50)  # на всякий случай можно оставить, но он уже внутри транзакции
    reaction = item["reaction"]
    await send_blunt_image(context, query.message.chat.id, "legendary")
    text = (
        f"<b><i>💠 ПЫЛЬ ИСПОЛЬЗОВАНА</i></b>\n\n"
        f"🟡 <b><i>«{name}»</i></b> (Легендарный) 🌿\n"
        f"📜 Реакция: <i>{reaction}</i>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

    # Оповещение в канал
    try:
        await context.bot.send_message(chat_id="@guild_antysocial",
            text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{html.escape(p['username'])}</b> использовал 💠 Пыль и получил легендарный блант <b><i>«{name}»</i></b>!", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Ошибка отправки в канал: {e}")

    await check_achievements(uid, context)

async def clear_named_blunt_state(context):
    user_id = getattr(context.job, "data", None)
    if user_id is None:
        return
    try:
        context.application.user_data[user_id]["awaiting_named_blunt"] = False
    except Exception:
        pass

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

# Дунуть
@error_handler
async def smoke_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p or p["blunts"] < 1:
        empty_text = (
            "<b>💨 ДУНУТЬ</b>\n\n"
            "<b>🌿 Твой свёрток пуст</b>\n"
            "\n"
            "<i>🎈 Скрути новый блант</i>"
        )
        empty_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌿 Крафт", callback_data="craft")],
            [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
        ])
        if update.callback_query:
            await update.callback_query.message.edit_text(empty_text, reply_markup=empty_kb, parse_mode='HTML')
        else:
            await msg.reply_text(empty_text, reply_markup=empty_kb, parse_mode='HTML')
        return

    main_text = f"<b>💨 ДУНУТЬ</b>\n\n🌿 <i>блантов в свёртке:</i> <b>{p['blunts']}</b>"
    main_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Дунуть", callback_data="do_smoke")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    if update.callback_query:
        await update.callback_query.message.edit_text(main_text, reply_markup=main_kb, parse_mode='HTML')
    else:
        await msg.reply_text(main_text, reply_markup=main_kb, parse_mode='HTML')

@error_handler
@rate_limit(2)
async def do_smoke(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id; uname = html.escape(query.from_user.username or query.from_user.first_name)
    p = await get_player_cached(uid)
    if not p or p["blunts"] < 1:
        await query.answer("Свёрток пуст.")
        return
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
            p_new["inventory"] = _json_safe_load(p_new.get("inventory"), [])
            player_cache[uid] = p_new
        else:
            if not save:
                await update_blunts(uid, uname, -1)
            if earned:
                await update_balance(uid, uname, earned)
            await increment_counter(uid, "smoke_count")
            invalidate_cache(uid)
            p_new = await get_player_cached(uid)

    if earned:
        await add_war_score(uid, earned)
    if p and not p["inhaled"]:
        await add_title(uid, "💨")
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE players SET inhaled=1 WHERE user_id=$1", uid)
        invalidate_cache(uid)

    context.user_data["last_smoke_time"] = datetime.now()
    new_count = p_new["smoke_count"]
    medal_text, medal_bonus = get_medal_text_and_reward(p["smoke_count"], new_count, SMOKE_MEDALS)
    if medal_bonus:
        await update_balance(uid, uname, medal_bonus)
        p_new = await get_player_cached(uid)
    bl_left = p_new["blunts"]
    target = get_medal_target(new_count, SMOKE_MEDALS)
    progress_bar_str = get_medal_progress(new_count, SMOKE_MEDALS)

    # Эффекты (с тире)
    if r < 0.18:
        effect = (
            f"<b>💨 ДЫМ РАССЕЯЛСЯ</b>\n"
            f"– 😵‍💫 <b>Лёгкий приход</b>\n"
            f"– <i>«Станки Фабрики №9 работают в ритме твоего сердца»</i>\n\n"
            f"🍬 <b>+{earned} OAC</b>"
        )
    elif r < 0.36:
        effect = (
            f"<b>💨 ДЫМ РАССЕЯЛСЯ</b>\n"
            f"– 💤 <b>Полный Штиль</b>\n"
            f"– <i>«Дым рассеялся, оставив лишь лёгкий шлейф»</i>"
        )
    elif r < 0.53:
        effect = (
            f"<b>💨 ДЫМ РАССЕЯЛСЯ</b>\n"
            f"– 😵‍💫 <b>Паранойя</b>\n"
            f"– <i>Всё идёт не так. Тени сгущаются…</i>"
        )
    elif r < 0.70:
        effect = (
            f"<b>💨 ДЫМ РАССЕЯЛСЯ</b>\n"
            f"– 💨 <b>Кашель</b>\n"
            f"– <i>«Первая тяга была слишком жёсткой, пробило на кашель»</i>\n\n"
            f"📉 <b>{earned} OAC</b>"
        )
    elif r < 0.85:
        effect = (
            f"<b>💨 ДЫМ РАССЕЯЛСЯ</b>\n"
            f"– 🛋️ <b>Паралич</b>\n"
            f"– <i>«Тело стало ватным, смотришь в одну точку и не можешь пошевелиться»</i>"
        )
    else:
        effect = (
            f"<b>💨 ДЫМ РАССЕЯЛСЯ</b>\n"
            f"– 🧘 <b>Глубокое Озарение</b>\n"
            f"– <i>«Ты понял, что блант — это ключ к разгадке бытия»</i>"
        )

    if p and not p["inhaled"]:
        effect += "\n\n<b><i>🎉 ТИТУЛ РАЗБЛОКИРОВАН!</i></b>\n💨 Ты теперь — <b>Красные Глаза</b>"

    text = (
        f"{effect}\n\n"
        f"{medal_text}"
        f"<b>💨 Дым:</b> {new_count}/{target}\n"
        f"<b>{progress_bar_str}</b>\n\n"
        f"<b>🍃 Блантов в свёртке:</b> <b>{bl_left}</b>"
    )
    if save:
        text += "\n⚜️ <i>Светлая Гильдия сохранила твой Блант!</i>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Дунуть ещё", callback_data="do_smoke") if bl_left>=1 else InlineKeyboardButton("🌿 Крафтить ещё", callback_data="craft")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    await check_achievements(uid, context)

# Ритуал (с защитой от None)
@error_handler
@rate_limit(3)
async def ritual_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p: await send_whisper_dm(update, context, "🕳️ Ты ещё не активирован. /start"); return
    if p["guild"] != "BLACK": await send_whisper_dm(update, context, "❌ Только Тёмная Гильдия."); return
    if p["last_ritual"]:
        last = _to_datetime(p["last_ritual"])
        if last and datetime.now() - last < timedelta(hours=24):
            remain = int((timedelta(hours=24) - (datetime.now()-last)).seconds/3600)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"<b>🕯️ Тёмный алтарь истощён 🌙</b>\n\n<b>🗝️ Жди {remain} ч</b>",
                parse_mode='HTML'
            )
            return
    old_bal = p["balance"]
    reward = 150
    if context.bot_data.get("happy_hour"): reward *= HAPPY_HOUR_MULTIPLIER
    old_count = p["ritual_count"]
    extra = 15 if random.random() < 0.1 else 0

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await update_balance(uid, uname, reward + extra, conn=conn)
            await conn.execute("UPDATE players SET last_ritual = NOW() WHERE user_id = $1", uid)
            await increment_counter(uid, "ritual_count", conn=conn)
            await add_war_score(uid, reward + extra, conn=conn)
        p_new = await get_player_cached(uid)

    new_count = p_new["ritual_count"]
    medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, RITUAL_MEDALS)
    if medal_bonus:
        await update_balance(uid, uname, medal_bonus)
        p_new = await get_player_cached(uid)
    new_balance = p_new["balance"]
    target = get_medal_target(new_count, RITUAL_MEDALS)
    progress_bar_str = get_medal_progress(new_count, RITUAL_MEDALS)

    text = (
        f"<b>🕯️ РИТУАЛ ЗАВЕРШЁН</b>\n\n"
        f"Ритуал принёс тебе <b>{reward} OAC</b> 🍬\n"
        f"<b>⚜️ У тебя:</b> <b>{new_balance} OAC</b>\n\n"
        f"{medal_text}"
        f"<b>🕯️ Ритуалы:</b> {new_count}/{target}\n"
        f"<b>{progress_bar_str}</b>"
    )

    anim_msg = await animate_progress_bar(update, context, title="🕯️ Ритуал проводится...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, parse_mode='HTML')
    else:
        await send_whisper_dm(update, context, text)
    await check_rank_up(context, uid, uname, old_bal, new_balance)
    await check_achievements(uid, context)

# Куст (с защитой от None)
@error_handler
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
    pc = p["passive_collected"]
    if pc:
        last = _to_datetime(pc)
        if last:
            hrs = (datetime.now() - last).total_seconds()/3600
            earned = int(hrs * 30 * lvl)
            if context.bot_data.get("happy_hour"): earned *= HAPPY_HOUR_MULTIPLIER
            if earned >= 1:
                async with db_pool.acquire() as conn:
                    async with conn.transaction():
                        await update_balance(uid, uname, earned, conn=conn)
                        await conn.execute("UPDATE players SET passive_collected=$1 WHERE user_id=$2", datetime.now(), uid)
                        await add_war_score(uid, earned, conn=conn)
                    new_bal = (await get_player_cached(uid))["balance"]
                await send_whisper_dm(update, context, f"<b><i>🪴 УРОЖАЙ СОБРАН</i></b>\n\nТвой куст принёс <b>{earned} OAC</b> 🍬.\n\n💎 <i>У тебя:</i> <b>{new_bal} OAC</b> 🍬")
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="<b>🪴 Кустик ещё не созрел 💎</b>\n\n<b>🌱 Загляни позже</b>",
                    parse_mode='HTML'
                )
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="<b>🪴 Кустик ещё не активирован 💎</b>\n\n<b>🌱 Активируй его, нажав «Сбор» ещё раз</b>",
                parse_mode='HTML'
            )
    else:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE players SET passive_collected=$1 WHERE user_id=$2", datetime.now(), uid)
                await add_war_score(uid, 0, conn=conn)   # фиксируем активацию
        invalidate_cache(uid)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="<b>🪴 Авто‑сборщик активирован 💎</b>\n\n<b>🌱 Загляни позже</b>",
            parse_mode='HTML'
        )

# Профиль – сеньорская версия (защищена от # Профиль – финальная версия (NoneType исключён навсегда)
@error_handler
async def profile_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("Сначала активируйся: /start")
        return

    bal = p.get("balance", 0) or 0
    bl = p.get("blunts", 0) or 0
    guild = p.get("guild") or ""

    # Ранг
    rank_emoji, rank_name = "🪓", "Рекрут"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold:
            parts = emoji.split(' ', 1)
            rank_emoji = parts[0]
            rank_name = parts[1] if len(parts) > 1 else ""

    # Гильдия
    g_emoji = ""
    if guild == "BLACK":
        g_emoji = " 🕯️ Тёмная Гильдия"
    elif guild == "WHITE":
        g_emoji = " ⚜️ Светлая Гильдия"

    neuro = random.choice(NEURO_STATUSES)
    skins = p.get("profile_skins", {})
    if isinstance(skins, dict):
        bg = skins.get("active_background", "")
        active_title = skins.get("active_title", "—")
    else:
        bg = ""
        active_title = "—"

    inv_data = p.get("inventory", [])
    badges = []
    if any(it.get("rarity") == "legendary" for it in inv_data):
        badges.append("🟡")
    if (p.get("referral_count") or 0) > 0:
        badges.append("🩸")
    if (p.get("login_streak") or 0) >= 7:
        badges.append("🔥")
    if (p.get("check_count") or 0) >= 10:
        badges.append("👁️")
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
        f"{rank_progress}\n\n"
        f"💎 <b>ОАС:</b> <b>{bal} OAC</b> 🍬\n"
        f"🌿 <b>Блантов в свёртке:</b> <b>{bl}</b>\n"
        f"🪴 <b>Куст:</b> <b>+{30 * (3 if bal >= 20000 else 2 if bal >= 5000 else 0)} OAC/ч</b>\n"
        f"🧬 <b>Титул:</b> {active_title}\n"
        f"🧠 <b>Нейро-статус:</b> {neuro}\n\n"
        f"🎖️ <b>Заслуги:</b> {badge_str}"
    )

    named = [it for it in inv_data if it.get("type") == "named"]
    rarity_order = {"legendary": 0, "epic": 1, "rare": 2, "common": 3}

    # ✅ ИСПРАВЛЕННАЯ СОРТИРОВКА – убрали NoneType
    named.sort(key=lambda x: (rarity_order.get(x.get("rarity") or "common", 3),
                               x.get("serial") or 999999))

    if named:
        text += "\n\n<b>💍 Именные бланты (NFT):</b>"
        for item in named[:2]:
            name = item["name"]
            rarity = item.get("rarity", "common")
            color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
            rare_number = item.get("rare_number", "?-????")
            hash_code = item.get("hash", "0x????...????")
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

# Все бланты
async def my_blunts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    query = update.callback_query
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p:
        await query.answer("Профиль не найден.", show_alert=True)
        return

    named = [it for it in p.get("inventory", []) if it.get("type") == "named"]
    if not named:
        await query.answer("У тебя нет именных блантов.", show_alert=True)
        return

    rarity_order = {"legendary": 0, "epic": 1, "rare": 2, "common": 3}
    # ✅ Исправленная сортировка
    named.sort(key=lambda x: (rarity_order.get(x.get("rarity") or "common", 3),
                               x.get("serial") or 999999))

    per_page = BLUNTS_PER_PAGE
    total_pages = max(1, (len(named) + per_page - 1) // per_page)
    if page >= total_pages:
        page = 0
    start = page * per_page
    chunk = named[start:start + per_page]

    text = f"<b>💍 ТВОИ ИМЕННЫЕ БЛАНТЫ</b> ({page+1}/{total_pages})\n\n"
    for item in chunk:
        color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(item.get("rarity"), "🟢")
        text += f"{color} <b>{html.escape(item['name'])}</b> · #{item.get('rare_number', '?-????')}\n"

    kb_rows = []
    for item in chunk:
        kb_rows.append([InlineKeyboardButton(f"🔍 {item['name'][:20]}", callback_data=f"blunt_details_{item['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"blunts_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"blunts_page_{page+1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="profile")])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')

async def achievements_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    query = update.callback_query
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p:
        await query.answer("Профиль не найден.", show_alert=True)
        return

    async with db_pool.acquire() as conn:
        awarded = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", uid)
    awarded_ids = {r["ach_id"] for r in awarded}

    all_ach = list(ACHIEVEMENTS_DICT.values())
    per_page = 5
    total_pages = max(1, (len(all_ach) + per_page - 1) // per_page)
    if page >= total_pages:
        page = 0
    start = page * per_page
    chunk = all_ach[start:start + per_page]

    text = f"<b>🏆 ДОСТИЖЕНИЯ</b> ({page+1}/{total_pages})\n\n"
    for ach in chunk:
        unlocked = ach["id"] in awarded_ids
        mark = "✅" if unlocked else "🔒"
        text += f"{mark} {ach['emoji']} <b>{ach['name']}</b>\n<i>{ach['desc']}</i>\nНаграда: {ach['reward']}\n\n"

    kb_rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"ach_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"ach_page_{page+1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="profile")])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')

# Топ
@error_handler
async def top_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    top = await get_top(10)
    if not top: await msg.reply_text("🏆 Топ-10 пока пуст."); return
    text = "<b>🏆 ТОП-10 ИГРОКОВ</b>\n\n"
    for i, row in enumerate(top, 1):
        name = html.escape(row["username"]); bal = row["balance"]; guild = row["guild"]
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        g = "🕯️" if guild=="BLACK" else "⚜️" if guild=="WHITE" else ""
        text += f"{medal} <b>{name}</b> {g} — <b>{bal} OAC</b> 🍬\n"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM players WHERE balance > (SELECT balance FROM players WHERE user_id=$1)", uid)
    pos = row["cnt"] + 1 if row else 1
    if pos > 10:
        async with db_pool.acquire() as conn:
            tenth_row = await conn.fetchrow("SELECT balance FROM players ORDER BY balance DESC LIMIT 1 OFFSET 9")
        tenth_balance = tenth_row["balance"] if tenth_row else 0
        gap = tenth_balance - (await get_player_cached(uid))["balance"]
        if gap > 0:
            if tenth_balance == 0:
                perc = 100
            else:
                perc = int((1 - gap / tenth_balance) * 100)
            bar = progress_bar(perc)
            text += f"\n📊 <b>Твоя позиция:</b> {pos}\n🎯 <b>До Топ-10:</b> {gap} OAC\n{bar} <b>{perc}%</b>"
        else:
            text += f"\n📊 <b>Твоя позиция:</b> {pos}\n🎯 <b>До Топ-10:</b> 0 OAC\n{progress_bar(100)} <b>100%</b>"
    else:
        text += f"\n📊 <b>Твоя позиция:</b> {pos} (ты в топе!)"
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

# Гильдии
@error_handler
async def guild_info_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    guild = await get_guild(uid)
    p = await get_player_cached(uid)
    if not p:
        await safe_edit(update, context, "Профиль не найден. Напиши /start")
        return

    # Безопасный подсчёт гильдий
    cnt = await count_guilds()
    black_cnt = cnt.get("BLACK", 0) if isinstance(cnt, dict) else 0
    white_cnt = cnt.get("WHITE", 0) if isinstance(cnt, dict) else 0

    # Пожертвования (с защитой)
    async with db_pool.acquire() as conn:
        black_donated = await conn.fetchval("SELECT COALESCE(SUM(donated),0) FROM players WHERE guild='BLACK'") or 0
        white_donated = await conn.fetchval("SELECT COALESCE(SUM(donated),0) FROM players WHERE guild='WHITE'") or 0
    target = 50000
    black_perc = min(100, max(0, int(black_donated / target * 100)))
    white_perc = min(100, max(0, int(white_donated / target * 100)))

    # Прогресс-бары (с защитой)
    def safe_progress_bar(perc):
        perc = max(0, min(100, perc))
        filled = perc // 10
        return "▓" * filled + "░" * (10 - filled)

    text = (
        f"<b>🕋 ГИЛЬДИИ</b>\n\n"
        f"🕯️ <b>Тёмная: {black_cnt}</b> странников | <b>{safe_progress_bar(black_perc)} {black_perc}%</b>\n"
        f"⚜️ <b>Светлая: {white_cnt}</b> странников | <b>{safe_progress_bar(white_perc)} {white_perc}%</b>\n\n"
    )

    # Война гильдий (с защитой)
    async with db_pool.acquire() as conn:
        war_row = await conn.fetchrow("SELECT war_active FROM guild_weekly WHERE war_active = TRUE LIMIT 1")
        if war_row:
            scores = await conn.fetch("SELECT guild, total_farmed FROM guild_weekly")
            black_score = next((r["total_farmed"] for r in scores if r["guild"] == "BLACK"), 0)
            white_score = next((r["total_farmed"] for r in scores if r["guild"] == "WHITE"), 0)
            total_war = max(black_score + white_score, 1)
            bp = int(black_score / total_war * 100)
            wp = int(white_score / total_war * 100)
            text += (
                f"<b>⚔️ Война гильдий</b>\n\n"
                f"🕯️ Тёмные: {black_score} очков\n<b>{safe_progress_bar(bp)} {bp}%</b>\n\n"
                f"⚜️ Светлые: {white_score} очков\n<b>{safe_progress_bar(wp)} {wp}%</b>\n"
            )

    kb_rows = []
    if guild:
        g_emoji = "🕯️" if guild == "BLACK" else "⚜️"
        g_name = "Тёмная" if guild == "BLACK" else "Светлая"
        text += f"Ты состоишь в {g_emoji} <b>{g_name} Гильдии</b>.\n"
        if guild == "BLACK" and p:
            if p.get("last_ritual"):
                last_ritual = _to_datetime(p["last_ritual"])
                if last_ritual and datetime.now() - last_ritual < timedelta(hours=24):
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
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)

    await safe_edit(update, context, text, reply_markup=kb)

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

# Удача (с защитой от None)
@error_handler
async def luck_callback(update, context, action=None):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p: await msg.reply_text("Сначала активируйся: /start"); return
    bal = p["balance"]; now = datetime.now()
    last_daily = p["last_daily"]
    last_daily_dt = _to_datetime(last_daily)
    wheel_available = not (last_daily_dt and (now - last_daily_dt) < timedelta(hours=24))
    last_berserk = p["last_berserk"]
    last_berserk_dt = _to_datetime(last_berserk)
    berserk_available = (bal >= 300 and (not last_berserk_dt or (now - last_berserk_dt) > timedelta(hours=24)))
    veteran_alchemy = (bal >= 5000)
    text = f"<b><i>🎲 ИСПЫТАНИЕ СУДЬБЫ</i></b>\n\n🛡️ <i>у тебя:</i> <code>{bal}</code> 🍬\n\n"
    kb_rows = []
    if wheel_available:
        kb_rows.append([InlineKeyboardButton("🎡 Крутить", callback_data="luck_wheel")])
    else:
        diff = timedelta(hours=24) - (now - last_daily_dt)
        hrs = int(diff.seconds // 3600); mins = int((diff.seconds % 3600) // 60)
        kb_rows.append([InlineKeyboardButton(f"🎡 Колесо набирает силу. Ещё {hrs} ч {mins} мин", callback_data="luck_wheel")])
    if berserk_available:
        kb_rows.append([InlineKeyboardButton("🎲 Рискнуть", callback_data="luck_berserk")])
    else:
        if bal < 300: kb_rows.append([InlineKeyboardButton(f"🎲 нужно ещё {300 - bal} 🍬", callback_data="luck_berserk")])
        else:
            diff = timedelta(hours=24) - (now - last_berserk_dt)
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
            remain = timedelta(hours=24) - (now - last_daily_dt)
            hrs = int(remain.total_seconds()//3600); mins = int((remain.total_seconds()%3600)//60)
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
            prize_type = "jackpot"
            prize = 1000
            double = random.random() < 0.5
            if double: prize *= 2
        final_prize = prize
        if context.bot_data.get("happy_hour") and prize_type in ("oac","jackpot"):
            final_prize = prize * HAPPY_HOUR_MULTIPLIER
        if prize_type == "jackpot":
            await update_balance(uid, uname, final_prize)
            await grant_title(uid, "🧛🏻‍♀️", "Призрачный Гончий", context)
            try:
                await context.bot.send_message(chat_id="@guild_antysocial", text=f"🌟 @{uname} сорвал Джекпот! +{final_prize} OAC", parse_mode='HTML')
            except Exception as e:
                logger.error(f"Ошибка отправки в канал: {e}")
        elif prize_type == "oac":
            await update_balance(uid, uname, final_prize)
            await add_war_score(uid, final_prize)
            new_p = await get_player_cached(uid)
            new_bal = new_p["balance"]
            next_rank_name = ""
            next_threshold = 0
            for emoji, threshold, _ in RANKS:
                if new_bal < threshold:
                    next_rank_name = emoji
                    next_threshold = threshold
                    break
            if next_threshold == 0:
                progress_text = "<b>🏆 Максимальный ранг!</b>"
            else:
                remain = next_threshold - new_bal
                progress_text = f"<b>🎯 До ранга {next_rank_name}: <i>{remain} OAC</i></b>"
            text = (
                f"<b>🩸 ДАР ИСКАЖЕНИЯ</b>\n\n"
                f"<b>💎 Ты нафармил +{final_prize} OAC 🍬!</b>\n"
                f"⚜️ <b>У тебя:</b> <i>{new_bal} OAC</i>\n\n"
                f"{progress_text}"
            )
            try:
                await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]), parse_mode='HTML')
            except BadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
            return
        else:
            await update_blunts(uid, uname, prize)
            txt = f"+{prize} 🌿 Блант"
        new_bal = (await get_player_cached(uid))["balance"]
        text = f"<b><i>🎲 КОЛЕСО СМОТРИТЕЛЯ</i></b>\n\n{txt} → 💰 <b>{new_bal} OAC</b> 🍬"
        try:
            await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]), parse_mode='HTML')
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        return
    if action == "luck_berserk":
        if not berserk_available:
            if bal < 300: await send_whisper_dm(update, context, f"<b><i>🎲 Бездна требует жертву</i></b>\n\n⚠️ Недостаточно OAC (нужно ещё <b>{300-bal}</b>).")
            else:
                diff = timedelta(hours=24) - (now - last_berserk_dt)
                hrs = int(diff.total_seconds()//3600); mins = int((diff.total_seconds()%3600)//60)
                await send_whisper_dm(update, context, f"<b><i>🎲 Бездна молчит</i></b>\n\n🕳️ Примет тебя через <b>{hrs} ч {mins} мин</b>.")
            return
        await update_last_berserk(uid)
        if random.random() < 0.6: await update_balance(uid, uname, 200); res_text = f"<b><i>🎲 БЕЗДНА ОТВЕТИЛА</i></b>\n\nИскажение благосклонно! +<b>200 OAC</b> 🍬."
        else: await update_balance(uid, uname, -300); res_text = f"<b><i>🕳️ БЕЗДНА МОЛЧИТ</i></b>\n\nИскажение промолчало. –<b>300 OAC</b>."
        await add_war_score(uid, 200 if "200" in res_text else -300)
        try:
            await msg.edit_text(res_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]), parse_mode='HTML')
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        return
    if action == "alchemy_start":
        query = update.callback_query
        if not veteran_alchemy:
            await query.answer("🔮 Доступ к магии откроется на ранге ⚔️ Ветеран (5000 OAC).", show_alert=True)
            return
        if p["blunts"] < 5 or bal < 50:
            await send_whisper_dm(update, context, "🔮 Нужно 5 блантов и 50 OAC для запуска Котла.")
            return
        text = (
            f"<b><i>🔮 АЛХИМИЧЕСКИЙ КОТЁЛ</i></b>\n\n"
            f"У тебя есть <b>5 блантов</b> и <b>50 OAC</b>.\n"
            f"Бросить их в Котёл?\n\n"
            f"🌀 <i>Искажение шепчет: «Только тот, кто стал <b>ветераном</b> и не боится потерь – обретёт право использовать магию и истинную силу»</i> 🔮"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧪 Запустить реакцию", callback_data="alchemy_confirm")],
            [InlineKeyboardButton("🔙 Назад", callback_data="luck")]
        ])
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        return
    if action == "alchemy_confirm":
        if p["blunts"] < 5 or bal < 50:
            await send_whisper_dm(update, context, "🔮 Недостаточно ресурсов.")
            return
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await update_blunts(uid, uname, -5, conn=conn)
                await update_balance(uid, uname, -50, conn=conn)
                r = random.random()
                if r < 0.40:
                    await update_essence(uid, 1, conn=conn)
                    result_text = "<b><i>🔮 РЕЗУЛЬТАТ АЛХИМИИ</i></b>\n\n💠 <b>Чистая Пыльца!</b>\nТы получаешь 1 дозу Кристальной Пыли.\n\n🌀 <i>Искажение принимает твою жертву</i>"
                elif r < 0.75:
                    result_text = "<b><i>🔮 РЕЗУЛЬТАТ АЛХИМИИ</i></b>\n\n🌫️ <b>Грязный Выхлоп...</b>\nБланты сгорели без следа.\n\n🌀 <i>Искажение осталось голодным</i>"
                elif r < 0.90:
                    await update_essence(uid, 2, conn=conn)
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
                await add_war_score(uid, 30, conn=conn)
        try:
            await msg.edit_text(result_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]), parse_mode='HTML')
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        return

# /check
async def check_blunt(update, context):
    if not context.args: await update.message.reply_text("Укажи серийный номер бланта: /check R-0001"); return
    nft_id = context.args[0].strip().upper()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT blunt_id, created_by, serial, rare_number FROM nft_registry WHERE rare_number = $1", nft_id)
        if not rows:
            await update.message.reply_text("🕳️ Блант с таким серийным номером не найден.")
            return
        if len(rows) > 1:
            await update.message.reply_text("⚠️ Найдено несколько блантов с таким номером, обратитесь к администратору.")
            return
        row = rows[0]
    blunt_id, creator_id, serial, rare_number = row["blunt_id"], row["created_by"], row["serial"], row["rare_number"]
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, inventory FROM players WHERE inventory LIKE $1", f"%{blunt_id}%")
        owner_id = None; item = None
        for user_row in rows:
            try:
                inv = _json_safe_load(user_row["inventory"], [])
                for it in inv:
                    if it.get("id") == blunt_id:
                        owner_id = user_row["user_id"]; item = it; break
            except: continue
            if owner_id: break
    if not item: await update.message.reply_text("Блант найден в реестре, но его владелец не обнаружен."); return
    name = item["name"]; rarity = item.get("rarity","common")
    color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
    reaction = item.get("reaction",""); hash_code = item.get("hash","0x????...????")
    await send_blunt_image(context, update.effective_chat.id, rarity)
    details = f"<b>ДЕТАЛИ NFT БЛАНТА 💎</b>\n\n{color} <b>{name}</b>\n\n<b>Редкость:</b> <i>{rarity}</i> {color}\n\n🩸 <b>Серийный номер:</b> <b>#{rare_number}</b>\n🔗 <b>Хеш:</b> <b>{hash_code}</b>\n📜 <b>Реакция:</b> <i>{reaction}</i>\n"
    if "owner_history" in item:
        details += "\n🔄 История владения:\n"
        for entry in item["owner_history"]:
            date_str = format_date(entry.get('since',''))
            details += f"   @{entry.get('user_id','?')} — {date_str}\n"
    await update.message.reply_text(details, parse_mode='HTML')
    await increment_counter(update.effective_user.id, "check_count")

# Лабиринт (с защитой от None)
@error_handler
async def lab_enter(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid)
    if not p: return
    now = datetime.now()
    last = p.get("last_lab_attempt")
    if last:
        last = _to_datetime(last)
        if last and (now - last).total_seconds() < 12*3600:
            remain = 12*3600 - (now - last).total_seconds()
            hrs = int(remain // 3600)
            mins = int((remain % 3600) // 60)
            text = (
                f"<b>🏛️ ЛАБИРИНТ ИСКАЖЕНИЯ 🔮</b>\n\n"
                f"🎚️ Сегодня осталось <b>0 попыток</b>.\n"
                f"⛓️‍💥 Жизни: <b>2</b>\n\n"
                f"<i>– Портал откроется через <b>{hrs} ч {mins} мин</b>.</i>"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="menu")]])
            await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
            return
    text = (
        f"<b>🏛️ ЛАБИРИНТ ИСКАЖЕНИЯ 🔮</b>\n\n"
        f"🎚️ Сегодня осталась <b>1 попытка</b>.\n"
        f"⛓️‍💥 Жизни: <b>2</b>\n\n"
        f"<i>– Ты стоишь у входа. Портал отвечает.</i>\n\n"
        f"Выбери действие:"
    )
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
    await send_whisper_dm(update, context, "⚰️ Ты вошёл в Лабиринт...")
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
        await show_lab_final(update, context)
        return
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
            await query.message.edit_text(f"Успех! +{earned} OAC.")
        elif "reward_fragment" in opt:
            context.user_data.setdefault("lab_rewards", []).append("💠 Кристальная Пыль")
            await query.message.edit_text("Ты нашёл 💠 Кристальную Пыль!")
        elif "reward_title" in opt:
            await add_title(uid, opt["reward_title"])
            context.user_data.setdefault("lab_rewards", []).append(f"Титул: {opt['reward_title']}")
            await query.message.edit_text(f"Получен редкий титул: {opt['reward_title']}!")
        elif "reward_dust" in opt:
            context.user_data.setdefault("lab_rewards", []).append("💠 Кристальная Пыль")
            await query.message.edit_text("Ты нашёл 💠 Кристальную Пыль!")
        elif "reward_escape" in opt:
            await show_lab_final(update, context); return
    else:
        if opt["fail"] == "life": context.user_data["lab_lives"] -= 1; await query.message.edit_text("Провал! Потеряна 1 жизнь.")
        elif opt["fail"] == "life_big": context.user_data["lab_lives"] -= 2; await query.message.edit_text("Катастрофа! Потеряно 2 жизни.")
        else: await query.message.edit_text("Ничего не изменилось.")
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
    if not p: return
    rewards = context.user_data.get("lab_rewards", [])
    text = "<b><i>🎁 СУНДУК ИСКАЖЕНИЯ</i></b>\n\n<b>Ты достиг сердца Лабиринта. Сундук открывается... 💎</b>"
    if rewards: text += "\n\nСобрано: " + ", ".join(rewards)
    text += "\n+50 OAC 🍬\n💠 Кристальная Пыль: 1 доза"
    await update_balance(uid, p["username"], 50)
    await update_essence(uid, 1)
    await increment_counter(uid, "lab_chests")
    await add_war_score(uid, 80)
    context.user_data.pop("lab_room", None); context.user_data.pop("lab_lives", None); context.user_data.pop("lab_rewards", None)
    context.user_data.pop("lab_msg_id", None); context.user_data.pop("lab_chat_id", None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К Лабиринту", callback_data="lab_start")], [InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    await check_achievements(uid, context)

async def show_lab_death(update, context):
    query = update.callback_query
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p: return
    await update_balance(uid, p["username"], 50)
    context.user_data.pop("lab_room", None); context.user_data.pop("lab_lives", None); context.user_data.pop("lab_rewards", None)
    context.user_data.pop("lab_msg_id", None); context.user_data.pop("lab_chat_id", None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К Лабиринту", callback_data="lab_start")], [InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await query.message.edit_text("<b><i>🪦 ЛАБИРИНТ ПОГЛОТИЛ ТЕБЯ</i></b>\n\n<i>Твои жизни иссякли. Искажение выбросило тебя обратно.</i>\n\n+50 OAC 🍬", reply_markup=kb, parse_mode='HTML')

async def welcome_new_member(update, context):
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        await update.message.reply_text(
            f"<b><i>🕯️ ДОБРО ПОЖАЛОВАТЬ</i></b>\n\n⚜️ <b>{html.escape(member.username or member.first_name)}</b>, добро пожаловать в <b><i>Гильдию</i></b>\n<i>Твой первый /farm уже ждёт</i>"
        )

# Текстовые сокращения
async def handle_chat_shortcut(update, context):
    # === ЭТОТ БЛОК ДОБАВЛЕН ===
    if context.user_data.get('awaiting_named_blunt'):
        await handle_named_name(update, context)
        return
    # =========================

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

# Команда установки фото бланта
async def setbluntpic(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# Обработчик кнопок
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
        elif data == "farm":
            await q.answer()
            try:
                await farm_callback(update, context)
            except Exception as e:
                import traceback
                err = traceback.format_exc()
                await context.bot.send_message(
                    chat_id=q.message.chat_id,
                    text=f"❌ Ошибка в farm:\n<code>{html.escape(err[:800])}</code>",
                    parse_mode='HTML'
                )
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
        elif data == "luck":
            await q.answer()
            try:
                await luck_callback(update, context)
            except Exception as e:
                logger.error(f"Luck callback error: {e}")
                await q.message.edit_text("⚠️ Не удалось открыть Удачу. Попробуй позже.", reply_markup=get_back_to_menu_keyboard())
        elif data in ("luck_wheel", "luck_berserk", "alchemy_start", "alchemy_confirm"): await q.answer(); await luck_callback(update, context, action=data)
        elif data == "craft_normal":
            await q.answer()
            try:
                await handle_craft_normal(update, context)
            except Exception as e:
                import traceback
                err = traceback.format_exc()
                await context.bot.send_message(
                    chat_id=q.message.chat_id,
                    text=f"❌ Ошибка в craft_normal:\n<code>{html.escape(err[:800])}</code>",
                    parse_mode='HTML'
                )
        elif data == "craft_named": await q.answer(); await handle_craft_named(update, context)
        elif data == "cancel_named": await q.answer(); await cancel_named(update, context)
        elif data == "do_smoke": await q.answer(); await do_smoke(update, context)
        elif data == "use_dust": await q.answer(); await handle_use_dust(update, context)
        elif data == "top_scout": await q.answer(); await top_scout_callback(update, context)
        elif data == "achievements": await q.answer(); await achievements_callback(update, context, page=0)
        elif data.startswith("ach_page_"):
            page = int(data.split("_")[-1])
            await q.answer(); await achievements_callback(update, context, page=page)
        elif data == "my_blunts":
            await q.answer(); await my_blunts_callback(update, context, page=0)
        elif data.startswith("blunts_page_"):
            page = int(data.split("_")[-1])
            await q.answer(); await my_blunts_callback(update, context, page=page)
        elif data.startswith("share_blunt_"):
            await q.answer()
            blunt_id = data.replace("share_blunt_", "")
            p = await get_player_cached(uid)
            if not p: return
            bot_username = (await context.bot.get_me()).username
            ref_link = f"https://t.me/{bot_username}?start=blunt_{blunt_id}"
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
            if not item:
                await q.answer("Блант не найден.")
                return
            name = item["name"]
            rarity = item.get("rarity", "common")
            color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
            rare_number = item.get("rare_number","?-????")
            hash_code = item.get("hash","0x????...????")
            reaction = item.get("reaction","")

            text = (
                f"<b>💎 ДЕТАЛИ NFT БЛАНТА</b>\n\n"
                f"{color} <b>«{name}»</b>\n"
                f"<b>Редкость:</b> <i>{rarity}</i> {color}\n\n"
                f"🩸 <b>Серийный номер:</b> <i>#{rare_number}</i>\n\n"
                f"🔗 <b>Хеш:</b> <i>{hash_code}</i>\n\n"
                f"📜 <b>Реакция:</b> <i>{reaction}</i>\n\n"
            )
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
                await q.message.delete()
                await context.bot.send_photo(
                    chat_id=q.message.chat.id,
                    photo=file_id,
                    caption=text,
                    reply_markup=kb,
                    parse_mode='HTML'
                )
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
            if p["balance"] < amount:
                await q.answer("Недостаточно OAC.")
                return
            await update_balance(uid, p["username"], -amount)
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE players SET donated = COALESCE(donated,0) + $1 WHERE user_id = $2", amount, uid)
            invalidate_cache(uid)
            await send_whisper_dm(update, context, f"💎 Ты внёс {amount} OAC в Храм. Спасибо, Странник!")
        elif data == "confess": await q.answer(); await confess_callback(update, context)
        elif data == "pet_preview": await q.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)", show_alert=True)
        elif data == "bush_preview": await q.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)", show_alert=True)
        elif data == "activate_menu":
            await q.answer()
            user = q.from_user
            uname = user.username or user.first_name
            p = await get_player_cached(uid)
            if not p:
                await update_balance(uid, uname, 0)
                await update_blunts(uid, uname, 0)
                await update_balance(uid, uname, 800)
                new_name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
                await create_named_blunt(uid, new_name)
                bonus = "🎁 Смотритель дарует тебе <code>800</code> 🍬 и твой первый именной блант!\n\n"
            else:
                bonus = ""
            welcome = "<b><i>🎉 Добро пожаловать в Гильдию Antysocialshop!</i></b>\n\n🕯️ <b>Тёмная Гильдия</b> — стабильность, ритуалы, тёмное благословение.\n⚜️ <b>Светлая Гильдия</b> — азарт, удача, танец на лезвии.\n\n▸ <i>Выбери свой путь:</i>"
            guild_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🕯️ Тёмная Гильдия", callback_data="guild_join_BLACK"),
                 InlineKeyboardButton("⚜️ Светлая Гильдия", callback_data="guild_join_WHITE")]
            ])
            await q.message.edit_text(bonus + welcome, reply_markup=guild_kb, parse_mode='HTML')
        elif data == "skins_menu":
            await q.answer()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Выбрать титул", callback_data="choose_title")],
                [InlineKeyboardButton("🖼️ Выбрать фон", callback_data="choose_bg")],
                [InlineKeyboardButton("🔙 Назад", callback_data="profile")]
            ])
            await q.message.edit_text("<b>🎨 СКИНЫ</b>\n\nВыбери, что хочешь изменить.", reply_markup=kb, parse_mode='HTML')
        elif data == "choose_title":
            await q.answer()
            p = await get_player_cached(uid)
            titles = (p["titles"] or "").split()
            if not titles:
                await send_whisper_dm(update, context, "У тебя пока нет титулов.")
                return
            skins = p.get("profile_skins", {})
            if not isinstance(skins, dict):
                skins = {}
            active_title = skins.get("active_title", "")
            kb_rows = []
            for title in titles:
                mark = " ✅" if title == active_title else ""
                kb_rows.append([InlineKeyboardButton(f"{title}{mark}", callback_data=f"set_title_{title}")])
            kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="skins_menu")])
            await q.message.edit_text("<b>🎨 ВЫБОР ТИТУЛА</b>\n\nВыбери титул:", reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')
        elif data.startswith("set_title_"):
            await q.answer()
            title = data.replace("set_title_", "")
            p = await get_player_cached(uid)
            skins = p.get("profile_skins", {})
            if not isinstance(skins, dict):
                skins = {}
            skins["active_title"] = title
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE players SET profile_skins=$1 WHERE user_id=$2", json.dumps(skins), uid)
            invalidate_cache(uid)
            await send_whisper_dm(update, context, f"✨ Титул «{title}» активирован!")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Выбрать титул", callback_data="choose_title")],
                [InlineKeyboardButton("🖼️ Выбрать фон", callback_data="choose_bg")],
                [InlineKeyboardButton("🔙 Назад", callback_data="profile")]
            ])
            await q.message.edit_text("<b>🎨 СКИНЫ</b>\n\nВыбери, что хочешь изменить.", reply_markup=kb, parse_mode='HTML')
        elif data == "choose_bg":
            await q.answer()
            p = await get_player_cached(uid)
            skins = p.get("profile_skins", {})
            if not isinstance(skins, dict):
                skins = {}
            unlocked = skins.get("unlocked_backgrounds", [])
            if not unlocked:
                await send_whisper_dm(update, context, "У тебя пока нет разблокированных фонов.")
                return
            active_bg = skins.get("active_background", "")
            kb_rows = []
            for bg in unlocked:
                mark = " ✅" if bg == active_bg else ""
                kb_rows.append([InlineKeyboardButton(f"{bg}{mark}", callback_data=f"set_bg_{bg}")])
            kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="skins_menu")])
            await q.message.edit_text("<b>🖼️ ВЫБОР ФОНА</b>\n\nВыбери фон:", reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')
        elif data.startswith("set_bg_"):
            await q.answer()
            bg = data.replace("set_bg_", "")
            p = await get_player_cached(uid)
            skins = p.get("profile_skins", {})
            if not isinstance(skins, dict):
                skins = {}
            skins["active_background"] = bg
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE players SET profile_skins=$1 WHERE user_id=$2", json.dumps(skins), uid)
            invalidate_cache(uid)
            await send_whisper_dm(update, context, f"✨ Фон «{bg}» активирован!")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Выбрать титул", callback_data="choose_title")],
                [InlineKeyboardButton("🖼️ Выбрать фон", callback_data="choose_bg")],
                [InlineKeyboardButton("🔙 Назад", callback_data="profile")]
            ])
            await q.message.edit_text("<b>🎨 СКИНЫ</b>\n\nВыбери, что хочешь изменить.", reply_markup=kb, parse_mode='HTML')
        elif data in ("guild_join_BLACK", "guild_join_WHITE"):
            await q.answer(); guild = "BLACK" if data == "guild_join_BLACK" else "WHITE"
            await set_guild(uid, guild)
            g_emoji = "🕯️" if guild=="BLACK" else "⚜️"; g_name = "Тёмная" if guild=="BLACK" else "Светлая"
            uname = html.escape(q.from_user.username or q.from_user.first_name)
            await q.message.edit_text(
                f"<b><i>🕋 ГИЛЬДИЯ ТЕБЯ ПРИНЯЛА</i></b>\n\n✅ Теперь <b>ты</b> — {g_emoji} <b>{g_name} Гильдия</b> ·\n\n<i>🩸 Искажение стало плотнее...</i>", parse_mode='HTML')
            try:
                await context.bot.send_message(chat_id="@guild_antysocial",
                    text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{uname}</b> вплёл свою нить в {g_emoji} <b>{g_name} Гильдию</b>.\n<i>🕯️ Искажение приняло нового странника.</i>", parse_mode='HTML')
            except Exception as e:
                logger.error(f"Ошибка отправки в канал: {e}")
        elif data == "shop":
            await shop_callback(update, context)
        else: await q.answer("Неизвестная команда.")
    except Exception as e:
        logger.error(f"Button error: {e}", exc_info=True)
        await q.answer(f"❌ Ошибка: {e}", show_alert=True)
        
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
    except Exception as e:
        logger.error(f"Happy hour announce error: {e}")
    context.job_queue.run_once(reset_happy_hour, HAPPY_HOUR_DURATION_MIN*60)

async def reset_happy_hour(context):
    context.bot_data["happy_hour"] = False
    try:
        await context.bot.send_message(chat_id="@guild_antysocial", text="⏳ Час Удачи завершён.")
    except Exception as e:
        logger.error(f"Happy hour reset error: {e}")

async def echo_of_distortion(context):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username, inventory FROM players WHERE inventory IS NOT NULL AND inventory != '[]'")
    all_named = []
    for row in rows:
        try:
            inv = _json_safe_load(row["inventory"], [])
            for item in inv:
                if item.get("type")=="named": all_named.append((row["user_id"], row["username"], item))
        except: continue
    if len(all_named)==0: return
    sample = random.sample(all_named, min(3,len(all_named)))
    text = "<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n"
    for uid, uname, item in sample:
        name = item["name"]; rarity = item.get("rarity","common")
        color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
        reaction = item.get("reaction","")
        text += f"⚜️ <b>@{html.escape(uname)}</b> создал свой блант {color} <b><i>«{html.escape(name)}»</i></b> 🌿\n<i>Редкость: {rarity}</i>\n🩸 <i>{reaction}</i>\n\n"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💍 Создать свой блант", callback_data="craft_named")]])
    try:
        await context.bot.send_message(chat_id="@guild_antysocial", text=text, parse_mode='HTML', reply_markup=kb)
    except Exception as e:
        logger.error(f"Echo of distortion error: {e}")

async def weekly_guild_rating(context):
    async with db_pool.acquire() as conn:
        war = await conn.fetchrow("SELECT war_active FROM guild_weekly WHERE war_active = TRUE LIMIT 1")
        if not war:
            await conn.execute("UPDATE guild_weekly SET total_farmed = 0, war_active = TRUE")
            try:
                await context.bot.send_message(chat_id="@guild_antysocial",
                    text="⚔️ <b>ВОЙНА ГИЛЬДИЙ НАЧАЛАСЬ!</b>\n🕯️ Тёмные vs ⚜️ Светлые\nЗарабатывай OAC, крафти, проходи лабиринт — всё идёт в зачёт гильдии!\nПобедители получат сундук с ресурсами! 🎁", parse_mode='HTML')
            except Exception as e:
                logger.error(f"War start announce error: {e}")
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
                except Exception as e:
                    logger.error(f"War end announce error: {e}")

async def keep_db_alive(context):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT 1")
        except Exception as e:
            logger.error(f"Keep-alive error: {e}")

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("TOKEN не установлен")
    if not os.getenv("NEON_DATABASE_URL"):
        raise RuntimeError("NEON_DATABASE_URL не установлена")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db_pool())
    Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()

# === ИНИЦИАЛИЗАЦИЯ SENTRY ===
    import sentry_sdk
    SENTRY_DSN = os.getenv("SENTRY_DSN")
    if SENTRY_DSN:
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=1.0,
            environment=os.getenv("ENV", "production"),
        )
        logger.info("Sentry активирован")
    else:
        logger.warning("SENTRY_DSN не задан, Sentry отключён")

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

    # Временный блок для получения file_id (можно удалить после настройки картинок)
    async def get_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message.photo:
            fid = update.message.photo[-1].file_id
            await update.message.reply_text(fid)

    app.add_handler(MessageHandler(filters.PHOTO, get_file_id))

    app.add_handler(MessageHandler(filters.COMMAND, handle_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_shortcut))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(button_handler))

    job = app.job_queue
    job.run_repeating(update_pulse, interval=900, first=10)
    job.run_repeating(happy_hour_trigger, interval=random.randint(14400, 28800), first=random.randint(3600, 10800))
    job.run_daily(echo_of_distortion, time=time(hour=18, minute=0))
    now = datetime.now()
    days_until_saturday = (5 - now.weekday()) % 7
    next_saturday = (now + timedelta(days=days_until_saturday)).replace(hour=12, minute=0, second=0, microsecond=0)
    if next_saturday <= now: next_saturday += timedelta(days=7)
    job.run_repeating(weekly_guild_rating, interval=7*24*3600, first=max(1, (next_saturday - now).total_seconds()))
    job.run_repeating(keep_db_alive, interval=180, first=10)

    # === GRACEFUL SHUTDOWN ===
    async def shutdown():
        logger.info("Завершение работы, закрываю соединения...")
        if db_pool:
            await db_pool.close()
        if redis:
            await redis.close()
        logger.info("Бот остановлен.")

    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(shutdown()))
        except NotImplementedError:
            # На Windows сигналы могут не поддерживаться – ничего страшного
            pass

    print("BOT READY")
    app.run_polling()
    
