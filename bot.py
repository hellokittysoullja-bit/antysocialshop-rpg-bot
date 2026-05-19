# bot.py — ANTY SOCIAL SHOP RPG v7.6.6.6 FINAL FIXED
import asyncio, logging, os, random, re, json, hashlib, html
from datetime import datetime, timedelta, date, time
from dataclasses import dataclass, field
from threading import Thread
import time
import sys
import asyncpg
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.error import BadRequest, Forbidden

from telegram.ext import AIORateLimiter

from typing import Optional, List, Any, Dict, NamedTuple, Callable
import functools

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)
from telegram.error import RetryAfter

import enum
from pydantic import BaseModel, Field, ConfigDict

from enum import Enum, auto

class AlchemyResult(Enum):
    SUCCESS = auto()
    NO_RESOURCES = auto()
# ============================================================
# ДЕКОРАТОРЫ (объявлены первыми, доступны везде)
# ============================================================
def safe_callback(func: Callable):
    """Декоратор, который логирует ошибки callback-запросов."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            logger.error(f"Callback error in {func.__name__}: {e}", exc_info=True)
            try:
                await update.callback_query.answer(f"❌ Ошибка: {e}", show_alert=True)
            except Exception:
                pass
    return wrapper


def error_handler(func):
    """Middleware: перехватывает исключения в обработчиках."""
    @functools.wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            logger.error(f"Unhandled error in {func.__name__}:", exc_info=True)
            if 'awaiting_named_blunt' in context.user_data:
                context.user_data['awaiting_named_blunt'] = False
            if update.callback_query:
                await update.callback_query.answer("⚠️ Внутренняя ошибка. Админ уже в курсе.", show_alert=True)
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ Что-то пошло не так. Попробуйте позже."
                )
            if ADMIN_ID:
                try:
                    err_msg = f"🚨 <b>Ошибка в {func.__name__}</b>\n<code>{html.escape(str(e))}</code>"
                    await context.bot.send_message(chat_id=ADMIN_ID, text=err_msg, parse_mode='HTML')
                except Exception as notify_err:
                    logger.error(f"Failed to notify admin: {notify_err}")
    return wrapper


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

# ============================================================
# КОНЕЦ БЛОКА ДЕКОРАТОРОВ
# ============================================================

# Проверка: если retry – модуль, а не функция, будет ошибка
assert callable(retry), "retry должен быть функцией, а не модулем!"

# ============================================================
# ГЛОБАЛЬНЫЙ КОНФИГ ИГРЫ (редактируй здесь, не трогая код)
# ============================================================
GAME_CONFIG = {
    "craft_cost": 15,                # стоимость обычного бланта
    "named_blunt_cost": 50,          # стоимость именного бланта
    "farm_cooldown_hours": 0.5,      # кулдаун фарма в часах
    "ritual_cooldown_hours": 24,     # кулдаун ритуала в часах
    "lab_cooldown_hours": 12,        # кулдаун лабиринта в часах
    "veteran_threshold": 5000,       # порог ранга "Ветеран"
    "phantom_threshold": 20000,      # порог ранга "Призрак"
    "necromant_threshold": 50000,    # порог ранга "Некромант"
}

PET_CONFIG = {
    "dog": {
        "name": "🐕 Песик",
        "price": 3000,
        "max_name_len": 15,
    },
}

# ── Хелпер проверки ранга (чтобы не дублировать if balance >= 5000) ──
def has_rank(balance: int, rank_name: str = "Ветеран") -> bool:
    thresholds = {
        "Ветеран": GAME_CONFIG["veteran_threshold"],
        "Призрак": GAME_CONFIG["phantom_threshold"],
        "Некромант": GAME_CONFIG["necromant_threshold"],
    }
    return balance >= thresholds.get(rank_name, 0)

# ── Хелпер проверки существования игрока ──
def ensure_player_exists(player) -> bool:
    """True, если игрок реально сохранён в БД."""
    return player is not None and getattr(player, 'exists', False)

# ── Исключения ──────────────────────────────────────────────
class UnknownWarActionError(Exception):
    """В конфиге отсутствует цена действия."""

# ── Enum действий ───────────────────────────────────────────
class WarAction(enum.Enum):
    FARM = "farm"
    CRAFT = "craft"
    NAMED_CRAFT = "named_craft"
    DUST_USE = "dust_use"
    BERSERK_WIN = "berserk_win"
    BERSERK_LOSE = "berserk_lose"
    ALCHEMY = "alchemy"
    LAB_WIN = "lab_win"
    LAB_DEATH = "lab_death"
    RITUAL = "ritual"
    CONFESS = "confess"
    DAILY = "daily"


# ── Конфиг очков (frozen) ──
class WarConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    points: dict[WarAction, int] = Field(default_factory=lambda: {
        WarAction.FARM: 0,
        WarAction.CRAFT: 10,
        WarAction.NAMED_CRAFT: 25,
        WarAction.DUST_USE: 50,
        WarAction.BERSERK_WIN: 200,
        WarAction.BERSERK_LOSE: -300,
        WarAction.ALCHEMY: 30,
        WarAction.LAB_WIN: 80,
        WarAction.LAB_DEATH: 0,
        WarAction.RITUAL: 0,
        WarAction.CONFESS: 0,
        WarAction.DAILY: 0,
    })

# ── Настройки окружения ──
class WarSettings:
    def __init__(self):
        self.cache_ttl = int(os.getenv("WAR_CACHE_TTL", "60"))
        self.retry_max = int(os.getenv("WAR_RETRY_MAX", "3"))
        self.retry_wait_sec = float(os.getenv("WAR_RETRY_WAIT_SEC", "0.5"))
        self.redis_url = os.getenv("WAR_REDIS_URL", "redis://localhost")

# ── Сервис войны ──
class GuildWarService:
    CACHE_KEY = "war_active"

    def __init__(self, db_pool, redis_client, config: WarConfig, settings: WarSettings):
        self.db_pool = db_pool
        self.redis = redis_client
        self.config = config
        self.settings = settings
        self.logger = logging.getLogger("war_service")
        self._last_redis_err = 0.0

        self._add_score_retry = retry(
            stop=stop_after_attempt(self.settings.retry_max),
            wait=wait_exponential(multiplier=1, min=self.settings.retry_wait_sec, max=5),
            retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, OSError, TimeoutError)),
            before_sleep=before_sleep_log(self.logger, logging.WARNING),
            reraise=True,
        )(self._add_score_impl)

    async def _get_active(self, conn):
        row = await conn.fetchrow("SELECT is_active FROM war_state WHERE id=1")
        return row["is_active"] if row else False

    async def is_war_active(self, conn=None) -> bool:
        try:
            if self.redis:
                cached = await self.redis.get(self.CACHE_KEY)
                if cached is not None:
                    return cached == b"1"
        except Exception:
            now = time.time()
            if now - self._last_redis_err > 60:
                self.logger.warning("Redis unavailable for war cache")
                self._last_redis_err = now

        if conn is not None:
            active = await self._get_active(conn)
        else:
            async with self.db_pool.acquire() as c:
                active = await self._get_active(c)

        try:
            if self.redis:
                await self.redis.setex(self.CACHE_KEY, self.settings.cache_ttl,
                                       b"1" if active else b"0")
        except Exception:
            pass
        return active

    async def start_war(self):
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(1)")
                await conn.execute("UPDATE war_state SET is_active = TRUE WHERE id=1")
                week_start = datetime.now().date() - timedelta(days=datetime.now().weekday())
                await conn.execute("DELETE FROM guild_weekly WHERE week_start < $1", week_start)
        await self.invalidate_cache()
        self.logger.info("War started")

    async def stop_war(self):
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(1)")
                await conn.execute("UPDATE war_state SET is_active = FALSE WHERE id=1")
        await self.invalidate_cache()
        self.logger.info("War stopped")

    async def add_score(self, user_id: int, action: WarAction, conn=None) -> None:
        points = self.config.points.get(action)
        if points is None:
            raise UnknownWarActionError(f"No points defined for {action}")
        if points == 0:
            return

        if self.redis:
            week_start = (datetime.now().date() - timedelta(days=datetime.now().weekday())).isoformat()
            idemp_key = f"war_score:{user_id}:{action.value}:{week_start}"
            success = await self.redis.setnx(idemp_key, 1)
            if success:
                await self.redis.expire(idemp_key, 60 * 60 * 24 * 7)
            else:
                self.logger.debug("Duplicate war score blocked: %s", idemp_key)
                return

        await self._add_score_retry(user_id, points, action, conn)

    async def add_score_raw(self, user_id: int, points: int, conn=None) -> None:
        if points == 0:
            return
        await self._add_score_retry(user_id, points, None, conn)

    async def _add_score_impl(self, user_id: int, points: int, action: WarAction | None, conn=None):
        async def _execute(c):
            row = await c.fetchrow("SELECT is_active FROM war_state WHERE id=1 FOR UPDATE")
            if not row or not row["is_active"]:
                return
            guild_row = await c.fetchrow("SELECT guild FROM players WHERE user_id=$1", user_id)
            guild = guild_row["guild"] if guild_row else None
            if guild not in ("BLACK", "WHITE"):
                return
            week_start = datetime.now().date() - timedelta(days=datetime.now().weekday())
            await c.execute(
                "INSERT INTO guild_weekly (guild, week_start, total_score) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (guild, week_start) DO UPDATE SET "
                "total_score = guild_weekly.total_score + EXCLUDED.total_score",
                guild, week_start, points,
            )
            self.logger.info("War score: user=%d action=%s guild=%s points=%d",
                             user_id, action.value if action else "raw", guild, points)

        if conn is not None:
            await _execute(conn)
        else:
            async with self.db_pool.acquire() as c:
                async with c.transaction():
                    await _execute(c)

    async def invalidate_cache(self):
        try:
            if self.redis:
                await self.redis.delete(self.CACHE_KEY)
        except Exception:
            pass

class Player(BaseModel):
    user_id: int
    username: str = ""
    balance: int = 0
    blunts: int = 0
    guild: Optional[str] = None
    last_farm: Optional[datetime] = None
    last_ritual: Optional[datetime] = None
    last_daily: Optional[datetime] = None
    titles: str = ""
    last_farm_date: Optional[date] = None
    passive_level: int = 0
    passive_collected: Optional[datetime] = None
    karma: int = 0
    inhaled: int = 0
    smoke_count: int = 0
    farm_count: int = 0
    craft_count: int = 0
    ritual_count: int = 0
    referral_count: int = 0
    last_berserk: Optional[datetime] = None
    inventory: List[Any] = Field(default_factory=list)
    invited_by: Optional[int] = None
    profile_skins: dict = Field(default_factory=dict)
    login_streak: int = 0
    last_login_date: Optional[date] = None
    oath: str = ""
    keys: int = 0
    check_count: int = 0
    m_essence: int = 0
    lab_chests: int = 0
    lab_deaths: int = 0
    alchemy_count: int = 0
    last_lab_attempt: Optional[datetime] = None
    donated: int = 0
    pending_transfer: Optional[dict] = None
    lab_depth: int = 1
    pet: str = ""           # 🐕 Песик
    pet_name: str = ""      # кличка
    exists: bool = False   # True, если игрок загружен из БД

    model_config = ConfigDict(populate_by_name=True)

import functools
import asyncio
import uvloop

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

# === ВЕБ-СЕРВЕР (Flask в отдельном потоке) ===
import threading

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Antysocialshop RPG Bot is alive!"

@web_app.route("/healthz")
def healthz():
    """Проверяет, жив ли бот и подключена ли БД."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            return "Event loop closed", 500
        if db_pool is None:
            return "No DB pool", 503
        loop.run_until_complete(_check_db())
        return "OK", 200
    except Exception as e:
        return str(e), 500

async def _check_db():
    async with db_pool.acquire() as conn:
        await conn.execute("SELECT 1")

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port, threaded=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout         
)
logging.getLogger("telegram").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)
# ---------- МАКСИМАЛЬНАЯ ОТЛАДКА ----------
import sys
import traceback

def log_unhandled_exception(exc_type, exc_value, exc_traceback):
    logger.critical("НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ:", exc_info=(exc_type, exc_value, exc_traceback))
    traceback.print_exception(exc_type, exc_value, exc_traceback)
sys.excepthook = log_unhandled_exception

# Уровни логирования
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger("telegram").setLevel(logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.INFO)

logger.info("===== БОТ ЗАПУСКАЕТСЯ =====")
logger.info(f"Python {sys.version}")
logger.info(f"Рабочая директория: {os.getcwd()}")

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


class PlayerRepository:
    @staticmethod
    async def get_by_id(user_id: int) -> Player:
        # Пробуем Redis
        if redis:
            key = f"player:{user_id}"
            data = await redis.get(key)
            if data:
                return Player.parse_raw(data)

        # Fallback – in‑memory кэш (словарь)
        if user_id in player_cache:
            p = player_cache[user_id]
            return Player(**p)

        # Запрос к БД
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM players WHERE user_id = $1", user_id)
        if row:
            p = dict(row)
            p["inventory"] = _json_safe_load(p.get("inventory"), [])
            p["profile_skins"] = _json_safe_load(p.get("profile_skins"), {})
            player = Player(**p)
            player.exists = True

            # Кэшируем
            if redis:
                await redis.setex(f"player:{user_id}", 10, player.json())
            else:
                player_cache[user_id] = player.dict()
            return player

        # Игрок не найден – возвращаем новый объект (он создастся в БД при первом save)
        return Player(user_id=user_id)

    @staticmethod
    async def save(player: Player, conn=None):
        inv_json = json.dumps(player.inventory, default=str)
        skins_json = json.dumps(player.profile_skins, default=str)

        async def _write(c):
            await c.execute("""
                INSERT INTO players (user_id, username, balance, blunts, guild, last_farm,
                    last_ritual, last_daily, titles, last_farm_date, passive_level,
                    passive_collected, karma, inhaled, smoke_count, farm_count,
                    craft_count, ritual_count, referral_count, last_berserk,
                    inventory, invited_by, profile_skins, login_streak,
                    last_login_date, oath, keys, check_count, m_essence,
                    lab_chests, lab_deaths, alchemy_count, last_lab_attempt,
                    donated, pending_transfer, lab_depth, pet, pet_name)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                        $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,
                        $30,$31,$32,$33,$34,$35,$36,$37,$38)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    balance = EXCLUDED.balance,
                    blunts = EXCLUDED.blunts,
                    guild = EXCLUDED.guild,
                    last_farm = EXCLUDED.last_farm,
                    last_ritual = EXCLUDED.last_ritual,
                    last_daily = EXCLUDED.last_daily,
                    titles = EXCLUDED.titles,
                    last_farm_date = EXCLUDED.last_farm_date,
                    passive_level = EXCLUDED.passive_level,
                    passive_collected = EXCLUDED.passive_collected,
                    karma = EXCLUDED.karma,
                    inhaled = EXCLUDED.inhaled,
                    smoke_count = EXCLUDED.smoke_count,
                    farm_count = EXCLUDED.farm_count,
                    craft_count = EXCLUDED.craft_count,
                    ritual_count = EXCLUDED.ritual_count,
                    referral_count = EXCLUDED.referral_count,
                    last_berserk = EXCLUDED.last_berserk,
                    inventory = EXCLUDED.inventory,
                    invited_by = EXCLUDED.invited_by,
                    profile_skins = EXCLUDED.profile_skins,
                    login_streak = EXCLUDED.login_streak,
                    last_login_date = EXCLUDED.last_login_date,
                    oath = EXCLUDED.oath,
                    keys = EXCLUDED.keys,
                    check_count = EXCLUDED.check_count,
                    m_essence = EXCLUDED.m_essence,
                    lab_chests = EXCLUDED.lab_chests,
                    lab_deaths = EXCLUDED.lab_deaths,
                    alchemy_count = EXCLUDED.alchemy_count,
                    last_lab_attempt = EXCLUDED.last_lab_attempt,
                    donated = EXCLUDED.donated,
                    pending_transfer = EXCLUDED.pending_transfer,
                    lab_depth = EXCLUDED.lab_depth,
                    pet = EXCLUDED.pet,
                    pet_name = EXCLUDED.pet_name
            """,
                player.user_id, player.username, player.balance, player.blunts,
                player.guild, player.last_farm, player.last_ritual, player.last_daily,
                player.titles, player.last_farm_date, player.passive_level,
                player.passive_collected, player.karma, player.inhaled,
                player.smoke_count, player.farm_count, player.craft_count,
                player.ritual_count, player.referral_count, player.last_berserk,
                inv_json, player.invited_by, skins_json,
                player.login_streak, player.last_login_date, player.oath,
                player.keys, player.check_count, player.m_essence,
                player.lab_chests, player.lab_deaths, player.alchemy_count,
                player.last_lab_attempt, player.donated, player.pending_transfer,
                player.lab_depth, player.pet, player.pet_name
            )

        if conn is not None:
            await _write(conn)
        else:
            async with db_pool.acquire() as new_conn:
                await _write(new_conn)

        # Инвалидируем кэш
        if redis:
            await redis.delete(f"player:{player.user_id}")
        else:
            player_cache.pop(player.user_id, None)

    @staticmethod
    async def atomic_update(user_id: int, update_func):
        """
        Атомарно блокирует игрока (SELECT ... FOR UPDATE),
        выполняет переданную функцию update_func(player, conn),
        сохраняет модель и возвращает результат update_func.
        Если игрок не найден, возвращает None.
        """
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM players WHERE user_id = $1 FOR UPDATE",
                    user_id
                )
                if not row:
                    return None

                # Строим модель из строки БД
                p = dict(row)
                p["inventory"] = _json_safe_load(p.get("inventory"), [])
                p["profile_skins"] = _json_safe_load(p.get("profile_skins"), {})
                player = Player(**p)

                # Вызываем игровую логику
                result = await update_func(player, conn)

                # Сохраняем модель (передаём соединение, чтобы остаться в транзакции)
                await PlayerRepository.save(player, conn=conn)

                # Возвращаем результат (например, данные для сообщения)
                return result

    @staticmethod
    async def claim_daily(user_id: int, today: date, streak: int, reward_oac: int,
                          title: Optional[str], inventory_items: dict) -> bool:
        """
        Атомарно начисляет ежедневную награду.
        Возвращает True, если награда начислена, False – если сегодня уже заходил.
        """
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM players WHERE user_id = $1 FOR UPDATE", user_id
                )
                if not row:
                    return False
                p = dict(row)
                p["inventory"] = _json_safe_load(p.get("inventory"), [])
                p["profile_skins"] = _json_safe_load(p.get("profile_skins"), {})
                player = Player(**p)

                if player.last_login_date == today:
                    return False

                player.balance = (player.balance or 0) + reward_oac
                player.login_streak = streak
                player.last_login_date = today

                if title:
                    current = (player.titles or "").strip()
                    if title not in current:
                        player.titles = f"{current} {title}".strip()

                for field, amount in inventory_items.items():
                    if hasattr(player, field):
                        setattr(player, field, (getattr(player, field) or 0) + amount)

                await PlayerRepository.save(player, conn=conn)
                return True

