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

async def get_player_cached(user_id, fields=None):
    # Пробуем Redis
    if redis:
        key = f"player:{user_id}"
        data = await redis.get(key)
        if data:
            p = json.loads(data)
            if isinstance(p, dict):
                # Если запрошены конкретные поля – возвращаем только их
                if fields:
                    return {k: p.get(k) for k in fields if k in p}
                return p

    # Fallback – старый кэш в памяти
    if user_id in player_cache:
        p = player_cache[user_id]
        if fields:
            return {k: p.get(k) for k in fields if k in p}
        return p

    # Запрос к БД (только нужные колонки)
    if fields:
        columns = ", ".join(fields)
    else:
        columns = "*"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {columns} FROM players WHERE user_id=$1", user_id)
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
            if p.get(field) is None:
                p[field] = 0
        # Инвентарь и скины загружаем только если нужно
        if fields is None or "inventory" in fields:
            p["inventory"] = _json_safe_load(p.get("inventory"), [])
        else:
            p["inventory"] = []   # пустой список, если не запросили
        if fields is None or "profile_skins" in fields:
            p["profile_skins"] = _json_safe_load(p.get("profile_skins"), {})
        else:
            p["profile_skins"] = {}

        # Сохраняем в Redis (TTL 10 секунд) или в словарь
        if redis:
            await redis.setex(key, 10, json.dumps(p, default=str))
        else:
            player_cache[user_id] = p
        # Возвращаем только запрошенные поля
        if fields:
            return {k: p.get(k) for k in fields if k in p}
        return p
    return None

def invalidate_cache(user_id):
    """Сбрасывает кэш игрока (Redis + in-memory). Безопасен для синхронных вызовов."""
    if redis:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(redis.delete(f"player:{user_id}"))
            else:
                logger.warning("Redis invalidation skipped – no running loop")
        except RuntimeError:
            logger.warning("Redis invalidation skipped – no event loop")
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
        if r < 0.02: rarity = "legendary"    # 2%
        elif r < 0.15: rarity = "epic"       # 13% (0.02–0.15)
        elif r < 0.45: rarity = "rare"       # 30% (0.15–0.45)
        else: rarity = "common"              # 55% (0.45–1.00)

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
        "serial": None,
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

async def _award_achievement_rewards(user_id, player, reward_text, context):
    if not reward_text:
        return
    parts = [p.strip() for p in reward_text.split(",") if p.strip()]
    for part in parts:
        if part.startswith("+") and "OAC" in part:
            clean = part.replace(" ", "")
            m = re.search(r"\+(\d+)", clean)
            if m:
                amount = int(m.group(1))
                await update_balance(user_id, player.get("username"), amount)
                player["balance"] = (player.get("balance", 0) + amount)
        elif part.startswith("Титул "):
            await add_title(user_id, part.replace("Титул ", "").strip())
        elif part.startswith("Фон "):
            bg = part.replace("Фон ", "").strip()
            skins = player.get("profile_skins", {})
            if not isinstance(skins, dict):
                skins = {}
            unlocked = skins.get("unlocked_backgrounds", [])
            if bg and bg not in unlocked:
                unlocked.append(bg)
            skins["unlocked_backgrounds"] = unlocked
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE players SET profile_skins=$1 WHERE user_id=$2", json.dumps(skins), user_id)
            invalidate_cache(user_id)
        elif part.startswith("Рамка "):
            frame = part.replace("Рамка ", "").strip()
            skins = player.get("profile_skins", {})
            if not isinstance(skins, dict):
                skins = {}
            unlocked = skins.get("unlocked_frames", [])
            if frame and frame not in unlocked:
                unlocked.append(frame)
            skins["unlocked_frames"] = unlocked
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE players SET profile_skins=$1 WHERE user_id=$2", json.dumps(skins), user_id)
            invalidate_cache(user_id)
        else:
            logger.warning(f"Неизвестный формат награды: {part} для пользователя {user_id}")

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
            elif ach_id == "smoke_1" and p.get("smoke_count", 0) >= 1:
                condition_met = True
            elif ach_id == "balance_1000" and balance >= 1000:
                condition_met = True
            elif ach_id == "smoke_10" and p.get("smoke_count", 0) >= 10:
                condition_met = True
            elif ach_id == "craft_15" and p.get("craft_count", 0) >= 15:
                condition_met = True
            elif ach_id == "ritual_5" and p.get("ritual_count", 0) >= 5:
                condition_met = True
            elif ach_id == "craft_50" and p.get("craft_count", 0) >= 50:
                condition_met = True
            elif ach_id == "smoke_25" and p.get("smoke_count", 0) >= 25:
                condition_met = True
            elif ach_id == "lab_first" and p.get("lab_chests", 0) >= 1:
                condition_met = True
            elif ach_id == "referral_1" and p.get("referral_count", 0) >= 1:
                condition_met = True
            elif ach_id == "streak_7" and p.get("login_streak", 0) >= 7:
                condition_met = True
            elif ach_id == "balance_20000" and balance >= 20000:
                condition_met = True
            elif ach_id == "lab_chest_3" and p.get("lab_chests", 0) >= 3:
                condition_met = True
            elif ach_id == "rank_phantom" and balance >= 20000:
                condition_met = True
            elif ach_id == "balance_50000" and balance >= 50000:
                condition_met = True
            elif ach_id == "check_10" and p.get("check_count", 0) >= 10:
                condition_met = True
            elif ach_id == "lab_death_5" and p.get("lab_deaths", 0) >= 5:
                condition_met = True
            elif ach_id == "lab_chest_10" and p.get("lab_chests", 0) >= 10:
                condition_met = True
            elif ach_id == "craft_250" and p.get("craft_count", 0) >= 250:
                condition_met = True
            elif ach_id == "alchemy_15" and p.get("alchemy_count", 0) >= 15:
                condition_met = True
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

WHISPERS = [
    "💠 Кристалл твоей судьбы пульсирует",
    "🩸 Искажение шепчет твоё имя",
    "🌙 «Ночь опустилась на Гильдию. Смотритель пробудился.»",
    "🍃 Ветер приносит запах свежего бланта.",
    "💎 ОАС — не просто монеты, это кровь Искажения.",
    "🕯️ Алтари ждут твоих подношений.",
    "🌿 Сегодня отличный день для крафта.",
    "⚔️ Война гильдий уже близко – готовься."
]
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
    
    await conn.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='players' AND column_name='pending_transfer'
        ) THEN
            ALTER TABLE players ADD COLUMN pending_transfer JSONB DEFAULT NULL;
        END IF;
    END $$;
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
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, balance, guild FROM players ORDER BY balance DESC LIMIT $1",
            limit
        )
    return [dict(row) for row in rows]

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