@db_retry()
async def create_named_blunt(user_id, name, rarity=None, conn=None):
    if rarity not in ("common", "rare", "epic", "legendary"):
        r = random.random()
        if r < 0.02: rarity = "legendary"
        elif r < 0.15: rarity = "epic"
        elif r < 0.45: rarity = "rare"
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
        "serial": None,
        "rare_number": rare_number,
        "hash": hash_code,
        "reaction": reaction,
        "created_at": datetime.utcnow().isoformat(),
        "owner_history": [{"user_id": str(user_id), "since": datetime.utcnow().isoformat()}],
    }

    player = await PlayerRepository.get_by_id(user_id)
    player.inventory = _json_safe_load(player.inventory, [])
    player.inventory.append(item)
    await PlayerRepository.save(player, conn=conn)
    return item


async def _award_achievement_rewards(user_id, player, reward_text, context):
    """
    Выдаёт награды за достижения, работая напрямую с моделью Player.
    player – это уже объект Player (не словарь), но для обратной совместимости
    поддерживается и старый вызов со словарём (тогда преобразуем).
    """
    if not reward_text:
        return

    # Универсально получаем модель Player (если передали словарь – загружаем)
    if isinstance(player, dict):
        player = await PlayerRepository.get_by_id(user_id)
    if not player or not player.user_id:
        return

    parts = [p.strip() for p in reward_text.split(",") if p.strip()]
    for part in parts:
        if part.startswith("+") and "OAC" in part:
            clean = part.replace(" ", "")
            m = re.search(r"\+(\d+)", clean)
            if m:
                amount = int(m.group(1))
                player.balance = (player.balance or 0) + amount

        elif part.startswith("Титул "):
            title = part.replace("Титул ", "").strip()
            if title:
                titles = (player.titles or "").split()
                if title not in titles:
                    titles.append(title)
                    player.titles = " ".join(titles).strip()

        elif part.startswith("Фон "):
            bg = part.replace("Фон ", "").strip()
            skins = player.profile_skins or {}
            if not isinstance(skins, dict):
                skins = {}
            unlocked = skins.get("unlocked_backgrounds", [])
            if bg and bg not in unlocked:
                unlocked.append(bg)
            skins["unlocked_backgrounds"] = unlocked
            player.profile_skins = skins

        elif part.startswith("Рамка "):
            frame = part.replace("Рамка ", "").strip()
            skins = player.profile_skins or {}
            if not isinstance(skins, dict):
                skins = {}
            unlocked = skins.get("unlocked_frames", [])
            if frame and frame not in unlocked:
                unlocked.append(frame)
            skins["unlocked_frames"] = unlocked
            player.profile_skins = skins

        else:
            logger.warning(f"Неизвестный формат награды: {part} для пользователя {user_id}")

    # Сохраняем все изменения разом
    await PlayerRepository.save(player)

async def check_achievements(user_id, context):
    player = await PlayerRepository.get_by_id(user_id)
    if not player or not player.user_id:
        return

    # Кэш полученных ачивок
    awarded_key = f"ach:{user_id}"
    awarded = set()
    if redis:
        cached = await redis.get(awarded_key)
        if cached:
            awarded = set(json.loads(cached))

    if not awarded:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", user_id)
            awarded = {r["ach_id"] for r in rows}
            if redis:
                await redis.setex(awarded_key, 60, json.dumps(list(awarded)))

    # Словарь условий: ach_id -> (поле_модели, порог)
    ACHIEVEMENT_CONDITIONS = {
        "farm_1": ("farm_count", 1),
        "craft_1": ("craft_count", 1),
        "smoke_1": ("smoke_count", 1),
        "balance_1000": ("balance", 1000),
        "smoke_10": ("smoke_count", 10),
        "craft_15": ("craft_count", 15),
        "ritual_5": ("ritual_count", 5),
        "craft_50": ("craft_count", 50),
        "smoke_25": ("smoke_count", 25),
        "lab_first": ("lab_chests", 1),
        "referral_1": ("referral_count", 1),
        "streak_7": ("login_streak", 7),
        "balance_20000": ("balance", 20000),
        "lab_chest_3": ("lab_chests", 3),
        "rank_phantom": ("balance", 20000),
        "balance_50000": ("balance", 50000),
        "check_10": ("check_count", 10),
        "lab_death_5": ("lab_deaths", 5),
        "lab_chest_10": ("lab_chests", 10),
        "craft_250": ("craft_count", 250),
        "alchemy_15": ("alchemy_count", 15),
    }

    async with db_pool.acquire() as conn:
        for ach in ACHIEVEMENTS:
            ach_id = ach["id"]
            if ach_id == "lunar_lord":
                continue

            condition_met = False
            if ach_id in ACHIEVEMENT_CONDITIONS:
                field, threshold = ACHIEVEMENT_CONDITIONS[ach_id]
                if getattr(player, field, 0) >= threshold:
                    condition_met = True

            if condition_met and ach_id not in awarded:
                await conn.execute(
                    "INSERT INTO achievements_awarded(user_id, ach_id, awarded_at) VALUES($1, $2, NOW()) ON CONFLICT DO NOTHING",
                    user_id, ach_id
                )
                # Награду адаптируем – используем player.username из модели
                await _award_achievement_rewards(user_id, {"username": player.username, "profile_skins": player.profile_skins, "balance": player.balance}, ach.get("reward", ""), context)
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

                # Сбрасываем кэш, потому что список изменился
                if redis:
                    await redis.delete(awarded_key)
                awarded.add(ach_id)

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
            await _award_achievement_rewards(user_id, {"username": player.username, "profile_skins": player.profile_skins, "balance": player.balance}, lunar.get("reward", ""), context)
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
            if redis:
                await redis.delete(awarded_key)
                
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

    # Шаг 1: Выполняем все миграции через временное соединение,
    # чтобы гарантировать актуальность схемы перед созданием пула.
    async with asyncpg.create_pool(database_url, min_size=1, max_size=1, command_timeout=15) as migration_pool:
        async with migration_pool.acquire() as conn:
            await create_tables(conn)
            await _run_migrations(conn)
            await init_redis()

    # Шаг 2: Создаём основной пул, который будет использоваться всем ботом.
    # Кэш подготовленных запросов будет чистым и соответствующим новой схеме.
    db_pool = await asyncpg.create_pool(
        database_url,
        min_size=5,
        max_size=20,
        command_timeout=15
    )
    logger.info("База данных Neon инициализирована (пул 5-20, таймаут 10с).")

async def _run_migrations(conn):
    """Все миграции, которые необходимо применить перед запуском."""
    # Добавление столбца war_active в guild_weekly (если его ещё нет)
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
    # Добавление pending_transfer в players
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
    # Добавление lab_depth в players
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='players' AND column_name='lab_depth'
            ) THEN
                ALTER TABLE players ADD COLUMN lab_depth INTEGER DEFAULT 1;
            END IF;
        END $$;
    """)

    # ===== Новые миграции для сервиса войны =====
    # 1. Таблица состояния войны
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS war_state (
            id INTEGER PRIMARY KEY DEFAULT 1,
            is_active BOOLEAN DEFAULT FALSE
        );
        INSERT INTO war_state (id, is_active) VALUES (1, FALSE)
        ON CONFLICT (id) DO NOTHING;
    """)

    # 2. Добавляем week_start в guild_weekly, если его нет (старые базы)
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='guild_weekly' AND column_name='week_start'
            ) THEN
                ALTER TABLE guild_weekly ADD COLUMN week_start DATE;
            END IF;
        END $$;
    """)

    # 3. Переименовываем total_farmed -> total_score, если столбец ещё старый
    await conn.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='guild_weekly' AND column_name='total_farmed')
            THEN
                ALTER TABLE guild_weekly RENAME COLUMN total_farmed TO total_score;
            END IF;
        END $$;
    """)

    # 4. Если после переименования total_score всё ещё отсутствует (новая база),
    #    создаём его с нуля
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='guild_weekly' AND column_name='total_score'
            ) THEN
                ALTER TABLE guild_weekly ADD COLUMN total_score INTEGER DEFAULT 0;
            END IF;
        END $$;
    """)

    # 5. Удаляем устаревший war_active из guild_weekly, если он там есть
    await conn.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='guild_weekly' AND column_name='war_active'
            ) THEN
                ALTER TABLE guild_weekly DROP COLUMN war_active;
            END IF;
        END $$;
    """)

    # 6. Уникальный индекс для UPSERT по (guild, week_start)
    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_guild_week
        ON guild_weekly (guild, week_start);
    """)

    # Таблица для хранения file_id и других настроек
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)

    # === Финальная проверка целостности ===
    try:
        await conn.execute("SELECT 1 FROM war_state LIMIT 1")
        await conn.execute("SELECT total_score, week_start FROM guild_weekly LIMIT 0")
        logger.info("✅ Миграции успешно применены, целостность БД подтверждена")
    except Exception as e:
        logger.critical("❌ Ошибка целостности после миграций: %s", e)
        raise RuntimeError("Database integrity check failed") from e
    
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

    # Гарантируем наличие столбца pending_transfer
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

    # Гарантируем наличие столбца lab_depth
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='players' AND column_name='lab_depth'
            ) THEN
                ALTER TABLE players ADD COLUMN lab_depth INTEGER DEFAULT 1;
            END IF;
        END $$;
    """)


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
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
   
async def get_setting(key: str, default: str = "") -> str:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM bot_settings WHERE key=$1", key)
        return row["value"] if row else default

async def set_setting(key: str, value: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, value
        )

async def send_whisper(context, chat_id, text):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Whisper error: {e}")

# КОМАНДА УСТАНОВКИ ФОТО БЛАНТА
async def safe_send_blunt_image(context, chat_id, rarity):
    """
    Отправляет фото бланта с максимальной защитой.
    - Если file_id нет -> сообщение об отсутствии.
    - Если file_id есть, но невалиден -> автосброс, уведомление админа, сообщение игроку.
    - Если ошибка Telegram (флуд, таймаут) -> повтор через retry.
    - При любом другом сбое -> сообщение игроку, лог ошибки.
    """
    file_id = BLUNT_IMAGES.get(rarity)
    if not file_id:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🖼️ Изображение отсутствует. Админ скоро добавит.",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error("Не удалось отправить сообщение об отсутствии изображения: %s", e)
        return

    try:
        await context.bot.send_photo(chat_id=chat_id, photo=file_id)
    except BadRequest as e:
        if "Wrong file identifier" in str(e):
            logger.warning("Невалидный file_id для %s, сброшен", rarity)
            # Сброс невалидного ID
            BLUNT_IMAGES.pop(rarity, None)
            try:
                await set_setting(f"blunt_image_{rarity}", "")
            except Exception as ex:
                logger.error("Не удалось сбросить file_id в БД: %s", ex)
            # Уведомление админа
            if ADMIN_ID:
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"⚠️ Изображение для {rarity} недействительно. Обновите: /setbluntpic {rarity}"
                    )
                except Exception as ex:
                    logger.error("Не удалось уведомить админа: %s", ex)
            # Сообщение игроку
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🟡 Легендарный бланк создан!\n<i>Изображение временно недоступно.</i>",
                    parse_mode='HTML'
                )
            except Exception as ex:
                logger.error("Не удалось отправить fallback-сообщение игроку: %s", ex)
        else:
            # Другие BadRequest (например, неправильный chat_id) логируем, но не роняем
            logger.error("BadRequest при отправке фото: %s", e)
    except RetryAfter as e:
        logger.warning("RetryAfter при отправке фото: %s сек", e.retry_after)
    except Exception as e:
        logger.error("Непредвиденная ошибка при отправке фото: %s", e)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Произошла ошибка при отправке изображения. Мы уже знаем и чиним.",
                parse_mode='HTML'
            )
        except Exception:
            pass

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

def next_sunday_str() -> str:
    now = datetime.now()
    days_until_sunday = (6 - now.weekday()) % 7
    if days_until_sunday == 0:
        days_until_sunday = 7
    next_sunday = now + timedelta(days=days_until_sunday)
    return next_sunday.strftime("%d.%m")

async def add_title(user_id, emoji, conn=None):
    player = await PlayerRepository.get_by_id(user_id)
    titles = (player.titles or "").split()
    if emoji not in titles:
        titles.append(emoji)
        player.titles = " ".join(titles).strip()
        await PlayerRepository.save(player, conn=conn)

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

def progress_bar(percent):
    filled = int(percent / 10)
    empty = 10 - filled
    return "▓" * filled + "░" * empty

def get_rank_info(balance: int):
    """Возвращает эмодзи и название ранга по балансу."""
    if balance >= 50000:
        return "🪬", "Некромант"
    elif balance >= 20000:
        return "🪦", "Призрак"
    elif balance >= 5000:
        return "⚔️", "Ветеран"
    else:
        return "🪓", "Рекрут"

async def process_daily_login(user_id: int, context) -> None:
    today = date.today()
    player = await PlayerRepository.get_by_id(user_id)
    if not player or not player.user_id:
        return

    last = _parse_last_login_date(player.last_login_date)
    if last == today:
        return

    streak = (player.login_streak or 0) + 1 if last and (today - last).days == 1 else 1
    reward = _calculate_reward(streak, daily_config)

    # Атомарно применяем награду с повторной проверкой даты после блокировки
    async def _apply_daily(p, conn):
        p_date = _parse_last_login_date(p.last_login_date)
        if p_date == today:
            return False   # уже начислено другим запросом

        p.balance += reward.total_oac
        p.login_streak = streak
        p.last_login_date = today

        # Начисление титула
        if reward.title:
            current_titles = (p.titles or "").strip()
            if reward.title not in current_titles:
                if current_titles:
                    p.titles = f"{current_titles} {reward.title}".strip()
                else:
                    p.titles = reward.title

        # Предметы (только blunts, остальное — просто текст в сообщении)
        for field, qty in reward.inventory_items.items():
            if field == "blunts":
                p.blunts += qty
        return True

    result = await PlayerRepository.atomic_update(user_id, _apply_daily)
    if not result:
        if result is False:
            logger.info("Daily already claimed (race prevented) for user %d", user_id)
        else:
            logger.warning("Daily login: atomic_update returned None for user %d", user_id)
        return

    logger.info("Daily login processed", extra={
        "user_id": user_id, "streak": streak,
        "reward_oac": reward.total_oac, "title": reward.title,
        "items": reward.inventory_items
    })

    try:
        text = _build_daily_message(streak, reward, daily_config)
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.error("Failed to send daily login msg", extra={"user_id": user_id}, exc_info=True)

    try:
        await check_achievements(user_id, context)
    except Exception:
        logger.exception("Achievement check failed", extra={"user_id": user_id})


# ── Конфигурация кулдаунов (можно править без захода в функцию) ──
MAIN_MENU_COOLDOWNS = {
    "farm": {
        "text": "🍬 Фармить",
        "cooldown_hours": FARM_COOLDOWN_HOURS,
        "last_attr": "last_farm",
        "format": "min",
    },
    "ritual": {
        "text": "🕯️ Ритуал",
        "cooldown_hours": 24,
        "last_attr": "last_ritual",
        "format": "hrs",
        "guild_only": "BLACK",        # <-- убрать, если нужна всем
    },
    "lab": {
        "text": "🏛️ Лабиринт",
        "cooldown_hours": 12,
        "last_attr": "last_lab_attempt",
        "format": "full",
    },
}

def _format_cooldown(player, now, key: str) -> str:
    """Возвращает текст кнопки с кулдауном или без."""
    config = MAIN_MENU_COOLDOWNS.get(key)
    if not config:
        return ""

    if config.get("guild_only") and (not player or player.guild != config["guild_only"]):
        return ""

    text = config["text"]
    last_attr = config.get("last_attr")
    if not last_attr or not player:
        return text

    last_time = getattr(player, last_attr, None)
    if not last_time:
        return text

    cooldown_hours = config["cooldown_hours"]
    remain = timedelta(hours=cooldown_hours) - (now - last_time)
    if remain.total_seconds() <= 0:
        return text

    fmt = config.get("format", "min")
    if fmt == "min":
        mins = int(remain.total_seconds() // 60)
        return f"{text} ⏳ {mins} мин"
    elif fmt == "hrs":
        hrs = int(remain.total_seconds() // 3600)
        return f"{text} ⏳ {hrs} ч"
    else:  # full
        hrs = int(remain.total_seconds() // 3600)
        mins = int((remain.total_seconds() % 3600) // 60)
        return f"{text} ⏳ {hrs} ч {mins} мин"


_menu_cache = {}

def invalidate_menu_cache(user_id: int):
    """Сброс кэша меню для конкретного пользователя."""
    _menu_cache.pop(user_id, None)

async def get_main_menu_keyboard(user_id):
    now = time.time()
    if user_id in _menu_cache:
        cached_time, kb, whisper = _menu_cache[user_id]
        if now - cached_time < 2:
            return kb, whisper

    whisper = random.choice(WHISPERS)
    player = await PlayerRepository.get_by_id(user_id)
    balance = player.balance if player else 0
    now_dt = datetime.now()

    # ── Кнопки с кулдаунами ──
    farm_text = _format_cooldown(player, now_dt, "farm")
    ritual_text = _format_cooldown(player, now_dt, "ritual")
    lab_text = _format_cooldown(player, now_dt, "lab")

    # ── Кнопки условий ──
    bush_btn = (
        InlineKeyboardButton("🪴 Куст", callback_data="collect")
        if balance >= 5000
        else InlineKeyboardButton("🪴 Куст 🔒", callback_data="bush_preview")
    )
    pet_btn = (
        InlineKeyboardButton("🐾 Питомец", callback_data="pet_preview")
        if balance >= 5000
        else InlineKeyboardButton("🐾 Питомец 🔒", callback_data="pet_preview")
    )

    # ── Сборка клавиатуры ──
    keyboard = [
        [InlineKeyboardButton(farm_text, callback_data="farm")],
        [InlineKeyboardButton("🌿 Крафт", callback_data="craft"),
         InlineKeyboardButton("💨 Дунуть", callback_data="smoke")],
        [bush_btn],
        [InlineKeyboardButton("⚜️ Профиль", callback_data="profile"),
         InlineKeyboardButton("🏆 Лидеры", callback_data="top")],
    ]

    # Группа гильдия + питомец + (опционально ритуал)
    guild_row = [InlineKeyboardButton("🕋 Гильдия", callback_data="guild_info")]
    if ritual_text:                         # <-- если кнопка ритуала сгенерировалась
        guild_row.append(InlineKeyboardButton(ritual_text, callback_data="ritual"))
    guild_row.append(pet_btn)
    keyboard.append(guild_row)

    keyboard.append([
        InlineKeyboardButton("🎲 Удача", callback_data="luck"),
        InlineKeyboardButton(lab_text, callback_data="lab_start"),
    ])
    keyboard.append([InlineKeyboardButton("🛒 Магазин", callback_data="shop")])

    kb = InlineKeyboardMarkup(keyboard)
    _menu_cache[user_id] = (now, kb, whisper)
    return kb, whisper

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])

# ========== ОБРАБОТЧИКИ КОМАНД (полный, надёжный, с лабиринтом) ==========
logger = logging.getLogger(__name__)   # ← 

# --- Retry-обёртки для Telegram API (обработка 429) ---
@retry(
    retry=retry_if_exception_type(RetryAfter),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    reraise=True
)
async def safe_send(context, chat_id, text, **kwargs):
    """Отправка сообщения с автоматическим повтором при 429."""
    return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)

@retry(
    retry=retry_if_exception_type(RetryAfter),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    reraise=True
)
async def safe_edit(message, text, **kwargs):
    """Редактирование сообщения с автоматическим повтором при 429."""
    return await message.edit_text(text, **kwargs)
    
#короче тут у нас этот ёбаный эдит

async def edit_or_reply(update, context, text, reply_markup=None, parse_mode='HTML', disable_web_page_preview=True):
    chat_id = update.effective_chat.id
    message = update.callback_query.message if update.callback_query else update.message
    try:
        if message and message.text:
            await safe_edit(message, text, reply_markup=reply_markup,
                            parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)
        else:
            raise BadRequest("no text to edit")
    except (BadRequest, Forbidden) as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            return
        logger.warning("edit_or_reply fallback to safe_send: %s", e, extra={"chat_id": chat_id})
        try:
            await safe_send(context, chat_id, text, reply_markup=reply_markup,
                            parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)
        except Exception as send_error:
            logger.error("safe_send also failed: %s", send_error, exc_info=True)
    except Exception as e:
        logger.exception("Unexpected error in edit_or_reply")
        try:
            await safe_send(context, chat_id, text)
        except Exception:
            pass

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
                f"<b>⚜️ Ранг: {curr_emoji} → {next_emoji}</b>\n"
                f"🎯 <b>{bar} {progress}%</b>\n"
                f"<b>{balance} / {next_th} OAC 💎</b>"
            )
    return ""
    
#---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ /start

async def _handle_referral(update, context, uid, player):
    """Атомарно обрабатывает реферальную ссылку blunt_..."""
    if not context.args or not context.args[0].startswith("blunt_"):
        return

    ref_blunt_id = context.args[0].replace("blunt_", "")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, inventory FROM players")
    creator_id = None
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

    if not creator_id or creator_id == uid:
        return

    creator = await PlayerRepository.get_by_id(creator_id)
    if not creator or player.invited_by:
        return

    # Атомарно начисляем рефереру бонусы и связываем игроков
    async def _ref(p, conn):
        p.balance = (p.balance or 0) + 50
        p.referral_count = (p.referral_count or 0) + 1
        name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
        await create_named_blunt(creator_id, name, rarity="legendary", conn=conn)
        if "🩸" not in (p.titles or ""):
            p.titles = f"{p.titles or ''} 🩸".strip()
        # Связываем реферала с создателем
        player.invited_by = creator_id
        await PlayerRepository.save(player, conn=conn)

    await PlayerRepository.atomic_update(creator_id, _ref)

  # Оповещение в канал (закомментировано для безопасного старта)
    # try:
    #     uname = html.escape(update.effective_user.username or update.effective_user.first_name or "Странник")
    #     await context.bot.send_message(
    #         chat_id="@guild_antysocial",
    #         text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{uname}</b> был призван нитью @{html.escape(creator.username)}.\n🕸️ Искажение становится плотнее...",
    #         parse_mode='HTML'
    #     )
    # except Exception as e:
    #     logger.error(f"Ошибка отправки в канал: {e}")


async def _create_new_player(update, context, uid, username):
    """Создаёт нового игрока и отправляет приветствие с выбором гильдии."""
    player = Player(user_id=uid, username=username, balance=800)
    new_name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
    await create_named_blunt(uid, new_name)
    await PlayerRepository.save(player)

    bonus = "🎁 Смотритель дарует тебе <code>800</code> 🍬 и твой первый именной блант!\n\n"
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
    await update.effective_message.reply_text(bonus + welcome, reply_markup=guild_kb, parse_mode='HTML')


async def _show_main_menu(update, context, player, user):
    """Формирует и отправляет главное меню."""
    bal = player.balance
    guild = player.guild

    # Определение текущего и следующего ранга
    rank_emoji, rank_name = "🪓", "Рекрут"
    next_rank_emoji, next_rank_name, next_threshold = "", "", 0
    for i, (emoji, threshold, _) in enumerate(RANKS):
        if bal >= threshold:
            rank_emoji = emoji.split(' ', 1)[0]
            rank_name = emoji.split(' ', 1)[1] if ' ' in emoji else emoji
            if i + 1 < len(RANKS):
                next_rank_emoji = RANKS[i+1][0].split(' ', 1)[0]
                next_rank_name = RANKS[i+1][0].split(' ', 1)[1] if ' ' in RANKS[i+1][0] else RANKS[i+1][0]
                next_threshold = RANKS[i+1][1]
        else:
            next_rank_emoji = emoji.split(' ', 1)[0]
            next_rank_name = emoji.split(' ', 1)[1] if ' ' in emoji else emoji
            next_threshold = threshold
            break

    display_name = user.first_name or user.username or "Странник"
    rank_display = f"{rank_emoji} {rank_name}" if rank_name else rank_emoji
    whisper = random.choice(WHISPERS)

    # Приветствие и гильдия
    back = f"<b>⚔️ С возвращением в Гильдию, {rank_display} {html.escape(display_name)}.</b>\n\n"
    if guild == "BLACK":
        back += "<b>🔮 Ты — часть Темной Гильдии. 🕯️Ритуалы ждут тебя</b>\n"
    elif guild == "WHITE":
        back += "<b>🔮 Ты — часть Светлой Гильдии. ⚜️Исповедь очищает душу и ждёт тебя</b>\n"
    else:
        back += "<b>🔮 Ты пока не в Гильдии. Нажми 🕋Гильдии чтобы вступить!</b>\n"

    # Мотивационная строка
    if next_threshold > 0:
        gap = next_threshold - bal
        back += f"\n<b>🎉 До следующего ранга {next_rank_emoji} {next_rank_name} осталось {gap} OAC 🍬!</b>"
    else:
        back += f"\n<b>⚡ Ты достиг вершины! Твой ранг — {rank_emoji} {rank_name}.</b>"

    # Подсказка для новичков
    farm_count = player.farm_count
    guild_joined = guild is not None
    craft_count = player.craft_count
    is_veteran = bal >= 5000

    if farm_count == 0:
        hint = "<b>💡 Твой первый шаг: нажми 🍬 Фармить и получи свои первые OAC!</b>"
    elif not guild_joined:
        hint = "<b>💡 Отлично! Теперь вступи в 🕋 Гильдию — это откроет ритуалы и исповеди.</b>"
    elif craft_count == 0:
        hint = "<b>💡 Попробуй 🌿 Крафт, чтобы создать свой первый Блант!</b>"
    elif is_veteran:
        hint = "<b>💡 Исследуй 🔮 Алхимию и корми своего 🐾 питомца!</b>"
    else:
        hint = "<b>💡 Исследуй 🏛️ Лабиринт! Он полон опасностей и наград.</b>"

    menu_text = f"<b>🎮 ГЛАВНОЕ МЕНЮ</b>\n\n<i>{whisper}</i>\n\n" + back + "\n\n" + hint
    kb, _ = await get_main_menu_keyboard(player.user_id)
    await update.effective_message.reply_text(menu_text, reply_markup=kb, parse_mode='HTML')

# САМА ФУНКЦИЯ START — ТОНКИЙ ОРКЕСТРАТОР
# --------------------------------------------------------------------------- def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    uid = user.id
    username = user.username or user.first_name

    # 1. Получаем игрока (или пустышку)
    player = await PlayerRepository.get_by_id(uid)

    # 2. Обрабатываем реферала (если есть аргумент)
    await _handle_referral(update, context, uid, player)

    # 3. Если игрока нет в БД – создаём новичка и выходим
    if not player or not player.exists:
        await _create_new_player(update, context, uid, username)
        return

    # 4. Ежедневный вход
    await process_daily_login(uid, context)

    # 5. Показываем главное меню
    await _show_main_menu(update, context, player, user)
    
# ---------------------------------------------------------------------------
# Конфигурация (все правила в одном месте)
@dataclass(frozen=True)
class StreakConfig:
    base_rewards: Dict[int, int] = field(default_factory=lambda: {
        1: 10, 2: 15, 3: 20, 4: 25, 5: 30, 6: 35, 7: 50,
        8: 55, 9: 60, 10: 65, 11: 70, 12: 75, 13: 80, 14: 100
    })
    max_streak_display: int = 14
    hot_streak_threshold: int = 3
    hot_streak_multiplier: float = 1.1
    random_bonus_chance: float = 0.2

    random_bonus_weights: Dict[str, float] = field(default_factory=lambda: {
        "extra_oac": 0.4,
        "blunt": 0.3,
        "focus": 0.2,
        "life": 0.1
    })
    extra_oac_range: tuple = (5, 20)

    title_rewards: Dict[int, str] = field(default_factory=lambda: {
        7: "🕊️",
        14: "🔮"
    })
    title_descriptions: Dict[str, str] = field(default_factory=lambda: {
        "🕊️": "🎁 Бонус 7-го дня:\n🎉 Разблокирован Титул: 🕊️ «Семь Шагов» 💎",
        "🔮": "🎁 Бонус 14-го дня:\n🎉 Разблокирован Титул: 🔮 «Хранитель Хрустального Шара» 💎"
    })

    # Маппинг item (из конфига) → поле модели и читаемое имя
    item_to_field: Dict[str, str] = field(default_factory=lambda: {
        "blunt": "blunts",
        "focus": "focus",
        "life": "lives"
    })
    item_display_names: Dict[str, str] = field(default_factory=lambda: {
        "blunts": "+1 блант",
        "focus": "+1 Фокус",
        "lives": "+1 жизнь"
    })


daily_config = StreakConfig()

# ---------------------------------------------------------------------------
# Результат расчёта награды
class RewardResult(NamedTuple):
    total_oac: int
    title: Optional[str]
    inventory_items: Dict[str, int]  # имя поля → количество

# Расчёт награды (чистая функция)
# ---------------------------------------------------------------------------