# Финальный безопасный редактор сообщений
# Финальный безопасный редактор сообщений
async def safe_edit(update: Update, context, text: str, reply_markup=None, parse_mode='HTML'):
    """Безопасное редактирование: при невозможности отредактировать – отправляет новое сообщение."""
    try:
        if update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await update.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            return
        if "there is no text in the message to edit" in err_msg:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        logger.warning(f"safe_edit fallback due to: {e}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"safe_edit unexpected: {e}", exc_info=True)
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as send_error:
            logger.error(f"safe_edit even send_message failed: {send_error}")

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
        
import asyncio
from telegram.error import BadRequest

async def animate_progress_bar(update, context, title="", duration=0.6, steps=4):
    """
    Быстрая и надёжная анимация прогресс-бара.
    - duration: общее время анимации в секундах (рекомендуется 0.4–0.8).
    - steps: количество кадров (3–5). Чем меньше шагов, тем меньше запросов.
    Возвращает None, если не удалось отправить даже первое сообщение.
    """
    chat_id = update.effective_chat.id
    title_text = f"<b>{title}</b>" if title else ""

    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{title_text}\n[░░░░░░░░░░] 0%",
            parse_mode='HTML'
        )
    except Exception:
        return None

    step_delay = duration / steps
    for i in range(1, steps + 1):
        await asyncio.sleep(step_delay)
        filled = "▓" * (i * (10 // steps))   # масштабируем заполнение
        empty = "░" * (10 - len(filled))
        percent = i * (100 // steps)
        try:
            await asyncio.wait_for(
                msg.edit_text(f"{title_text}\n[{filled}{empty}] {percent}%", parse_mode='HTML'),
                timeout=0.4
            )
        except (BadRequest, asyncio.TimeoutError, Exception):
            # При любой ошибке (включая таймаут) прекращаем анимацию
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
    last = p.get("last_login_date")
    streak = p.get("login_streak", 0) or 0

    if isinstance(last, str):
        try:
            last = datetime.strptime(last, "%Y-%m-%d").date()
        except ValueError:
            last = None

    if last != today:
        if last and (today - last).days == 1:
            streak += 1
        else:
            streak = 1

        # Базовые награды
        rewards = {
            1:10, 2:15, 3:20, 4:25, 5:30, 6:35, 7:50,
            8:55, 9:60, 10:65, 11:70, 12:75, 13:80, 14:100
        }
        base_reward = rewards.get(streak, 100)
        bonus = 0
        bonus_msg = ""

        # Особые дни
        if streak == 7:
            await add_title(user_id, "🕊️")
            bonus_msg = (
                "\n<b>🎁 Бонус 7-го дня:</b>"
                "\n🎉 Разблокирован Титул: 🕊️ «Семь Шагов» 💎"
                "\n🌟 Титул добавлен в профиль!"
            )
        elif streak == 14:
            await add_title(user_id, "🔮")
            bonus_msg = (
                "\n<b>🎁 Бонус 14-го дня:</b>"
                "\n🎉 Разблокирован Титул: 🔮 «Хранитель Хрустального Шара» 💎"
                "\n🌟 Титул добавлен в профиль!"
            )

        # Горячая серия (3+ дней) – бонус 10%
        hot_streak = streak >= 3
        if hot_streak:
            base_reward = int(base_reward * 1.1)

        # Случайный бонус (20%)
        random_bonus = ""
        if random.random() < 0.2:
            r = random.random()
            if r < 0.4:
                extra_oac = random.randint(5, 20)
                base_reward += extra_oac
                random_bonus = f"\n<b>🎲 Удача дня:</b> +{extra_oac} OAC!"
            elif r < 0.7:
                random_bonus = "\n<b>🎲 Удача дня:</b> +1 блант!"
            elif r < 0.9:
                random_bonus = "\n<b>🎲 Удача дня:</b> +1 Фокус!"
            else:
                random_bonus = "\n<b>🎲 Удача дня:</b> +1 жизнь!"

        total_reward = base_reward + bonus

        # Обновление базы
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE players SET login_streak=$1, last_login_date=$2 WHERE user_id=$3",
                streak, today, user_id
            )
        invalidate_cache(user_id)
        await update_balance(user_id, p.get("username"), total_reward)

        if streak in (7, 14):
            await check_achievements(user_id, context)

        # Стиль заголовка и прогресс-бара
        if streak >= 8:
            title = "<b>🔮 ХРУСТАЛЬНЫЙ ШАР ВЕРНОСТИ 🔮</b>"
            filled_char = "🔮"
            period_desc = "Твоя преданность вознаграждена…"
        elif streak >= 3:
            title = "<b>🔮 КРИСТАЛЛ СУДЬБЫ 🔮</b>"
            filled_char = "🟪"
            period_desc = "Твоя верность начинает сиять…"
        else:
            title = "<b>💠 ЕЖЕДНЕВНЫЙ ВХОД 💠</b>"
            filled_char = None
            period_desc = "Багрянец отмечает твой путь"

        # Прогресс-бар
        if filled_char:
            empty_char = "⬛️"
            filled = filled_char * min(streak, 14)
            empty = empty_char * max(0, 14 - min(streak, 14))
            bar = f"{filled}{empty}  ({min(streak,14)}/14)"
        else:
            percent = int(streak / 14 * 100)
            filled_len = int(streak / 14 * 10)
            bar = f"{'▓' * filled_len}{'░' * (10 - filled_len)} {percent}%"

        text = (
            f"{title}\n\n"
            f"<b>День {streak}.</b> {period_desc}\n\n"
            f"{bar}\n\n"
            f"<b>+{total_reward} OAC</b>{bonus_msg}{random_bonus}"
        )
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
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
    p = await get_player_cached(uid, fields=["balance", "last_farm", "farm_count", "smoke_count"])

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
    # Анимация, затем редактируем её сообщение в результат (без кнопок)
    anim_msg = await animate_progress_bar(update, context, title="🍬 Фармим...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, parse_mode='HTML')
    else:
        # fallback – новое сообщение, не трогаем меню
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML')

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
    blunts = p.get("blunts", 0) or 0
    craft_count = p.get("craft_count", 0) or 0

    # Определяем текущую медаль крафта и следующий порог
    medal_name = CRAFT_MEDALS[0][1]
    target = CRAFT_MEDALS[0][0]
    for threshold, name, _ in CRAFT_MEDALS:
        if craft_count >= threshold:
            medal_name = name
        else:
            target = threshold
            break
    else:
        target = craft_count  # максимум

    text = (
        f"<b>🌿 КРАФТ БЛАНТА</b>\n\n"
        f"<b>💎 у тебя: {bal} оас 🍬</b>\n\n"
        f"<b>🌿 Блантов в свёртке: {blunts}</b>\n"
        f"<b>🎯 Крафтинг: {craft_count}/{target} | {medal_name}</b>\n\n"
        f"<b>🕯️ Обычный блант — 15 оас</b>\n"
        f"<b>💍 Именной блант — 50 оас</b>\n"
        f"   <i>🟢 55% | 🔵 30% | 🟣 13% | 🟡 2%</i>"
    )
    if p and p.get("m_essence", 0) > 0:
        text += f"\n\n<b>💠 у тебя есть Кристальная Пыль</b> (<i>{p['m_essence']} доза</i>)"

    kb_rows = [
        [InlineKeyboardButton("🌿 Обычный блант (15 🍬)", callback_data="craft_normal")],
        [InlineKeyboardButton("💍 Именной блант (50 🍬)", callback_data="craft_named")],
    ]
    if p and p.get("m_essence", 0) > 0:
        kb_rows.append([InlineKeyboardButton(f"💠 Использовать Пыль (1 доза)", callback_data="use_dust")])
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
    await send_reply(update, context, text, InlineKeyboardMarkup(kb_rows))

@error_handler
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
    anim_msg = await animate_progress_bar(update, context, title="🌿 Скручиваем Блант...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML')
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
    
#===== ФУНКЦИЯ ПЕРЕДАЧИ БЛАНТА =====
async def transfer_blunt(sender_id: int, receiver_id: int, blunt_id: str):
    """Передаёт именной блант от отправителя получателю."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Захватываем инвентарь отправителя
            row = await conn.fetchrow(
                "SELECT inventory FROM players WHERE user_id = $1 FOR UPDATE", sender_id
            )
            if not row:
                raise ValueError("Отправитель не найден")
            inv = _json_safe_load(row["inventory"], [])
            
            item = None
            for it in inv:
                if it.get("id") == blunt_id and it.get("type") == "named":
                    item = it
                    break
            if not item:
                raise ValueError("Блант не найден или не является именным")
            
            # Удаляем у отправителя
            inv.remove(item)
            await conn.execute(
                "UPDATE players SET inventory = $1 WHERE user_id = $2",
                json.dumps(inv, default=str), sender_id
            )
            
            # Обновляем историю владения
            if "owner_history" not in item:
                item["owner_history"] = []
            item["owner_history"].append({
                "user_id": str(receiver_id),
                "since": datetime.utcnow().isoformat()
            })
            
            # Добавляем получателю
            row_rec = await conn.fetchrow(
                "SELECT inventory FROM players WHERE user_id = $1 FOR UPDATE", receiver_id
            )
            rec_inv = _json_safe_load(row_rec["inventory"], []) if row_rec else []
            rec_inv.append(item)
            await conn.execute(
                "UPDATE players SET inventory = $1 WHERE user_id = $2",
                json.dumps(rec_inv, default=str), receiver_id
            )
    
    invalidate_cache(sender_id)
    invalidate_cache(receiver_id)

# ===== НОВЫЕ ФУНКЦИИ ДЛЯ ОБМЕНА БЛАНТАМИ =====
async def gift_blunt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало процесса дарения – запрос получателя."""
    query = update.callback_query
    await query.answer()
    blunt_id = query.data.replace("gift_blunt_", "")
    context.user_data["gifting_blunt_id"] = blunt_id
    await query.message.edit_text(
        "🎁 <b>ПОДАРИТЬ БЛАНТ</b>\n\n"
        "Введи <b>@username</b> или <b>числовой ID</b> игрока, которому хочешь передать блант.\n"
        "Для отмены нажми кнопку ниже.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_gift")
        ]])
    )

async def cancel_gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена дарения."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("gifting_blunt_id", None)
    # Вернёмся в меню или в список блантов – здесь просто в профиль
    await profile_callback(update, context)