def _calculate_reward(streak: int, config: StreakConfig) -> RewardResult:
    base = config.base_rewards.get(streak, 100)

    title = config.title_rewards.get(streak)

    if streak >= config.hot_streak_threshold:
        base = int(base * config.hot_streak_multiplier)

    inventory_items: Dict[str, int] = {}

    if random.random() < config.random_bonus_chance:
        bonus_type = random.choices(
            population=list(config.random_bonus_weights.keys()),
            weights=list(config.random_bonus_weights.values()),
            k=1
        )[0]

        if bonus_type == "extra_oac":
            extra = random.randint(*config.extra_oac_range)
            base += extra
        else:
            field_name = config.item_to_field.get(bonus_type)
            if field_name:
                inventory_items[field_name] = 1

    return RewardResult(total_oac=base, title=title, inventory_items=inventory_items)

# Формирование сообщения с улучшенным прогресс-баром (пункт 7)
# ---------------------------------------------------------------------------

def _build_daily_message(streak: int, reward: RewardResult, config: StreakConfig) -> str:
    # Стиль заголовка
    if streak >= 8:
        title = "<b>🔮 ХРУСТАЛЬНЫЙ ШАР ВЕРНОСТИ 🔮</b>"
        filled_char, empty_char = "🔮", "⬛️"
        desc = "Твоя преданность вознаграждена…"
    elif streak >= 3:
        title = "<b>🔮 КРИСТАЛЛ СУДЬБЫ 🔮</b>"
        filled_char, empty_char = "🟪", "⬛️"
        desc = "Твоя верность начинает сиять…"
    else:
        title = "<b>💠 ЕЖЕДНЕВНЫЙ ВХОД 💠</b>"
        filled_char, empty_char = None, None
        desc = "Багрянец отмечает твой путь"

    display = min(streak, config.max_streak_display)

    # Прогресс-бар (пункт 7 – улучшен для первых дней)
    if filled_char:
        # Для streak < 3 заполняем хотя бы один символ, чтобы не было пустого бара
        filled_count = max(1, display)  # минимум 1 блок, если вообще есть заполнение
        empty_count = config.max_streak_display - filled_count
        bar = filled_char * filled_count + empty_char * empty_count
        bar += f"  ({display}/{config.max_streak_display})"
    else:
        # Для первых дней без иконок – показываем процент
        percent = int(display / config.max_streak_display * 100)
        filled_len = max(1, int(display / config.max_streak_display * 10))  # минимум 1 блок
        bar = f"{'▓' * filled_len}{'░' * (10 - filled_len)} {percent}%"

    # Бонусное сообщение о титуле
    title_msg = ""
    if reward.title:
        title_msg = "\n<b>" + config.title_descriptions.get(reward.title, "") + "</b>"

    # Сообщение о предметах (читаемые имена из item_display_names)
    item_msg = ""
    if reward.inventory_items:
        names = []
        for field, qty in reward.inventory_items.items():
            display_name = config.item_display_names.get(field, f"+{qty} {field}")
            names.append(display_name)
        if names:
            item_msg = "\n<b>🎲 Удача дня:</b> " + ", ".join(names) + "!"

    return (
        f"{title}\n\n"
        f"<b>День {streak}.</b> {desc}\n\n"
        f"{bar}\n\n"
        f"<b>+{reward.total_oac} OAC</b>{title_msg}{item_msg}"
    )
# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _parse_last_login_date(last) -> Optional[date]:
    if isinstance(last, str):
        try:
            return datetime.strptime(last, "%Y-%m-%d").date()
        except ValueError:
            return None
    return last


async def _check_achievements(user_id: int, context) -> None:
    # await AchievementService.check_and_award(user_id, context)
    pass

async def grant_title(user_id, emoji, name, context):
    await add_title(user_id, emoji)

#===•===****=====ФАРМ ОАС=====*****=====
# ============================================================
# FARM – атомарный сбор OAC
# ============================================================

def _calculate_farm_reward(player, context) -> tuple[int, bool, bool]:
    """
    Чистая логика расчёта награды за фарм.
    Возвращает (earned, crit, happy).
    Не содержит побочных эффектов.
    """
    now = datetime.now()

    earned = random.randint(FARM_MIN, FARM_MAX)

    # Бонус от числа выкуренных блантов (5%)
    if player.smoke_count > 0:
        earned += int(earned * 0.05)

    # Бонус, если курили в последние 5 минут
    last_smoke = context.user_data.get("last_smoke_time")
    if last_smoke and (now - last_smoke).total_seconds() < 300:
        earned += random.randint(3, 5)

    # Happy hour – удвоение
    happy = context.bot_data.get("happy_hour", False)
    if happy:
        earned *= HAPPY_HOUR_MULTIPLIER

    # Критический удар (x10) с шансом 1%
    crit = random.randint(1, 100) == 1
    if crit:
        earned *= 10

    return earned, crit, happy


def _format_farm_message(earned: int, crit: bool, happy: bool,
                         medal_text: str, new_count: int, target: int,
                         new_balance: int) -> str:
    """Формирует HTML-сообщение с результатами фарма."""
    crit_str = " (крит x10!)" if crit else ""
    happy_str = " 🌟x2" if happy else ""
    progress_bar_str = get_medal_progress(new_count, FARM_MEDALS)
    rank_progress = get_rank_progress(new_balance)

    return (
        f"<b>💎 Ты нафармил: +{earned} oac</b> 🍬{crit_str}{happy_str}\n\n"
        f"<b>⚜️ у тебя:</b> <i>{new_balance} oac 🎉</b>\n\n"
        f"{medal_text}"
        f"<b>🎯 Фарминг: {new_count}/{target}</b>\n"
        f"{progress_bar_str}\n\n"
        f"{rank_progress}"
    )


@error_handler
@rate_limit(3)
async def farm_callback(update, context):
    user, _ = get_user_and_msg(update)
    uid = user.id
    uname = user.username or user.first_name
    now = datetime.now()

    # --- Атомарная бизнес-логика ---
    async def _farm(player, conn):
        if player.last_farm and (now - player.last_farm) < timedelta(hours=FARM_COOLDOWN_HOURS):
            remain = int((timedelta(hours=FARM_COOLDOWN_HOURS) - (now - player.last_farm)).seconds / 60)
            return ("cooldown", remain)

        old_balance = player.balance  # запоминаем старый баланс до изменений

        earned, crit, happy = _calculate_farm_reward(player, context)

        old_count = player.farm_count
        new_count = old_count + 1
        medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, FARM_MEDALS)

        player.balance += earned + medal_bonus
        player.farm_count = new_count
        player.last_farm = now
        player.last_farm_date = date.today()

        # Военный счёт
        war_service = context.bot_data.get("war_service")
        if war_service:
            await war_service.add_score_raw(uid, earned + medal_bonus, conn)

        return ("ok", earned, crit, happy, medal_text, new_count, player.balance, old_balance)

    # --- Выполнение атомарного обновления ---
    result = await PlayerRepository.atomic_update(uid, _farm)
    if result is None:
        await update.effective_message.reply_text("Сначала активируйся: /start")
        return

    status, *data = result
    if status == "cooldown":
        remain = data[0]
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<b>🍬 OAC копятся 🌱</b>\n\n<b>🍃 Подожди {remain} мин</b>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
        )
        if update.callback_query:
            await update.callback_query.answer()
        return

    earned, crit, happy, medal_text, new_count, new_balance, old_balance = data

    if crit:
        uname_escaped = html.escape(uname)
        # await send_whisper(context, "@guild_antysocial", f"🌟 @{uname_escaped} наткнулся на <i>Золотую жилу</i>! +{earned} OAC 🍬")

    target = get_medal_target(new_count, FARM_MEDALS)
    text = _format_farm_message(earned, crit, happy, medal_text, new_count, target, new_balance)

    anim_msg = await animate_progress_bar(update, context, title="🍬 Фармим...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML')

    # --- Пост-проверки ---
    await check_rank_up(context, uid, uname, old_balance, new_balance)
    await check_achievements(uid, context)
    
# ============================================================
# КРАФТ ОБЫЧНЫХ И ИМЕННЫХ БЛАНТОВ – атомарное создание блантов
# ============================================================

# ── Вспомогательная чистая функция ──
def _get_craft_stats(balance: int, blunts: int, craft_count: int) -> dict:
    """Возвращает текущий медальный прогресс и следующую цель крафта."""
    medal_name = CRAFT_MEDALS[0][1]
    target = CRAFT_MEDALS[0][0]
    for threshold, name, _ in CRAFT_MEDALS:
        if craft_count >= threshold:
            medal_name = name
        else:
            target = threshold
            break
    else:
        target = craft_count
    return {"medal_name": medal_name, "target": target}


def _format_craft_menu_text(balance: int, blunts: int, craft_count: int,
                            medal_name: str, target: int, m_essence: int) -> str:
    """HTML‑текст меню крафта."""
    text = (
        f"<b>🌱 КРАФТ БЛАНТА</b>\n\n"
        f"<b>💎 у тебя: {balance} оас 🍬</b>\n\n"
        f"<b>🌿 Блантов в свёртке: {blunts}</b>\n"
        f"<b>🎯 Крафтинг: {craft_count}/{target} | {medal_name}</b>\n\n"
        f"<b>🌿 Блант — 15 OAC 🍬</b>\n"
        f"<b>💍 Именной блант — 50 OAC 🍬</b>\n"
        f"<b>Шансы:</b> <i>🟢 55% | 🔵 30% | 🟣 13% | 🟡 2%</i>"
    )
    if m_essence > 0:
        text += f"\n\n<b>💠 у тебя есть Кристальная Пыль</b> (<i>{m_essence} доза</i>)"
    return text


def _build_craft_keyboard(m_essence: int) -> InlineKeyboardMarkup:
    """Клавиатура крафта."""
    kb_rows = [
        [InlineKeyboardButton("🌿 Обычный блант (15 🍬)", callback_data="craft_normal")],
        [InlineKeyboardButton("💍 Именной блант (50 🍬)", callback_data="craft_named")],
    ]
    if m_essence > 0:
        kb_rows.append([InlineKeyboardButton("💠 Использовать Пыль (1 доза)", callback_data="use_dust")])
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
    return InlineKeyboardMarkup(kb_rows)


def _format_normal_craft_message(medal_text: str, new_count: int, target: int,
                                 blunts: int, new_balance: int) -> str:
    """Сообщение после обычного крафта."""
    progress_bar_str = get_medal_progress(new_count, CRAFT_MEDALS)
    return (
        f"<b>🌿 БЛАНТ СКРУЧЕН!</b>\n\n"
        f"<b>💎 Потрачено:</b> <b>15 OAC 🍬</b>\n"
        f"<b>⚜️ У тебя:</b> <b>{new_balance} OAC 🍬</b>\n\n"
        f"{medal_text}"
        f"<b>🎯 Крафтинг:</b> {new_count}/{target}\n"
        f"{progress_bar_str}\n\n"
        f"<b>🍃 Блантов в свёртке:</b> <b>{blunts}</b>"
    )


def _format_dust_message(name: str, reaction: str) -> str:
    """Сообщение после использования Кристальной Пыли."""
    return (
        f"<b><i>💠 ПЫЛЬ ИСПОЛЬЗОВАНА</i></b>\n\n"
        f"🟡 <b><i>«{name}»</i></b> (Легендарный) 🌿\n"
        f"📜 Реакция: <i>{reaction}</i>"
    )


# ── Обработчики ──
@error_handler
@rate_limit(2)
async def craft_callback(update, context):
    user, _ = get_user_and_msg(update)
    uid = user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player or not player.user_id:
        await update.effective_message.reply_text("Сначала активируйся: /start")
        return

    stats = _get_craft_stats(player.balance, player.blunts, player.craft_count)
    text = _format_craft_menu_text(player.balance, player.blunts, player.craft_count,
                                   stats["medal_name"], stats["target"], player.m_essence)
    kb = _build_craft_keyboard(player.m_essence)
    await edit_or_reply(update, context, text, reply_markup=kb)


@error_handler
async def handle_craft_normal(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    async def _craft(player, conn):
        if player.balance < GAME_CONFIG["craft_cost"]:
            return ("no_money",)

        old_count = player.craft_count
        new_count = old_count + 1
        medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, CRAFT_MEDALS)

        player.balance -= GAME_CONFIG["craft_cost"]
        player.blunts += 1
        player.craft_count = new_count

        if random.random() < 0.05:
            player.blunts += 1

        player.balance += medal_bonus

        war_service = context.bot_data.get("war_service")
        if war_service:
            await war_service.add_score(uid, WarAction.CRAFT, conn)

        return ("ok", medal_text, new_count, player.blunts, player.balance)

    result = await PlayerRepository.atomic_update(uid, _craft)
    if result is None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Сначала активируйся: /start")
        return

    status, *data = result
    if status == "no_money":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<b>❌ Недостаточно OAC.</b>\n🕯️ Требуется <b>{GAME_CONFIG['craft_cost']} OAC</b> 🍬.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]]),
            parse_mode='HTML'
        )
        return

    medal_text, new_count, blunts, new_balance = data
    target = get_medal_target(new_count, CRAFT_MEDALS)
    text = _format_normal_craft_message(medal_text, new_count, target, blunts, new_balance)

    # Кнопки «Скрафтить ещё» и «Назад»
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌿 Скрафтить ещё", callback_data="craft_normal")],
        [InlineKeyboardButton("🔙 Назад", callback_data="craft")]
    ])

    anim_msg = await animate_progress_bar(update, context, title="🌿 Скручиваем Блант...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb, parse_mode='HTML')

    await check_achievements(uid, context)


@error_handler
async def handle_craft_named(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player or player.balance < GAME_CONFIG["named_blunt_cost"]:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<b>🔮 ИСКАЖЕНИЕ МОЛЧИТ</b>\n\n<i>🛡️ Недостаточно OAC.</i>\n🕯️ Требуется <b>{GAME_CONFIG['named_blunt_cost']} OAC</b> 🍬.",
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
    try:
        user = update.effective_user
        uid = user.id
        name = update.message.text.strip()[:25]
        if not name:
            await update.message.reply_text("❌ Имя не может быть пустым.")
            return

        async def _named(player, conn):
            if player.balance < GAME_CONFIG["named_blunt_cost"]:
                return ("no_money",)
            player.balance -= 50
            player.craft_count = (player.craft_count or 0) + 1
            item = await create_named_blunt(uid, name, rarity=None, conn=conn)

            # Очки войны внутри атомарной транзакции
            war_service = context.bot_data.get("war_service")
            if war_service:
                await war_service.add_score(uid, WarAction.NAMED_CRAFT, conn)
            else:
                logger.warning("GuildWarService not found")

            return ("ok", item)

        result = await PlayerRepository.atomic_update(uid, _named)
        if result is None:
            await update.message.reply_text("Сначала активируйся: /start")
            return
        status, data = result[0], result[1] if len(result) > 1 else None
        if status == "no_money":
            await update.message.reply_text(f"<b>🔮 ИСКАЖЕНИЕ МОЛЧИТ</b>\n\n<i>🛡️ Недостаточно OAC.</i>\n🕯️ Требуется <b>{GAME_CONFIG['named_blunt_cost']} OAC 🍬</b>.")
            return

        item = data
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

        # ── Оповещение в канал (закомментировано) ──
        # try:
        #     uname = html.escape(user.username or user.first_name)
        #     await context.bot.send_message(
        #         chat_id="@guild_antysocial",
        #         text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{uname}</b> создал свой именной Блант {color} "
        #              f"<b><i>«{name_escaped}»</i></b> 🌿\n<i>Редкость: {item['rarity']}</i>\n🩸 <i>{reaction}</i>",
        #         parse_mode='HTML'
        #     )
        # except Exception as e:
        #     logger.error(f"Ошибка отправки в канал: {e}")

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


@error_handler
async def handle_use_dust(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    player = await PlayerRepository.get_by_id(uid)
    if not player or (player.m_essence or 0) < 1:
        await query.answer("Нет Кристальной Пыли.", show_alert=True)
        return

    async def _use_dust(p, conn):
        if (p.m_essence or 0) < 1:
            return ("no_dust",)

        p.m_essence -= 1
        name = random.choice([
            "Крик Бездны", "Пепел Короля", "Шёпот Склепа",
            "Коготь Хаоса", "Вздох Пожирателя"
        ])
        item = await create_named_blunt(uid, name, rarity="legendary", conn=conn)

        war_service = context.bot_data.get("war_service")
        if war_service:
            await war_service.add_score(uid, WarAction.DUST_USE, conn)
        else:
            logger.warning("GuildWarService not found")

        return ("ok", item, name)

    result = await PlayerRepository.atomic_update(uid, _use_dust)
    if result is None:
        await query.answer("Профиль не найден.", show_alert=True)
        return

    status, *data = result
    if status == "no_dust":
        await query.answer("Нет Кристальной Пыли.", show_alert=True)
        return

    item, name = data
    reaction = item["reaction"]

    await send_blunt_image(context, query.message.chat.id, "legendary")
    text = _format_dust_message(name, reaction)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

    # ── Оповещение в канал (закомментировано) ──
    # try:
    #     await context.bot.send_message(chat_id="@guild_antysocial",
    #         text=f"<b><i>⚜️ ЭХО ИСКАЖЕНИЯ 🩸</i></b>\n\n🎉 <b>@{html.escape(player.username)}</b> использовал 💠 Пыль и получил легендарный Блант <b><i>«{name}»💍</i></b>!",
    #         parse_mode='HTML')
    # except Exception as e:
    #     logger.error(f"Ошибка отправки в канал: {e}")

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
    
# ====== ФУНКЦИЯ ПЕРЕДАЧИ БЛАНТА (АТОМАРНАЯ, БЕЗОПАСНАЯ) =====
class TransferError(Exception):
    pass
class BluntNotFound(TransferError):
    pass
class SameUserError(TransferError):
    pass

async def transfer_blunt(sender_id: int, receiver_id: int, blunt_id: str) -> None:
    if sender_id == receiver_id:
        raise SameUserError("Нельзя передать блант самому себе")
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                sender_row = await conn.fetchrow("SELECT * FROM players WHERE user_id = $1 FOR UPDATE", sender_id)
                receiver_row = await conn.fetchrow("SELECT * FROM players WHERE user_id = $1 FOR UPDATE", receiver_id)
                if not sender_row or not receiver_row:
                    raise TransferError("Игрок не найден")
                sender = Player(**dict(sender_row))
                receiver = Player(**dict(receiver_row))
                sender.inventory = _json_safe_load(sender.inventory, [])
                receiver.inventory = _json_safe_load(receiver.inventory, [])
                receiver.profile_skins = _json_safe_load(receiver.profile_skins, {})
                sender.inventory = _json_safe_load(sender.inventory, [])
                receiver.inventory = _json_safe_load(receiver.inventory, [])
                if not isinstance(sender.inventory, list):
                    raise TransferError("Инвентарь отправителя повреждён")
                if not isinstance(receiver.inventory, list):
                    receiver.inventory = []
                item = None
                for it in sender.inventory:
                    if it.get("id") == blunt_id and it.get("type") == "named":
                        item = it
                        break
                if not item:
                    raise BluntNotFound("Блант не найден или не является именным")
                initial_len = len(sender.inventory)
                sender.inventory.remove(item)
                if len(sender.inventory) == initial_len:
                    raise TransferError("Не удалось удалить предмет")
                if any(it.get("id") == blunt_id for it in sender.inventory):
                    raise TransferError("Обнаружен дубликат бланта")
                if "owner_history" not in item:
                    item["owner_history"] = []
                item["owner_history"].append({
                    "user_id": str(receiver_id),
                    "since": datetime.utcnow().isoformat()
                })
                receiver.inventory.append(item)
                await PlayerRepository.save(sender, conn=conn)
                await PlayerRepository.save(receiver, conn=conn)
                logger.info(f"Blunt {blunt_id} передан от {sender_id} к {receiver_id}")
    except TransferError:
        raise
    except Exception as e:
        logger.exception("Неожиданная ошибка при передаче бланта")
        raise TransferError("Внутренняя ошибка передачи") from e

# ===== НОВЫЕ ФУНКЦИИ ДЛЯ ОБМЕНА БЛАНТАМИ =====
async def gift_blunt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    query = update.callback_query
    await query.answer()
    context.user_data.pop("gifting_blunt_id", None)
    await profile_callback(update, context)

async def handle_gift_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "gifting_blunt_id" not in context.user_data:
        return
    text = update.message.text.strip()
    receiver_id = None
    if text.isdigit():
        receiver_id = int(text)
    elif text.startswith("@"):
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id FROM players WHERE LOWER(username) = LOWER($1)", text.lstrip("@")
            )
            if row:
                receiver_id = row["user_id"]
    if not receiver_id:
        await update.message.reply_text("❌ Игрок не найден.")
        return
    if receiver_id == update.effective_user.id:
        await update.message.reply_text("❌ Нельзя подарить блант самому себе.")
        return
    blunt_id = context.user_data.pop("gifting_blunt_id")
    try:
        await transfer_blunt(update.effective_user.id, receiver_id, blunt_id)
        await update.message.reply_text("✅ Блант успешно подарен! 🎁")
        try:
            await context.bot.send_message(chat_id=receiver_id, text="🎁 Вам подарили именной блант! Проверьте инвентарь.")
        except Exception:
            pass
    except ValueError as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    except Exception as e:
        logger.error(f"Gift error: {e}")
        await update.message.reply_text("⚠️ Внутренняя ошибка. Попробуй позже.")

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
    uid = user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player or not player.user_id:
        await msg.reply_text("Сначала активируйся: /start")
        return

    if player.blunts < 1:
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

    main_text = f"<b>💨 ДУНУТЬ</b>\n\n🌿 <i>блантов в свёртке:</i> <b>{player.blunts}</b>"
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
    uid = query.from_user.id
    uname = html.escape(query.from_user.username or query.from_user.first_name)

    async def _smoke(player, conn):
        if (player.blunts or 0) < 1:
            return ("no_blunts",)
        save = (player.guild == "WHITE" and random.randint(1, 100) <= 20)
        r = random.random()
        earned = 0
        if r < 0.18:
            earned = random.randint(15, 40)
            if context.bot_data.get("happy_hour"):
                earned *= HAPPY_HOUR_MULTIPLIER
        elif r < 0.70:
            earned = -5

        old_count = player.smoke_count or 0
        new_count = old_count + 1
        medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, SMOKE_MEDALS)

        if not save:
            player.blunts -= 1
        player.smoke_count = new_count
        player.balance = (player.balance or 0) + earned + medal_bonus
        if not player.inhaled:
            player.inhaled = 1

        # военный счёт (новый сервис)
        war_service = context.bot_data.get("war_service")
        if war_service:
            await war_service.add_score_raw(uid, earned + medal_bonus, conn)

        return ("ok", earned, r, save, medal_text, new_count, player.blunts, player.balance)

    result = await PlayerRepository.atomic_update(uid, _smoke)
    if result is None:
        await query.answer("Профиль не найден.")
        return

    status, *data = result
    if status == "no_blunts":
        empty_text = (
            "<b>💨 ДУНУТЬ</b>\n\n"
            "<b>🌿 Твой свёрток пуст</b>\n\n"
            "<i>🎈 Скрути новый блант</i>"
        )
        empty_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌿 Крафт", callback_data="craft")],
            [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
        ])
        await query.message.edit_text(empty_text, reply_markup=empty_kb, parse_mode='HTML')
        return

    earned, r, save, medal_text, new_count, bl_left, new_balance = data

    # Эффекты (без изменений, твоя оригинальная логика)
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

    target = get_medal_target(new_count, SMOKE_MEDALS)
    progress_bar_str = get_medal_progress(new_count, SMOKE_MEDALS)

    text = (
        f"{effect}\n\n"
        f"{medal_text}"
        f"<b>💨 Дым:</b> {new_count}/{target}\n"
        f"{progress_bar_str}\n\n"
        f"<b>🍃 Блантов в свёртке:</b> <b>{bl_left}</b>"
    )
    if save:
        text += "\n⚜️ <i>Светлая Гильдия сохранила твой Блант!</i>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Дунуть ещё", callback_data="do_smoke") if bl_left >= 1 else InlineKeyboardButton("🌿 Крафтить ещё", callback_data="craft")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    await check_achievements(uid, context)

# Ритуал (с защитой от None)
@error_handler
@rate_limit(3)
async def ritual_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    uname = html.escape(user.username or user.first_name)
    now = datetime.now()

    async def _ritual(player, conn):
        if player.guild != "BLACK":
            return ("wrong_guild",)
        if player.last_ritual and (now - player.last_ritual) < timedelta(hours=GAME_CONFIG["ritual_cooldown_hours"]):
            remain = int((timedelta(hours=GAME_CONFIG["ritual_cooldown_hours"]) - (now - player.last_ritual)).seconds / 3600)
            return ("cooldown", remain)

        reward = 150
        if context.bot_data.get("happy_hour"):
            reward *= HAPPY_HOUR_MULTIPLIER
        extra = 15 if random.random() < 0.1 else 0

        old_count = player.ritual_count
        new_count = old_count + 1
        medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, RITUAL_MEDALS)

        player.balance += reward + extra + medal_bonus
        player.ritual_count = new_count
        player.last_ritual = now

        # Военный счёт (новый сервис)
        war_service = context.bot_data.get("war_service")
        if war_service:
            await war_service.add_score_raw(uid, reward + extra + medal_bonus, conn)

        return ("ok", reward, extra, medal_text, new_count, player.balance)

    result = await PlayerRepository.atomic_update(uid, _ritual)
    if result is None:
        await msg.reply_text("Профиль не найден.")
        return
    status, *data = result
    if status == "wrong_guild":
        await send_whisper_dm(update, context, "❌ Только Тёмная Гильдия.")
        return
    if status == "cooldown":
        remain = data[0]
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<b>🕯️ Тёмный алтарь истощён 🌙</b>\n\n<b>🗝️ Жди {remain} ч</b>",
            parse_mode='HTML'
        )
        return

    reward, extra, medal_text, new_count, new_balance = data
    target = get_medal_target(new_count, RITUAL_MEDALS)
    progress_bar_str = get_medal_progress(new_count, RITUAL_MEDALS)

    text = (
        f"<b>🕯️ РИТУАЛ ЗАВЕРШЁН 🎉</b>\n\n"
        f"Ритуал принёс тебе <b>{reward} OAC</b> 🍬\n"
        f"<b>⚜️ У тебя:</b> <b>{new_balance} OAC 🪽</b>\n\n"
        f"{medal_text}"
        f"<b>🕯️ Ритуалы:</b> {new_count}/{target}\n"
        f"<b>{progress_bar_str}</b>"
    )
    anim_msg = await animate_progress_bar(update, context, title="🕯️ Ритуал проводится...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML')

    await check_achievements(uid, context)

# КУСТИК (с защитой от None)
@error_handler
async def collect_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    uname = html.escape(user.username or user.first_name)
    now = datetime.now()

    async def _collect(player, conn):
        if not has_rank(player.balance, "Ветеран"):
            return ("low_rank",)
        lvl = 3 if player.balance >= 20000 else 2
        if not player.passive_collected:
            player.passive_collected = now
            return ("activated",)
        last_collect = _to_datetime(player.passive_collected)
        hrs = (now - last_collect).total_seconds() / 3600 if last_collect else 0
        earned = int(hrs * 30 * lvl)
        if context.bot_data.get("happy_hour"):
            earned *= HAPPY_HOUR_MULTIPLIER
        if earned < 1:
            return ("not_ready",)
        player.balance += earned
        player.passive_collected = now

        war_service = context.bot_data["war_service"]
        await war_service.add_score_raw(uid, earned, conn)

        return ("ok", earned, player.balance)

    result = await PlayerRepository.atomic_update(uid, _collect)
    if result is None:
        await send_whisper_dm(update, context, "Профиль не найден.")
        return
    status, *data = result
    if status == "low_rank":
        await send_whisper_dm(update, context, "❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)")
        return
    if status == "activated":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="<b>🪴 Авто‑сборщик активирован 💎</b>\n\n<b>🌱 Загляни позже</b>",
            parse_mode='HTML'
        )
        return
    if status == "not_ready":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="<b>🪴 Кустик ещё не созрел 💎</b>\n\n<b>🌱 Загляни позже</b>",
            parse_mode='HTML'
        )
        return

    earned, new_bal = data
    await send_whisper_dm(update, context,
        f"<b><i>🪴 УРОЖАЙ СОБРАН</i></b>\n\nТвой куст принёс <b>{earned} OAC</b> 🍬.\n\n💎 <i>У тебя:</i> <b>{new_bal} OAC</b> 🍬")

# Профиль – премиум-карточка, сеньорская версия (аватарка + текст + кнопки)
@error_handler
@rate_limit(2)
async def profile_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    uname = html.escape(user.username or user.first_name)

    player = await PlayerRepository.get_by_id(uid)
    if not player or not player.user_id:
        await msg.reply_text("Сначала активируйся: /start")
        return

    # Теперь все поля берутся напрямую из модели (никаких .get)
    bal = player.balance or 0
    bl = player.blunts or 0
    guild = player.guild or ""

    # Ранг
    rank_emoji, rank_name = "🪓", "Рекрут"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold:
            rank_emoji = emoji
            rank_name = emoji_to_name(emoji)

    # Гильдия
    g_emoji = ""
    if guild == "BLACK":
        g_emoji = " 🕯️ Тёмная Гильдия"
    elif guild == "WHITE":
        g_emoji = " ⚜️ Светлая Гильдия"

    neuro = random.choice(NEURO_STATUSES)
    skins = player.profile_skins or {}
    bg = skins.get("active_background", "")
    active_title = skins.get("active_title", "—")

    inv_data = player.inventory or []
    badges = []
    if any(it.get("rarity") == "legendary" for it in inv_data):
        badges.append("🟡")
    if player.referral_count > 0:
        badges.append("🩸")
    if player.login_streak >= 7:
        badges.append("🔥")
    if player.check_count >= 10:
        badges.append("👁️")
    badge_str = ' '.join(badges) if badges else "—"

    rank_progress = get_rank_progress(bal)

    # --- Питомец (добавлено) ---
    pet_line = ""
    if player.pet:
        pet_line = f"🐾 <b>Питомец:</b> {player.pet}"
        if player.pet_name:
            pet_line += f" «{player.pet_name}»"
        pet_line += "\n"
    # --------------------------

    text = (
        f"<b>⚜️ ПРОФИЛЬ</b>\n"
        f"👤 <b>{uname}</b>{g_emoji}\n"
        f"🫧 Фон: {bg}\n\n"
        f"{rank_progress}\n\n"
        f"💎 <b>ОАС:</b> <b>{bal} OAC</b> 🍬\n"
        f"🌿 <b>Блантов в свёртке:</b> <b>{bl}</b>\n"
        f"🪴 <b>Куст:</b> <b>+{30 * (3 if bal >= 20000 else 2 if bal >= 5000 else 0)} OAC/ч</b>\n"
        f"🧬 <b>Титул:</b> {active_title}\n"
        f"🧠 <b>Нейро-статус:</b> <i>{neuro}</i>\n"
        f"{pet_line}"
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

    # Аватар
    photo_id = None
    try:
        photos = await context.bot.get_user_profile_photos(uid, limit=1)
        if photos.photos:
            photo_id = photos.photos[0][0].file_id
    except:
        pass

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

async def handle_set_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    new_title = query.data.replace("set_title_", "")

    async def _set(p, conn):
        skins = p.profile_skins or {}
        if not isinstance(skins, dict):
            skins = {}
        skins["active_title"] = new_title
        p.profile_skins = skins
        # Дополнительно можно добавить титул в titles (если надо)
        titles = (p.titles or "").split()
        if new_title not in titles:
            titles.append(new_title)
            p.titles = " ".join(titles).strip()
        return new_title  # чтобы вывести сообщение

    result = await PlayerRepository.atomic_update(user_id, _set)
    if result is None:
        await query.answer("Профиль не найден", show_alert=True)
        return

    # Подтверждение игроку
    await context.bot.send_message(
        chat_id=query.message.chat.id,
        text=f"✨ Титул «{new_title}» активирован!"
    )
    # Возвращаемся в меню кастомизации (можно вызвать функцию skins_menu)
    await skins_menu_handler(update, context)

async def handle_set_bg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    new_bg = query.data.replace("set_bg_", "")

    async def _set(p, conn):
        skins = p.profile_skins or {}
        if not isinstance(skins, dict):
            skins = {}
        skins["active_background"] = new_bg
        p.profile_skins = skins
        return new_bg

    result = await PlayerRepository.atomic_update(user_id, _set)
    if result is None:
        await query.answer("Профиль не найден", show_alert=True)
        return

    await context.bot.send_message(
        chat_id=query.message.chat.id,
        text=f"✨ Фон «{new_bg}» активирован!"
    )
    await skins_menu_handler(update, context)

async def skins_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Выбрать титул", callback_data="choose_title")],
        [InlineKeyboardButton("🖼️ Выбрать фон", callback_data="choose_bg")],
        [InlineKeyboardButton("🔙 Назад", callback_data="profile")]
    ])
    await query.message.edit_text(
        "<b>🎨 СКИНЫ</b>\n\nВыбери, что хочешь изменить.",
        reply_markup=kb,
        parse_mode='HTML'
    )