async def handle_gift_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текст с именем получателя, вызывает transfer_blunt."""
    if "gifting_blunt_id" not in context.user_data:
        return  # не в процессе дарения
    
    text = update.message.text.strip()
    receiver_id = None
    
    # Пытаемся распарсить как числовой ID
    if text.isdigit():
        receiver_id = int(text)
    # Или как @username
    elif text.startswith("@"):
        # Нужно найти user_id по username. В текущей БД username хранится в players.
        # Простой способ – поискать в БД
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id FROM players WHERE LOWER(username) = LOWER($1)", text.lstrip("@")
            )
            if row:
                receiver_id = row["user_id"]
    
    if not receiver_id:
        await update.message.reply_text("❌ Игрок не найден. Попробуй ещё раз или отмени.")
        return
    
    if receiver_id == update.effective_user.id:
        await update.message.reply_text("❌ Нельзя подарить блант самому себе.")
        return
    
    blunt_id = context.user_data.pop("gifting_blunt_id")
    try:
        await transfer_blunt(update.effective_user.id, receiver_id, blunt_id)
        await update.message.reply_text("✅ Блант успешно подарен! 🎁")
        # Уведомим получателя
        try:
            await context.bot.send_message(
                chat_id=receiver_id,
                text="🎁 Вам подарили именной блант! Проверьте инвентарь."
            )
        except Exception:
            pass
    except ValueError as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    except Exception as e:
        logger.error(f"Gift error: {e}")
        await update.message.reply_text("⚠️ Внутренняя ошибка. Попробуй позже.")

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
        f"<b>🕯️ РИТУАЛ ЗАВЕРШЁН 🎉</b>\n\n"
        f"Ритуал принёс тебе <b>{reward} OAC</b> 🍬\n"
        f"<b>⚜️ У тебя:</b> <b>{new_balance} OAC</b>\n\n"
        f"{medal_text}"
        f"<b>🕯️ Ритуалы:</b> {new_count}/{target}\n"
        f"<b>{progress_bar_str}</b>"
    )

    # Анимация с проверкой переменной
    anim_msg = await animate_progress_bar(update, context, title="🕯️ Ритуал проводится...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML')

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

# Профиль – премиум-карточка, сеньорская версия (аватарка + текст + кнопки)
@error_handler
@rate_limit(2)
async def profile_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p:
        await msg.reply_text("Сначала активируйся: /start")
        return

    # Изоляция данных (защита от None)
    bal = p.get("balance", 0) or 0
    bl = p.get("blunts", 0) or 0
    guild = p.get("guild") or ""

    # Ранг
    rank_emoji, rank_name = "🪓", "Рекрут"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold:
            rank_emoji = emoji
            rank_name = emoji_to_name(emoji)

    # Жирное название ранга для отображения
    rank_name_bold = f"<b>{rank_name}</b>"

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
        f"🧠 <b>Нейро-статус:</b> <i>{neuro}</i>\n\n"
        f"🎖️ <b>Заслуги:</b> {badge_str}"
    )

    named = [it for it in inv_data if it.get("type") == "named"]
    rarity_order = {"legendary": 0, "epic": 1, "rare": 2, "common": 3}
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
    kb = InlineKeyboardMarkup(kb_rows)

    # Получаем аватарку
    photo_id = None
    try:
        photos = await context.bot.get_user_profile_photos(uid, limit=1)
        if photos.photos:
            photo_id = photos.photos[0][0].file_id
    except:
        pass

    # Отправка: одно сообщение с аватаркой, текстом и кнопками (или просто текст, если фото нет)
    if photo_id:
        await context.bot.send_photo(
            chat_id=msg.chat.id,
            photo=photo_id,
            caption=text,
            reply_markup=kb,
            parse_mode='HTML'
        )
    else:
        await msg.reply_text(text, reply_markup=kb, parse_mode='HTML')

# Все бланты
@error_handler
@rate_limit(1)
async def my_blunts_callback(update, context, page=0):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p:
        return

    # Безопасно получаем инвентарь (уже список, как в profile_callback)
    inv_data = p.get("inventory", [])
    named = [it for it in inv_data if it.get("type") == "named"]

    # Сортировка: сначала по редкости, потом по серийному номеру
    rarity_order = {"legendary": 0, "epic": 1, "rare": 2, "common": 3}
    named.sort(key=lambda x: (rarity_order.get(x.get("rarity") or "common", 3),
                               x.get("serial") or 999999))

    if not named:
        await query.message.edit_text(
            "💎 У тебя пока нет именных блантов.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 В профиль", callback_data="profile")]
            ])
        )
        return

    total_pages = (len(named) + BLUNTS_PER_PAGE - 1) // BLUNTS_PER_PAGE
    start = page * BLUNTS_PER_PAGE
    end = start + BLUNTS_PER_PAGE
    page_blunts = named[start:end]

    # Заголовок
    text = f"<b>💎 ТВОИ ИМЕННЫЕ БЛАНТЫ ({page+1}/{total_pages})</b>\n\n"

    # Строки с блантами
    for i, item in enumerate(page_blunts, 1):
        name = item["name"]
        rarity = item.get("rarity", "common")
        color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
        rare_number = item.get("rare_number", "?-????")
        hash_code = item.get("hash", "0x????...????")
        text += f"<b>{i}) «{html.escape(name)}»</b> {color} · #{rare_number} · {hash_code}\n"

    # Кнопки действий с каждым блантом
    kb_rows = []
    for i, item in enumerate(page_blunts, 1):
        row = [
            InlineKeyboardButton(f"💍 Детали ({i})", callback_data=f"blunt_details_{item['id']}"),
            InlineKeyboardButton("🔗", callback_data=f"share_blunt_{item['id']}")
        ]
        kb_rows.append(row)

    # Навигационные кнопки
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"blunts_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("▶️ Далее", callback_data=f"blunts_page_{page+1}"))
    if nav_buttons:
        kb_rows.append(nav_buttons)

    kb_rows.append([InlineKeyboardButton("🔙 В профиль", callback_data="profile")])

    await safe_edit(update, context, text, reply_markup=InlineKeyboardMarkup(kb_rows))

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
from datetime import datetime, timedelta
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
import html

# --- Вспомогательная функция: ближайшее воскресенье 00:00 ---
def next_sunday_str() -> str:
    """Возвращает дату ближайшего воскресенья в формате ДД.ММ.
    Если сегодня воскресенье, берём следующее."""
    now = datetime.now()                    # при необходимости добавьте часовой пояс
    days_until_sunday = (6 - now.weekday()) % 7
    if days_until_sunday == 0:
        days_until_sunday = 7               # следующее воскресенье, а не сегодня
    next_sunday = now + timedelta(days=days_until_sunday)
    return next_sunday.strftime("%d.%m")


@error_handler
async def top_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    top = await get_top(10)                 # должен возвращать поле user_id
    if not top:
        await safe_edit(update, context, "🏆 Топ-10 пока пуст.")
        return

    first_balance = top[0]["balance"]
    p = await get_player_cached(uid, fields=["balance", "guild"])
    my_balance = p["balance"] if p else 0

    # Шапка
    text = "<b>💎 ТОП-10 ИГРОКОВ 🏆</b>\n\n"

    my_position = None
    for i, row in enumerate(top, 1):
        bal = row["balance"]
        percent = int(bal / first_balance * 100) if first_balance else 100
        filled = percent // 10
        bar = "▓" * filled + "░" * (10 - filled)

        # Префикс с эмодзи и номером
        if i == 1:
            prefix = "🥇 1. "
        elif i == 2:
            prefix = "🥈 2. "
        elif i == 3:
            prefix = "🥉 3. "
        elif i == 4:
            prefix = "⚜️ 4. "
        elif i == 5:
            prefix = "🌿 5. "
        elif i == 6:
            prefix = "🫧 6. "
        elif i == 7:
            prefix = "🪄 7. "
        elif i == 8:
            prefix = "🎈 8. "
        elif i == 9:
            prefix = "🍀 9. "
        else:  # i == 10
            prefix = "🌱 10. "

        # Гильдия
        guild = row.get("guild", "")
        if guild == "BLACK":
            g_emoji, g_name = "🕯️", "<b>Тёмная Гильдия</b>"
        elif guild == "WHITE":
            g_emoji, g_name = "⚜️", "<b>Светлая Гильдия</b>"
        else:
            g_emoji, g_name = "🩸", "<b>Без гильдии</b>"

        # Ранг
        rank_emoji, rank_name = get_rank_info(bal)

        # Никнейм (экранирование HTML)
        username = html.escape(row["username"])

        text += (
            f"{prefix}<b>{username}</b> {g_emoji} — {bal} оас 🍬\n"
            f"   <i>{bar} {percent}%</i>\n"
            f"   {g_emoji} {g_name} | {rank_emoji} <b>{rank_name}</b>\n\n"
        )

        # Определяем позицию текущего игрока
        if row.get("user_id") == uid:
            my_position = i

    # --- Блок позиции игрока (динамическая дата) ---
    deadline = next_sunday_str()

    if my_position == 1:
        text += (
            f"<b>✦ 📊 Твоя позиция: 1 — ТЫ ДЕРЖИШЬ ТРОН 💎🫧 ✦</b>\n\n"
            f"<b>🏆 УДЕРЖИ трон до {deadline} — получишь:</b>\n\n"
            "<b>   🎁 Скин: «Корона Бездны» — уникальная рамка профиля</b>\n"
            "<b>   ⚜️ Титул: «Властелин Рейтинга»</b>\n"
        )
    elif my_position == 2:
        text += (
            f"<b>✦ 📊 Твоя позиция: 2 — ТЫ В ШАГЕ ОТ ТРОНА 💎 ✨ ✦</b>\n\n"
            f"<b>🏆 УДЕРЖИСЬ в топ-3 до {deadline} и получишь:</b>\n\n"
            "<b>   🎁 Скин: «Золотой Венец» — фон профиля</b>\n"
            "<b>   ⚜️ Титул: «Хранитель Топа»</b>\n"
        )
    elif my_position == 3:
        text += (
            f"<b>✦ 📊 Твоя позиция: 3 — ТЫ В ТРОЙКЕ ЛИДЕРОВ 💎 ✨ ✦</b>\n\n"
            f"<b>🏆 УДЕРЖИСЬ в топ-3 до {deadline} — получишь:</b>\n\n"
            "<b>   🎁 Скин: «Золотой Венец» — фон профиля</b>\n"
            "<b>   ⚜️ Титул: «Хранитель Топа»</b>\n"
        )
    # ... остальные elif / else
    elif my_position is not None:            # 4-10 места
        third_balance = top[2]["balance"] if len(top) >= 3 else 0
        gap = third_balance - my_balance
        if gap > 0:
            text += (
                f"✦ 📊 Твоя позиция: {my_position} — "
                f"осталось 🎯 {gap} оас 🍬 до ТРОЙКИ ЛИДЕРОВ 💎🏆 ✦\n"
            )
        else:
            text += f"✦ 📊 Твоя позиция: {my_position} ✦\n"
    else:                                    # вне топа
        async with db_pool.acquire() as conn:
            cnt_row = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM players WHERE balance > $1",
                my_balance
            )
        pos = cnt_row["cnt"] + 1 if cnt_row else 1
        async with db_pool.acquire() as conn:
            tenth_row = await conn.fetchrow(
                "SELECT balance FROM players ORDER BY balance DESC LIMIT 1 OFFSET 9"
            )
        tenth_balance = tenth_row["balance"] if tenth_row else 0
        gap_to_top10 = tenth_balance - my_balance
        if gap_to_top10 > 0:
            text += (
                f"✦ 📊 Твоя позиция: {pos} — "
                f"осталось 🎯 {gap_to_top10} оас 🍬 до ТОП-10 💎🏆 ✦\n"
            )
        else:
            text += f"✦ 📊 Твоя позиция: {pos} — ты уже в топе! 💎 ✦\n"

    # --- Кнопки ---
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Разведка", callback_data="top_scout")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])

    if update.callback_query:
        await safe_edit(update, context, text, reply_markup=kb, parse_mode="HTML")
    else:
        await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")


def get_rank_info(balance):
    """Возвращает эмодзи и название ранга."""
    if balance >= 50000: return "🪬", "Некромант"
    elif balance >= 20000: return "🪦", "Призрак"
    elif balance >= 5000: return "⚔️", "Ветеран"
    return "🪓", "Рекрут"

def get_rank_info(balance):
    """Возвращает эмодзи и название ранга."""
    if balance >= 50000: return "🪬", "Некромант"
    elif balance >= 20000: return "🪦", "Призрак"
    elif balance >= 5000: return "⚔️", "Ветеран"
    return "🪓", "Рекрут"


def get_rank_info(balance):
    """Возвращает эмодзи и название ранга по балансу."""
    if balance >= 50000:
        return "🪬", "Некромант"
    elif balance >= 20000:
        return "🪦", "Призрак"
    elif balance >= 5000:
        return "⚔️", "Ветеран"
    else:
        return "🪓", "Рекрут"

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

    def safe_progress_bar(perc):
        perc = max(0, min(100, perc))
        filled = perc // 10
        return "▓" * filled + "░" * (10 - filled)

    # Прогресс-бары под названиями гильдий
    text = (
        f"<b>🕋 ГИЛЬДИИ</b>\n\n"
        f"🕯️ <b>Тёмная Гильдия: {black_cnt}</b> странников\n"
        f"<b>{safe_progress_bar(black_perc)} {black_perc}%</b>\n\n"
        f"⚜️ <b>Светлая Гильдия: {white_cnt}</b> странников\n"
        f"<b>{safe_progress_bar(white_perc)} {white_perc}%</b>\n\n"
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
        kb_rows.append([
            InlineKeyboardButton("🏛️ Храм", callback_data="guild_shrine"),
            InlineKeyboardButton("⚔️ Война", callback_data="guild_war")
        ])
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

    guild = p["guild"]
    async with db_pool.acquire() as conn:
        total_donated = await conn.fetchval(
            "SELECT COALESCE(SUM(donated),0) FROM players WHERE guild=$1", guild
        ) or 0

    levels = [
        {"level": 1, "cost": 0,      "bonus": 0},
        {"level": 2, "cost": 15000,  "bonus": 5},
        {"level": 3, "cost": 45000,  "bonus": 10},
        {"level": 4, "cost": 100000, "bonus": 15},
        {"level": 5, "cost": 250000, "bonus": 25},
    ]

    current_level = 1
    for lvl in levels:
        if total_donated >= lvl["cost"]:
            current_level = lvl["level"]

    if current_level < 5:
        next_level = levels[current_level]
        needed = next_level["cost"] - total_donated
        progress = int(total_donated / next_level["cost"] * 100) if next_level["cost"] > 0 else 100
    else:
        next_level = None
        needed = 0
        progress = 100

    bonus = levels[current_level-1]["bonus"]
    bar = progress_bar(progress)

    text = (
        f"<b>🏛️ ХРАМ ГИЛЬДИИ</b>\n\n"
        f"🫧 <b>{guild}</b> Гильдия\n"
        f"🌱 Уровень: <b>{current_level}</b>/5\n"
        f"🎉 Бонус фарма: <b>+{bonus}%</b>\n\n"
    )
    if current_level < 5:
        text += (
            f"<i>До уровня {current_level+1}:</i>\n"
            f"<b>{bar} {progress}%</b>\n"
            f"🍃 {total_donated} / {next_level['cost']} OAC\n\n"
        )
    else:
        text += "<b>✨ Храм полностью возвышен! ✨</b>\n\n"

    text += "<i>Каждое пожертвование усиливает всех членов гильдии.</i>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Внести 100 OAC", callback_data="shrine_donate_100"),
         InlineKeyboardButton("💎 Внести 500 OAC", callback_data="shrine_donate_500")],
        [InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    
@error_handler
async def guild_war_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p:
        await safe_edit(update, context, "Профиль не найден.")
        return

    async with db_pool.acquire() as conn:
        war = await conn.fetchrow("SELECT war_active FROM guild_weekly WHERE war_active = TRUE LIMIT 1")
        if not war:
            await safe_edit(update, context, "🕊️ Сейчас мирное время.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]]))
            return

        scores = await conn.fetch("SELECT guild, total_farmed FROM guild_weekly")
        black_score = next((r["total_farmed"] for r in scores if r["guild"] == "BLACK"), 0)
        white_score = next((r["total_farmed"] for r in scores if r["guild"] == "WHITE"), 0)

        top_black = await conn.fetch(
            "SELECT username, donated FROM players WHERE guild='BLACK' ORDER BY donated DESC LIMIT 3"
        )
        top_white = await conn.fetch(
            "SELECT username, donated FROM players WHERE guild='WHITE' ORDER BY donated DESC LIMIT 3"
        )

    total = max(black_score + white_score, 1)
    bp = int(black_score / total * 100)
    wp = int(white_score / total * 100)

    def safe_bar(perc):
        perc = max(0, min(100, perc))
        filled = perc // 10
        return "▓" * filled + "░" * (10 - filled)

    text = (
        f"<b>⚔️ БИТВА ГИЛЬДИЙ</b>\n\n"
        f"🕯️ <b>Тёмная Гильдия:</b> {black_score} очков\n"
        f"<b>{safe_bar(bp)} {bp}%</b>\n\n"
        f"⚜️ <b>Светлая Гильдия:</b> {white_score} очков\n"
        f"<b>{safe_bar(wp)} {wp}%</b>\n\n"
    )

    if top_black:
        text += "🕯️ <b>Герои Тьмы:</b>\n"
        for i, row in enumerate(top_black, 1):
            text += f"  {i}. {html.escape(row['username'])} — {row['donated']} очков\n"
    if top_white:
        text += "⚜️ <b>Герои Света:</b>\n"
        for i, row in enumerate(top_white, 1):
            text += f"  {i}. {html.escape(row['username'])} — {row['donated']} очков\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]
    ])
    await safe_edit(update, context, text, reply_markup=kb)

@error_handler
async def confess_callback(update, context):
    """Исповедь (для Светлой Гильдии) – работает и по кнопке, и по команде /repent."""
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid)

    # Проверки
    if not p:
        await context.bot.send_message(chat_id=uid, text="Сначала активируйся: /start")
        return
    if p.get("guild") != "WHITE":
        if update.callback_query:
            await update.callback_query.answer("Только для Светлой Гильдии.", show_alert=True)
        else:
            await msg.reply_text("❌ Только для Светлой Гильдии.")
        return
    if p.get("blunts", 0) < 1:
        if update.callback_query:
            await update.callback_query.answer("Нужен 1 блант.", show_alert=True)
        else:
            await msg.reply_text("❌ Нужен 1 блант.")
        return

    # Списание бланта
    await update_blunts(uid, p.get("username"), -1)
    p = await get_player_cached(uid)

    # Случайный результат
    r = random.random()
    if r < 0.70:
        reward = random.randint(100, 200)
        await update_balance(uid, p.get("username"), reward)
        text = f"<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\nБлагословение! +{reward} OAC."
    elif r < 0.95:
        await update_essence(uid, 1)
        text = "<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\nТы получил 💠 Кристальную Пыль."
    else:
        # Редкий случай – бесплатный легендарный блант
        name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
        await create_named_blunt(uid, name, rarity="legendary")
        text = f"<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\n🌟 Чудо! Легендарный блант «{name}»!"

    # Отправка результата
    if update.callback_query:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]]),
            parse_mode='HTML'
        )
    else:
        await msg.reply_text(text, parse_mode='HTML')

@error_handler
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

    if update.callback_query:
        await safe_edit(update, context, text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💍 Создать именной блант", callback_data="craft_named")],
                [InlineKeyboardButton("🔙 Назад", callback_data="profile")]
            ]))
    else:
        await msg.reply_text(text, parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
            ]))

@error_handler
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

# Удача – сеньорская версия (ленивая загрузка, атомарность, safe_edit)
@error_handler
@rate_limit(2)
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

    # ── ГЛАВНОЕ МЕНЮ (обновлённый дизайн) ──
    text = (
        "<b>🍀 УДАЧА</b>\n\n"
        "<i>🌀 «Испытай свою удачу и выиграй OAC 🍬 и редкие эксклюзивные вещи!» 🪽</i>\n\n"
        "🎡 <b>Крутить Колесо</b> — ежедневный выигрыш 🎉\n"
        "🍀 <b>Рискнуть</b> — бросить вызов и отдать 300 оас ради джекпота 💫\n"
        "⚗️ <b>Алхимия</b> — древнее искусство, магия для достойных 🔮"
    )

    kb_rows = []

    if wheel_available:
        kb_rows.append([InlineKeyboardButton("🎡 Крутить", callback_data="luck_wheel")])
    else:
        diff = timedelta(hours=24) - (now - last_daily_dt)
        hrs = int(diff.seconds // 3600); mins = int((diff.seconds % 3600) // 60)
        kb_rows.append([InlineKeyboardButton(f"🎡 Колесо набирает силу. Ещё {hrs} ч {mins} мин", callback_data="luck_wheel")])

    if berserk_available:
        kb_rows.append([InlineKeyboardButton("🍀 Рискнуть", callback_data="luck_berserk")])
    else:
        if bal < 300: kb_rows.append([InlineKeyboardButton(f"🍀 нужно ещё {300 - bal} 🍬", callback_data="luck_berserk")])
        else:
            diff = timedelta(hours=24) - (now - last_berserk_dt)
            hrs = int(diff.seconds // 3600); mins = int((diff.seconds % 3600) // 60)
            kb_rows.append([InlineKeyboardButton(f"🍀 Бездна шепчет всё громче. Жди {hrs} ч {mins} мин", callback_data="luck_berserk")])

    if veteran_alchemy:
        kb_rows.append([InlineKeyboardButton("🔮 Алхимия", callback_data="alchemy_start")])
    else:
        kb_rows.append([InlineKeyboardButton("🔮 Алхимия 🔒", callback_data="alchemy_start")])

    kb_rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)

    # 🎡 Колесо
    if action == "luck_wheel":
        if not wheel_available:
            remain = timedelta(hours=24) - (now - last_daily_dt)
            hrs = int(remain.total_seconds()//3600); mins = int((remain.total_seconds()%3600)//60)
            await send_whisper_dm(update, context, f"<b><i>🎡 Колесо не готово</i></b>\n\n💎 Испытаешь через <b>{hrs} ч {mins} мин</b>.")
            return

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

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                if prize_type == "jackpot":
                    await update_balance(uid, uname, final_prize, conn=conn)
                elif prize_type == "oac":
                    await update_balance(uid, uname, final_prize, conn=conn)
                    await add_war_score(uid, final_prize, conn=conn)
                else:
                    await update_blunts(uid, uname, prize, conn=conn)
                await update_last_daily(uid, conn=conn)
            new_p = await get_player_cached(uid, fields=["balance"])
            new_bal = new_p["balance"] if new_p else bal

        if prize_type == "jackpot":
            await grant_title(uid, "🧛🏻‍♀️", "Призрачный Гончий", context)
            try:
                await context.bot.send_message(chat_id="@guild_antysocial",
                    text=f"🌟 @{uname} сорвал Джекпот! +{final_prize} OAC", parse_mode='HTML')
            except Exception as e:
                logger.error(f"Ошибка отправки в канал: {e}")
            text = f"<b>🎰 ДЖЕКПОТ!</b>\n\nТы выиграл <b>{final_prize} OAC</b> 🍬!\n\n<b>⚜️ У тебя:</b> <i>{new_bal} OAC</i>"
        elif prize_type == "oac":
            next_rank_name, next_threshold = "", 0
            for emoji, threshold, _ in RANKS:
                if new_bal < threshold:
                    next_rank_name = emoji; next_threshold = threshold
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
        else:
            text = f"<b><i>🌱 КОЛЕСО СМОТРИТЕЛЯ</i></b>\n\n+{prize} 🌿 Блант → 💰 <b>{new_bal} OAC</b> 🍬"

        await safe_edit(update, context, text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]))
        return

    # 🎲 Берсерк
    if action == "luck_berserk":
        if not berserk_available:
            if bal < 300:
                await send_whisper_dm(update, context, f"<b><i>🎲 Бездна требует жертву</i></b>\n\n⚠️ Недостаточно OAC (нужно ещё <b>{300-bal}</b>).")
            else:
                diff = timedelta(hours=24) - (now - last_berserk_dt)
                hrs = int(diff.total_seconds()//3600); mins = int((diff.total_seconds()%3600)//60)
                await send_whisper_dm(update, context, f"<b><i>🎲 Бездна молчит</i></b>\n\n🕳️ Примет тебя через <b>{hrs} ч {mins} мин</b>.")
            return

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                if random.random() < 0.6:
                    await update_balance(uid, uname, 200, conn=conn)
                    res_text = f"<b><i>🎲 БЕЗДНА ОТВЕТИЛА</i></b>\n\nИскажение благосклонно! +<b>200 OAC</b> 🍬."
                else:
                    await update_balance(uid, uname, -300, conn=conn)
                    res_text = f"<b><i>🕯️ БЕЗДНА МОЛЧИТ</i></b>\n\nИскажение промолчало. –<b>300 OAC</b>."
                await update_last_berserk(uid, conn=conn)
                await add_war_score(uid, 200 if "200" in res_text else -300, conn=conn)

        await safe_edit(update, context, res_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]))
        return

    # 🔮 Алхимия (меню)
    if action == "alchemy_start":
        query = update.callback_query
        if not veteran_alchemy:
            await query.answer(
                "🔮 <b>Магия неподвластна тебе.</b> ⚔️\n\n"
                "Только тот, кто достиг ⚔️ Ветерана (5000 OAC) — обретёт право использовать алхимию 🗝️.",
                show_alert=True
            )
            return

        # Всегда показываем меню алхимии (даже если ресурсов не хватает)
        text = (
            "<b>🔮 АЛХИМИЧЕСКИЙ КОТЁЛ</b>\n\n"
            f"<b>💎 У тебя: {bal} OAC 🍬</b>\n"
            f"<b>🌿 Блантов в свёртке: {p['blunts']}</b>\n\n"
            "<b>⚗️ Стоимость запуска:</b>\n"
            "   🕯️ 10 Блантов\n"
            "   🍬 250 OAC\n\n"
            "<b>🍀 Шансы реакции:</b>\n"
            "   💠 Чистая Пыльца (1 доза) — 40%\n"
            "   🌫️ Грязный Выхлоп (ничего) — 35%\n"
            "   ✨ Мерцающая Пыльца (2 дозы) — 15%\n"
            "   🌟 Философский Камень (легендарный блант) — 10%\n\n"
            "<i>«Только тот, кто достиг <b>ветерана</b> и не боится потерь — "
            "обретёт право 🗝️ использовать магию и истинную силу»</i> 🔮"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧪 Запустить реакцию ⚗️", callback_data="alchemy_confirm")],
            [InlineKeyboardButton("🔙 Назад", callback_data="luck")]
        ])
        await safe_edit(update, context, text, reply_markup=kb)
        return

    # 🔮 Алхимия (запуск реакции)
    if action == "alchemy_confirm":
        if p["blunts"] < 10 or bal < 250:
            await update.callback_query.answer(
                "<b>❌ Недостаточно ресурсов</b>\n\n"
                f"🕯️ Нужно 10 блантов (у вас {p['blunts']})\n"
                f"🍬 Нужно 250 OAC (у вас {bal})",
                show_alert=True
            )
            return

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await update_blunts(uid, uname, -10, conn=conn)
                await update_balance(uid, uname, -250, conn=conn)
                r = random.random()
                if r < 0.40:
                    await update_essence(uid, 1, conn=conn)
                    result_text = "<b>💠 Чистая Пыльца!</b>\n\n+1 Кристальная Пыль"
                elif r < 0.75:
                    result_text = "<b>🌫️ Грязный Выхлоп...</b>\n\nБланты сгорели без следа."
                elif r < 0.90:
                    await update_essence(uid, 2, conn=conn)
                    result_text = "<b>✨ Мерцающая Пыльца!</b>\n\n+2 Кристальной Пыли"
                else:
                    name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа","Коготь Хаоса","Вздох Пожирателя"])
                    item = await create_named_blunt(uid, name, rarity="legendary", conn=conn)
                    result_text = f"<b>🌟 Философский Камень!</b>\n\nЛегендарный блант «{name}»!"
                    try:
                        await context.bot.send_message(chat_id="@guild_antysocial",
                            text=f"🌟 @{uname} провёл Алхимический Ритуал и получил легендарный блант «{name}»!", parse_mode='HTML')
                    except Exception as e:
                        logger.error(f"Ошибка отправки в канал: {e}")
                await add_war_score(uid, 30, conn=conn)

        await safe_edit(update, context, result_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]))
        return

    # Если ни одно действие не указано — показываем главное меню удачи
    await safe_edit(update, context, text, reply_markup=kb)

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

# ============================================================
# ЛАБИРИНТ ИСКАЖЕНИЯ — ИТОГОВАЯ СЕНЬОР-ВЕРСИЯ (ПОЛНАЯ ЗАМЕНА)
# ============================================================

import copy

LABYRINTH_ROOMS = [
    # === БАЗОВЫЕ КОМНАТЫ ===
    {
        "name": "👁️ Зал Наблюдателя",
        "desc": "Тебе кажется, что глаза на потолке — это отражения твоих собственных сомнений. Но они моргают",
        "actions": {
            "attack": {"costs": [10, 25, 50], "risks": [0.20, 0.60, 0.85], "rewards": [(10,30), (40,80), (80,160)]},
            "special": {"name": "🕯️ Зажечь свечу", "cost": 5, "risk": 0.70, "effect": "focus", "value": 1}
        }
    },
    {
        "name": "⚗️ Алтарь Теней",
        "desc": "Густая кровь капает с алтаря. Тени шепчут о силе",
        "actions": {
            "attack": {"costs": [10, 25, 50], "risks": [0.20, 0.60, 0.85], "rewards": [(15,35), (45,85), (85,170)]},
            "special": {"name": "📜 Прочесть руны", "cost": 5, "risk": 0.60, "effect": "amulet"}
        }
    },
    {
        "name": "🌀 Водоворот Хаоса",
        "desc": "Воздух дрожит, затягивая в воронку. Прямо в центре — мерцающий сгусток",
        "actions": {
            "attack": {"costs": [10, 25, 50], "risks": [0.20, 0.60, 0.85], "rewards": [(20,40), (50,90), (90,180)]},
            "special": {"name": "🌀 Схватить сгусток", "cost": 5, "risk": 0.50, "effect": "next_boost", "value": 0.5}
        }
    },
    {
        "name": "☠️ Склеп Короля",
        "desc": "Груды костей, трон из черепов. С них свисают драгоценные камни",
        "actions": {
            "attack": {"costs": [10, 25, 50], "risks": [0.20, 0.60, 0.85], "rewards": [(25,50), (60,120), (120,250)]},
            "special": {"name": "💎 Сорвать камень", "cost": 5, "risk": 0.80, "effect": "oac", "value": (20,50)}
        }
    },
    # === ГЛУБОКИЕ ЭТАЖИ ===
    {
        "name": "🩸 Чертог Крови",
        "desc": "Стены сочатся тёмной кровью. Воздух тяжёлый от древних жертв",
        "actions": {
            "attack": {"costs": [15, 30, 55], "risks": [0.25, 0.65, 0.90], "rewards": [(30,60), (70,130), (130,280)]},
            "special": {"name": "💉 Испить из чаши", "cost": 5, "risk": 0.60, "effect": "heal", "value": 30}
        }
    },
    {
        "name": "🔮 Зал Пророчеств",
        "desc": "Тысячи свечей озаряют карты судьбы. Грядущее можно увидеть, если осмелишься заглянуть",
        "actions": {
            "attack": {"costs": [10, 25, 50], "risks": [0.20, 0.60, 0.85], "rewards": [(20,40), (50,90), (90,200)]},
            "special": {"name": "🔮 Заглянуть в будущее", "cost": 5, "risk": 1.0, "effect": "reveal"}
        }
    },
    {
        "name": "🗝️ Сокровищница Теней",
        "desc": "Призрачные сундуки парят в воздухе. Они манят блеском, но стража не дремлет",
        "actions": {
            "attack": {"costs": [10, 25, 50], "risks": [0.20, 0.60, 0.85], "rewards": [(25,50), (60,120), (120,250)]},
            "special": {"name": "💎 Взять самоцвет", "cost": 5, "risk": 0.80, "effect": "oac", "value": (20,50)}
        }
    },
    {
        "name": "🪞 Галерея Отражений",
        "desc": "В зеркалах движутся не твои копии. Они живут своей жизнью и зовут тебя",
        "actions": {
            "attack": {"costs": [10, 25, 50], "risks": [0.20, 0.60, 0.85], "rewards": [(15,35), (45,85), (85,170)]},
            "special": {"name": "🪞 Коснуться отражения", "cost": 5, "risk": 0.50, "effect": "mirror_hp"}
        }
    },
    # === ДВЕ НОВЫЕ КОМНАТЫ ===
    {
        "name": "🔥 Жертвенный Костер",
        "desc": "Языки пламени пляшут на костях. Брось в огонь часть себя — и получишь силу",
        "actions": {
            "attack": {"costs": [10, 25, 50], "risks": [0.20, 0.60, 0.85], "rewards": [(20,40), (50,90), (90,180)]},
            "special": {"name": "🔥 Бросить в огонь 20 HP", "cost": 20, "risk": 0.90, "effect": "sacrifice_boost", "value": 0.8}
        }
    },
    {
        "name": "👻 Шепчущий Коридор",
        "desc": "Голоса нашёптывают тебе удачу и погибель. Что выберешь?",
        "actions": {
            "attack": {"costs": [10, 25, 50], "risks": [0.20, 0.60, 0.85], "rewards": [(15,35), (45,85), (85,170)]},
            "special": {"name": "👂 Прислушаться", "cost": 5, "risk": 0.50, "effect": "gamble"}
        }
    }
]


# ─── ВХОД В ЛАБИРИНТ ────────────────────────────────────────
async def lab_enter(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid, fields=["last_lab_attempt", "lab_depth"])
    if not p:
        return
    depth = p.get("lab_depth", 1) or 1
    now = datetime.now()
    last = p.get("last_lab_attempt")
    if last:
        last = _to_datetime(last)
        if last and (now - last).total_seconds() < 12 * 3600:
            remain = 12 * 3600 - (now - last).total_seconds()
            hrs = int(remain // 3600)
            mins = int((remain % 3600) // 60)
            text = (
                f"<b>🏛️ ЛАБИРИНТ ИСКАЖЕНИЯ — ЭТАЖ {depth}</b>\n\n"
                f"<i>– Портал откроется через <b>{hrs} ч {mins} мин</b>.</i>"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="menu")]])
            await safe_edit(update, context, text, reply_markup=kb)
            return

    total_rooms = 4 + depth
    text = (
        f"<b>🏛️ ЛАБИРИНТ ИСКАЖЕНИЯ — ЭТАЖ {depth}</b>\n\n"
        f"🔮 <i>\"Ты стоишь у входа. Древние построили его, чтобы испытывать души. "
        f"Лишь достойные достигают сердца лабиринта. "
        f"В конце лабиринта всех ждет сундук с наградой\"</i> 🎁\n\n"
        f"<b>💎 1 попытка</b>\n"
        f"<b>⛓️‍💥 2 жизни</b>\n"
        f"<b>🗝️ Комнат: {total_rooms}</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍃 Войти в лабиринт", callback_data="lab_enter_confirm")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    await safe_edit(update, context, text, reply_markup=kb)


# ─── ПОДГОТОВКА К ЗАБЕГУ ────────────────────────────────────
async def lab_enter_confirm(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid, fields=["lab_depth"])
    depth = p.get("lab_depth", 1) or 1 if p else 1
    total_rooms = 4 + depth
    now = datetime.now()
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET last_lab_attempt=$1 WHERE user_id=$2", now, uid)
    invalidate_cache(uid)

    context.user_data["lab_room"] = 1
    context.user_data["lab_hp"] = 100
    context.user_data["lab_max_hp"] = 100
    context.user_data["lab_focus"] = 3
    context.user_data["lab_rewards"] = []
    context.user_data["lab_depth"] = depth
    context.user_data["lab_total_rooms"] = total_rooms
    context.user_data["lab_attack_bonus"] = 0.0
    context.user_data["lab_focused_attack"] = False
    context.user_data["lab_curse_rooms"] = 0

    room = random.choice(LABYRINTH_ROOMS)
    context.user_data["lab_current_room"] = room
    await show_lab_room(update, context)


# ─── ОТОБРАЖЕНИЕ КОМНАТЫ ─────────────────────────────────────
async def show_lab_room(update, context):
    room_index = context.user_data.get("lab_room", 1)
    hp = context.user_data.get("lab_hp", 100)
    max_hp = context.user_data.get("lab_max_hp", 100)
    focus = context.user_data.get("lab_focus", 3)
    total_rooms = context.user_data.get("lab_total_rooms", 5)
    depth = context.user_data.get("lab_depth", 1)
    attack_bonus = context.user_data.get("lab_attack_bonus", 0.0)
    focused = context.user_data.get("lab_focused_attack", False)
    curse = context.user_data.get("lab_curse_rooms", 0)

    if room_index > total_rooms:
        await show_lab_final(update, context)
        return

    # масштабирование комнаты под глубину
    base_room = random.choice(LABYRINTH_ROOMS)
    room = copy.deepcopy(base_room)
    risk_mult = 1.0 + (depth - 1) * 0.05
    reward_mult = 1.0 + (depth - 1) * 0.10
    atk = room["actions"]["attack"]
    atk["risks"] = [min(0.95, r * risk_mult) for r in atk["risks"]]
    atk["rewards"] = [(int(lo * reward_mult), int(hi * reward_mult)) for lo, hi in atk["rewards"]]
    context.user_data["lab_current_room"] = room

    # прогресс-бар здоровья
    hp_percent = int(hp / max_hp * 10)
    hp_bar = "▓" * hp_percent + "░" * (10 - hp_percent)

    # прогресс-бар комнат
    filled = "▓" * room_index
    empty = "░" * (total_rooms - room_index)
    room_bar = f"🚪{filled}{empty}🎁 {room_index}/{total_rooms}"

    text = (
        f"<b>🗝️ {room['name']}</b>\n\n"
        f"<i>\"{room['desc']}\"</i>\n\n"
        f"<b>❤️ HP: [{hp_bar}] {hp}/{max_hp}</b>\n"
        f"<b>⚡ Фокус: {focus}/3</b>\n"
        f"<b>Пройдено: {room_bar}</b>"
    )
    if attack_bonus > 0:
        text += f"\n<b>⚔️ Бонус атаки: +{int(attack_bonus*100)}%</b>"
    if focused:
        text += "\n🌀 <b>Концентрация активна</b> (следующая атака гарантирована)"
    if curse > 0:
        text += f"\n🌑 <b>Порча:</b> риск повышен (ещё {curse} комн.)"
    if hp < 30:
        text += "\n⚠️ <b>Вы тяжело ранены! Действия опаснее</b>"

    # кнопки
    kb_rows = []
    atk = room["actions"]["attack"]
    kb_rows.append([
        InlineKeyboardButton(f"⚔️ 🟢 (-{atk['costs'][0]} hp)", callback_data="lab_attack_0"),
        InlineKeyboardButton(f"⚔️ 🟡 (-{atk['costs'][1]} hp)", callback_data="lab_attack_1"),
        InlineKeyboardButton(f"⚔️ 🔴 (-{atk['costs'][2]} hp)", callback_data="lab_attack_2")
    ])
    sp = room["actions"]["special"]
    kb_rows.append([InlineKeyboardButton(f"{sp['name']} (-{sp['cost']} hp)", callback_data="lab_special")])

    if focus > 0 and not focused:
        kb_rows.append([InlineKeyboardButton("🌀 Сконцентрироваться (1⚡)", callback_data="lab_focus_use")])

    kb_rows.append([InlineKeyboardButton("🏃 Бежать (бесплатно)", callback_data="lab_escape")])

    chat_id = context.user_data.get("lab_chat_id")
    msg_id = context.user_data.get("lab_msg_id")
    kb = InlineKeyboardMarkup(kb_rows)
    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, reply_markup=kb, parse_mode='HTML'
            )
        except BadRequest:
            query = update.callback_query
            if query:
                await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    else:
        query = update.callback_query
        lab_msg = await query.message.reply_text(text, reply_markup=kb, parse_mode='HTML')
        context.user_data["lab_msg_id"] = lab_msg.message_id
        context.user_data["lab_chat_id"] = lab_msg.chat.id


# ─── ОБРАБОТКА ДЕЙСТВИЙ ──────────────────────────────────────
async def handle_lab_option(update, context):
    query = update.callback_query
    data = query.data
    await query.answer()

    hp = context.user_data.get("lab_hp", 100)
    focus = context.user_data.get("lab_focus", 3)
    max_hp = context.user_data.get("lab_max_hp", 100)
    room = context.user_data.get("lab_current_room")
    if not room:
        return

    # ─── КОНЦЕНТРАЦИЯ ───
    if data == "lab_focus_use":
        if focus <= 0:
            await query.answer("Нет фокуса.", show_alert=True)
            return
        if context.user_data.get("lab_focused_attack", False):
            await query.answer("Уже сконцентрированы.", show_alert=True)
            return
        context.user_data["lab_focus"] = focus - 1
        context.user_data["lab_focused_attack"] = True
        await query.answer("Концентрация! Следующая атака будет успешной.")
        await show_lab_room(update, context)
        return

    # ─── АТАКА ───
    if data.startswith("lab_attack_"):
        level = int(data.split("_")[-1])
        atk = room["actions"]["attack"]
        cost = atk["costs"][level]
        risk = atk["risks"][level]
        reward_range = atk["rewards"][level]

        if hp < cost:
            await query.answer("Недостаточно HP.", show_alert=True)
            return

        hp -= cost
        context.user_data["lab_hp"] = hp

        # штраф за низкое HP
        if hp < 30:
            risk += 0.15
        elif hp < 60:
            risk += 0.05
        # учёт порчи
        curse = context.user_data.get("lab_curse_rooms", 0)
        if curse > 0:
            risk += 0.10
            context.user_data["lab_curse_rooms"] = curse - 1
        risk = min(0.98, risk)

        focused = context.user_data.get("lab_focused_attack", False)
        if focused:
            success = True
            context.user_data["lab_focused_attack"] = False
        else:
            success = random.random() < risk

        if success:
            base_earned = random.randint(*reward_range)
            bonus = context.user_data.get("lab_attack_bonus", 0.0)
            if bonus > 0:
                base_earned = int(base_earned * (1 + bonus))
                context.user_data["lab_attack_bonus"] = 0.0
            # амулет не расходуется при успехе
            context.user_data.setdefault("lab_rewards", []).append(base_earned)
            await query.answer(f"Успех! +{base_earned} OAC")
        else:
            # проверка амулета
            if context.user_data.get("lab_amulet"):
                context.user_data["lab_amulet"] = False
                await query.answer("Амулет защитил тебя! Урон не получен.")
            else:
                extra_dmg = random.randint(5, 15)
                hp -= extra_dmg
                context.user_data["lab_hp"] = hp
                await query.answer(f"Провал! -{cost+extra_dmg} HP")

        if hp <= 0:
            context.user_data["lab_hp"] = 0
            await show_lab_death(update, context)
            return

        context.user_data["lab_room"] += 1
        await show_lab_room(update, context)
        return

    # ─── УНИКАЛЬНОЕ ДЕЙСТВИЕ ───
    elif data == "lab_special":
        sp = room["actions"]["special"]
        cost = sp["cost"]
        if hp < cost:
            await query.answer("Недостаточно HP.", show_alert=True)
            return

        hp -= cost
        context.user_data["lab_hp"] = hp

        effect = sp["effect"]
        success = random.random() < sp["risk"]

        if effect == "focus":
            if success:
                context.user_data["lab_focus"] = min(3, focus + sp.get("value", 1))
                await query.answer("+1 Фокус!")
            else:
                await query.answer("Ничего не произошло.")
        elif effect == "heal":
            if success:
                heal = sp.get("value", 30)
                context.user_data["lab_hp"] = min(max_hp, hp + heal)
                await query.answer(f"+{heal} HP!")
            else:
                context.user_data["lab_hp"] = max(0, hp - 10)
                await query.answer("Проклятая кровь! -10 HP")
        elif effect == "oac":
            if success:
                oac = random.randint(*sp["value"])
                context.user_data.setdefault("lab_rewards", []).append(oac)
                await query.answer(f"+{oac} OAC!")
            else:
                await query.answer("Тени отобрали твою находку.")
        elif effect == "next_boost":
            if success:
                context.user_data["lab_attack_bonus"] = sp.get("value", 0.5)
                await query.answer("Следующая атака будет мощнее!")
            else:
                await query.answer("Сгусток рассеялся.")
        elif effect == "reveal":
            await query.answer(f"Осталось комнат: {context.user_data.get('lab_total_rooms', 5) - context.user_data.get('lab_room', 1)}")
        elif effect == "mirror_hp":
            if success:
                new_hp = random.randint(20, 80)
                context.user_data["lab_hp"] = new_hp
                await query.answer(f"Отражение изменило тебя! HP = {new_hp}")
            else:
                await query.answer("Зеркало разбилось.")
        elif effect == "amulet":
            if success:
                context.user_data["lab_amulet"] = True
                await query.answer("Руны создали защитный амулет!")
            else:
                await query.answer("Руны погасли.")
        elif effect == "sacrifice_boost":
            if success:
                context.user_data["lab_attack_bonus"] = sp.get("value", 0.8)
                await query.answer("Пламя принимает жертву! +80% к атаке.")
            else:
                extra_dmg = random.randint(10, 20)
                context.user_data["lab_hp"] = max(0, hp - extra_dmg)
                await query.answer(f"Огонь отверг тебя! -{extra_dmg} HP")
        elif effect == "gamble":
            outcomes = [
                ("heal", 20),
                ("focus_gain", 1),
                ("oac_win", random.randint(30, 60)),
                ("damage", -15),
                ("curse", None)
            ]
            outcome = random.choice(outcomes)
            if outcome[0] == "heal":
                context.user_data["lab_hp"] = min(max_hp, hp + outcome[1])
                await query.answer(f"Голос исцелил тебя! +{outcome[1]} HP")
            elif outcome[0] == "focus_gain":
                context.user_data["lab_focus"] = min(3, focus + 1)
                await query.answer("Голос дарует озарение! +1 Фокус")
            elif outcome[0] == "oac_win":
                context.user_data.setdefault("lab_rewards", []).append(outcome[1])
                await query.answer(f"Награда из темноты! +{outcome[1]} OAC")
            elif outcome[0] == "damage":
                context.user_data["lab_hp"] = max(0, hp + outcome[1])
                await query.answer(f"Проклятие! {outcome[1]} HP")
            elif outcome[0] == "curse":
                context.user_data["lab_curse_rooms"] = 2
                await query.answer("Голос наслал порчу... Риск повышен на 2 комнаты.")

        await show_lab_room(update, context)
        return

    # ─── БЕГСТВО ───
    elif data == "lab_escape":
        hp = min(max_hp, hp + random.randint(15, 25))
        context.user_data["lab_hp"] = hp
        await query.answer("Ты сбежал, восстановив немного HP.")
        await show_lab_room(update, context)
        return

    await query.answer("Действие не реализовано")


# ─── ФИНАЛЬНЫЙ СУНДУК ────────────────────────────────────────
async def show_lab_final(update, context):
    query = update.callback_query
    uid = query.from_user.id
    p = await get_player_cached(uid, fields=["lab_depth"])
    if not p: return
    depth = p.get("lab_depth", 1) or 1
    rewards = context.user_data.get("lab_rewards", [])
    total_oac = sum(rewards) + 50
    await update_balance(uid, p.get("username"), total_oac)
    await update_essence(uid, 1)
    await increment_counter(uid, "lab_chests")
    await add_war_score(uid, 80)

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET lab_depth = lab_depth + 1 WHERE user_id=$1", uid)
    invalidate_cache(uid)

    context.user_data.pop("lab_hp", None)
    context.user_data.pop("lab_focus", None)
    context.user_data.pop("lab_room", None)

    text = (
        f"<b>🎁 СУНДУК ИСКАЖЕНИЯ</b>\n\n"
        f"<i>Ты достиг цели! Древние награждают достойных.</i>\n\n"
        f"<b>+{total_oac} OAC</b>\n"
        f"<b>💠 Кристальная Пыль: 1</b>\n"
        f"<b>🏆 Глубина увеличена! (Этаж {depth+1})</b>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К Лабиринту", callback_data="lab_start")],
                               [InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await safe_edit(update, context, text, reply_markup=kb)
    await check_achievements(uid, context)


# ─── СМЕРТЬ В ЛАБИРИНТЕ ──────────────────────────────────────
async def show_lab_death(update, context):
    query = update.callback_query
    uid = query.from_user.id
    p = await get_player_cached(uid, fields=["lab_depth"])
    if not p: return
    depth = p.get("lab_depth", 1) or 1
    await update_balance(uid, p.get("username"), 50)

    context.user_data.pop("lab_hp", None)
    context.user_data.pop("lab_focus", None)
    context.user_data.pop("lab_room", None)

    text = (
        f"<b>🪦 БЕЗДНА ПОГЛОТИЛА ТЕБЯ</b>\n\n"
        f"<i>Твоё здоровье иссякло</i>\n\n"
        f"<b>+50 OAC</b> (утешительный приз)\n"
        f"<b>Глубина: {depth}</b>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К Лабиринту", callback_data="lab_start")],
                               [InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await safe_edit(update, context, text, reply_markup=kb)

async def welcome_new_member(update, context):
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        await update.message.reply_text(
            f"<b><i>🕯️ ДОБРО ПОЖАЛОВАТЬ</i></b>\n\n⚜️ <b>{html.escape(member.username or member.first_name)}</b>, добро пожаловать в <b><i>Гильдию</i></b>\n<i>Твой первый /farm уже ждёт</i>"
        )

# Текстовые сокращения
async def handle_chat_shortcut(update, context):
    if not update.message or not update.message.text:
        return
    if context.user_data.get('awaiting_named_blunt'):
        await handle_named_name(update, context)
        return
    if context.user_data.get('gifting_blunt_id'):
        await handle_gift_username(update, context)
        return

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

# Обработчик кнопок + словарь колбэков
# ========== СЛОВАРЬ КОЛБЭКОВ ==========
CALLBACKS = {
    "menu": "menu",
    "farm": farm_callback,
    "craft": craft_callback,
    "smoke": smoke_callback,
    "ritual": ritual_callback,
    "collect": collect_callback,
    "profile": profile_callback,
    "top": top_callback,
    "guild_info": guild_info_callback,
    "rules": rules_callback,
    "privilege": privilege_callback,
    "catalog": catalog_callback,
    "luck": luck_callback,
    "craft_normal": handle_craft_normal,
    "craft_named": handle_craft_named,
    "cancel_named": cancel_named,
    "do_smoke": do_smoke,
    "use_dust": handle_use_dust,
    "top_scout": top_scout_callback,
    "achievements": achievements_callback,
    "my_blunts": my_blunts_callback,
    "lab_start": lab_enter,
    "lab_enter_confirm": lab_enter_confirm,
    "lab_escape": show_lab_final,
    "guild_shrine": guild_shrine_callback,
    "guild_war": guild_war_callback,
    "confess": confess_callback,
    "pet_preview": pet_preview,
    "bush_preview": "bush_preview",
    "activate_menu": "activate_menu",
    "skins_menu": "skins_menu",
    "choose_title": "choose_title",
    "choose_bg": "choose_bg",
    "shop": shop_callback,
}

# ========== ОБРАБОТЧИК КНОПОК (СЕНЬОРСКИЙ) ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = q.from_user.id
    try:
        # 1. Обработка специальных кнопок со своими параметрами
        if data == "menu":
            await q.answer()
            kb, whisper = await get_main_menu_keyboard(uid)
            menu_text = f"<b>🎮 ГЛАВНОЕ МЕНЮ</b>\n\n<i>{whisper}</i>"
            try:
                await q.message.edit_text(menu_text, reply_markup=kb, parse_mode='HTML')
            except Exception:
                await q.message.reply_text(menu_text, reply_markup=kb, parse_mode='HTML')
            return

        if data == "bush_preview" or data == "pet_preview":
            await q.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)", show_alert=True)
            return

        if data == "activate_menu":
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
            return

        if data == "skins_menu":
            await q.answer()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Выбрать титул", callback_data="choose_title")],
                [InlineKeyboardButton("🖼️ Выбрать фон", callback_data="choose_bg")],
                [InlineKeyboardButton("🔙 Назад", callback_data="profile")]
            ])
            await q.message.edit_text("<b>🎨 СКИНЫ</b>\n\nВыбери, что хочешь изменить.", reply_markup=kb, parse_mode='HTML')
            return

        if data == "choose_title":
            await q.answer()
            p = await get_player_cached(uid)
            titles = (p.get("titles") or "").split()
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
            return

        if data == "choose_bg":
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
            return

        # 2. Обработчики с параметрами (пагинация, колбэки с префиксами)
        if data.startswith("ach_page_"):
            page = int(data.split("_")[-1])
            await achievements_callback(update, context, page=page)
            return

        if data.startswith("blunts_page_"):
            page = int(data.split("_")[-1])
            await my_blunts_callback(update, context, page=page)
            return

        if data.startswith("share_blunt_"):
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
            return
            
        if data.startswith("gift_blunt_"):
            await gift_blunt_start(update, context)
            return

        if data == "cancel_gift":
            await cancel_gift(update, context)
            return

        if data.startswith("blunt_details_"):
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
    [InlineKeyboardButton("🔗 Поделиться", callback_data=f"share_blunt_{blunt_id}"),
     InlineKeyboardButton("🎁 Подарить", callback_data=f"gift_blunt_{blunt_id}")],
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
            return

        if data.startswith("lab_option_"):
            await handle_lab_option(update, context, int(data.split("_")[-1]))
            return

        if data in ("shrine_donate_100", "shrine_donate_500"):
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
            return

        if data in ("guild_join_BLACK", "guild_join_WHITE"):
            await q.answer()
            guild = "BLACK" if data == "guild_join_BLACK" else "WHITE"
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
            return

        # 3. Простые колбэки из словаря
        if data in ("luck_wheel", "luck_berserk", "alchemy_start", "alchemy_confirm"):
            await q.answer()
            await luck_callback(update, context, action=data)
            return

        if data in ("luck_wheel", "luck_berserk", "alchemy_start", "alchemy_confirm"):
            await q.answer()
            await luck_callback(update, context, action=data)
            return

        handler = CALLBACKS.get(data)
        if handler:
            await q.answer()
            await handler(update, context)
        else:
            await q.answer("Неизвестная команда.")

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
    