# Все бланты
@error_handler
@rate_limit(1)
async def my_blunts_callback(update, context, page=0):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player:
        return

    inv_data = player.inventory or []
    named = [it for it in inv_data if it.get("type") == "named"]

    rarity_order = {"legendary": 0, "epic": 1, "rare": 2, "common": 3}
    named.sort(key=lambda x: (rarity_order.get(x.get("rarity") or "common", 3),
                               x.get("serial") or 999999))

    if not named:
        await edit_or_reply(update, context, "💎 У тебя пока нет именных блантов.",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔙 В профиль", callback_data="profile")]
                            ]))
        return

    total_pages = (len(named) + BLUNTS_PER_PAGE - 1) // BLUNTS_PER_PAGE
    start = page * BLUNTS_PER_PAGE
    end = start + BLUNTS_PER_PAGE
    page_blunts = named[start:end]

    text = f"<b>💎 ТВОИ ИМЕННЫЕ БЛАНТЫ ({page+1}/{total_pages})</b>\n\n"
    for i, item in enumerate(page_blunts, 1):
        name = item["name"]
        rarity = item.get("rarity", "common")
        color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
        rare_number = item.get("rare_number", "?-????")
        hash_code = item.get("hash", "0x????...????")
        text += f"<b>{i}) «{html.escape(name)}»</b> {color} · #{rare_number} · {hash_code}\n"

    kb_rows = []
    for i, item in enumerate(page_blunts, 1):
        row = [
            InlineKeyboardButton(f"💍 Детали ({i})", callback_data=f"blunt_details_{item['id']}"),
            InlineKeyboardButton("🔗", callback_data=f"share_blunt_{item['id']}")
        ]
        kb_rows.append(row)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"blunts_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("▶️ Далее", callback_data=f"blunts_page_{page+1}"))
    if nav_buttons:
        kb_rows.append(nav_buttons)
    kb_rows.append([InlineKeyboardButton("🔙 В профиль", callback_data="profile")])

    await edit_or_reply(update, context, text, reply_markup=InlineKeyboardMarkup(kb_rows))

async def achievements_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    query = update.callback_query
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player or not player.user_id:
        await query.answer("Профиль не найден.", show_alert=True)
        return

    # Получаем список выданных достижений (можно вынести в PlayerRepository, но для простоты оставим так)
    async with db_pool.acquire() as conn:
        awarded = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id = $1", uid)
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
    await edit_or_reply(update, context, text, reply_markup=InlineKeyboardMarkup(kb_rows))


@error_handler
async def top_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    top = await get_top(10)
    if not top:
        await edit_or_reply(update, context, "🏆 Топ-10 пока пуст.")
        return

    first_balance = top[0]["balance"]
    player = await PlayerRepository.get_by_id(uid)
    my_balance = player.balance if player else 0

    text = "<b>💎 ТОП-10 ИГРОКОВ 🏆</b>\n\n"
    my_position = None

    for i, row in enumerate(top, 1):
        bal = row["balance"]
        percent = int(bal / first_balance * 100) if first_balance else 100
        filled = percent // 10
        bar = "▓" * filled + "░" * (10 - filled)

        # Префикс с эмодзи и номером
        if i == 1: prefix = "🥇 1. "
        elif i == 2: prefix = "🥈 2. "
        elif i == 3: prefix = "🥉 3. "
        elif i == 4: prefix = "⚜️ 4. "
        elif i == 5: prefix = "🌿 5. "
        elif i == 6: prefix = "🫧 6. "
        elif i == 7: prefix = "🪄 7. "
        elif i == 8: prefix = "🎈 8. "
        elif i == 9: prefix = "🍀 9. "
        else: prefix = "🌱 10. "

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
    elif my_position is not None:  # 4-10 места
        third_balance = top[2]["balance"] if len(top) >= 3 else 0
        gap = third_balance - my_balance
        if gap > 0:
            text += (
                f"✦ 📊 Твоя позиция: {my_position} — "
                f"осталось 🎯 {gap} оас 🍬 до ТРОЙКИ ЛИДЕРОВ 💎🏆 ✦\n"
            )
        else:
            text += f"✦ 📊 Твоя позиция: {my_position} ✦\n"
    else:  # вне топа
        async with db_pool.acquire() as conn:
            cnt_row = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM players WHERE balance > $1", my_balance
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

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Разведка", callback_data="top_scout")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode="HTML")

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
        g = "🕯️" if guild == "BLACK" else "⚜️" if guild == "WHITE" else ""
        text += f"{'🥇' if i==0 else '🥈' if i==1 else '🥉'} <b>{name}</b> {g}\n💰 {bal} OAC\n\n"
    await send_whisper_dm(update, context, text)

# Гильдии
@error_handler
async def guild_info_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player:
        await edit_or_reply(update, context, "Профиль не найден. Напиши /start")
        return

    guild = player.guild

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
        if guild == "BLACK":
            if player.last_ritual:
                last_ritual = _to_datetime(player.last_ritual)
                if last_ritual and datetime.now() - last_ritual < timedelta(hours=24):
                    diff = timedelta(hours=24) - (datetime.now() - last_ritual)
                    hrs = int(diff.seconds // 3600)
                    mins = int((diff.seconds % 3600) // 60)
                    kb_rows.append([InlineKeyboardButton(f"🕯️ Ритуал ({hrs} ч {mins} мин)", callback_data="ritual")])
                else:
                    kb_rows.append([InlineKeyboardButton("🕯️ Ритуал", callback_data="ritual")])
            else:
                kb_rows.append([InlineKeyboardButton("🕯️ Ритуал", callback_data="ritual")])
        elif guild == "WHITE":
            kb_rows.append([InlineKeyboardButton("⚜️ Исповедь", callback_data="confess")])
        kb_rows.append([
            InlineKeyboardButton("🏛️ Храм", callback_data="guild_shrine"),
            InlineKeyboardButton("⚔️ Война", callback_data="guild_war")
        ])
    else:
        text += "<i>🔮 Ты пока не в Гильдии. Выбери Светлую или Темную Гильдию!</i>\n"
        kb_rows.append([InlineKeyboardButton("🕯️ Вступить в Тёмную", callback_data="guild_join_BLACK"),
                        InlineKeyboardButton("⚜️ Вступить в Светлую", callback_data="guild_join_WHITE")])
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)

    await edit_or_reply(update, context, text, reply_markup=kb)

@error_handler
async def guild_shrine_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player or not player.guild:
        await query.answer("Ты не в гильдии.")
        return

    guild = player.guild
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

    async with db_pool.acquire() as conn:
        # Загружаем очки гильдий и героев одним запросом
        scores = await conn.fetch("SELECT guild, total_score FROM guild_weekly")
        black_score = next((r["total_score"] for r in scores if r["guild"] == "BLACK"), 0)
        white_score = next((r["total_score"] for r in scores if r["guild"] == "WHITE"), 0)

        # Если очков нет — война неактивна
        if black_score == 0 and white_score == 0:
            await edit_or_reply(update, context, "🕊️ Сейчас мирное время.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]]))
            return

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
    await edit_or_reply(update, context, text, reply_markup=kb)

@error_handler
async def confess_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id

    # Вся логика с проверками и изменениями внутри атомарной транзакции
    async def _confess(player, conn):
        # Проверки
        if not player or not player.user_id:
            return ("no_player",)
        if player.guild != "WHITE":
            return ("wrong_guild",)
        if (player.blunts or 0) < 1:
            return ("no_blunts",)

        # Списание бланта
        player.blunts -= 1

        # Случайный результат
        r = random.random()
        if r < 0.70:
            reward = random.randint(100, 200)
            player.balance = (player.balance or 0) + reward
            return ("ok", f"<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\nБлагословение! +{reward} OAC.")
        elif r < 0.95:
            player.m_essence = (player.m_essence or 0) + 1
            return ("ok", "<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\nТы получил 💠 Кристальную Пыль.")
        else:
            # Легендарный блант – создаём через create_named_blunt внутри транзакции
            name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
            await create_named_blunt(uid, name, rarity="legendary", conn=conn)
            return ("ok", f"<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\n🌟 Чудо! Легендарный блант «{name}»!")

    result = await PlayerRepository.atomic_update(uid, _confess)

    # Обработка результата
    if result is None:
        await context.bot.send_message(chat_id=uid, text="Сначала активируйся: /start")
        return

    status, data = result[0], result[1] if len(result) > 1 else ""
    if status == "no_player":
        await context.bot.send_message(chat_id=uid, text="Сначала активируйся: /start")
        return
    if status == "wrong_guild":
        if update.callback_query:
            await update.callback_query.answer("Только для Светлой Гильдии.", show_alert=True)
        else:
            await msg.reply_text("❌ Только для Светлой Гильдии.")
        return
    if status == "no_blunts":
        if update.callback_query:
            await update.callback_query.answer("Нужен 1 блант.", show_alert=True)
        else:
            await msg.reply_text("❌ Нужен 1 блант.")
        return

    # Успех
    if update.callback_query:
        await update.callback_query.message.edit_text(
            data,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]]),
            parse_mode='HTML'
        )
    else:
        await msg.reply_text(data, parse_mode='HTML')

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
        await edit_or_reply(update, context, text,
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
    player = await PlayerRepository.get_by_id(uid)
    if not player or not player.user_id:
        await msg.reply_text("Сначала активируйся: /start")
        return
    bal = player.balance
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
# ---------------------------------------------------------------------------
# Конфиг удачи (все числа в одном месте)
# ---------------------------------------------------------------------------
LUCK_CONFIG = {
    "wheel": {
        "rewards": [
            (0.40, 30, "oac"),
            (0.65, 75, "oac"),
            (0.80, 1, "blunt"),
            (0.90, 150, "oac"),
            (0.97, 2, "blunt"),
            (1.0, 1000, "jackpot"),
        ],
        "cooldown_hours": 24,
    },
    "berserk": {
        "cost": 300,
        "win_amount": 200,
        "lose_amount": 300,
        "cooldown_hours": 24,
    },
    "alchemy": {
        "cost_blunts": 10,
        "cost_oac": 250,
        "required_balance": 5000,  # ветеран
        "reactions": [
            (0.40, "dust", 1),
            (0.75, "none", 0),
            (0.90, "dust", 2),
            (1.0, "legendary", 1),
        ],
    },
    "war_points": {
        "wheel_oac": 0,        # не начисляем за колесо (или настрой)
        "berserk_win": 200,
        "berserk_lose": -300,
        "alchemy": 30,
    }
}


# ---------------------------------------------------------------------------
# Хелпер для ответа пользователю (защита от AttributeError)
# ---------------------------------------------------------------------------
async def _notify_user(update, context, text, show_alert=False, reply_markup=None):
    """Безопасно отправляет ответ: через callback или новым сообщением."""
    if update.callback_query:
        if show_alert:
            await update.callback_query.answer(text, show_alert=True)
        else:
            await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode='HTML')


# ============================================================
# УДАЧА – полная сеньорская версия
# ============================================================

LUCK_CONFIG = {
    "wheel": {
        "rewards": [
            (0.40, 30, "oac"),
            (0.65, 75, "oac"),
            (0.80, 1, "blunt"),
            (0.90, 150, "oac"),
            (0.97, 2, "blunt"),
            (1.0, 1000, "jackpot"),
        ],
        "cooldown_hours": 24,
    },
    "berserk": {
        "cost": 300,
        "win_amount": 200,
        "lose_amount": 300,
        "cooldown_hours": 24,
    },
    "alchemy": {
        "cost_blunts": 10,
        "cost_oac": 250,
        "required_balance": 5000,
        "reactions": [
            (0.40, "dust", 1),
            (0.75, "none", 0),
            (0.90, "dust", 2),
            (1.0, "legendary", 1),
        ],
        "legendary_names": [
            "Крик Бездны", "Пепел Короля", "Шёпот Склепа",
            "Коготь Хаоса", "Вздох Пожирателя"
        ],
    },
}


# ── Хелперы ─────────────────────────────────────────────────
async def _notify_user(update, context, text, show_alert=False, reply_markup=None):
    if update.callback_query:
        if show_alert:
            await update.callback_query.answer(text, show_alert=True)
        else:
            await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode='HTML')


def _check_wheel_availability(player, now, cooldown_hours):
    last = player.last_daily
    if not last:
        return True
    last_dt = _to_datetime(last)
    return not last_dt or (now - last_dt) >= timedelta(hours=cooldown_hours)


def _check_berserk_availability(player, now, cost, cooldown_hours):
    if player.balance < cost:
        return False
    last = player.last_berserk
    if not last:
        return True
    last_dt = _to_datetime(last)
    return not last_dt or (now - last_dt) >= timedelta(hours=cooldown_hours)


def _build_luck_keyboard(now, player, cfg, wheel_ok, berserk_ok, alchemy_ok):
    rows = []
    if wheel_ok:
        rows.append([InlineKeyboardButton("🎡 Крутить", callback_data="luck_wheel")])
    else:
        last_dt = _to_datetime(player.last_daily)
        diff = timedelta(hours=cfg["wheel"]["cooldown_hours"]) - (now - last_dt)
        hrs, mins = _format_remaining(diff)
        rows.append([InlineKeyboardButton(f"🎡 Колесо набирает силу. Ещё {hrs} ч {mins} мин", callback_data="luck_wheel")])

    if berserk_ok:
        rows.append([InlineKeyboardButton("🍀 Рискнуть", callback_data="luck_berserk")])
    else:
        if player.balance < cfg["berserk"]["cost"]:
            need = cfg["berserk"]["cost"] - player.balance
            rows.append([InlineKeyboardButton(f"🍀 нужно ещё {need} 🍬", callback_data="luck_berserk")])
        else:
            last_dt = _to_datetime(player.last_berserk)
            diff = timedelta(hours=cfg["berserk"]["cooldown_hours"]) - (now - last_dt)
            hrs, mins = _format_remaining(diff)
            rows.append([InlineKeyboardButton(f"🍀 Бездна шепчет всё громче. Жди {hrs} ч {mins} мин", callback_data="luck_berserk")])

    rows.append([InlineKeyboardButton("🔮 Алхимия", callback_data="alchemy_start") if alchemy_ok else InlineKeyboardButton("🔮 Алхимия 🔒", callback_data="alchemy_start")])
    rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
    return rows


def _format_remaining(td):
    total_seconds = int(td.total_seconds())
    hrs = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    return hrs, mins


# ── Основной обработчик ─────────────────────────────────────
@error_handler
@rate_limit(2)
async def luck_callback(update, context, action=None):
    user, msg = get_user_and_msg(update)
    uid = user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player or not player.user_id:
        await _notify_user(update, context, "Сначала активируйся: /start")
        return

    now = datetime.now()
    cfg = LUCK_CONFIG

    wheel_ok = _check_wheel_availability(player, now, cfg["wheel"]["cooldown_hours"])
    berserk_ok = _check_berserk_availability(player, now, cfg["berserk"]["cost"], cfg["berserk"]["cooldown_hours"])
    alchemy_ok = player.balance >= cfg["alchemy"]["required_balance"]

    # Получаем сервис войны один раз
    war_service = context.bot_data.get("war_service")

    if action == "luck_wheel":
        await _process_wheel(update, context, uid, player, cfg, war_service)
        return
    if action == "luck_berserk":
        await _process_berserk(update, context, uid, player, cfg, war_service)
        return
    if action == "alchemy_start":
        await _process_alchemy_start(update, context, player, cfg)
        return
    if action == "alchemy_confirm":
        await _process_alchemy_confirm(update, context, uid, player, cfg, war_service)
        return

    # Главное меню удачи
    text = (
        "<b>🍀 УДАЧА</b>\n\n"
        "<i>🌀 «Испытай свою удачу и выиграй OAC 🍬 и редкие эксклюзивные вещи!» 🪽</i>\n\n"
        "🎡 <b>Крутить Колесо</b> — ежедневный выигрыш 🎉\n"
        "🍀 <b>Рискнуть</b> — бросить вызов и отдать 300 оас ради джекпота 💫\n"
        "⚗️ <b>Алхимия</b> — древнее искусство, магия для достойных 🔮"
    )
    kb_rows = _build_luck_keyboard(now, player, cfg, wheel_ok, berserk_ok, alchemy_ok)
    kb = InlineKeyboardMarkup(kb_rows)
    await edit_or_reply(update, context, text, reply_markup=kb)


# ── Колесо ──────────────────────────────────────────────────
async def _process_wheel(update, context, uid, player, cfg, war_service):
    if not _check_wheel_availability(player, datetime.now(), cfg["wheel"]["cooldown_hours"]):
        await _notify_user(update, context, "🎡 Колесо пока недоступно. Загляни позже.")
        return

    async def _wheel(p, conn):
        r = random.random()
        prize, ptype = 0, "oac"
        for prob, amount, kind in cfg["wheel"]["rewards"]:
            if r < prob:
                prize, ptype = amount, kind
                break
        if ptype == "jackpot" and random.random() < 0.5:
            prize *= 2
        if context.bot_data.get("happy_hour") and ptype in ("oac", "jackpot"):
            prize *= HAPPY_HOUR_MULTIPLIER

        if ptype in ("oac", "jackpot"):
            p.balance += prize
        else:
            p.blunts += prize
        p.last_daily = datetime.now()

        if war_service and ptype in ("oac", "jackpot"):
            await war_service.add_score_raw(uid, prize, conn)

        return prize, ptype, p.balance

    result = await PlayerRepository.atomic_update(uid, _wheel)
    if result is None:
        logger.error("wheel atomic_update failed", extra={"user_id": uid})
        await _notify_user(update, context, "❌ Ошибка при обработке. Попробуй позже.")
        return
    prize, ptype, new_balance = result

    uname = html.escape(update.effective_user.username or update.effective_user.first_name)
    if ptype == "jackpot":
        msg_text = f"<b>🎰 ДЖЕКПОТ!</b>\n\nТы выиграл <b>{prize} OAC</b> 🎉!\n\n<b>⚜️ У тебя:</b> <i>{new_balance} OAC 🍬</i>"
    elif ptype == "oac":
        msg_text = f"<b>🩸 ДАР ИСКАЖЕНИЯ</b>\n\n<b>💎 Ты нафармил +{prize} OAC 🍬!</b>\n⚜️ <b>У тебя:</b> <i>{new_balance} OAC</i>"
    else:
        msg_text = f"<b><i>🌱 КОЛЕСО СМОТРИТЕЛЯ</i></b>\n\n+{prize} 🌿 Блант → 🍬 <b>{new_balance} OAC</b> 🍬"

    await edit_or_reply(update, context, msg_text,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]))


# ── Берсерк ─────────────────────────────────────────────────
async def _process_berserk(update, context, uid, player, cfg, war_service):
    if not _check_berserk_availability(player, datetime.now(), cfg["berserk"]["cost"], cfg["berserk"]["cooldown_hours"]):
        await _notify_user(update, context, "🍀 Берсерк недоступен! Проверь баланс или время.")
        return

    async def _berserk(p, conn):
        if p.balance < cfg["berserk"]["cost"]:
            return ("no_money", p.balance)

        if random.random() < 0.6:
            p.balance += cfg["berserk"]["win_amount"]
            res = f"<b><i>🎲 БЕЗДНА ОТВЕТИЛА</i></b>\n\nИскажение благосклонно! +<b>{cfg['berserk']['win_amount']} OAC</b> 🍬."
            if war_service:
                await war_service.add_score(uid, WarAction.BERSERK_WIN, conn)
        else:
            p.balance -= cfg["berserk"]["cost"]
            res = f"<b><i>🕯️ БЕЗДНА МОЛЧИТ</i></b>\n\nИскажение промолчало. –<b>{cfg['berserk']['cost']} OAC</b>."
            if war_service:
                await war_service.add_score(uid, WarAction.BERSERK_LOSE, conn)
        p.last_berserk = datetime.now()
        return ("ok", res, p.balance)

    result = await PlayerRepository.atomic_update(uid, _berserk)
    if result is None:
        logger.error("berserk atomic_update failed", extra={"user_id": uid})
        await _notify_user(update, context, "❌ Ошибка при обработке. Попробуй позже.")
        return
    status, *data = result
    if status == "no_money":
        await _notify_user(update, context, f"❌ Недостаточно OAC 🍬. Текущий баланс: {data[0]}")
        return
    res_text, _ = data
    await edit_or_reply(update, context, res_text,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]))


# ── Алхимия (начало) ────────────────────────────────────────
async def _process_alchemy_start(update, context, player, cfg):
    if not has_rank(player.balance, "Ветеран"):
        await _notify_user(update, context, f"❌ Доступно с ранга ⚔️ Ветеран ({GAME_CONFIG['veteran_threshold']} OAC 🍬)", show_alert=True)
        return
    text = (
        "<b>🔮 АЛХИМИЧЕСКИЙ КОТЁЛ</b>\n\n"
        f"<b>💎 У тебя: {player.balance} OAC 🍬</b>\n"
        f"<b>🌿 Блантов в свёртке: {player.blunts}</b>\n\n"
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
    await edit_or_reply(update, context, text, reply_markup=kb)


# ── Алхимия (запуск) ────────────────────────────────────────
async def _process_alchemy_confirm(update, context, uid, player, cfg, war_service):
    async def _alchemy(p, conn):
        if p.blunts < cfg["alchemy"]["cost_blunts"] or p.balance < cfg["alchemy"]["cost_oac"]:
            return (AlchemyResult.NO_RESOURCES,)
        p.blunts -= cfg["alchemy"]["cost_blunts"]
        p.balance -= cfg["alchemy"]["cost_oac"]
        r = random.random()
        res = ""
        for prob, effect, value in cfg["alchemy"]["reactions"]:
            if r < prob:
                if effect == "dust":
                    p.m_essence += value
                    res = f"<b>💠 {'Чистая' if value==1 else 'Мерцающая'} Пыльца!</b>\n\n+{value} Кристальной Пыли"
                elif effect == "legendary":
                    name = random.choice(cfg["alchemy"]["legendary_names"])
                    await create_named_blunt(uid, name, rarity="legendary", conn=conn)
                    res = f"<b>🌟 Философский Камень!</b>\n\nЛегендарный блант «{name}»!"
                else:
                    res = "<b>🌫️ Грязный Выхлоп...</b>\n\nБланты сгорели без следа."
                break
        else:
            logger.error("Alchemy: ни одна реакция не сработала, r=%s", r)
            res = "<b>🌫️ Грязный Выхлоп...</b>\n\nБланты сгорели без следа."

        if war_service:
            await war_service.add_score(uid, WarAction.ALCHEMY, conn)
        return (AlchemyResult.SUCCESS, res)

    result = await PlayerRepository.atomic_update(uid, _alchemy)
    if result is None:
        logger.error("alchemy atomic_update failed", extra={"user_id": uid})
        await _notify_user(update, context, "❌ Ошибка при обработке. Попробуй позже.")
        return

    status, *data = result
    if status == AlchemyResult.NO_RESOURCES:
        await _notify_user(update, context, "❌ Недостаточно ресурсов. Нужно 10 блантов и 250 OAC.", show_alert=True)
        return

    await edit_or_reply(update, context, data[0],
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="luck")]]))

# /check
async def check_blunt(update, context):
    if not context.args:
        await update.message.reply_text("Укажи серийный номер бланта: /check R-0001")
        return
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
    if not item:
        await update.message.reply_text("Блант найден в реестре, но его владелец не обнаружен.")
        return
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

    # Обновляем счётчик проверок через модель
    player = await PlayerRepository.get_by_id(update.effective_user.id)
    if player:
        player.check_count = (player.check_count or 0) + 1
        await PlayerRepository.save(player)

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
    player = await PlayerRepository.get_by_id(uid)
    if not player:
        return
    depth = player.lab_depth or 1
    now = datetime.now()
    last = player.last_lab_attempt
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
            await edit_or_reply(update, context, text, reply_markup=kb)
            return
    total_rooms = 4 + depth
    text = (
        f"<b>🏛️ ЛАБИРИНТ ИСКАЖЕНИЯ — ЭТАЖ {depth}</b>\n\n"
        f"🔮 <i>\"Ты стоишь у входа...\"</i> 🎁\n\n"
        f"<b>💎 1 попытка</b>\n"
        f"<b>⛓️‍💥 2 жизни</b>\n"
        f"<b>🗝️ Комнат: {total_rooms}</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍃 Войти в лабиринт", callback_data="lab_enter_confirm")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    await edit_or_reply(update, context, text, reply_markup=kb)

# ─── ПОДГОТОВКА К ЗАБЕГУ ────────────────────────────────────
async def lab_enter_confirm(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    depth = player.lab_depth or 1 if player else 1
    total_rooms = 4 + depth
    now = datetime.now()
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE players SET last_lab_attempt=$1 WHERE user_id=$2", now, uid)

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

    if redis:
        state = {k: context.user_data[k] for k in (
            "lab_room","lab_hp","lab_max_hp","lab_focus","lab_rewards",
            "lab_depth","lab_total_rooms","lab_attack_bonus",
            "lab_focused_attack","lab_curse_rooms"
        )}
        await redis.setex(f"lab_state:{uid}", 3600, json.dumps(state, default=str))

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
    player = await PlayerRepository.get_by_id(uid)
    if not player:
        return
    rewards = context.user_data.get("lab_rewards", [])
    total_oac = sum(rewards) + 50

    async def _lab_win(p, conn):
        p.balance += total_oac
        p.m_essence += 1
        p.lab_chests += 1
        p.lab_depth += 1

        # Военный счёт
        war_service = context.bot_data.get("war_service")
        if war_service:
            await war_service.add_score(uid, WarAction.LAB_WIN, conn)

    await PlayerRepository.atomic_update(uid, _lab_win)

    # очистка состояний
    for key in ("lab_hp", "lab_focus", "lab_room"):
        context.user_data.pop(key, None)

    depth = player.lab_depth + 1
    text = (
        f"<b>🎁 СУНДУК ИСКАЖЕНИЯ</b>\n\n"
        f"<i>Ты достиг цели! Древние награждают достойных.</i>\n\n"
        f"<b>+{total_oac} OAC</b>\n"
        f"<b>💠 Кристальная Пыль: 1</b>\n"
        f"<b>🏆 Глубина увеличена! (Этаж {depth})</b>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К Лабиринту", callback_data="lab_start")],
                               [InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await edit_or_reply(update, context, text, reply_markup=kb)
    await check_achievements(uid, context)

# ─── СМЕРТЬ В ЛАБИРИНТЕ ──────────────────────────────────────
async def show_lab_death(update, context):
    query = update.callback_query
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player:
        return
    depth = player.lab_depth or 1

    # атомарно начисляем утешительный приз и военные очки
    async def _lab_die(p, conn):
        p.balance += 50
        p.lab_deaths += 1

        war_service = context.bot_data.get("war_service")
        if war_service:
            await war_service.add_score(uid, WarAction.LAB_DEATH, conn)
        else:
            logger.warning("GuildWarService not found in bot_data")

    await PlayerRepository.atomic_update(uid, _lab_die)

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
    await edit_or_reply(update, context, text, reply_markup=kb)

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
    # Имя питомца – САМОЕ ПЕРВОЕ
    if context.user_data.get('awaiting_pet_name'):
        await handle_pet_name(update, context)
        return
    if context.user_data.get('awaiting_named_blunt'):
        await handle_named_name(update, context)
        return
    if context.user_data.get('gifting_blunt_id'):
        await handle_gift_username(update, context)
        return
    # ... остальные сокращения ...

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

# ============================================================
# ПИТОМЦЫ (полная версия без багов)
# ============================================================
@safe_callback
async def pet_preview(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    if player and player.pet:
        name_str = f" по кличке «{player.pet_name}»" if player.pet_name else ""
        await query.message.edit_text(f"Твой питомец: {player.pet}{name_str}")
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🐕 Купить Песика (3000 🍬)", callback_data="pet_buy_dog")],
            [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
        ])
        await query.message.edit_text(
            "🐾 <b>ПИТОМЦЫ</b>\n\nПока доступен только Песик.",
            reply_markup=kb,
            parse_mode='HTML'
        )

@safe_callback
async def pet_buy_dog_handler(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    async def _buy(p, conn):
        if p.pet:
            return ("already_have",)
        if p.balance < 3000:
            return ("no_money",)
        p.balance -= 3000
        p.pet = "🐕 Песик"
        p.pet_name = ""
        return ("ok",)

    result = await PlayerRepository.atomic_update(uid, _buy)
    if result is None:
        await query.answer("Ошибка.")
        return
    status = result[0]
    if status == "already_have":
        await query.answer("У тебя уже есть питомец!")
    elif status == "no_money":
        await query.answer("Недостаточно OAC. Нужно 3000 🍬")
    else:
        context.user_data['awaiting_pet_name'] = True
        await query.message.edit_text(
            "<b>🐕 Песик ждёт имя!</b>\n\n"
            "Введи имя для своего питомца (до 15 символов).\n"
            "Для отмены нажми кнопку ниже.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Пропустить (без имени)", callback_data="pet_name_skip")]
            ])
        )

async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🪪 Скидка", callback_data="privilege")],
        [InlineKeyboardButton("📦 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await query.message.edit_text("<b>🛒 МАГАЗИН</b>", reply_markup=kb, parse_mode='HTML')

@safe_callback
async def pet_name_skip_handler(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('awaiting_pet_name', None)
    await query.message.edit_text("Питомец останется без имени.")

async def handle_pet_name(update, context):
    """Обработчик ввода имени питомца."""
    name = update.message.text.strip()[:15]
    if not name:
        await update.message.reply_text("❌ Имя не может быть пустым.")
        return

    uid = update.effective_user.id
    async def _set_name(p, conn):
        p.pet_name = name
        return ("ok",)

    result = await PlayerRepository.atomic_update(uid, _set_name)
    if result is None:
        await update.message.reply_text("Ошибка при сохранении имени.")
    else:
        await update.message.reply_text(f"Отлично! Теперь твоего питомца зовут «{name}»! 🐕")
    context.user_data.pop('awaiting_pet_name', None)
    
async def check_blunt_pics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    status = []
    for rarity in ("common", "rare", "epic", "legendary"):
        file_id = BLUNT_IMAGES.get(rarity)
        if not file_id:
            status.append(f"❌ {rarity}: не задан")
        else:
            try:
                await context.bot.get_file(file_id)
                status.append(f"✅ {rarity}")
            except Exception:
                status.append(f"⚠️ {rarity}: невалидный file_id")
    await update.message.reply_text("\n".join(status))

# ========== ВСПОМОГАТЕЛЬНЫЕ ОБРАБОТЧИКИ КНОПОК =========
@safe_callback
async def menu_handler(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    kb, whisper = await get_main_menu_keyboard(uid)
    menu_text = f"<b>🎮 ГЛАВНОЕ МЕНЮ</b>\n\n<i>{whisper}</i>"
    try:
        await query.message.edit_text(menu_text, reply_markup=kb, parse_mode='HTML')
    except Exception:
        await query.message.reply_text(menu_text, reply_markup=kb, parse_mode='HTML')

@safe_callback
async def bush_preview_handler(update, context):
    query = update.callback_query
    await query.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)", show_alert=True)

@safe_callback
async def activate_menu_handler(update, context):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    uname = user.username or user.first_name
    uid = user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player or not player.user_id:
        player = Player(user_id=uid, username=uname, balance=800)
        new_name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
        await create_named_blunt(uid, new_name)
        await PlayerRepository.save(player)
        bonus = "🎁 Смотритель дарует тебе <code>800</code> 🍬 и твой первый именной блант!\n\n"
    else:
        bonus = ""
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
    await query.message.edit_text(bonus + welcome, reply_markup=guild_kb, parse_mode='HTML')

@safe_callback
async def skins_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Выбрать титул", callback_data="choose_title")],
        [InlineKeyboardButton("🖼️ Выбрать фон", callback_data="choose_bg")],
        [InlineKeyboardButton("🔙 Назад", callback_data="profile")]
    ])
    try:
        await query.message.edit_text(
            "<b>🎨 СКИНЫ</b>\n\nВыбери, что хочешь изменить.",
            reply_markup=kb,
            parse_mode='HTML'
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        # Если не удалось отредактировать, отправляем новое сообщение
        await query.message.reply_text(
            "<b>🎨 СКИНЫ</b>\n\nВыбери, что хочешь изменить.",
            reply_markup=kb,
            parse_mode='HTML'
        )

@safe_callback
async def choose_title_handler(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player:
        return
    titles = (player.titles or "").split()
    if not titles:
        await query.message.edit_text("У тебя пока нет титулов.", reply_markup=get_back_to_menu_keyboard())
        return
    skins = player.profile_skins or {}
    active_title = skins.get("active_title", "")
    kb_rows = []
    for title in titles:
        mark = " ✅" if title == active_title else ""
        kb_rows.append([InlineKeyboardButton(f"{title}{mark}", callback_data=f"set_title_{title}")])
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="skins_menu")])
    await query.message.edit_text(
        "<b>🎨 ВЫБОР ТИТУЛА</b>\n\nВыбери титул:",
        reply_markup=InlineKeyboardMarkup(kb_rows),
        parse_mode='HTML'
    )

@safe_callback
async def choose_bg_handler(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    if not player:
        return
    skins = player.profile_skins or {}
    unlocked = skins.get("unlocked_backgrounds", [])
    if not unlocked:
        await query.message.edit_text("У тебя пока нет разблокированных фонов.", reply_markup=get_back_to_menu_keyboard())
        return
    active_bg = skins.get("active_background", "")
    kb_rows = []
    for bg in unlocked:
        mark = " ✅" if bg == active_bg else ""
        kb_rows.append([InlineKeyboardButton(f"{bg}{mark}", callback_data=f"set_bg_{bg}")])
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="skins_menu")])
    await query.message.edit_text(
        "<b>🖼️ ВЫБОР ФОНА</b>\n\nВыбери фон:",
        reply_markup=InlineKeyboardMarkup(kb_rows),
        parse_mode='HTML'
    )

@safe_callback
async def handle_set_title(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    new_title = query.data.replace("set_title_", "")

    async def _set(p, conn):
        skins = p.profile_skins or {}
        skins["active_title"] = new_title
        p.profile_skins = skins
        return new_title

    result = await PlayerRepository.atomic_update(uid, _set)
    if result is None:
        await query.answer("Профиль не найден", show_alert=True)
        return
    await context.bot.send_message(chat_id=query.message.chat.id, text=f"✨ Титул «{new_title}» активирован!")
    await skins_menu_handler(update, context)

@safe_callback
async def handle_set_bg(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    new_bg = query.data.replace("set_bg_", "")

    async def _set(p, conn):
        skins = p.profile_skins or {}
        skins["active_background"] = new_bg
        p.profile_skins = skins
        return new_bg

    result = await PlayerRepository.atomic_update(uid, _set)
    if result is None:
        await query.answer("Профиль не найден", show_alert=True)
        return
    await context.bot.send_message(chat_id=query.message.chat.id, text=f"✨ Фон «{new_bg}» активирован!")
    await skins_menu_handler(update, context)

@safe_callback
async def blunt_details_handler(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    blunt_id = query.data.replace("blunt_details_", "")
    player = await PlayerRepository.get_by_id(uid)
    if not player:
        return
    inv = player.inventory or []
    item = next((it for it in inv if it.get("id") == blunt_id), None)
    if not item:
        await query.answer("Блант не найден.")
        return
    name = item["name"]
    rarity = item.get("rarity", "common")
    color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
    rare_number = item.get("rare_number", "?-????")
    hash_code = item.get("hash", "0x????...????")
    reaction = item.get("reaction", "")
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
            date_str = format_date(entry.get('since', ''))
            text += f"   <b>@{entry.get('user_id', '?')}</b> — {date_str}\n"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Поделиться", callback_data=f"share_blunt_{blunt_id}"),
         InlineKeyboardButton("🎁 Подарить", callback_data=f"gift_blunt_{blunt_id}")],
        [InlineKeyboardButton("🏆 К списку", callback_data="my_blunts")]
    ])
    file_id = BLUNT_IMAGES.get(rarity)
    if file_id:
        await query.message.delete()
        await context.bot.send_photo(
            chat_id=query.message.chat.id,
            photo=file_id,
            caption=text,
            reply_markup=kb,
            parse_mode='HTML'
        )
    else:
        await query.message.edit_text(text=text, reply_markup=kb, parse_mode='HTML')

@safe_callback
async def share_blunt_handler(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    blunt_id = query.data.replace("share_blunt_", "")
    player = await PlayerRepository.get_by_id(uid)
    if not player:
        return
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=blunt_{blunt_id}"
    inv = player.inventory or []
    item = next((it for it in inv if it.get("id") == blunt_id), None)
    username = html.escape(player.username)
    if item:
        name = item["name"]
        rarity = item.get("rarity", "common")
        color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
        text = (
            f"<b>{username}</b>\n\n"
            f"{color} <b>Имя NFT бланта: «{name}»</b>\n"
            f"🧬 <b>Редкость:</b> {rarity} {color}\n"
            f"🩸 <b>Серийный номер:</b> #{item.get('rare_number', '?-????')}\n"
            f"📜 <b>Реакция:</b> <i>{item.get('reaction', '')}</i>\n\n"
            f"<i>Присоединяйся к Искажению:</i>\n{ref_link}"
        )
    else:
        text = f"Блант не найден.\n{ref_link}"
    await context.bot.send_message(chat_id=query.message.chat.id, text=text, parse_mode='HTML')

@safe_callback
async def shrine_donate_handler(update, context):
    query = update.callback_query
    await query.answer()
    amount = 100 if query.data == "shrine_donate_100" else 500
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    if player.balance < amount:
        await query.answer("Недостаточно OAC.", show_alert=True)
        return
    player.balance -= amount
    player.donated = (player.donated or 0) + amount
    await PlayerRepository.save(player)
    await send_whisper_dm(update, context, f"💎 Ты внёс {amount} OAC в Храм. Спасибо, Странник!")

@safe_callback
async def guild_join_handler(update, context):
    query = update.callback_query
    await query.answer()
    guild = "BLACK" if query.data == "guild_join_BLACK" else "WHITE"
    uid = query.from_user.id
    player = await PlayerRepository.get_by_id(uid)
    player.guild = guild
    await PlayerRepository.save(player)
    g_emoji = "🕯️" if guild == "BLACK" else "⚜️"
    g_name = "Тёмная" if guild == "BLACK" else "Светлая"
    uname = html.escape(query.from_user.username or query.from_user.first_name)
    await query.message.edit_text(
        f"<b><i>🕋 ГИЛЬДИЯ ТЕБЯ ПРИНЯЛА</i></b>\n\n"
        f"✅ Теперь <b>ты</b> — {g_emoji} <b>{g_name} Гильдия</b> ·\n\n"
        f"<i>🩸 Искажение стало плотнее...</i>",
        parse_mode='HTML'
    )

@safe_callback
async def luck_wheel_handler(update, context):
    await luck_callback(update, context, action="luck_wheel")

@safe_callback
async def luck_berserk_handler(update, context):
    await luck_callback(update, context, action="luck_berserk")

@safe_callback
async def alchemy_start_handler(update, context):
    await luck_callback(update, context, action="alchemy_start")

@safe_callback
async def alchemy_confirm_handler(update, context):
    await luck_callback(update, context, action="alchemy_confirm")

@safe_callback
async def cancel_gift_handler(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("gifting_blunt_id", None)
    await profile_callback(update, context)

# ========== СЛОВАРЬ ПРОСТЫХ КОЛБЭКОВ ==========
CALLBACKS: Dict[str, Callable] = {
    "menu": menu_handler,
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
    "guild_shrine": guild_shrine_callback,
    "guild_war": guild_war_callback,
    "confess": confess_callback,
    "shop": shop_callback,
    "bush_preview": bush_preview_handler,
    "activate_menu": activate_menu_handler,
    "skins_menu": skins_menu_handler,
    "choose_title": choose_title_handler,
    "choose_bg": choose_bg_handler,
    "shrine_donate_100": shrine_donate_handler,
    "shrine_donate_500": shrine_donate_handler,
    "guild_join_BLACK": guild_join_handler,
    "guild_join_WHITE": guild_join_handler,
    "cancel_gift": cancel_gift_handler,
    "pet_preview": pet_preview,
    "pet_buy_dog": pet_buy_dog_handler,
    "pet_name_skip": pet_name_skip_handler,
}

# ========== ТОЧНЫЕ КОЛБЭКИ (без префиксов) ==========
EXACT_HANDLERS: Dict[str, Callable] = {
    "lab_special": handle_lab_option,
    "lab_focus_use": handle_lab_option,
    "lab_escape": handle_lab_option,
    "luck_wheel": luck_wheel_handler,
    "luck_berserk": luck_berserk_handler,
    "alchemy_start": alchemy_start_handler,
    "alchemy_confirm": alchemy_confirm_handler,
}

# ========== ПРЕФИКСНЫЕ КОЛБЭКИ ==========
PREFIX_HANDLERS: Dict[str, Callable] = {
    "ach_page_": achievements_callback,
    "blunts_page_": my_blunts_callback,
    "blunt_details_": blunt_details_handler,
    "share_blunt_": share_blunt_handler,
    "gift_blunt_": gift_blunt_start,
    "set_title_": handle_set_title,
    "set_bg_": handle_set_bg,
    "lab_attack_": handle_lab_option,
}

# ========== ЕДИНСТВЕННЫЙ button_handler ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    try:
        # 1. Точные колбэки
        if data in EXACT_HANDLERS:
            await EXACT_HANDLERS[data](update, context)
            return

        # 2. Префиксные колбэки с параметрами
        for prefix, handler in PREFIX_HANDLERS.items():
            if data.startswith(prefix):
                # Извлекаем page для пагинации
                if prefix in ("ach_page_", "blunts_page_"):
                    page = int(data.split("_")[-1])
                    await handler(update, context, page=page)
                else:
                    await handler(update, context)
                return

        # 3. Простые колбэки
        handler = CALLBACKS.get(data)
        if handler:
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
    # try: await context.bot.set_chat_description(chat_id="@guild_antysocial", description=desc)
    # except: pass

async def happy_hour_trigger(context):
    context.bot_data["happy_hour"] = True
    context.bot_data["happy_hour_end"] = datetime.now() + timedelta(minutes=HAPPY_HOUR_DURATION_MIN)
    # try:
    #     await context.bot.send_message(chat_id="@guild_antysocial", text="🎉 <b>ЧАС УДАЧИ!</b> 🌠 Все действия приносят x2 OAC 🍬 (30 минут)!", parse_mode='HTML')
    # except Exception as e:
    #     logger.error(f"Happy hour announce error: {e}")
    context.job_queue.run_once(reset_happy_hour, HAPPY_HOUR_DURATION_MIN*60)

async def reset_happy_hour(context):
    context.bot_data["happy_hour"] = False
    # try:
    #     await context.bot.send_message(chat_id="@guild_antysocial", text="⏳ Час Удачи завершён.")
    # except Exception as e:
    #     logger.error(f"Happy hour reset error: {e}")

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
    # try:
    #     await context.bot.send_message(chat_id="@guild_antysocial", text=text, parse_mode='HTML', reply_markup=kb)
    # except Exception as e:
    #     logger.error(f"Echo of distortion error: {e}")

async def weekly_guild_rating(context):
    async with db_pool.acquire() as conn:
        war = await conn.fetchrow("SELECT war_active FROM guild_weekly WHERE war_active = TRUE LIMIT 1")
        if not war:
            await conn.execute("UPDATE guild_weekly SET total_farmed = 0, war_active = TRUE")
            # try:
            #     await context.bot.send_message(chat_id="@guild_antysocial",
            #         text="⚔️ <b>ВОЙНА ГИЛЬДИЙ НАЧАЛАСЬ! 🎉</b>...", parse_mode='HTML')
            # except Exception as e:
            #     logger.error(f"War start announce error: {e}")
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
                # try:
                #     await context.bot.send_message(chat_id="@guild_antysocial",
                #         text=f"🎉 <b>ВОЙНА ГИЛЬДИЙ ЗАВЕРШЕНА!</b>...", parse_mode='HTML')
                # except Exception as e:
                #     logger.error(f"War end announce error: {e}")

async def keep_db_alive(context):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT 1")
        except Exception as e:
            logger.error(f"Keep-alive error: {e}")

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    if not TOKEN:
        raise RuntimeError("TOKEN не установлен")
    if not os.getenv("NEON_DATABASE_URL"):
        raise RuntimeError("NEON_DATABASE_URL не установлена")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db_pool())
    Thread(target=run_web_server, daemon=True).start()

    # Загружаем сохранённые file_id из БД (асинхронно, безопасно)
    async def load_blunt_images():
        for rarity in ("common", "rare", "epic", "legendary"):
            saved = await get_setting(f"blunt_image_{rarity}")
            if saved:
                BLUNT_IMAGES[rarity] = saved

    loop.run_until_complete(load_blunt_images())

    # ===== СОЗДАНИЕ ПРИЛОЖЕНИЯ С ЛИМИТЕРОМ =====
    app = (Application.builder()
           .token(TOKEN)
           .rate_limiter(AIORateLimiter())
           .build())

    # ИНИЦИАЛИЗАЦИЯ СЕРВИСА ВОЙНЫ
    war_settings = WarSettings()
    war_config = WarConfig()
    war_service = GuildWarService(db_pool, redis_client=redis, config=war_config, settings=war_settings)
    app.bot_data["war_service"] = war_service

    # Валидация изображений после запуска приложения
    async def check_all_blunt_images():
        invalid = []
        for rarity in ("common", "rare", "epic", "legendary"):
            file_id = BLUNT_IMAGES.get(rarity)
            if not file_id:
                invalid.append(rarity)
                continue
            try:
                await app.bot.get_file(file_id)
            except Exception:
                invalid.append(rarity)
                BLUNT_IMAGES.pop(rarity, None)
                await set_setting(f"blunt_image_{rarity}", "")
        if invalid:
            logger.warning("Невалидные изображения: %s", ", ".join(invalid))
            if ADMIN_ID:
                await app.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ При запуске обнаружены невалидные изображения: {', '.join(invalid)}.\n"
                         f"Они сброшены. Обновите через /setbluntpic."
                )

    app.job_queue.run_once(check_all_blunt_images, when=0)

    # ===== ИНИЦИАЛИЗАЦИЯ SENTRY =====
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
    # job.run_repeating(update_pulse, interval=900, first=10)
    # job.run_repeating(happy_hour_trigger, interval=random.randint(14400, 28800), first=random.randint(3600, 10800))
    # job.run_daily(echo_of_distortion, time=time(hour=18, minute=0))
    # job.run_repeating(weekly_guild_rating, interval=7*24*3600, first=max(1, (next_saturday - now).total_seconds()))
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
            pass

    # ===== ГЛОБАЛЬНЫЙ ОБРАБОТЧИК RetryAfter =====
    from telegram.error import RetryAfter

    async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        try:
            if isinstance(error, RetryAfter):
                logger.warning(f"Telegram попросил подождать {error.retry_after} сек.")
                await asyncio.sleep(error.retry_after)
            else:
                logger.critical("Глобальная ошибка:", exc_info=True)
        except Exception:
            import traceback
            traceback.print_exc()

    app.add_error_handler(global_error_handler)

    print("BOT READY")
    app.run_polling()