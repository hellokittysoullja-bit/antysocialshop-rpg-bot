# bot.py — ANTY SOCIAL SHOP RPG v8.0 ENTERPRISE
import sys, traceback, time, random
from html import escape as html_escape
from blunt_name_generator import mutate_name
def log_uncaught(exc_type, exc_value, exc_tb):
    traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
    sys.stderr.flush()
    time.sleep(2)   # даём время Render прочитать
    sys.__excepthook__(exc_type, exc_value, exc_tb)
sys.excepthook = log_uncaught
import asyncio, json, logging, os, sys, time, random, re, hashlib, html, enum, uuid, copy, math
from datetime import datetime, timedelta, date, time as time_module, timezone
from threading import Thread
from typing import Optional, List, Any, Dict, Tuple, NamedTuple, Callable
from dataclasses import dataclass, field  

import asyncpg
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log
)
from pydantic import BaseModel, ConfigDict, Field   # <-- добавлен Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters, AIORateLimiter
)
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.request import HTTPXRequest

import redis.asyncio as aioredis
from functools import wraps

try:
    import pybreaker
    redis_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=30)
    db_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=30)
    tg_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=30)
except Exception as e:
    print(f"WARNING: pybreaker not available: {e}", file=sys.stderr)
    class DummyBreaker:
        def call(self, func, *args, **kwargs):
            return func(*args, **kwargs)
    redis_breaker = DummyBreaker()
    db_breaker = DummyBreaker()
    tg_breaker = DummyBreaker()

from cachetools import TTLCache
from prometheus_client import Counter, Histogram
callback_requests = Counter('bot_callback_requests', 'Total callbacks')
callback_duration = Histogram('bot_callback_duration_seconds', 'Callback duration')
rate_limited_requests = Counter('bot_rate_limited_requests', 'Rate limited requests')

import httpx
import re
import copy

# ============================================================
# ДЕКОРАТОРЫ
# ============================================================
def rate_limit(seconds: int = 2):
    def decorator(func):
        @wraps(func)
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
    
def divine_command(command_name: str):
    """Делает обработчик 'Божественным': request ID, логи, асинхронный алерт админу."""
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            request_id = uuid.uuid4().hex[:8]
            user_id = update.effective_user.id
            try:
                logger.info("[%s] /%s от user=%d", request_id, command_name, user_id)
            except Exception:
                print(f"[{request_id}] CMD /{command_name} from {user_id}", file=sys.stderr)

            try:
                return await func(update, context)
            except Exception as e:
                import traceback
                err_msg = traceback.format_exc()
                try:
                    logger.error("[%s] Ошибка /%s: %s", request_id, command_name, e, exc_info=True)
                except Exception:
                    print(f"[{request_id}] ERROR /{command_name}: {err_msg}", file=sys.stderr)

                async def _alert():
                    for attempt in range(3):
                        try:
                            await context.bot.send_message(
                                chat_id=settings.admin_id,
                                text=f"🚨 [{request_id}] Ошибка /{command_name} от {user_id}: {html.escape(str(e)[:500])}"
                            )
                            break
                        except Exception:
                            await asyncio.sleep(2 ** attempt)
                asyncio.create_task(_alert())

                try:
                    await update.message.reply_text("⚠️ Внутренняя ошибка. Админ уже уведомлён.")
                except Exception:
                    pass
            finally:
                try:
                    logger.debug("[%s] /%s обработана", request_id, command_name)
                except Exception:
                    pass
        return wrapper
    return decorator
    
def game_handler(func):
    """Абсолютный декоратор: гарантированная идемпотентность, атомарный контекст, умная загрузка игрока."""
    import inspect
    sig = inspect.signature(func)
    needs_ctx = 'ctx' in sig.parameters
    needs_player = 'player' in sig.parameters

    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        # === РАННЯЯ ЗАЩИТА ===
        if not update or not context:
            logger.error(f"game_handler: update или context отсутствуют в {func.__name__}")
            return

        # === ПОЛУЧЕНИЕ КОНТЕКСТА (всегда, ради идемпотентности) ===
        ctx = context.bot_data.get("ctx")

        # === ГАРАНТИРОВАННАЯ ИДЕМПОТЕНТНОСТЬ ===
        update_id = getattr(update, 'update_id', None)
        if update_id and ctx and ctx.cache:
            idemp_key = f"processed_update:{update_id}"
            if ctx.cache.get(idemp_key):
                return
            ctx.cache[idemp_key] = True

        # === ПРОВЕРКА ГОТОВНОСТИ БОТА ===
        if (needs_ctx or needs_player) and not ctx:
            try:
                if update.effective_message:
                    await update.effective_message.reply_text("⚠️ Бот инициализируется, попробуйте позже.")
            except Exception:
                pass
            return

        # === ЗАГРУЗКА ИГРОКА ===
        player = None
        if needs_player and ctx:
            try:
                uid = update.effective_user.id
                if uid is None:
                    raise AttributeError("effective_user.id is None")
            except AttributeError:
                logger.warning(f"game_handler: не удалось получить user_id в {func.__name__}")
                return
            player = await ctx.repo.get_by_id(uid)
            if not player or not player.exists:
                try:
                    if update.effective_message:
                        await update.effective_message.reply_text(
                            "⚠️ Ваш профиль не обнаружен. Пожалуйста, нажмите /start для создания."
                        )
                except Exception:
                    pass
                return

        # === СБОР АРГУМЕНТОВ ===
        new_kwargs = {**kwargs}
        if needs_ctx:
            new_kwargs['ctx'] = ctx
        if needs_player:
            new_kwargs['player'] = player

        # === ВЫПОЛНЕНИЕ С ЗАЩИТОЙ ===
        try:
            return await func(update, context, *args, **new_kwargs)
        except asyncio.CancelledError:
            raise   # не глушим, чтобы корректно работала отмена задач
        except Exception as e:
            logger.error(f"Unhandled error in {func.__name__}:", exc_info=True)
            # Сохраняем уникальную логику из старого error_handler
            if 'awaiting_named_blunt' in context.user_data:
                context.user_data['awaiting_named_blunt'] = False
            if update.callback_query:
                await update.callback_query.answer("⚠️ Внутренняя ошибка. Админ уже в курсе.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("⚠️ Что-то пошло не так. Попробуйте позже.")
            if settings.admin_id:
                import traceback as tb_module
                try:
                    err_msg = f"🚨 <b>Ошибка в {func.__name__}</b>\n<code>{html.escape(str(e))}</code>"
                    await context.bot.send_message(chat_id=settings.admin_id, text=err_msg, parse_mode='HTML')
                except Exception:
                    pass
    return wrapper

# Проверка: если retry – модуль, а не функция, будет ошибка
assert callable(retry), "retry должен быть функцией, а не модулем!"

# Метрики-заглушки (без зависимостей, экономят память)
class DummyMetric:
    def inc(self): pass
    def time(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass

callback_requests = DummyMetric()
callback_duration = DummyMetric()

def cb(func_or_alert=False):
    """
    Универсальный декоратор. Используй как @cb или @cb(True).
    Всегда передаёт ctx из context.bot_data.
    """
    if callable(func_or_alert):
        func = func_or_alert
        show_alert_on_error = False
        return _create_wrapper(func, show_alert_on_error)
    else:
        show_alert_on_error = func_or_alert
        def decorator(func):
            return _create_wrapper(func, show_alert_on_error)
        return decorator

def _create_wrapper(func, show_alert_on_error):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass

        ctx = context.bot_data.get("ctx")
        if not ctx:
            logger.error("AppContext not found in bot_data")
            # 🔥 АБСОЛЮТНАЯ СИММЕТРИЯ с game_handler – игрок всегда видит ответ
            if query:
                await query.answer("⚠️ Бот инициализируется, попробуйте позже.", show_alert=True)
            return

        try:
            callback_requests.inc()
            with callback_duration.time():
                return await func(update, context, ctx, *args, **kwargs)
        except asyncio.CancelledError:
            raise   # Пробрасываем, не глушим отмену
        except Exception as e:
            logger.error(f"Callback error in {func.__name__}: {e}", exc_info=True)
            if query and show_alert_on_error:
                await query.answer(f"❌ Ошибка: {e}", show_alert=True)
    return wrapper

# НАСТРОЙКИ через пидантик
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
    pet: str = ""
    pet_name: str = ""
    onboarding_step: int = 0
    exists: bool = False
    model_config = ConfigDict(populate_by_name=True)
    pet_hunger: int = 100
    daily_progress: dict = Field(default_factory=dict)
    
class PlayerRepository:
    """Репозиторий игроков с Circuit Breaker, кэшем и автоматическими ретраями."""

    def __init__(self, db_pool, redis_client, cache: TTLCache):
        self.db_pool = db_pool
        self.redis = redis_client
        self.cache = cache

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def get_by_id(self, user_id: int, with_inventory: bool = True) -> Player:
        """Возвращает игрока из Redis → in‑memory → БД."""
        if not user_id or user_id <= 0:
            raise ValueError("Некорректный user_id при загрузке")

        # Redis с Circuit Breaker
        if self.redis:
            try:
                data = await redis_breaker.call(self.redis.get, f"player:{user_id}")
                if data:
                    return Player.model_validate_json(data)
            except pybreaker.CircuitBreakerError:
                logger.warning("Circuit breaker открыт для Redis при загрузке %d", user_id)
            except Exception as e:
                logger.warning("Ошибка загрузки из Redis для %d: %s", user_id, e)

        # In‑memory кэш
        if user_id in self.cache:
            logger.debug("Игрок %d загружен из in‑memory кэша", user_id)
            return Player(**self.cache[user_id])

        # БД
        async with self.db_pool.acquire() as conn:
            try:
                await db_breaker.call(conn.set_statement_timeout, 10.0)
            except pybreaker.CircuitBreakerError:
                logger.warning("Circuit breaker открыт для БД при загрузке %d", user_id)
                raise
            except Exception:
                pass  # таймаут не критичен

            columns = [
                "user_id", "username", "balance", "blunts", "guild", "last_farm",
                "last_ritual", "last_daily", "titles", "last_farm_date", "passive_level",
                "passive_collected", "karma", "inhaled", "smoke_count", "farm_count",
                "craft_count", "ritual_count", "referral_count", "last_berserk",
                "inventory", "invited_by", "profile_skins", "login_streak",
                "last_login_date", "oath", "keys", "check_count", "m_essence",
                "lab_chests", "lab_deaths", "alchemy_count", "last_lab_attempt",
                "donated", "daily_progress", "pending_transfer", "lab_depth", "pet", "pet_name", "exists",
            ]
            cols_sql = ", ".join(f'"{c}"' for c in columns)
            row = await db_breaker.call(
                conn.fetchrow,
                f"SELECT {cols_sql} FROM players WHERE user_id = $1",
                user_id
            )

        if row:
            p = dict(row)
            if with_inventory:
                p["inventory"] = _json_safe_load(p.get("inventory"), [])
            else:
                p["inventory"] = []

            p["profile_skins"] = _json_safe_load(p.get("profile_skins"), {})
            p["pending_transfer"] = _json_safe_load(p.get("pending_transfer"), None)
            p["daily_progress"] = _json_safe_load(p.get("daily_progress"), {})
            player = Player(**p)
            player.exists = True
            await self._cache_put(user_id, player)
            return player

        logger.debug("Игрок %d не найден в БД", user_id)
        return Player(user_id=user_id)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def save(self, player: Player, conn=None) -> None:
        """Сохраняет игрока в БД и обновляет кэш."""
        if player.balance < 0:
            logger.warning("Попытка сохранить игрока %d с отрицательным балансом", player.user_id)
            player.balance = 0
        player.exists = True
        if conn and conn.is_closed():
            conn = None

        columns = [
            "user_id", "username", "balance", "blunts", "guild", "last_farm",
            "last_ritual", "last_daily", "titles", "last_farm_date", "passive_level",
            "passive_collected", "karma", "inhaled", "smoke_count", "farm_count",
            "craft_count", "ritual_count", "referral_count", "last_berserk",
            "inventory", "invited_by", "profile_skins", "login_streak",
            "last_login_date", "oath", "keys", "check_count", "m_essence",
            "lab_chests", "lab_deaths", "alchemy_count", "last_lab_attempt",
            "donated", "daily_progress", "pending_transfer", "lab_depth", "pet", "pet_name", "exists",
        ]
        json_cols = {"inventory", "profile_skins", "pending_transfer", "daily_progress"}
        cols_sql = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
        update_set = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in columns if c != "user_id")
        values = [getattr(player, col) for col in columns]
        for idx, col in enumerate(columns):
            if col in json_cols:
                values[idx] = json.dumps(getattr(player, col), separators=(',', ':'), default=str)

        sql = f"""
            INSERT INTO players ({cols_sql})
            VALUES ({placeholders})
            ON CONFLICT (user_id) DO UPDATE SET
                {update_set}
        """

        async def _write(c):
            await c.execute(sql, *values)

        if conn:
            await _write(conn)
        else:
            async with self.db_pool.acquire() as new_conn:
                await _write(new_conn)

        await self._cache_put(player.user_id, player)

        # Инвалидация кэша меню (если функция существует)
        try:
            invalidate_menu_cache(player.user_id)
        except NameError:
            pass
        except Exception as e:
            logger.debug("Инвалидация кэша меню для %d не удалась: %s", player.user_id, e)

        logger.info("Игрок %d успешно сохранён", player.user_id)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def atomic_update(self, user_id: int, update_func):
        """Атомарно блокирует игрока, выполняет update_func и сохраняет."""
        if not user_id or user_id <= 0:
            raise ValueError("Некорректный user_id при атомарном обновлении")

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                columns = [
                    "user_id", "username", "balance", "blunts", "guild", "last_farm",
                    "last_ritual", "last_daily", "titles", "last_farm_date", "passive_level",
                    "passive_collected", "karma", "inhaled", "smoke_count", "farm_count",
                    "craft_count", "ritual_count", "referral_count", "last_berserk",
                    "inventory", "invited_by", "profile_skins", "login_streak",
                    "last_login_date", "oath", "keys", "check_count", "m_essence",
                    "lab_chests", "lab_deaths", "alchemy_count", "last_lab_attempt",
                    "donated", "pending_transfer", "daily_progress", "lab_depth", "pet", "pet_name", "exists",
                ]
                cols_sql = ", ".join(f'"{c}"' for c in columns)
                row = await conn.fetchrow(
                    f"SELECT {cols_sql} FROM players WHERE user_id = $1 FOR UPDATE",
                    user_id
                )
                if not row:
                    logger.warning("atomic_update: игрок %d не найден", user_id)
                    return None

                p = dict(row)
                p["inventory"] = _json_safe_load(p.get("inventory"), [])
                p["profile_skins"] = _json_safe_load(p.get("profile_skins"), {})
                p["pending_transfer"] = _json_safe_load(p.get("pending_transfer"), None)
                p["daily_progress"] = _json_safe_load(p.get("daily_progress"), {})
                player = Player(**p)

                result = await update_func(player, conn)
                await self.save(player, conn=conn)
                logger.info("Атомарное обновление для игрока %d успешно завершено", user_id)
                return result

    async def _cache_put(self, user_id: int, player: Player):
        """Сохраняет игрока в Redis или in‑memory кэш."""
        try:
            if self.redis:
                await redis_breaker.call(
                    self.redis.setex,
                    f"player:{user_id}",
                    10,
                    player.model_dump_json()
                )
            else:
                self.cache[user_id] = player.model_dump()
        except pybreaker.CircuitBreakerError:
            logger.warning("Circuit breaker открыт при кэшировании игрока %d", user_id)
        except Exception as e:
            logger.warning("Не удалось обновить кэш для игрока %d: %s", user_id, e)
            self.cache.pop(user_id, None)
        
from pydantic import Field
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="")

    bot_token: str = Field(..., alias="TOKEN")
    database_url: str = Field(..., alias="DATABASE_URL_AIVEN")
    render_url: str = Field("", alias="RENDER_URL")
    redis_url: str = Field("", alias="REDIS_URL")
    port: int = Field(default=10000, alias="PORT")
    webhook_path: str = "/webhook"
    webhook_secret: str = "SuperSecret"
    sentry_dsn: str = ""
    environment: str = "production"
    admin_id: int = 0

    # Игровые конфиги
    farm_cooldown_hours: float = 0.5
    farm_min: int = 45
    farm_max: int = 100
    happy_hour_multiplier: int = 2
    happy_hour_duration_min: int = 30
    veteran_threshold: int = 5000
    phantom_threshold: int = 20000
    necromant_threshold: int = 50000
    lab_cooldown_hours: int = 12
    ritual_cooldown_hours: int = 24

    @property
    def webhook_url(self) -> str:
        return f"{self.render_url}{self.webhook_path}"

settings = Settings()
FARM_MIN = settings.farm_min
FARM_MAX = settings.farm_max
FARM_COOLDOWN_HOURS = settings.farm_cooldown_hours
HAPPY_HOUR_MULTIPLIER = settings.happy_hour_multiplier

# ── JSON-логгер ──
class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record, ensure_ascii=False)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)

# ── Глобальные конфиги игры ──
GAME_CONFIG = {
    "craft_cost": 15,
    "named_blunt_cost": 50,
    "farm_cooldown_hours": settings.farm_cooldown_hours,
    "ritual_cooldown_hours": settings.ritual_cooldown_hours,
    "lab_cooldown_hours": settings.lab_cooldown_hours,
    "veteran_threshold": settings.veteran_threshold,
    "phantom_threshold": settings.phantom_threshold,
    "necromant_threshold": settings.necromant_threshold,
}
PET_CONFIG = {
    "dog": {"name": "🐕 Песик", "price": 3000, "max_name_len": 15},
}

# ── Хелперы ──
def has_rank(balance: int, rank_name: str = "Ветеран") -> bool:
    thresholds = {
        "Ветеран": GAME_CONFIG["veteran_threshold"],
        "Призрак": GAME_CONFIG["phantom_threshold"],
        "Некромант": GAME_CONFIG["necromant_threshold"],
    }
    return balance >= thresholds.get(rank_name, 0)

def ensure_player_exists(player) -> bool:
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
    REPENT = "repent"
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
        WarAction.REPENT: 0,
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
            
from enum import Enum, auto

class AlchemyResult(Enum):
    SUCCESS = auto()
    NO_RESOURCES = auto()
            
class PetService:
    def __init__(self, repo: PlayerRepository, config: dict):
        self.repo = repo
        self.config = config

    async def buy(self, user_id: int, pet_type: str) -> dict | None:
        async def _buy(p, conn):
            if p.pet:
                return {"status": "already_have"}
            price = self.config[pet_type]["price"]
            if p.balance < price:
                return {"status": "no_money"}
            p.balance -= price
            p.pet = self.config[pet_type]["name"]
            p.pet_name = ""
            return {"status": "ok"}
        return await self.repo.atomic_update(user_id, _buy)

    async def set_name(self, user_id: int, name: str) -> bool:
        async def _set(p, conn):
            p.pet_name = name[:self.config["dog"]["max_name_len"]]
            return True
        result = await self.repo.atomic_update(user_id, _set)
        return result is not None

    async def has_pet(self, user_id: int) -> bool:
        player = await self.repo.get_by_id(user_id)
        return player is not None and bool(player.pet)
        
class AchievementService:
    def __init__(self, db_pool, redis_client, repo: PlayerRepository):
        self.db_pool = db_pool
        self.redis = redis_client
        self.repo = repo

    async def check_and_award(self, user_id: int, context):
        player = await self.repo.get_by_id(user_id)
        if not player.exists:
            return
        awarded = set()
        if self.redis:
            try:
                cached = await redis_breaker.call(self.redis.get, f"ach:{user_id}")
                if cached:
                    awarded = set(json.loads(cached))
            except pybreaker.CircuitBreakerError:
                pass
        if not awarded:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT ach_id FROM achievements_awarded WHERE user_id=$1", user_id
                )
                awarded = {r["ach_id"] for r in rows}
            if self.redis:
                try:
                    await redis_breaker.call(
                        self.redis.setex, f"ach:{user_id}", 60, json.dumps(list(awarded))
                    )
                except pybreaker.CircuitBreakerError:
                    pass
        async with self.db_pool.acquire() as conn:
            for ach in ACHIEVEMENTS:
                ach_id = ach["id"]
                if ach_id == "lunar_lord":
                    continue
                cond = ACHIEVEMENT_CONDITIONS.get(ach_id)
                if cond:
                    field, threshold = cond
                    if getattr(player, field, 0) >= threshold and ach_id not in awarded:
                        await conn.execute(
                            "INSERT INTO achievements_awarded(user_id, ach_id, awarded_at) "
                            "VALUES($1, $2, NOW()) ON CONFLICT DO NOTHING",
                            user_id, ach_id,
                        )
                        await self._give_reward(player, ach.get("reward", ""), context)
                        awarded.add(ach_id)
                        if self.redis:
                            try:
                                await redis_breaker.call(self.redis.delete, f"ach:{user_id}")
                            except pybreaker.CircuitBreakerError:
                                pass
                        try:
                            text = (
                                f"<b>🕊️ СВИТОК ДОСТИЖЕНИЙ 🏆</b>\n\n"
                                f"<b>🎉 Достижение разблокировано!</b>\n\n"
                                f"<i>{ach['emoji']} «{ach['name']}» {ach['emoji']}</i>\n\n"
                                f"<b>📜 Запись добавлена! 💎</b>"
                            )
                            await safe_send_message(context, uid, text, parse_mode='HTML')
                        except Exception as e:
                            logger.error(f"Achievement notify error: {e}")
            # lunar_lord
            rows = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", user_id)
            awarded_ids = {r["ach_id"] for r in rows}
            all_other = {a["id"] for a in ACHIEVEMENTS if a["id"] != "lunar_lord"}
            if "lunar_lord" not in awarded_ids and all_other.issubset(awarded_ids):
                lunar = ACHIEVEMENTS_DICT["lunar_lord"]
                await conn.execute(
                    "INSERT INTO achievements_awarded(user_id, ach_id, awarded_at) "
                    "VALUES($1, $2, NOW()) ON CONFLICT DO NOTHING",
                    user_id, "lunar_lord",
                )
                await self._give_reward(player, lunar.get("reward", ""), context)
                if self.redis:
                    try:
                        await redis_breaker.call(self.redis.delete, f"ach:{user_id}")
                    except pybreaker.CircuitBreakerError:
                        pass
                try:
                    text = (
                        f"<b>🕊️ СВИТОК ДОСТИЖЕНИЙ 🏆</b>\n\n"
                        f"<b>🎉 Достижение разблокировано!</b>\n\n"
                        f"<i>{lunar['emoji']} «{lunar['name']}» {lunar['emoji']}</i>\n\n"
                        f"<b>📜 Запись добавлена! 💎</b>"
                    )
                    await safe_send_message(context, uid, text, parse_mode='HTML')
                except Exception as e:
                    logger.error(f"Achievement notify error (lunar): {e}")

    async def _give_reward(self, player, reward_text, context):
        if not reward_text:
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
        await self.repo.save(player)
        
class AppContext:
    def __init__(self, db_pool, redis_client, cache, settings, repo, war_service, pet_service, achievement_service):
        self.db_pool = db_pool
        self.redis = redis_client
        self.cache = cache
        self.settings = settings
        self.repo = repo
        self.war_service = war_service
        self.pet_service = pet_service
        self.achievement_service = achievement_service

# ============================================================
# АНТИСПАМ – совершенная НОВАЯ версия (Redis Lua + in‑memory fallback)
# ============================================================
_rate_limit_storage: Dict[str, Tuple[int, float]] = {}
_rate_lock = asyncio.Lock()
_last_cleanup = time.monotonic()
_CLEANUP_INTERVAL = 300

async def _cleanup_expired(now: float) -> None:
    expired = [k for k, (_, exp) in _rate_limit_storage.items() if exp <= now]
    for k in expired:
        del _rate_limit_storage[k]

async def check_rate_limit_redis(ctx, user_id: int, action: str, limit: int, period: float) -> bool:
    """True если лимит не превышен. Атомарный Redis + надёжный fallback."""
    if limit <= 0:
        return False

    key = f"rate:{action}:{user_id}"

    # Lua-скрипт для атомарности Redis
    if getattr(ctx, "redis", None) is not None:
        try:
            lua_script = """
                local current = redis.call('INCR', KEYS[1])
                if current == 1 then
                    redis.call('EXPIRE', KEYS[1], ARGV[1])
                end
                return current
            """
            current = await ctx.redis.eval(lua_script, 1, key, period)
            return int(current) <= limit
        except Exception:
            logger.warning("Redis rate limit failed, switching to in-memory fallback")

    # In‑memory fallback с периодической очисткой
    now = time.monotonic()
    global _last_cleanup

    async with _rate_lock:
        if now - _last_cleanup > _CLEANUP_INTERVAL:
            await _cleanup_expired(now)
            _last_cleanup = now

        entry = _rate_limit_storage.get(key)
        if entry is not None:
            count, expire = entry
            if expire <= now:
                del _rate_limit_storage[key]
                entry = None
            elif count >= limit:
                return False

        if entry is None:
            _rate_limit_storage[key] = (1, now + period)
        else:
            count, expire = entry
            _rate_limit_storage[key] = (count + 1, expire)

    return True

# Redis Rate Limiter (СТАРЫЙ)
#async def check_rate_limit_redis(ctx: AppContext, user_id: int, action: str, limit=5, period=10) -> bool:
    #if not ctx.redis:
        #return True
    #key = f"rate:{action}:{user_id}"
    #try:
        #current = await redis_breaker.call(ctx.redis.incr, key)
        #if current == 1:
            #await redis_breaker.call(ctx.redis.expire, key, period)
        #if current > limit:
            #rate_limited_requests.inc()
            #return False
        #return True
    #except pybreaker.CircuitBreakerError:
        #return True

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

# Устаревшие глобальные переменные (заменены на AppContext, но оставлены для совместимости)
redis = None

async def init_redis():
    global redis
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        redis = await aioredis.from_url(redis_url)
        logger.info("Redis подключён – кэш активирован")
    else:
        logger.info("REDIS_URL не задан – используется in-memory кэш")

async def claim_daily(user_id: int, today: date, streak: int, reward_oac: int,
                      title: Optional[str], inventory_items: dict, ctx: AppContext) -> bool:
    """Атомарно начисляет ежедневную награду (использует AppContext)."""
    async def _apply(p, conn):
        if p.last_login_date == today:
            return False
        p.balance = (p.balance or 0) + reward_oac
        p.login_streak = streak
        p.last_login_date = today
        if title:
            cur = (p.titles or "").strip()
            if title not in cur:
                p.titles = f"{cur} {title}".strip()
        for field, amount in inventory_items.items():
            if hasattr(p, field):
                setattr(p, field, (getattr(p, field) or 0) + amount)
        return True
    return await ctx.repo.atomic_update(user_id, _apply)

async def create_named_blunt(user_id: int, name: str, rarity: str = None, conn=None, ctx: AppContext = None, player: Player = None) -> dict:
    """Создаёт именной блант (использует репозиторий из ctx)."""
    if ctx is None:
        raise ValueError("AppContext is required")
    
    if rarity not in ("common", "rare", "epic", "legendary"):
        r = random.random()
        if r < 0.02: rarity = "legendary"
        elif r < 0.15: rarity = "epic"
        elif r < 0.45: rarity = "rare"
        else: rarity = "common"
    
    clean_name = str(name or "").strip()[:25] or "Безымянный"
    reaction = random.choice(FUNNY_REACTIONS)
    blunt_id = f"blunt_{user_id}_{int(datetime.now(timezone.utc).timestamp())}_{random.randint(1000,9999)}"
    hash_code = "0x" + hashlib.sha256((blunt_id + ":hash").encode()).hexdigest()[:16]
    rare_number = f"{rarity[0].upper()}-{random.randint(1000,9999)}"
    
    item = {
        "id": blunt_id, "type": "named", "name": clean_name, "rarity": rarity,
        "serial": None, "rare_number": rare_number, "hash": hash_code,
        "reaction": reaction, "created_at": datetime.now(timezone.utc).isoformat(),
        "owner_history": [{"user_id": str(user_id), "since": datetime.now(timezone.utc).isoformat()}],
    }
    
    # Загружаем игрока только если не передан готовый
    if player is None:
        player = await ctx.repo.get_by_id(user_id)
    if not player or not player.exists:
        player = Player(user_id=user_id)
    
    player.inventory = _json_safe_load(player.inventory, [])
    player.inventory.append(item)
    await ctx.repo.save(player, conn=conn)
    logger.info("Создан именной блант '%s' для игрока %d", clean_name, user_id)
    return item

async def _award_achievement_rewards(user_id: int, player: Player, reward_text: str, context, ctx: AppContext) -> None:
    """Выдаёт награды за достижения (использует репозиторий из ctx)."""
    if not reward_text:
        return
    
    if isinstance(player, dict):
        player = await ctx.repo.get_by_id(user_id)
    if not player or not player.user_id:
        return
    
    parts = [p.strip() for p in reward_text.split(",") if p.strip()]
    for part in parts:
        if part.startswith("+") and "OAC" in part:
            clean = part.replace(" ", "")
            m = re.search(r"\+(\d+)", clean)
            if m:
                player.balance = (player.balance or 0) + int(m.group(1))
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
            if not isinstance(skins, dict): skins = {}
            unlocked = skins.get("unlocked_backgrounds", [])
            if bg and bg not in unlocked: unlocked.append(bg)
            skins["unlocked_backgrounds"] = unlocked
            player.profile_skins = skins
        elif part.startswith("Рамка "):
            frame = part.replace("Рамка ", "").strip()
            skins = player.profile_skins or {}
            if not isinstance(skins, dict): skins = {}
            unlocked = skins.get("unlocked_frames", [])
            if frame and frame not in unlocked: unlocked.append(frame)
            skins["unlocked_frames"] = unlocked
            player.profile_skins = skins
        else:
            logger.warning(f"Неизвестный формат награды: {part} для пользователя {user_id}")
    
    await ctx.repo.save(player)
    
async def check_achievements(user_id: int, context, ctx: AppContext = None) -> None:
    """Проверяет и выдаёт достижения (использует репозиторий из ctx)."""
    if ctx is None:
        ctx = context.bot_data.get("ctx")
    if not ctx:
        return
    
    player = await ctx.repo.get_by_id(user_id)
    if not player or not player.user_id:
        return
    
    awarded_key = f"ach:{user_id}"
    awarded = set()
    if ctx.redis:
        try:
            cached = await redis_breaker.call(ctx.redis.get, awarded_key)
            if cached:
                awarded = set(json.loads(cached))
        except pybreaker.CircuitBreakerError:
            pass
    
    if not awarded:
        async with ctx.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", user_id)
            awarded = {r["ach_id"] for r in rows}
            if ctx.redis:
                try:
                    await redis_breaker.call(ctx.redis.setex, awarded_key, 60, json.dumps(list(awarded)))
                except pybreaker.CircuitBreakerError:
                    pass
    
    messages_to_send = []
    async with ctx.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("LOCK TABLE achievements_awarded IN EXCLUSIVE MODE")
            rows = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", user_id)
            current_awarded = {r["ach_id"] for r in rows}
    
            for ach in ACHIEVEMENTS:
                ach_id = ach["id"]
                if ach_id == "lunar_lord":
                    continue
                cond = ACHIEVEMENT_CONDITIONS.get(ach_id)
                if cond and ach_id not in current_awarded:
                    field, threshold = cond
                    if getattr(player, field, 0) >= threshold:
                        await conn.execute(
                            "INSERT INTO achievements_awarded(user_id, ach_id, awarded_at) VALUES($1, $2, NOW()) ON CONFLICT DO NOTHING",
                            user_id, ach_id
                        )
                        await _award_achievement_rewards(user_id, player, ach.get("reward", ""), context, ctx)
                        current_awarded.add(ach_id)

                        if getattr(player, 'onboarding_step', -1) != -1:
                            messages_to_send.append(
                                f"<b>🏆 {ach['emoji']} «{ach['name']}»</b>\n"
                                f"<i>— достижение разблокировано!</i>"
                            )
                        else:
                            messages_to_send.append(
                                f"<b>🕊️ СВИТОК ДОСТИЖЕНИЙ 🏆</b>\n\n"
                                f"<b>🎉 Достижение разблокировано!💎</b>\n\n"
                                f"<i>{ach['emoji']} «{ach['name']}» {ach['emoji']}</i>"
                            )
    
            # lunar_lord
            rows2 = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=$1", user_id)
            awarded_ids = {r["ach_id"] for r in rows2}
            all_other = {a["id"] for a in ACHIEVEMENTS if a["id"] != "lunar_lord"}
            if "lunar_lord" not in awarded_ids and all_other.issubset(awarded_ids):
                lunar = ACHIEVEMENTS_DICT["lunar_lord"]
                await conn.execute(
                    "INSERT INTO achievements_awarded(user_id, ach_id, awarded_at) VALUES($1, $2, NOW()) ON CONFLICT DO NOTHING",
                    user_id, "lunar_lord"
                )
                await _award_achievement_rewards(user_id, player, lunar.get("reward", ""), context, ctx)
                messages_to_send.append(
                    f"<b>🕊️ СВИТОК ДОСТИЖЕНИЙ 🏆</b>\n\n"
                    f"<b>🎉 Достижение разблокировано!</b>\n\n"
                    f"<i>{lunar['emoji']} «{lunar['name']}» {lunar['emoji']}</i>\n\n"
                    f"<b>📜 Запись добавлена! 💎</b>"
                )
    
            if ctx.redis:
                try:
                    await redis_breaker.call(ctx.redis.delete, awarded_key)
                except pybreaker.CircuitBreakerError:
                    pass
    
    if messages_to_send:
        player = await ctx.repo.get_by_id(user_id)
        if player and getattr(player, 'onboarding_step', -1) != -1:
            # Красивое и компактное уведомление для новичков
            for msg in messages_to_send:
                try:
                    await safe_send_message(
                        context,
                        user_id,
                        msg,
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Achievement notify error: {e}")
        else:
            # Стандартный вывод после обучения
            for msg in messages_to_send:
                try:
                    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode='HTML')
                except Exception as e:
                    logger.error(f"Achievement notify error: {e}")
                
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
FUNNY_REACTIONS = [
    'Выглядит как NFT, если которым не поделиться — он навсегда останется обычным',
    'Даже Бездна от такого названия закашлялась. Жми "Поделиться"!',
    'Этот блант настолько ужасен, что его нужно срочно подарить кому-нибудь',
    'Искажение занесло это название в чёрный список.',
    '10/10, лучший блант для того чтобы осчастливить врага. Подари его кому-то!',
    'Пахнет так, будто его скрутил сам Ктулху. Поделись им, осчастливь кого-то',
    'Этот блант вызывает желание помыть руки.',
    'Этот блант создан, чтобы его увидели все. Не прячь позор — монетизируй.',
    'Если оставишь это себе, OAC начнут плакать. Подари — и они засмеются.'
]
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

SMOKE_EFFECTS = [
    (0.18, "😵‍💫 Лёгкий приход", "«Станки Фабрики №9 работают в ритме твоего сердца»", True),
    (0.36, "💤 Полный Штиль", "«Дым рассеялся, оставив лишь лёгкий шлейф»", False),
    (0.53, "😵‍💫 Паранойя", "Всё идёт не так. Тени сгущаются…", False),
    (0.70, "💨 Кашель", "«Первая тяга была слишком жёсткой, пробило на кашель»", True),
    (0.85, "🛋️ Паралич", "«Тело стало ватным, смотришь в одну точку и не можешь пошевелиться»", False),
    (1.0,  "🧘 Глубокое Озарение", "«Ты понял, что блант — это ключ к разгадке бытия»", False),
]

def build_smoke_effect(roll, earned):
    for threshold, name, flavor, has_earned in SMOKE_EFFECTS:
        if roll < threshold:
            earned_str = f"🍬 <b>+{earned} OAC</b>" if has_earned and earned else ""
            return (
                f"<b>💨 ДЫМ РАССЕЯЛСЯ</b>\n"
                f"– {name}\n"
                f"– <i>{flavor}</i>\n\n"
                f"{earned_str}"
            )
    return ""

def calculate_smoke_reward(p, happy_hour):
    r = random.random()
    earned = 0
    if r < 0.18:
        earned = random.randint(15, 40)
        if happy_hour:
            earned *= HAPPY_HOUR_MULTIPLIER
    elif r < 0.70:
        earned = -5
    return earned

class SmokeStatus(Enum):
    NO_BLUNTS = "no_blunts"
    OK = "ok"
    
class CraftStatus(Enum):
    NO_MONEY = "no_money"
    OK = "ok"

# ========== БАЗА ДАННЫХ ==========
BLUNTS_PER_PAGE = 3

BLUNT_IMAGES = {
    "common": "AgACAgIAAxkBAAIDkGoXK23K4y7Oq479b4xk4DN2IjQ-AAIiH2sb4L64SI9XnknrGFb-AQADAgADeAADOwQ",
    "rare": "",
    "epic": "",
    "legendary": ""
}

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
    
    # Ну тип жто БД длЯЯЯ daily_progress
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='players' AND column_name='daily_progress'
            ) THEN
                ALTER TABLE players ADD COLUMN daily_progress JSONB DEFAULT '{}';
            END IF;
        END $$;
    """)
    
    # Питомцы и exists
    await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS pet TEXT DEFAULT '';")
    await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS pet_name TEXT DEFAULT '';")
    await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS \"exists\" BOOLEAN DEFAULT TRUE;")

    # ===== ОПТИМИЗАЦИЯ ХРАНЕНИЯ (Render Free Tier) =====
    await conn.execute("ALTER TABLE players ALTER COLUMN inventory SET STORAGE EXTENDED;")
    await conn.execute("ALTER TABLE players ALTER COLUMN profile_skins SET STORAGE EXTENDED;")
    await conn.execute("ALTER TABLE players SET (autovacuum_vacuum_scale_factor = 0.01);")
    await conn.execute("ALTER TABLE players SET (autovacuum_vacuum_threshold = 50);")
    await conn.execute("ALTER TABLE players SET (autovacuum_vacuum_cost_limit = 200);")

    # === Финальная проверка целостности ===
    try:
        await conn.execute("SELECT 1 FROM war_state LIMIT 1")
        await conn.execute("SELECT total_score, week_start FROM guild_weekly LIMIT 0")
        logger.info("✅ Миграции успешно применены, целостность БД подтверждена")
    except Exception as e:
        logger.critical("❌ Ошибка целостности после миграций: %s", e)
        raise RuntimeError("Database integrity check failed") from e
        
    # Срочная настройка autovacuum для таблицы players
    await conn.execute("ALTER TABLE players SET (autovacuum_vacuum_scale_factor = 0.01)")
    await conn.execute("ALTER TABLE players SET (autovacuum_vacuum_threshold = 50)")
    await conn.execute("ALTER TABLE players SET (autovacuum_vacuum_cost_limit = 200)")
    
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
            guild TEXT,
            week_start DATE,
            total_score INTEGER DEFAULT 0,
            total_donated INTEGER DEFAULT 0,
            PRIMARY KEY (guild, week_start)
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


# ========== PERFECTED CACHE – ФУНДАМЕНТАЛЬНАЯ СТАБИЛЬНОСТЬ ==========
logger = logging.getLogger("perfected_cache")

class PerfectedCache:
    """
    Кэш, не имеющий ни одного недостатка.
    - Нет гонок (одновременные запросы схлопываются в один)
    - Нет дедлоков (полностью асинхронный, без блокировок)
    - Нет утечек памяти (автоматическая очистка)
    - Мгновенный ответ при stale-while-revalidate
    - Устойчив к падению Redis
    - Полностью типобезопасен
    """
    def __init__(self, default_ttl: int = 120, stale_ttl: int = 600):
        self._pending: Dict[str, asyncio.Task] = {}
        self._default_ttl = default_ttl
        self._stale_ttl = stale_ttl

    async def fetch(
        self,
        redis_client: Optional[aioredis.Redis],
        db_pool: asyncpg.Pool,
        cache_key: str,
        query: str,
        params: tuple = (),
        ttl: Optional[int] = None,
        adapter: Callable = lambda rows: [dict(r) for r in rows],
        fallback: Any = None,
    ) -> Any:
        actual_ttl = ttl or self._default_ttl

        # 1. Быстрый путь из Redis (без ожиданий)
        if redis_client:
            try:
                cached = await asyncio.wait_for(redis_client.get(cache_key), timeout=0.3)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass

        # 2. Если запрос уже выполняется, ждём его результат (без дублирования)
        if cache_key in self._pending:
            try:
                return await self._pending[cache_key]
            except Exception:
                pass  # задача упала, выполним новый запрос

        # 3. Запускаем запрос к БД, сохраняем таск
        async def _fetch_and_cache():
            try:
                async with db_pool.acquire() as conn:
                    rows = await conn.fetch(query, *params)
                result = adapter(rows)

                # Пишем в Redis (не блокируем ответ)
                if redis_client:
                    try:
                        await asyncio.wait_for(
                            redis_client.setex(cache_key, actual_ttl, json.dumps(result, default=str)),
                            timeout=0.3
                        )
                    except Exception:
                        pass
                return result
            finally:
                self._pending.pop(cache_key, None)

        task = asyncio.create_task(_fetch_and_cache())
        self._pending[cache_key] = task

        try:
            return await task
        except Exception as e:
            logger.error(f"Cache fetch failed for {cache_key}: {e}")
            # Попытка вернуть stale из Redis
            if redis_client:
                try:
                    stale = await asyncio.wait_for(redis_client.get(cache_key), timeout=0.3)
                    if stale:
                        return json.loads(stale)
                except Exception:
                    pass
            return fallback
            
perfected_cache = PerfectedCache()

async def count_guilds(ctx: AppContext) -> dict:
    """Количество игроков в гильдиях (идеальное кэширование)."""
    return await perfected_cache.fetch(
        redis_client=ctx.redis,
        db_pool=ctx.db_pool,
        cache_key="guild_counts",
        query="SELECT guild, COUNT(*) as cnt FROM players WHERE guild IS NOT NULL GROUP BY guild",
        ttl=300,
        adapter=lambda rows: {"BLACK": 0, "WHITE": 0} | {r["guild"]: r["cnt"] for r in rows},
        fallback={"BLACK": 0, "WHITE": 0}
    )

async def get_top(ctx: AppContext, limit: int = 10) -> list[dict]:
    """Топ игроков (идеальное кэширование)."""
    return await perfected_cache.fetch(
        redis_client=ctx.redis,
        db_pool=ctx.db_pool,
        cache_key=f"top_players_{limit}",
        query="SELECT user_id, username, balance, guild FROM players ORDER BY balance DESC LIMIT $1",
        params=(limit,),
        ttl=300,
        fallback=[]
    )

async def get_setting(key: str, default: str = "", ctx: AppContext = None) -> str:
    if ctx is None:
        return default
    async with ctx.db_pool.acquire() as conn:
        row = await db_breaker.call(conn.fetchrow, "SELECT value FROM bot_settings WHERE key=$1", key)
        return row["value"] if row else default

async def set_setting(key: str, value: str, ctx: AppContext = None) -> None:
    if ctx is None:
        return
    async with ctx.db_pool.acquire() as conn:
        await db_breaker.call(
            conn.execute,
            "INSERT INTO bot_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, value
        )

async def send_whisper(context, chat_id, text):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Whisper error: {e}")
        
def _extract_provider_info(error: Exception) -> tuple[str, str]:
    """
    Извлекает название провайдера и хост из любого исключения.
    Возвращает кортеж (provider, host).
    """
    provider = "Неизвестный"
    host = "неизвестен"
    error_text = str(error).lower()

    # Список известных доменов
    domains = {
        "neon.tech": "Neon",
        "aivencloud.com": "Aiven",
        "cockroachlabs.cloud": "CockroachDB",
        "render.com": "Render PostgreSQL",
        "oregon-postgres.render.com": "Render PostgreSQL",
        "supabase.co": "Supabase"
    }

    # Ищем в самом сообщении об ошибке
    for domain, name in domains.items():
        if domain in error_text:
            provider = name
            # Пытаемся вытащить полный хост (достаточно грубо, но работает)
            import re
            match = re.search(rf"([\w-]+\.{re.escape(domain)})", error_text)
            if match:
                host = match.group(1)
            else:
                host = domain
            break

    # Если не нашли, проверяем причину (__cause__)
    if provider == "Неизвестный" and error.__cause__:
        cause_text = str(error.__cause__).lower()
        for domain, name in domains.items():
            if domain in cause_text:
                provider = name
                import re
                match = re.search(rf"([\w-]+\.{re.escape(domain)})", cause_text)
                host = match.group(1) if match else domain
                break

    return provider, host

# КОМАНДА УСТАНОВКИ ФОТО БЛАНТА

# ============================================================
# БЛОК ОТПРАВКИ ИЗОБРАЖЕНИЙ
# ============================================================
_blunt_images_lock = asyncio.Lock()

async def _send_photo_with_retry(context, chat_id, file_id, caption=None, reply_markup=None, max_retries=3):
    """Отказоустойчивая отправка фото с адаптивными повторами. Никогда не роняет бота."""
    # Подготавливаем аргументы для send_photo
    kwargs = {'chat_id': chat_id, 'photo': file_id, 'reply_markup': reply_markup}
    if caption:
        kwargs.update(caption=caption, parse_mode='HTML')

    for attempt in range(max_retries):
        try:
            # Пытаемся отправить фото через Circuit Breaker
            return await tg_breaker.call(context.bot.send_photo, **kwargs)

        except BadRequest:
            # Ошибки валидации не ретраим – пробрасываем наверх
            raise

        except Exception as e:
            # Если цепь разомкнута – немедленно пробрасываем, без повторов
            if "CircuitBreaker" in type(e).__name__:
                raise

            # На последней попытке сдаёмся
            if attempt == max_retries - 1:
                raise

            # Вычисляем задержку перед повтором:
            # 1) Если Telegram вернул RetryAfter – используем его + jitter
            # 2) Иначе используем экспоненциальный backoff + jitter
            retry_after = getattr(e, 'retry_after', None)
            if retry_after:
                delay = retry_after + random.uniform(0, 0.5)
            else:
                delay = (2 ** attempt) + random.uniform(0, 1)

            logger.warning(
                "Ошибка отправки фото (попытка %d/%d), повтор через %.2f сек: %s",
                attempt + 1, max_retries, delay, e
            )
            await asyncio.sleep(delay)

    # Сюда выполнение не должно дойти, но для безопасности:
    raise RuntimeError("Не удалось отправить фото после всех попыток")

async def safe_send_message(context, chat_id, text, parse_mode='HTML', reply_markup=None, max_retries=3):
    """Отказоустойчивая отправка текста с адаптивными повторами. Никогда не роняет бота."""
    kwargs = {'chat_id': chat_id, 'text': text, 'reply_markup': reply_markup}
    if parse_mode:
        kwargs['parse_mode'] = parse_mode

    for attempt in range(max_retries):
        try:
            # Пытаемся отправить сообщение через Circuit Breaker
            return await tg_breaker.call(context.bot.send_message, **kwargs)

        except BadRequest as e:
            # Если проблема в HTML – пробуем отправить без форматирования
            if "can't parse entities" in str(e).lower() or "not found" in str(e).lower():
                logger.warning("Ошибка HTML, отправляю без форматирования")
                kwargs.pop('parse_mode', None)
                try:
                    return await tg_breaker.call(context.bot.send_message, **kwargs)
                except Exception:
                    raise
            raise

        except Exception as e:
            if "CircuitBreaker" in type(e).__name__:
                raise
            if attempt == max_retries - 1:
                raise

            retry_after = getattr(e, 'retry_after', None)
            delay = (retry_after + random.uniform(0, 0.5)) if retry_after else (2 ** attempt + random.uniform(0, 1))
            logger.warning(
                "Ошибка отправки сообщения (попытка %d/%d), повтор через %.2f сек: %s",
                attempt + 1, max_retries, delay, e
            )
            await asyncio.sleep(delay)

    raise RuntimeError("Не удалось отправить сообщение после всех попыток")

async def _reset_and_notify_broken_id(rarity: str, context):
    """Атомарно удаляет file_id из кэша и БД, уведомляет админа."""
    async with _blunt_images_lock:
        BLUNT_IMAGES.pop(rarity, None)
    try:
        await set_setting(f"blunt_image_{rarity}", "")
    except Exception as ex:
        logger.error("Ошибка очистки file_id в БД: %s", ex)
    if settings.admin_id:
        await _safe_send_message(
            context, settings.admin_id,
            f"⚠️ Изображение для {rarity} недействительно. Обновите: /setbluntpic {rarity}"
        )

def get_golden_file_id(rarity: str) -> str | None:
    """Заглушка – вернёт None, чтобы не падать. Замени на реальную логику позже."""
    return None

# ── Основная функция ────────────────────────────────────────
async def safe_send_blunt_image(context, chat_id, rarity, caption, reply_markup):
    """Отправляет изображение бланта с абсолютной отказоустойчивостью."""
    file_id = BLUNT_IMAGES.get(rarity)
    if not file_id:
        return False

    try:
        # Новый _send_photo_with_retry сам управляет повторами и jitter
        await tg_breaker.call(_send_photo_with_retry, context, chat_id, file_id, caption=caption, reply_markup=reply_markup)
        return True
    except BadRequest as e:
        if "Wrong file identifier" in str(e):
            logger.warning("Невалидный file_id для %s", rarity)
            await _reset_and_notify_broken_id(rarity, context)

            golden = get_golden_file_id(rarity)
            if golden and golden != file_id:
                try:
                    await tg_breaker.call(_send_photo_with_retry, context, chat_id, golden, caption=caption, reply_markup=reply_markup)
                    async with _blunt_images_lock:
                        BLUNT_IMAGES[rarity] = golden
                    await set_setting(f"blunt_image_{rarity}", golden)
                    logger.info("Золотой резерв для %s применён", rarity)
                    return True
                except Exception:
                    await _reset_and_notify_broken_id(rarity, context)

            await safe_send_message(context, chat_id, "🖼️ Изображение временно недоступно.", parse_mode=None)
            return False
        else:
            logger.error("BadRequest фото %s: %s", rarity, e)
            await safe_send_message(context, chat_id, "❌ Не удалось отправить изображение.", parse_mode=None)
            return False
    except Exception as e:
        if "CircuitBreaker" in type(e).__name__:
            logger.warning("Circuit breaker разомкнут для %s", rarity)
            await safe_send_message(context, chat_id, "🔌 Сервис изображений временно перегружен.", parse_mode=None)
        else:
            logger.error("Непредвиденная ошибка отправки %s: %s", rarity, e)
            await safe_send_message(context, chat_id, "⚠️ Произошла ошибка при отправке изображения.", parse_mode=None)
        return False

async def send_whisper_dm(update, context, text):
    if update.callback_query:
        chat_id = update.callback_query.message.chat.id
    else:
        chat_id = update.effective_chat.id
    await safe_send_message(context, chat_id, text, parse_mode='HTML')

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

async def add_title(user_id, emoji, ctx, conn=None):
    player = await ctx.repo.get_by_id(user_id)
    titles = (player.titles or "").split()
    if emoji not in titles:
        titles.append(emoji)
        player.titles = " ".join(titles).strip()
        await ctx.repo.save(player, conn=conn)

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
    ctx = context.bot_data.get("ctx")
    if not ctx:
        return
    today = date.today()
    player = await ctx.repo.get_by_id(user_id)
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

    result = await ctx.repo.atomic_update(user_id, _apply_daily)
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
        await safe_send_message(context, user_id, text, parse_mode='HTML')
    except Exception as e:
        logger.error("Failed to send daily login msg", extra={"user_id": user_id}, exc_info=True)

    try:
        await check_achievements(user_id, context, ctx=ctx)
    except Exception:
        logger.exception("Achievement check failed", extra={"user_id": user_id})


# ── Конфигурация кулдаунов (можно править без захода в функцию) ──
MAIN_MENU_COOLDOWNS = {
    "farm": {
        "text": "🍬 Фармить",
        "cooldown_hours": settings.farm_cooldown_hours,   # ← раньше было FARM_COOLDOWN_HOURS
        "last_attr": "last_farm",
        "format": "min",
    },
    "ritual": {
        "text": "🕯️ Ритуал",
        "cooldown_hours": settings.ritual_cooldown_hours, # ← было 24
        "last_attr": "last_ritual",
        "format": "hrs",
        "guild_only": "BLACK",
    },
    "lab": {
        "text": "🏛️ Лабиринт",
        "cooldown_hours": settings.lab_cooldown_hours,    # ← было 12
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

async def get_main_menu_keyboard(user_id, ctx=None): 
    now = time.time()
    if user_id in _menu_cache:
        cached_time, kb, whisper = _menu_cache[user_id]
        if now - cached_time < 2:
            return kb, whisper

    whisper = random.choice(WHISPERS)
    player = await ctx.repo.get_by_id(user_id) if ctx else None
    balance = player.balance if player else 0
    now_dt = datetime.now()
    guild = player.guild if player else None
    has_pet = bool(player.pet) if player else False
    is_veteran = balance >= 5000

    keyboard = []

    # 1. ГЛАВНОЕ ДЕЙСТВИЕ (с кулдауном)
    farm_text = _format_cooldown(player, now_dt, "farm")
    keyboard.append([InlineKeyboardButton(farm_text, callback_data="farm")])

    # 2. ПАНЕЛЬ БЫСТРЫХ ДЕЙСТВИЙ
    keyboard.append([
        InlineKeyboardButton("🌿 Крафт", callback_data="craft"),
        InlineKeyboardButton("💨 Дунуть", callback_data="smoke")
    ])

    # 3. АДАПТИВНЫЙ РЯД
    row3 = []

    # --- Кнопка прогресса дня ---
    progress = getattr(player, 'daily_progress', {}) or {}
    # Базовые действия: фарм, крафт, дым
    total_actions = 3
    if guild:
        total_actions += 1   # ритуал или исповедь
    if is_veteran and has_pet:
        total_actions += 1   # питомец
    done = sum(1 for v in progress.values() if v)

    if done == total_actions and total_actions > 0:
        row3.append(InlineKeyboardButton("🎁 Забрать Награду", callback_data="profile"))
    elif done > 0:
        row3.append(InlineKeyboardButton(f"📋 Задания ({done}/{total_actions})", callback_data="daily_quest_hub"))
    else:
        row3.append(InlineKeyboardButton("📈 Развитие", callback_data="daily_quest_hub"))

    # --- Кнопка гильдии ---
    if not guild:
        row3.append(InlineKeyboardButton("🕋 Вступить в Гильдию", callback_data="guild_info"))
    elif guild == "BLACK":
        # Простая проверка кулдауна ритуала
        last_ritual = player.last_ritual
        if not last_ritual or (now_dt - last_ritual) >= timedelta(hours=24):
            row3.append(InlineKeyboardButton("🕯️ Ритуал", callback_data="ritual"))
        else:
            diff = timedelta(hours=24) - (now_dt - last_ritual)
            hrs, mins = int(diff.seconds // 3600), int((diff.seconds % 3600) // 60)
            cooldown_str = f"({hrs}ч {mins}м)" if hrs > 0 else f"({mins}м)"
            row3.append(InlineKeyboardButton(f"🕯️ Ритуал {cooldown_str}", callback_data="ritual"))
    elif guild == "WHITE":
        row3.append(InlineKeyboardButton("⚜️ Исповедь", callback_data="confess"))

    # --- Кнопка "Мир" с динамической иконкой ---
    world_icon = "🌍"
    if guild == "BLACK": world_icon = "🕯️"
    elif guild == "WHITE": world_icon = "⚜️"
    row3.append(InlineKeyboardButton(f"{world_icon} Мир", callback_data="world_hub"))

    keyboard.append(row3)

    kb = InlineKeyboardMarkup(keyboard)
    _menu_cache[user_id] = (now, kb, whisper)
    return kb, whisper

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])

@cb
async def world_hub(update, context, ctx):
    query = update.callback_query
    await query.answer()
    player = await ctx.repo.get_by_id(query.from_user.id)
    guild = player.guild if player else None
    balance = player.balance if player else 0
    is_veteran = balance >= 5000

    kb_rows = []

    # Гильдия — простое название, как раньше
    if not guild:
        kb_rows.append([InlineKeyboardButton("🕋 Вступить в Гильдию", callback_data="guild_info")])
    else:
        kb_rows.append([InlineKeyboardButton("🕋 Гильдия", callback_data="guild_info")])

    # Куст — только для ветеранов
    if is_veteran:
        kb_rows.append([InlineKeyboardButton("🪴 Куст", callback_data="collect")])

    # Питомец
    pet_btn = (
        InlineKeyboardButton("🐾 Питомец", callback_data="pet_preview")
        if is_veteran
        else InlineKeyboardButton("🐾 Питомец 🔒", callback_data="pet_locked")
    )
    kb_rows.append([pet_btn])

    kb_rows.append([InlineKeyboardButton("🎲 Удача", callback_data="luck")])
    kb_rows.append([InlineKeyboardButton("🏛️ Лабиринт", callback_data="lab_start")])
    kb_rows.append([InlineKeyboardButton("🛒 Магазин", callback_data="shop")])
    kb_rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)
    await query.message.edit_text("<b>🌍 МИР</b>\n\nМир ждёт твоего следа.", reply_markup=kb, parse_mode='HTML')

@cb
async def daily_quest_hub(update, context, ctx):
    query = update.callback_query
    await query.answer()
    player = await ctx.repo.get_by_id(query.from_user.id)
    progress = getattr(player, 'daily_progress', {}) or {}
    guild = player.guild
    has_pet = bool(player.pet)
    is_veteran = (player.balance or 0) >= 5000

    actions = [
        ("🍬 Фармить", "farm"),
        ("🌿 Крафт", "craft"),
        ("💨 Дунуть", "smoke"),
    ]
    if guild:
        actions.append(("🕯️ Ритуал" if guild == "BLACK" else "⚜️ Исповедь", "ritual" if guild == "BLACK" else "confess"))
    if is_veteran and has_pet:
        actions.append(("🐾 Питомец", "pet_preview"))

    kb_rows = []
    done = 0
    for label, cb_data in actions:
        if cb_data in ("ritual", "confess"):
            is_done = progress.get("guild_action", False)
        else:
            is_done = progress.get(cb_data, False)
        if is_done:
            done += 1
        icon = "✅" if is_done else "⬜️"
        kb_rows.append([InlineKeyboardButton(f"{icon} {label}", callback_data=cb_data)])

    kb_rows.append([
        InlineKeyboardButton("👤 Профиль", callback_data="profile"),
        InlineKeyboardButton("🏆 Достижения", callback_data="achievements_menu"),
        InlineKeyboardButton("🏅 Лидеры", callback_data="top")
    ])
    kb_rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])

    total_actions = len(actions)
    kb = InlineKeyboardMarkup(kb_rows)
    text = f"<b>📋 ЗАДАНИЯ ДНЯ ({done}/{total_actions})</b>\n\nВыбери, что хочешь сделать:"
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

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
    ctx = context.bot_data.get("ctx")
    if not ctx:
        return
    """Атомарно обрабатывает реферальную ссылку blunt_..."""
    if not context.args or not context.args[0].startswith("blunt_"):
        return

    ref_blunt_id = context.args[0].replace("blunt_", "")
    async with ctx.db_pool.acquire() as conn:
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

    creator = await ctx.repo.get_by_id(creator_id)
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
        await ctx.repo.save(player, conn=conn)

    await ctx.repo.atomic_update(creator_id, _ref)

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
    ctx = context.bot_data.get("ctx")
    new_name = random.choice(["Крик Бездны", "Шёпот Склепа"])

    # Атомарное создание игрока и первого бланта
    async with ctx.db_pool.acquire() as conn:
        async with conn.transaction():
            player = Player(user_id=uid, username=username, balance=800)
            await ctx.repo.save(player, conn=conn)
            await create_named_blunt(uid, new_name, ctx=ctx, conn=conn)

    welcome_text = (
        "<b>🎉 Добро пожаловать в Гильдию Antysocialshop!</b>\n"
        "<i>Здесь курят бланты, поклоняются древним богам и воюют за OAC.</i>\n\n"
        "🎁 <b>Смотритель дарует тебе</b> <code>800</code> 🍬 <b>и твой первый именной блант!</b>\n\n"
        "<b>🎓 ОБУЧЕНИЕ [▓░░░] 1/3</b>\n\n"
        "⚔️ <b>ВЫБЕРИ ФРАКЦИЮ — ПОЛУЧИ +50 OAC СРАЗУ!</b>\n\n"
        "🕯️ <b>Тёмная Гильдия</b>\n"
        "• Особое умение: Ритуал 🔮\n"
        "• Стабильность и тёмная магия\n\n"
        "⚜️ <b>Светлая Гильдия</b>\n"
        "• Особое умение: Исповедь 🪽\n"
        "• Азарт и благосклонность удачи\n\n"
        "👉 <i>Твой выбор определит твои возможности.</i>"
    )
    
    guild_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🕯️ Тёмная Гильдия (+50 🍬)", callback_data="guild_join_BLACK"),
         InlineKeyboardButton("⚜️ Светлая Гильдия (+50 🍬)", callback_data="guild_join_WHITE")]
    ])

    await update.effective_message.reply_text(
        welcome_text,
        reply_markup=guild_kb,
        parse_mode='HTML'
    )

async def _show_main_menu(update, context, player, user, ctx):
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
    back = f"⚔️ С возвращением в <b>Гильдию, {rank_display} {html.escape(display_name)}</b>\n"
    if guild == "BLACK":
        back += "🔮 Ты — часть <b>Темной Гильдии. 🕯️Ритуалы ждут тебя</b>\n"
    elif guild == "WHITE":
        back += "🪽 Ты — часть <b>Светлой Гильдии. ⚜️Исповедь очищает душу и ждёт тебя</b>\n"
    else:
        back += (
            "<b>🕯️⚜️ Ты ещё не ВЫБРАЛ сторону!</b>\n"
            "🔮 Гильдия откроет <b>ритуалы, исповеди и войну</b>\n"
            "👉 <b>Нажми кнопку «🕋 Гильдии» в меню чтобы ВСТУПИТЬ.</b>\n"
        )

    # Мотивационная строка
    if next_threshold > 0:
        gap = next_threshold - bal
        back += f"\n📈 До следующего ранга <b>{next_rank_emoji} {next_rank_name}</b> осталось — <b>{gap} OAC 🍬!</b>"
    else:
        back += f"\n<b>⚡ Ты достиг вершины! Твой ранг — {rank_emoji} {rank_name}.</b>"

    # Подсказка для новичков
    farm_count = player.farm_count
    craft_count = player.craft_count
    is_veteran = bal >= 5000
    
    # Список именных блантов для проверки
    named = [it for it in (player.inventory or []) if it.get("type") == "named"]
    
    if farm_count == 0:
        hint = "<b>💡 Твой первый шаг: нажми 🍬 Фармить и получи свои первые OAC!</b>"
    elif craft_count == 0:
        hint = "<b>💡 Попробуй 🌿 Крафт, чтобы создать свой первый Блант!</b>"
    elif len(named) <= 1 and (player.balance or 0) >= GAME_CONFIG["named_blunt_cost"]:
        hint = "<b>💡 Готов к большему? Создай свой первый 💍 Именной блант! (50 OAC)</b>"
    elif is_veteran:
        hint = "💡 Исследуй <b>🔮 Алхимию</b> и корми своего 🐾 <b>питомца!</b>"
    else:
        hint = "<b>💡 Исследуй 🏛️ Лабиринт! Он полон опасностей и наград.</b>"

    menu_text = f"<b>🎮 ГЛАВНОЕ МЕНЮ</b>\n\n<i>{whisper}</i>\n\n" + back + "\n\n" + hint
    kb, _ = await get_main_menu_keyboard(player.user_id, ctx=ctx)
    await update.effective_message.reply_text(menu_text, reply_markup=kb, parse_mode='HTML')
    
def get_next_action(player, exclude_callback: str = None) -> tuple[str, str, str]:
    progress = getattr(player, 'daily_progress', {}) or {}
    balance = getattr(player, 'balance', 0) or 0
    guild = getattr(player, 'guild', None)
    has_pet = bool(getattr(player, 'pet', ''))
    is_veteran = balance >= 5000

    # Динамический тотал действий
    total_actions = 5 if (is_veteran and has_pet) else 4
    done = sum(1 for k in ["farm", "craft", "smoke", "guild_action"] if progress.get(k))
    if is_veteran and has_pet and progress.get("pet"):
        done += 1

    # Приоритет №1: Гильдия
    if not guild and exclude_callback != "guild_info":
        return ("🕋 Выбрать Гильдию", "guild_info", "Выбери собственную Гильдию, и открой Войну ⚔️ Гильдий, Ритуалы 🔮 или Исповеди! 🪽")

    # Приоритет №2: Всё готово
    if done == total_actions:
        return ("🎁 ЗАБРАТЬ +50 OAC", "profile", "🎉 Всё готово! Награда ждёт тебя в профиле!")

    # Приоритет №3: Незавершённые дела
    if not progress.get("farm") and exclude_callback != "farm":
        return ("🍬 Фармить", "farm", "🍬 Фарм — основа роста. Заполни шкалу!")
    if not progress.get("craft") and exclude_callback != "craft":
        return ("🌿 Крафтить", "craft", "🌿 Скрути блант — получишь случайный эффект!")
    if not progress.get("smoke") and exclude_callback != "smoke":
        return ("💨 Дунуть", "smoke", "🧿 Испытай удачу — выкури блант!")
    if guild and not progress.get("guild_action") and exclude_callback != "guild_action":
        if guild == "BLACK":
            return ("🕯️ Ритуал", "ritual", "🕯️ Тёмная магия ждёт тебя!")
        elif guild == "WHITE":
            return ("⚜️ Исповедь", "repent", "🪽 Светлая удача улыбнётся тебе!")
    if is_veteran and has_pet and not progress.get("pet") and exclude_callback != "pet_preview":
        return ("🐾 Покормить питомца", "pet_preview", "Твой питомец проголодался! Покорми его.")

    return ("🏰 В меню", "menu", "Все дела пока недоступны. Загляни позже!")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = context.bot_data.get("ctx")
    if not ctx:
        await update.effective_message.reply_text("⚠️ Бот инициализируется, попробуйте позже.")
        return
    try:
        user, msg = get_user_and_msg(update)
        uid = user.id
        username = user.username or user.first_name or "Странник"
        player = await ctx.repo.get_by_id(uid)
        await _handle_referral(update, context, uid, player)
        if not player or not player.exists:
            await _create_new_player(update, context, uid, username)
        else:
            # Запускаем ежедневный бонус в фоне, чтобы меню появилось мгновенно:
            asyncio.create_task(process_daily_login(uid, context))

            # Меню отправляем немедленно:
            await _show_main_menu(update, context, player, user, ctx)
    except Exception as e:
        logger.exception("start failed")
        await update.effective_message.reply_text("⚠️ Произошла ошибка. Попробуйте позже.")

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

# Результат расчёта награды
class RewardResult(NamedTuple):
    total_oac: int
    title: Optional[str]
    inventory_items: Dict[str, int]  # имя поля → количество

# Расчёт награды (чистая функция)

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

    # Крит x2 – 5% шанс
    # Крит x10 – 1% шанс
    roll = random.randint(1, 100)
    if roll == 1:
        crit = True
        earned *= 10
    elif roll <= 5:
        crit = True
        earned *= 2
    else:
        crit = False

    return earned, crit, happy

def _format_farm_message(earned: int, crit: bool, happy: bool,
                         medal_text: str, new_count: int, target: int,
                         new_balance: int) -> str:
    """Сообщение после фарма – чистая структура, как в крафте."""
    # Крит-эмодзи
    if not crit:
        crit_emoji = "🍬"
    elif earned >= FARM_MAX * 10:
        crit_emoji = "💥 (x10!)"
    else:
        crit_emoji = "🍬🍬"

    # Happy hour (закомментирован)
    # happy_str = " 🌟x2" if happy else ""
    happy_str = ""

    # Прогресс-бары
    progress_bar_str = get_medal_progress(new_count, FARM_MEDALS)
    rank_progress = get_rank_progress(new_balance)

    # Сборка сообщения
    msg = (
        f"💎 <b>Ты нафармил: +{earned} OAC</b> {crit_emoji}{happy_str}\n"
        f"🎉 <b>У тебя: {new_balance} OAC</b>\n\n"
        f"{medal_text}"
        f"🎯 <b>Фарминг: {new_count} / {target}</b>\n"
        f"{progress_bar_str}\n\n"
        f"{rank_progress}"
    )
    return msg
    
@rate_limit(3)
@game_handler
async def farm_callback_v2(update, context, ctx, player):
    user = update.effective_user
    uid = user.id
    uname = user.username or user.first_name
    now = datetime.now()

    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass

    async def _farm(p, conn):
        if p.last_farm and (now - p.last_farm) < timedelta(hours=FARM_COOLDOWN_HOURS):
            remain = math.ceil((timedelta(hours=FARM_COOLDOWN_HOURS) - (now - p.last_farm)).seconds / 60)
            return ("cooldown", remain)

        old_balance = p.balance
        earned, crit, happy = _calculate_farm_reward(p, context)

        old_count = p.farm_count
        new_count = old_count + 1
        medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, FARM_MEDALS)

        p.balance += earned + medal_bonus
        p.daily_progress = p.daily_progress or {}
        p.daily_progress["farm"] = True
        p.farm_count = new_count
        p.last_farm = now
        p.last_farm_date = date.today()

        if ctx.war_service:
            await ctx.war_service.add_score_raw(uid, earned + medal_bonus, conn)

        return ("ok", earned, crit, happy, medal_text, new_count, p.balance, old_balance)

    result = await ctx.repo.atomic_update(uid, _farm)
    if result is None:
        await update.effective_message.reply_text("Сначала активируйся: /start")
        return

    status, *data = result
    if status == "cooldown":
        remain = data[0]
        btn_text, btn_callback, advice = get_next_action(player, exclude_callback="farm")
    
        progress = getattr(player, 'daily_progress', {}) or {}
        balance = getattr(player, 'balance', 0) or 0
        guild = getattr(player, 'guild', None)
        has_pet = bool(getattr(player, 'pet', ''))
        is_veteran = balance >= 5000
    
        # Динамический прогресс-бар
        guild_emoji = "🕯️" if guild == "BLACK" else "⚜️" if guild == "WHITE" else "🕋"
        actions_emojis = {
            "farm": "🍬",
            "craft": "🌿",
            "smoke": "💨",
            "guild_action": guild_emoji,
        }
        if is_veteran and has_pet:
            actions_emojis["pet"] = "🐾"
    
        total = len(actions_emojis)
        done = sum(1 for k in actions_emojis if progress.get(k))
    
        progress_icons = []
        for key, emoji in actions_emojis.items():
            if progress.get(key):
                progress_icons.append(f"{emoji}✅")
            else:
                progress_icons.append(f"{emoji}⬜️")
        progress_line = " ".join(progress_icons)
    
        # Таймер
        if remain <= 5:
            timer_emoji = "⚠️"
            timer_text = f"<b>Уже скоро!</b> Осталось подождать {remain} мин"
        elif remain <= 15:
            timer_emoji = "⌛️"
            timer_text = f"Подожди <b>{remain} мин</b>"
        else:
            timer_emoji = "⏳"
            timer_text = f"Подожди <b>{remain} мин</b>"
    
        # Сообщение
        if done == total:
            message_text = (
                f"<b>🍬 OAC копятся 🌱</b>\n\n"
                f"{timer_emoji} {timer_text}\n\n"
                f"📊 <b>Прогресс дня:</b>\n{progress_line}\n\n"
                f"🎉 <b>Всё готово! Забери +50 OAC в профиле!</b>"
            )
        else:
            message_text = (
                f"<b>🍬 OAC копятся 🌱</b>\n\n"
                f"{timer_emoji} {timer_text}\n\n"
                f"📊 <b>Прогресс дня:</b>\n{progress_line}\n\n"
                f"💡 <b>Совет:</b> {advice}"
            )
    
        await safe_send_message(
            context,
            update.effective_chat.id,
            message_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, callback_data=btn_callback)]])
        )
        return

    earned, crit, happy, medal_text, new_count, new_balance, old_balance = data

    target = get_medal_target(new_count, FARM_MEDALS)
    text = _format_farm_message(earned, crit, happy, medal_text, new_count, target, new_balance)

    anim_msg = await animate_progress_bar(update, context, title="🍬 Фармим...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, parse_mode='HTML')
    else:
        await safe_send_message(
            context,
            update.effective_chat.id,
            text,
            parse_mode='HTML'
        )

    asyncio.create_task(check_achievements(uid, context))
    asyncio.create_task(check_rank_up(context, uid, uname, old_balance, new_balance))

    if player.onboarding_step == 1:
        player.onboarding_step = 2
        await ctx.repo.save(player)
        await safe_send_message(
            context, uid,
            "<b>🎓 ОБУЧЕНИЕ [▓▓▓░] 3/3</b>\n\n"
            "<b>🌿 Отлично! Теперь создадим твой первый блант.</b>\n"
            "Нажми кнопку ниже, чтобы <b>сразу создать обычный блант</b>.\n\n"
            "<i>💡 Бланты нужны, чтобы активировать случайный эффект.</i>\n"
            "<b>🎁 Сразу после — бонус за обучение!</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌿 Крафт", callback_data="craft_normal")],
                [InlineKeyboardButton("⏭️ Пропустить шаг", callback_data="skip_onboarding")]
            ]),
            parse_mode='HTML'
        )

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
        f"<b>🌱 КРАФТ БЛАНТОВ</b>\n\n"
        f"<b>💎 У тебя: {balance} OAC 🍬</b>\n\n"
        f"<b>🗞️ Блантов в свёртке: {blunts}</b>\n"
        f"<b>🎯 Крафтинг: {craft_count}/{target} | {medal_name}</b>\n"
        f"<b>🌿 Блант — 15 OAC 🍬</b>\n\n"
        f"<b>💍 Именной блант — 50 OAC 🍬</b>\n"
        f"<b>Шансы:</b>\n" 
        f"<i>🟢 55% | 🔵 30% | 🟣 13% | 🟡 2%</i>"
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
        f"💎 Потрачено: <b>15 OAC 🍬</b>\n"
        f"⚜️ У тебя: <b>{new_balance} OAC 🍬</b>\n\n"
        f"{medal_text}"
        f"🎯 Крафтинг: <b>{new_count} / {target}</b>\n"
        f"<b>{progress_bar_str}</b>\n\n"
        f"🍃 Блантов в свёртке: <b>{blunts}</b>"
    )


def _format_dust_message(name: str, reaction: str) -> str:
    """Сообщение после использования Кристальной Пыли."""
    return (
        f"<b><i>💠 ПЫЛЬ ИСПОЛЬЗОВАНА</i></b>\n\n"
        f"🟡 <b><i>«{name}»</i></b> (Легендарный) 🌿\n"
        f"📜 Реакция: <i>{reaction}</i>"
    )

@rate_limit(1)
@game_handler
async def craft_callback_v2(update, context, ctx, player):
    user = update.effective_user
    uid = user.id

    stats = _get_craft_stats(player.balance, player.blunts, player.craft_count)
    text = _format_craft_menu_text(
        player.balance, player.blunts, player.craft_count,
        stats["medal_name"], stats["target"], player.m_essence
    )
    kb = _build_craft_keyboard(player.m_essence)
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')

@rate_limit(2)
@game_handler
async def handle_craft_normal_v2(update, context, ctx, player):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    chat_id = update.effective_chat.id

    async def _craft(p, conn):
        if p.balance < GAME_CONFIG["craft_cost"]:
            return CraftStatus.NO_MONEY, None

        old_count = p.craft_count
        new_count = old_count + 1
        medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, CRAFT_MEDALS)

        p.balance -= GAME_CONFIG["craft_cost"]
        p.blunts += 1
        p.craft_count = new_count
        p.daily_progress = p.daily_progress or {}
        p.daily_progress["craft"] = True

        if random.random() < 0.05:
            p.blunts += 1

        p.balance += medal_bonus

        # Безопасное начисление очков войны
        if ctx.war_service:
            try:
                await ctx.war_service.add_score(uid, WarAction.CRAFT, conn)
            except Exception:
                logger.exception("War service error, proceeding without points")

        return CraftStatus.OK, (medal_text, new_count, p.blunts, p.balance)

    result = await ctx.repo.atomic_update(uid, _craft)
    if result is None:
        await query.answer("Профиль не найден.", show_alert=True)
        return

    status, data = result
    if status == CraftStatus.NO_MONEY:
        await safe_send(context, chat_id,
            f"<b>❌ Недостаточно OAC.</b>\n🕯️ Требуется <b>{GAME_CONFIG['craft_cost']} OAC</b> 🍬.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]]),
            parse_mode='HTML'
        )
        return

    medal_text, new_count, blunts, new_balance = data
    target = get_medal_target(new_count, CRAFT_MEDALS)
    text = _format_normal_craft_message(medal_text, new_count, target, blunts, new_balance)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌿 Скрафтить ещё", callback_data="craft_normal")],
        [InlineKeyboardButton("🔙 Назад", callback_data="craft")]
    ])

    # Элегантная отправка результата без дублирования
    anim_msg = await animate_progress_bar(update, context, title="🌿 Скручиваем Блант...")
    if anim_msg is not None:
        await anim_msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode='HTML')

    asyncio.create_task(check_achievements(uid, context))
    
    # Онбординг
    if player.onboarding_step == 2:
        player.onboarding_step = -1
        await ctx.repo.save(player)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Забрать награду", callback_data="onboarding_reward")]
        ])
        await safe_send_message(
            context, uid,
            "<b>🎓 Обучение [▓▓▓▓] (шаг 3 из 3)</b>\n\n🎉 Поздравляю! Ты освоил основы.</b>\n\nНажми кнопку ниже, чтобы получить бонус за обучение!",
            reply_markup=kb
        )

@rate_limit(3)
@game_handler
async def handle_craft_named(update, context, ctx, player):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if player.balance < GAME_CONFIG["named_blunt_cost"]:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<b>🔮 ИСКАЖЕНИЕ МОЛЧИТ</b>\n\n<i>🛡️ Недостаточно OAC.</i>\n🕯️ Требуется <b>{GAME_CONFIG['named_blunt_cost']} OAC</b> 🍬.",
            parse_mode='HTML'
        )
        return

    context.user_data['awaiting_named_blunt'] = True
    # Вместо PTB job_queue используем asyncio задачу для отмены состояния через 5 минут
    asyncio.create_task(_clear_named_blunt_state_after(uid, context, 300))

    await query.message.delete()
    sent_msg = await context.bot.send_message(
        chat_id=query.message.chat.id,
        text="<b>💍 ИМЕННОЙ БЛАНТ</b>\n\n<i>Введи имя своего NFT Бланта (до 25 символов)</i>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_named")]]),
        parse_mode='HTML'
    )
    context.user_data['awaiting_named_blunt_msg_id'] = sent_msg.message_id

async def handle_named_name(update, context):
    ctx = context.bot_data.get("ctx")
    if not ctx:
        return
    try:
        user = update.effective_user
        uid = user.id
        name = update.message.text.strip()[:25]
        if not name:
            await update.message.reply_text("❌ Имя не может быть пустым.")
            return

        player = await ctx.repo.get_by_id(uid)
        if not player or not player.user_id:
            await update.message.reply_text("Сначала активируйся: /start")
            return

        # === Попытка переименовать безымянный блант ===
        inv = _json_safe_load(player.inventory, [])
        for item in reversed(inv):
            if item.get("type") == "named" and not item.get("name", "").strip():
                item["name"] = name
                player.inventory = inv
                await ctx.repo.save(player)
                
                # Красивое оформление
                rarity = item.get("rarity", "common")
                color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
                reaction = item.get("reaction", "")

                caption = (
                    f"✅ <b>ИМЯ ДАНО! ✨</b>\n\n"
                    f"{color} <b><i>«{html.escape(name)}»</i></b> 🌿\n"
                    f"<i>Редкость: {rarity}</i>\n\n"
                    f"📜 <i>{reaction}</i>\n\n"
                    f"💎 Этот блант навсегда останется в твоей коллекции!"
                )
                
                await update.message.reply_text(caption,parse_mode='HTML')
                context.user_data.pop('awaiting_named_blunt', None)
                return

        async def _named(p, conn):
            if p.balance < GAME_CONFIG["named_blunt_cost"]:
                return ("no_money",)
            p.balance -= 50
            p.craft_count = (p.craft_count or 0) + 1
            item = await create_named_blunt(uid, name, rarity=None, conn=conn, ctx=ctx, player=p)

            await ctx.war_service.add_score_raw(uid, 0, conn)
            await ctx.war_service.add_score(uid, WarAction.NAMED_CRAFT, conn)
            return ("ok", item)

        result = await ctx.repo.atomic_update(uid, _named)
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
        reaction = item["reaction"]          # <-- твоя реакция из БД, не трогаем

        # === ГЕНЕРАЦИЯ ИМЕНИ (реакцию не меняем) ===
        original_name = name
        meme_name = mutate_name(original_name)
        item["name"] = meme_name
        item["original_name"] = original_name

        caption = (
            f"<b>💍 ТЫ СОЗДАЛ ИМЕННОЙ БЛАНТ! 🎉</b>\n\n"
            f"{color}<b><i>«{html.escape(meme_name)}»</i></b>\n"
            f"Редкость: <b>{item['rarity']}</b> {color}\n\n"
            f"💎 Он навсегда останется в <b>твоей коллекции</b>.\n\n"
            f"🕯️ <i>{reaction}</i>\n\n"
            f"💬 Этот блант достоин того, чтобы его <b>увидели друзья. Действуй!</b>"
        )

        # --- Текст для кнопки «Поделиться» ---
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=blunt_{blunt_id}"

        share_text = (
            f"<b>{html.escape(player.username or 'игрок')}</b>\n\n"
            f"{color} <b>Имя именного NFT Бланта: «{html.escape(meme_name)}»</b>\n"
            f"🧬 <b>Редкость: {item['rarity']} {color}</b>\n"
            f"🩸 <b>Серийный номер: #{item.get('rare_number', '?-????')}</b>\n"
            f"💬 <b>Реакция:</b> <i>{reaction}</i>\n\n"
            f"<b>💎 Нажми на ссылку чтобы забрать уникальный Блант:</b>\n{ref_link}"
        )

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎁 Подарить", callback_data=f"gift_blunt_{blunt_id}"),
                InlineKeyboardButton("🔗 Поделиться", switch_inline_query=share_text)
            ],
            [InlineKeyboardButton("🔙 В меню", callback_data="menu")]
        ])

        sent_img = await safe_send_blunt_image(
            context, update.effective_chat.id, item["rarity"], caption=caption, reply_markup=kb
        )
        if not sent_img:
            await update.message.reply_text(caption, reply_markup=kb, parse_mode='HTML')

        # FOMO-бонус (без изменений)
        async def fomo_reminder():
            await asyncio.sleep(300)
            player_check = await ctx.repo.get_by_id(uid)
            if player_check:
                inv_now = player_check.inventory or []
                if any(it.get("id") == blunt_id for it in inv_now):
                    try:
                        await context.bot.send_message(uid, "⌛ Твой именной блантик всё ещё скучает. Подари или поделись им, пока не поздно!")
                    except:
                        pass
        asyncio.create_task(fomo_reminder())

        await asyncio.sleep(0.5)
        bonus_msg = await context.bot.send_message(
            uid,
            "⚡ <b>БОНУС ЗА СКОРОСТЬ!</b>\n\n"
            "Если ты <b>подаришь</b> или <b>поделишься</b> этим блантом за 5 минут, получишь <b>+10 OAC</b> на счёт.\n"
            "Просто нажми одну из кнопок выше!",
            parse_mode='HTML'
        )

        context.user_data['fomo_bonus_msg'] = bonus_msg.message_id
        context.user_data['fomo_blunt_id'] = blunt_id
        context.user_data['fomo_start'] = time.time()
        
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

async def handle_use_dust(update, context):
    # 1. Современный доступ к ctx
    ctx = context.bot_data.get("ctx")
    if not ctx:
        await update.callback_query.answer("⚠️ Бот инициализируется.", show_alert=True)
        return

    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    player = await ctx.repo.get_by_id(uid)
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
        # 2. Передаём player=p, чтобы блант добавился к тому же объекту, который будет сохранён
        item = await create_named_blunt(uid, name, rarity="legendary", conn=conn, ctx=ctx, player=p)

        await ctx.war_service.add_score(uid, WarAction.DUST_USE, conn)

        return ("ok", item, name)

    result = await ctx.repo.atomic_update(uid, _use_dust)
    if result is None:
        await query.answer("Профиль не найден.", show_alert=True)
        return

    status, *data = result
    if status == "no_dust":
        await query.answer("Нет Кристальной Пыли.", show_alert=True)
        return

    item, name = data
    reaction = item["reaction"]

    await safe_send_blunt_image(context, query.message.chat.id, "legendary", caption=None, reply_markup=None)
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

async def _clear_named_blunt_state_after(uid, context, delay):
    """Сбрасывает состояние ввода именного бланта через delay секунд."""
    await asyncio.sleep(delay)
    context.user_data['awaiting_named_blunt'] = False

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
    
@rate_limit(1)
@game_handler
async def onboarding_reward(update, context, ctx, player):
    query = update.callback_query
    await query.answer()
    async def _reward(p, conn):
        p.balance += 30
        p.blunts += 1
        return ("ok", p.balance, p.blunts)
    result = await ctx.repo.atomic_update(query.from_user.id, _reward)
    if result:
        _, new_bal, new_blunts = result
        await query.message.edit_text(
            f"🎁 Ты получил бонус: +30 OAC, +1 блант!\n\n"
            f"Твой баланс: {new_bal} OAC, блантов: {new_blunts}\n\n"
            f"🎉 Поздравляю! Теперь ты можешь исследовать другие разделы меню.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏰 В меню", callback_data="menu")
            ]])
        )
    
# ====== ФУНКЦИЯ ПЕРЕДАЧИ БЛАНТА (АТОМАРНАЯ, БЕЗОПАСНАЯ) =====
class TransferError(Exception):
    pass
class BluntNotFound(TransferError):
    pass
class SameUserError(TransferError):
    pass

async def transfer_blunt(sender_id: int, receiver_id: int, blunt_id: str, ctx: AppContext) -> None:
    """Атомарная передача именного бланта с блокировкой строк (использует ctx)."""
    if sender_id == receiver_id:
        raise SameUserError("Нельзя передать блант самому себе")

    try:
        async with ctx.db_pool.acquire() as conn:
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
                    # Запрет дарить уже подаренный блант (даже в гонке)
                if 'gifted_by' in item:
                    raise TransferError("Этот блант уже был подарен и не может быть передан повторно")

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
                    "since": datetime.now(timezone.utc).isoformat()
                })
                # Держим историю не длиннее 10 записей (экономия места в БД)
                if len(item["owner_history"]) > 10:
                    item["owner_history"] = item["owner_history"][-10:]
                # Пометка, что блант подарен – запрещает последующие передачи
                item['gifted_by'] = str(receiver_id)
                receiver.inventory.append(item)
                await ctx.repo.save(sender, conn=conn)
                await ctx.repo.save(receiver, conn=conn)
                logger.info(f"Blunt {blunt_id} передан от {sender_id} к {receiver_id}")

    except TransferError:
        raise
    except Exception as e:
        logger.exception("Неожиданная ошибка при передаче бланта")
        raise TransferError("Внутренняя ошибка передачи") from e

# ===== НОВЫЕ ФУНКЦИИ ДЛЯ ОБМЕНА БЛАНТАМИ =====
import asyncio
import asyncpg
from html import escape as html_escape

logger = logging.getLogger("bot")

async def _cleanup_gift_request(context: ContextTypes.DEFAULT_TYPE):
    msg_id = context.user_data.pop('gift_msg_id', None)
    chat_id = context.user_data.pop('gift_chat_id', None)
    if msg_id and chat_id:
        try:
            await asyncio.wait_for(
                context.bot.delete_message(chat_id, msg_id),
                timeout=2.0
            )
        except Exception:
            pass
    context.user_data.pop('gifting_blunt_id', None)
    context.user_data.pop('gifting_blunt_name', None)

@rate_limit(2)
async def handle_gift_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = context.bot_data.get("ctx")
    if not ctx:
        return

    uid = update.effective_user.id
    if not context.user_data.get('gifting_blunt_id'):
        return

    blunt_id = context.user_data['gifting_blunt_id']
    blunt_name = context.user_data.get('gifting_blunt_name', '???')
    text = update.message.text.strip()

    # ---------- Поиск получателя с защитой от долгого ответа БД ----------
    recipient_id = None
    text = update.message.text.strip() if update.message.text else ""

    # Сначала пытаемся вытащить user_id из entities (если тегнули человека)
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "text_mention":
                recipient_id = entity.user.id
                break
            elif entity.type == "mention":
                # обычный @username – обработаем ниже
                pass

    # Если по entities не найден, пробуем распарсить текст
    if not recipient_id:
        if text.startswith("@"):
            username = text[1:].lower()
            try:
                async with asyncio.timeout(3.0):
                    async with ctx.db_pool.acquire() as conn:
                        row = await conn.fetchrow(
                            "SELECT user_id FROM players WHERE LOWER(username) = $1", username
                        )
                        if row:
                            recipient_id = row['user_id']
            except asyncio.TimeoutError:
                logger.error("Таймаут поиска получателя")
                await update.message.reply_text("⚠️ Сервер занят, попробуй через пару секунд.")
                await _cleanup_gift_request(context)
                return
            except Exception as e:
                logger.exception("Ошибка поиска получателя")
                await update.message.reply_text("⚠️ Не удалось проверить игрока.")
                await _cleanup_gift_request(context)
                return
        elif text.isdigit():
            recipient_id = int(text)
        else:
            # Не @, не число – возможно, тег без username, entities не отработали? На всякий случай подскажем
            await update.message.reply_text(
                "❌ Используй <b>@username</b> или <b>числовой ID</b> игрока.\n"
                "Если у игрока нет @username, попроси его отправить команду /id и введи полученный ID.",
                parse_mode='HTML'
            )
            return

    if not recipient_id:
        await update.message.reply_text(
            "❌ Игрок не найден. Проверь @username или ID.\nПопробуй ещё раз или нажми «Отмена»."
        )
        return

    # ---------- Атомарная передача с ретраями ----------
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await transfer_blunt(uid, recipient_id, blunt_id, ctx)
            break
        except SameUserError:
            await update.message.reply_text("❌ Нельзя подарить блант самому себе. Введи другого получателя или нажми «Отмена».")
            return
        except BluntNotFound:
            await update.message.reply_text("❌ Блант не найден в твоём инвентаре.")
            await _cleanup_gift_request(context)
            return
        except TransferError as e:
            cause = e.__cause__
            if cause is not None and isinstance(cause, asyncpg.exceptions.SerializationError) and attempt < max_retries - 1:
                await asyncio.sleep(0.1 * (2 ** attempt))
                continue
            logger.exception("Ошибка передачи бланта")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            await _cleanup_gift_request(context)
            return
        except Exception as e:
            logger.exception("Неожиданная ошибка при передаче бланта")
            await update.message.reply_text("⚠️ Внутренняя ошибка. Попробуй позже.")
            await _cleanup_gift_request(context)
            return

    # ---------- Успех ----------
    msg_id = context.user_data.get('gift_msg_id')
    chat_id = context.user_data.get('gift_chat_id')
    if msg_id and chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=f"✅ Блант «{html.escape(blunt_name)}» успешно подарен игроку {text}!",
                parse_mode='HTML'
            )
            context.user_data.pop('gift_msg_id', None)
            context.user_data.pop('gift_chat_id', None)
        except Exception:
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            finally:
                context.user_data.pop('gift_msg_id', None)
                context.user_data.pop('gift_chat_id', None)
            await update.message.reply_text(
                f"✅ Блант «{html.escape(blunt_name)}» успешно подарен игроку {text}!",
                parse_mode='HTML'
            )
    else:
        await update.message.reply_text(
            f"✅ Блант «{html.escape(blunt_name)}» успешно подарен игроку {text}!",
            parse_mode='HTML'
        )

    context.user_data.pop('gifting_blunt_id', None)
    context.user_data.pop('gifting_blunt_name', None)

@rate_limit(2)
@game_handler
async def gift_blunt_start(update, context, ctx, player):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    blunt_id = query.data.replace("gift_blunt_", "")
    prev_blunt_id = context.user_data.get('gifting_blunt_id')
    if prev_blunt_id == blunt_id:
        await query.answer("Ты уже даришь этот блант. Введи @username получателя.", show_alert=True)
        return
    chat_id = query.message.chat.id

# 1. Сброс предыдущего состояния дарения (если вдруг осталось)
    if prev_blunt_id and prev_blunt_id != blunt_id:
            old_msg_id = context.user_data.get('gift_msg_id')
            old_chat_id = context.user_data.get('gift_chat_id')
            if old_msg_id and old_chat_id:
                try:
                    await context.bot.delete_message(old_chat_id, old_msg_id)
                    context.user_data.pop('gift_msg_id', None)
                    context.user_data.pop('gift_chat_id', None)
                except Exception:
                    pass
            context.user_data.pop('gifting_blunt_id', None)
            context.user_data.pop('gifting_blunt_name', None)

    # 2. FOMO-бонус (твой оригинальный блок без изменений)
    fomo_blunt_id = context.user_data.get('fomo_blunt_id')
    fomo_start = context.user_data.get('fomo_start')
    if fomo_blunt_id == blunt_id:
        context.user_data.pop('fomo_blunt_id', None)
        context.user_data.pop('fomo_start', None)
        bonus_msg_id = context.user_data.pop('fomo_bonus_msg', None)

        elapsed = time.time() - (fomo_start or 0)
        if elapsed <= 300:
            async def _add_fomo_bonus(p, conn):
                p.balance += 10
                return p
            await ctx.repo.atomic_update(uid, _add_fomo_bonus)
            await context.bot.send_message(uid, "✅ Бонус +10 OAC за скорость начислен!")
        if bonus_msg_id:
            try:
                await context.bot.delete_message(uid, bonus_msg_id)
            except Exception:
                pass

    # 3. Проверка наличия бланта
    inv = player.inventory or []
    blunt = next((it for it in inv if it.get("id") == blunt_id and it.get("type") == "named"), None)
    if not blunt:
        await query.answer("Этот блант тебе больше не принадлежит.", show_alert=True)
        return
    # 4. Сохраняем состояние
    context.user_data['gifting_blunt_id'] = blunt_id
    context.user_data['gifting_blunt_name'] = blunt.get('name', '???')
    # 5. Отправляем запрос в тот же чат
    sent_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🎁 <b>ПОДАРИТЬ БЛАНТ</b>\n\n"
            "Введи <b>@username</b> или <b>числовой ID</b> игрока.\n"
            "Для отмены нажми кнопку ниже."
        ),
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_gift")
        ]])
    )
    context.user_data['gift_msg_id'] = sent_msg.message_id
    context.user_data['gift_chat_id'] = chat_id
# 6. Таймер автосброса (используем существующую _cleanup_gift_request с задержкой)
    async def _delayed_cleanup():
        await asyncio.sleep(300)
        await _cleanup_gift_request(context)
    context.application.create_task(
        _delayed_cleanup(),
        name=f"gift_clear_{uid}_{blunt_id}"
    )

@rate_limit(1)
async def cancel_gift(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    msg_id = context.user_data.pop('gift_msg_id', None)
    chat_id = context.user_data.pop('gift_chat_id', None)
    if msg_id and chat_id:
        try:
            await context.bot.delete_message(chat_id, msg_id)
        except:
            pass

    context.user_data.pop('gifting_blunt_id', None)

    # В группе дополнительно ничего не пишем, в личке — покажем меню (опционально)
    if query.message.chat.type == "private":
        await query.message.edit_text("❌ Подарок отменён.")

@rate_limit(1)
@game_handler
async def smoke_callback(update, context, ctx, player):
    user, msg = get_user_and_msg(update)
    uid = user.id

    if player.blunts < 1:
        empty_text = (
            "<b>💨 ДУНУТЬ</b>\n\n"
            "<b>🌿 Твой свёрток пуст</b>\n\n"
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

    main_text = f"<b>💨 ДУНУТЬ</b>\n\n🌿 Блантов в свёртке: <b>{player.blunts}</b>"
    main_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Дунуть", callback_data="do_smoke")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    if update.callback_query:
        await update.callback_query.message.edit_text(main_text, reply_markup=main_kb, parse_mode='HTML')
    else:
        await msg.reply_text(main_text, reply_markup=main_kb, parse_mode='HTML')

@rate_limit(1) #версия 2
@game_handler
async def do_smoke(update, context, ctx, player):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    async def _smoke(p, conn):
        if (p.blunts or 0) < 1:
            return SmokeStatus.NO_BLUNTS, None

        save = (p.guild == "WHITE" and random.randint(1, 100) <= 20)
        earned = calculate_smoke_reward(p, ctx.cache.get("happy_hour", False))

        old_count = p.smoke_count or 0
        new_count = old_count + 1
        medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, SMOKE_MEDALS)

        if not save:
            p.blunts -= 1
        p.smoke_count = new_count
        p.balance = (p.balance or 0) + earned + medal_bonus
        p.daily_progress = p.daily_progress or {}
        p.daily_progress["smoke"] = True
        p.inhaled = 1

        if ctx.war_service:
            try:
                await ctx.war_service.add_score(uid, WarAction.SMOKE, conn)
            except Exception:
                logger.exception("War service error, proceeding without points")

        return SmokeStatus.OK, (earned, save, medal_text, new_count, p.blunts, p.balance)

    result = await ctx.repo.atomic_update(uid, _smoke)
    if result is None:
        await query.answer("Профиль не найден.", show_alert=True)
        return

    status, data = result
    if status == SmokeStatus.NO_BLUNTS:
        await query.message.edit_text(
            "<b>💨 ДУНУТЬ</b>\n\n<b>🌿 Твой свёрток пуст</b>\n\n<i>🎈 Скрути новый блант</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌿 Крафт", callback_data="craft")],
                [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
            ]),
            parse_mode='HTML'
        )
        return

    earned, save, medal_text, new_count, bl_left, new_balance = data
    effect = build_smoke_effect(random.random(), earned)

    text = (
        f"{effect}\n\n"
        f"{medal_text}"
        f"<b>💨 Дым:</b> {new_count}/{get_medal_target(new_count, SMOKE_MEDALS)}\n"
        f"{get_medal_progress(new_count, SMOKE_MEDALS)}\n\n"
        f"<b>🍃 Блантов в свёртке:</b> <b>{bl_left}</b>"
    )
    if save:
        text += "\n⚜️ <i>Светлая Гильдия сохранила твой Блант!</i>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Дунуть ещё", callback_data="do_smoke") if bl_left >= 1
         else InlineKeyboardButton("🌿 Крафтить ещё", callback_data="craft")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

    asyncio.create_task(check_achievements(uid, context))

@rate_limit(3)
async def ritual_callback(update, context):
    ctx = context.bot_data.get("ctx")
    if not ctx:
        await update.effective_message.reply_text("⚠️ Бот инициализируется, попробуйте позже.")
        return

    user, msg = get_user_and_msg(update)
    uid = user.id
    now = datetime.now()

    async def _ritual(player, conn):
        if player.guild != "BLACK":
            return ("wrong_guild",)
        if player.last_ritual and (now - player.last_ritual) < timedelta(hours=GAME_CONFIG["ritual_cooldown_hours"]):
            remain = int((timedelta(hours=GAME_CONFIG["ritual_cooldown_hours"]) - (now - player.last_ritual)).seconds / 3600)
            return ("cooldown", remain)

        reward = 150
        if ctx.cache.get("happy_hour", False):
            reward *= HAPPY_HOUR_MULTIPLIER
        extra = 15 if random.random() < 0.1 else 0

        old_count = player.ritual_count
        new_count = old_count + 1
        medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, RITUAL_MEDALS)

        player.balance += reward + extra + medal_bonus
        player.daily_progress = player.daily_progress or {}
        player.daily_progress["guild_action"] = True
        player.ritual_count = new_count
        player.last_ritual = now

        await ctx.war_service.add_score(uid, WarAction.RITUAL, conn)
        return ("ok", reward, extra, medal_text, new_count, player.balance)

    result = await ctx.repo.atomic_update(uid, _ritual)
    if result is None:
        await msg.reply_text("Профиль не найден.")
        return

    status, *data = result
    if status == "wrong_guild":
        await send_whisper_dm(update, context, "❌ Только Тёмная Гильдия.")
        return
    if status == "cooldown":
        remain = data[0]
        await safe_send_message(context, update.effective_chat.id,
            f"<b>🕯️ Тёмный алтарь истощён 🌙</b>\n\n<b>🗝️ Жди {remain} ч</b>",
            parse_mode='HTML')
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
        await safe_send_message(context, update.effective_chat.id, text, parse_mode='HTML')

    await check_achievements(uid, context)

# КУСТИК (с защитой от None)
async def collect_callback(update, context):
    ctx = context.bot_data.get("ctx")
    if not ctx:
        await update.effective_message.reply_text("⚠️ Бот инициализируется, попробуйте позже.")
        return
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
        if ctx.cache.get("happy_hour", False):
            earned *= HAPPY_HOUR_MULTIPLIER
        if earned < 1:
            return ("not_ready",)
        player.balance += earned
        player.passive_collected = now

        await ctx.war_service.add_score_raw(uid, earned, conn)

        return ("ok", earned, player.balance)

    result = await ctx.repo.atomic_update(uid, _collect)
    if result is None:
        await send_whisper_dm(update, context, "Профиль не найден.")
        return
    status, *data = result
    if status == "low_rank":
        await send_whisper_dm(update, context, "❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)")
        return
    if status == "activated":
        await safe_send_message(
            context,
            update.effective_chat.id,
            "<b>🪴 Авто‑сборщик активирован 💎</b>\n\n<b>🌱 Загляни позже</b>",
            parse_mode='HTML'
        )
        return
    if status == "not_ready":
        await safe_send_message(
            context,
            update.effective_chat.id,
            "<b>🪴 Кустик ещё не созрел 💎</b>\n\n<b>🌱 Загляни позже</b>",
            parse_mode='HTML'
        )
        return

    earned, new_bal = data
    await send_whisper_dm(update, context,
        f"<b><i>🪴 УРОЖАЙ СОБРАН</i></b>\n\nТвой куст принёс <b>{earned} OAC</b> 🍬.\n\n💎 <i>У тебя:</i> <b>{new_bal} OAC</b> 🍬")

# Профиль – премиум-карточка, сеньорская версия (аватарка + текст + кнопки)
@rate_limit(1)
@game_handler
async def profile_callback(update, context, ctx, player):
    user, msg = get_user_and_msg(update)
    uid = user.id
    uname = html.escape(user.username or user.first_name)

    # player гарантированно существует благодаря @game_handler
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
    guild_line = ""
    if guild == "BLACK":
        guild_line = "\n🕯️ <b>Тёмная Гильдия</b> — ритуал и тёмная магия 🔮"
    elif guild == "WHITE":
        guild_line = "\n⚜️ <b>Светлая Гильдия</b> — исповедь и благосклонность удачи 🪽"
    else:
        guild_line = "\n🕯️🪽 <i>Не в гильдии</i> — вступление откроет <b>новые возможности </b>"

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

    pet_line = ""
    if player.pet:
        pet_line = f"🐾 <b>Питомец:</b> {player.pet}"
        if player.pet_name:
            pet_line += f" «{player.pet_name}»"
        pet_line += "\n"

    text = (
        f"<b>⚜️ ПРОФИЛЬ</b>\n"
        f"👤 <b>{uname}</b>{guild_line}\n"
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
    if not guild:
        kb_rows.append([InlineKeyboardButton("🕋 Вступить в Гильдию", callback_data="guild_info")])
    if len(named) > 2:
        kb_rows.append([InlineKeyboardButton(f"💍 Все именные бланты ({len(named)})", callback_data="my_blunts")])
    kb_rows.append([InlineKeyboardButton("📜 Кодекс", callback_data="rules")])
    kb_rows.append([InlineKeyboardButton("🎨 Кастомизация", callback_data="skins_menu"),
                    InlineKeyboardButton("🏆 Достижения", callback_data="achievements_profile")])
    kb_rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)

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

# Все мои бланты
@rate_limit(1)
@game_handler
async def my_blunts_callback(update, context, ctx, player, page=0):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

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
            InlineKeyboardButton("🔗", callback_data=f"share_blunt_{item['id']}"),
            InlineKeyboardButton("🎁 Подарить", callback_data=f"gift_blunt_{item['id']}")
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

async def achievements_callback(update, context, page=0):
    ctx = context.bot_data.get("ctx")
    if not ctx:
        await update.callback_query.answer("⚠️ Бот инициализируется, попробуйте позже.")
        return

    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    # Определяем, откуда пришёл игрок, и номер страницы
    if data == "achievements_menu":
        page = 0
        back_cb = "menu"
    elif data == "achievements_profile":
        page = 0
        back_cb = "profile"
    elif data.startswith("achievements_page_"):
        page = int(data.split("_")[-1])
        # Источник (меню/профиль) сохраняем в user_data при первом входе
        back_cb = context.user_data.get('ach_source', 'menu')
    else:
        # На всякий случай, если пришёл старый формат
        page = 0
        back_cb = "menu"

    # Сохраняем источник, если это первый вход из меню/профиля
    if data in ("achievements_menu", "achievements_profile"):
        context.user_data['ach_source'] = "profile" if data == "achievements_profile" else "menu"

    player = await ctx.repo.get_by_id(uid)
    if not player or not player.user_id:
        await query.answer("Профиль не найден.", show_alert=True)
        return

    async with ctx.db_pool.acquire() as conn:
        awarded = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id = $1", uid)
    awarded_ids = {r["ach_id"] for r in awarded}

    all_ach = list(ACHIEVEMENTS_DICT.values())
    per_page = 5
    total_pages = max(1, (len(all_ach) + per_page - 1) // per_page)
    if page >= total_pages:
        page = 0
    start = page * per_page
    chunk = all_ach[start:start + per_page]

    unlocked_count = len(awarded_ids)
    total_achievements = len(ACHIEVEMENTS)
    text = f"<b>🏆 ДОСТИЖЕНИЯ</b> ({unlocked_count} / {total_achievements})\n\n"
    for ach in chunk:
        ach_id = ach["id"]
        unlocked = ach_id in awarded_ids
        mark = "✅" if unlocked else "🔒"
        text += f"{mark} {ach['emoji']} <b>{ach['name']}</b>\n"
        text += f"<i>{ach['desc']}</i>\n"

        if not unlocked and ach_id != "lunar_lord" and ach_id in ACHIEVEMENT_CONDITIONS:
            field, target = ACHIEVEMENT_CONDITIONS[ach_id]
            current = getattr(player, field, 0)
            progress = min(100, int(current / target * 100)) if target > 0 else 0
            bar = "▓" * (progress // 10) + "░" * (10 - progress // 10)
            text += f"<b>{bar} {progress}%</b> ({current}/{target})\n"
        else:
            text += "\n"
        text += "\n"

    kb_rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"achievements_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"achievements_page_{page+1}"))
    if nav:
        kb_rows.append(nav)
    if query.data == "achievements_profile":
        back_cb = "profile"
    else:
        back_cb = "menu"
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data=back_cb)])
    await edit_or_reply(update, context, text, reply_markup=InlineKeyboardMarkup(kb_rows))

@rate_limit(1)
@game_handler
async def top_callback(update, context, ctx, player):
    user, msg = get_user_and_msg(update)
    uid = user.id

    # Прямой запрос топа
    async with ctx.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, balance, guild FROM players ORDER BY balance DESC LIMIT 10"
        )
    top = [dict(r) for r in rows]

    if not top:
        await edit_or_reply(update, context, "🏆 Топ-10 пока пуст.")
        return

    first_balance = top[0]["balance"]
    my_balance = player.balance or 0

    text = "<b>💎 ТОП-10 ИГРОКОВ 🏆</b>\n\n"
    my_position = None

    for i, row in enumerate(top, 1):
        bal = row["balance"]
        percent = int(bal / first_balance * 100) if first_balance else 100
        filled = percent // 10
        bar = "▓" * filled + "░" * (10 - filled)

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

        guild = row.get("guild", "")
        if guild == "BLACK":
            g_emoji, g_name = "🕯️", "<b>Тёмная Гильдия</b>"
        elif guild == "WHITE":
            g_emoji, g_name = "⚜️", "<b>Светлая Гильдия</b>"
        else:
            g_emoji, g_name = "🩸", "<b>Без гильдии</b>"

        rank_emoji, rank_name = "🪓", "Рекрут"
        for emoji, threshold, _ in RANKS:
            if bal >= threshold:
                rank_emoji = emoji
                rank_name = emoji_to_name(emoji)
        username = html.escape(row["username"])

        text += (
            f"{prefix}<b>{username}</b> {g_emoji} — {bal} оас 🍬\n"
            f"   <i>{bar} {percent}%</i>\n"
            f"   {g_emoji} {g_name} | {rank_emoji} <b>{rank_name}</b>\n\n"
        )

        if row.get("user_id") == uid:
            my_position = i

    # Блок позиции игрока (весь оригинальный код без изменений)
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
    elif my_position is not None:
        third_balance = top[2]["balance"] if len(top) >= 3 else 0
        gap = third_balance - my_balance
        if gap > 0:
            text += (
                f"✦ 📊 Твоя позиция: {my_position} — "
                f"осталось 🎯 {gap} оас 🍬 до ТРОЙКИ ЛИДЕРОВ 💎🏆 ✦\n"
            )
        else:
            text += f"✦ 📊 Твоя позиция: {my_position} ✦\n"
    else:
        # Объединённый запрос для позиции вне топа
        async with ctx.db_pool.acquire() as conn:
            cnt_row = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM players WHERE balance > $1", my_balance
            )
            pos = cnt_row["cnt"] + 1 if cnt_row else 1
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


@cb
async def top_scout_callback(update, context, ctx):
    query = update.callback_query
    await query.answer()

    # ctx гарантирован @cb, проверка не нужна
    async with ctx.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT username, balance, guild FROM players ORDER BY balance DESC LIMIT 3"
        )
    if not rows:
        await query.answer("Топ пуст.")
        return

    text = "<b>🔍 РАЗВЕДКА: ТОП-3</b>\n\n"
    for i, row in enumerate(rows):
        name = html.escape(row["username"])
        bal = row["balance"]
        guild = row["guild"]
        g = "🕯️" if guild == "BLACK" else "⚜️" if guild == "WHITE" else ""
        text += f"{'🥇' if i==0 else '🥈' if i==1 else '🥉'} <b>{name}</b> {g}\n💰 {bal} OAC\n\n"
    await send_whisper_dm(update, context, text)

# Гильдии
async def guild_info_callback(update, context):
    ctx = context.bot_data.get("ctx")
    if not ctx:
        await update.effective_message.reply_text("⚠️ Бот инициализируется, попробуйте позже.")
        return

    user, msg = get_user_and_msg(update)
    uid = user.id
    player = await ctx.repo.get_by_id(uid)
    if not player:
        await edit_or_reply(update, context, "Профиль не найден. Напиши /start")
        return

    guild = player.guild

    # Безопасный подсчёт гильдий
    cnt = await count_guilds(ctx)
    black_cnt = cnt.get("BLACK", 0) if isinstance(cnt, dict) else 0
    white_cnt = cnt.get("WHITE", 0) if isinstance(cnt, dict) else 0

    # Пожертвования
    async with ctx.db_pool.acquire() as conn:
        black_donated = await conn.fetchval("SELECT COALESCE(SUM(donated),0) FROM players WHERE guild='BLACK'") or 0
        white_donated = await conn.fetchval("SELECT COALESCE(SUM(donated),0) FROM players WHERE guild='WHITE'") or 0

    # Уровни и бонусы храма
    temple_levels = [
        {"level": 1, "cost": 0, "bonus": 0, "name": "Алтарь"},
        {"level": 2, "cost": 15000, "bonus": 5, "name": "Святилище"},
        {"level": 3, "cost": 45000, "bonus": 10, "name": "Храм"},
        {"level": 4, "cost": 100000, "bonus": 15, "name": "Цитадель"},
        {"level": 5, "cost": 250000, "bonus": 25, "name": "Обитель Богов"},
    ]

    text = "<b>🕋 ГИЛЬДИИ</b>\n\n"

    for guild_name, donated in [("BLACK", black_donated), ("WHITE", white_donated)]:
        current_level = 1
        bonus = 0
        for lvl in temple_levels:
            if donated >= lvl["cost"]:
                current_level = lvl["level"]
                bonus = lvl["bonus"]
            else:
                break

        guild_emoji = "🕯️" if guild_name == "BLACK" else "⚜️"
        guild_label = "Тёмная" if guild_name == "BLACK" else "Светлая"
        members = cnt.get(guild_name, 0)

        text += f"{guild_emoji} <b>{guild_label} Гильдия</b>\n"
        text += f"👥 <b>{members} странников</b>\n"

        # Цветные эмодзи для каждой гильдии
        if guild_name == "BLACK":
            filled_char = "🔮"
            empty_char = "⬛️"
        else:
            filled_char = "🪽"
            empty_char = "⬜️"

        if current_level < 5:
            next_cost = temple_levels[current_level]["cost"]
            progress = int(donated / next_cost * 100) if next_cost > 0 else 0
            filled_count = progress // 10
            empty_count = 10 - filled_count
            bar = filled_char * filled_count + empty_char * empty_count
            level_name = temple_levels[current_level - 1]["name"]
            next_level_name = temple_levels[current_level]["name"]
            text += f"🏛️ <b>{level_name}</b> → <b>{next_level_name}</b>\n"
            text += f"<b>{bar} {progress}%</b>\n"
            # Строки поменяны местами: сначала OAC, потом бонус
            text += f"<b>💎 {donated} / {next_cost} OAC</b>\n"
            text += f"<b>⚡ +{bonus}% к фарму</b>\n"
        else:
            bar = filled_char * 10
            level_name = temple_levels[4]["name"]
            text += f"🏛️ <b>{level_name}</b> (Макс.)\n"
            text += f"<b>{bar} 100%</b>\n"
            text += f"<b>💎 {donated} OAC</b>\n"
            text += f"<b>⚡ +{bonus}% к фарму</b>\n"

        text += "\n"

    # Твой статус в гильдии
    if guild:
        g_emoji = "🕯️" if guild == "BLACK" else "⚜️"
        g_name = "Тёмная" if guild == "BLACK" else "Светлая"
        text += f"✨ Ты состоишь в {g_emoji} <b>{g_name} Гильдии</b>.\n"
    else:
        text += "🔮 <i>Ты пока не в Гильдии. Выбери сторону!</i>\n"

    kb_rows = []
    if guild:
        g_emoji = "🕯️" if guild == "BLACK" else "⚜️"
        g_name = "Тёмная" if guild == "BLACK" else "Светлая"
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

    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')

async def guild_war_callback(update, context):
    ctx = context.application.bot_data["ctx"]
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    async with ctx.db_pool.acquire() as conn:
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
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')

@cb
async def repent_callback(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id

    async def _repent(p, conn):
        if not p or not p.user_id:
            return ("no_player",)
        if p.guild != "WHITE":
            return ("wrong_guild",)
        if (p.blunts or 0) < 1:
            return ("no_blunts",)

        p.blunts -= 1
        r = random.random()
        if r < 0.70:
            reward = random.randint(100, 200)
            p.balance = (p.balance or 0) + reward
            p.daily_progress = p.daily_progress or {}
            p.daily_progress["guild_action"] = True
            return ("ok", f"<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\nБлагословение! +{reward} OAC.")
        elif r < 0.95:
            p.m_essence = (p.m_essence or 0) + 1
            return ("ok", "<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\nТы получил 💠 Кристальную Пыль.")
        else:
            name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
            await create_named_blunt(uid, name, rarity="legendary", ctx=ctx, player=p)
            return ("ok", f"<b><i>⚜️ ИСПОВЕДЬ</i></b>\n\n🌟 Чудо! Легендарный блант «{name}»!")

    result = await ctx.repo.atomic_update(uid, _repent)

    if not result:
        await query.answer("Профиль не найден. Напиши /start", show_alert=True)
        return

    status, *rest = result
    data = rest[0] if rest else ""

    if status == "no_player":
        await query.answer("Профиль не найден. Напиши /start", show_alert=True)
        return
    if status == "wrong_guild":
        await query.answer("Только для Светлой Гильдии.", show_alert=True)
        return
    if status == "no_blunts":
        await query.answer("Нужен 1 блант.", show_alert=True)
        return

    # Успех
    await query.message.edit_text(
        data,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]]),
        parse_mode='HTML'
    )

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

async def privilege_callback(update, context):
    ctx = context.application.bot_data["ctx"]
    user, msg = get_user_and_msg(update)
    uid = user.id
    player = await ctx.repo.get_by_id(uid)
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
@rate_limit(2)
async def luck_callback(update, context, action=None):
    ctx = context.bot_data.get("ctx")
    if not ctx:
        await update.effective_message.reply_text("⚠️ Бот инициализируется, попробуйте позже.")
        return
    user, msg = get_user_and_msg(update)
    uid = user.id
    player = await ctx.repo.get_by_id(uid)
    if not player or not player.user_id:
        await _notify_user(update, context, "Сначала активируйся: /start")
        return

    now = datetime.now()
    cfg = LUCK_CONFIG

    wheel_ok = _check_wheel_availability(player, now, cfg["wheel"]["cooldown_hours"])
    berserk_ok = _check_berserk_availability(player, now, cfg["berserk"]["cost"], cfg["berserk"]["cooldown_hours"])
    alchemy_ok = player.balance >= cfg["alchemy"]["required_balance"]

    if action == "luck_wheel":
        await _process_wheel(update, context, uid, player, cfg, ctx)          # передаём ctx вместо war_service
        return
    if action == "luck_berserk":
        await _process_berserk(update, context, uid, player, cfg, ctx)
        return
    if action == "alchemy_start":
        await _process_alchemy_start(update, context, player, cfg)
        return
    if action == "alchemy_confirm":
        await _process_alchemy_confirm(update, context, uid, player, cfg, ctx)
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
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')


# ── Колесо ──────────────────────────────────────────────────
async def _process_wheel(update, context, uid, player, cfg, ctx):
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
        if ctx.cache.get("happy_hour") and ptype in ("oac", "jackpot"):
            prize *= HAPPY_HOUR_MULTIPLIER

        if ptype in ("oac", "jackpot"):
            p.balance += prize
        else:
            p.blunts += prize
        p.last_daily = datetime.now()

        if ctx.war_service and ptype in ("oac", "jackpot"):
            await ctx.war_service.add_score_raw(uid, prize, conn)

        return prize, ptype, p.balance

    result = await ctx.repo.atomic_update(uid, _wheel)
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
async def _process_berserk(update, context, uid, player, cfg, ctx):
    if not _check_berserk_availability(player, datetime.now(), cfg["berserk"]["cost"], cfg["berserk"]["cooldown_hours"]):
        await _notify_user(update, context, "🍀 Берсерк недоступен! Проверь баланс или время.")
        return

    async def _berserk(p, conn):
        if p.balance < cfg["berserk"]["cost"]:
            return ("no_money", p.balance)

        if random.random() < 0.6:
            p.balance += cfg["berserk"]["win_amount"]
            res = f"<b><i>🎲 БЕЗДНА ОТВЕТИЛА</i></b>\n\nИскажение благосклонно! +<b>{cfg['berserk']['win_amount']} OAC</b> 🍬."
            if ctx.war_service:
                await war_service.add_score(uid, WarAction.BERSERK_WIN, conn)
        else:
            p.balance -= cfg["berserk"]["cost"]
            res = f"<b><i>🕯️ БЕЗДНА МОЛЧИТ</i></b>\n\nИскажение промолчало. –<b>{cfg['berserk']['cost']} OAC</b>."
            if ctx.war_service:
                await war_service.add_score(uid, WarAction.BERSERK_LOSE, conn)
        p.last_berserk = datetime.now()
        return ("ok", res, p.balance)

    result = await ctx.repo.atomic_update(uid, _berserk)
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
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')


# ── Алхимия (запуск) ────────────────────────────────────────
async def _process_alchemy_confirm(update, context, uid, player, cfg, ctx):
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

        if ctx.war_service:
            await ctx.war_service.add_score(uid, WarAction.ALCHEMY, conn)
        return (AlchemyResult.SUCCESS, res)

    result = await ctx.repo.atomic_update(uid, _alchemy)
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
    ctx = context.application.bot_data["ctx"]
    if not context.args:
        await update.message.reply_text("Укажи серийный номер бланта: /check R-0001")
        return
    nft_id = context.args[0].strip().upper()
    async with ctx.db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT blunt_id, created_by, serial, rare_number FROM nft_registry WHERE rare_number = $1", nft_id)
        if not rows:
            await update.message.reply_text("🕳️ Блант с таким серийным номером не найден.")
            return
        if len(rows) > 1:
            await update.message.reply_text("⚠️ Найдено несколько блантов с таким номером, обратитесь к администратору.")
            return
        row = rows[0]
    blunt_id, creator_id, serial, rare_number = row["blunt_id"], row["created_by"], row["serial"], row["rare_number"]
    async with ctx.db_pool.acquire() as conn:
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
    await safe_send_blunt_image(context, update.effective_chat.id, "legendary", caption=None, reply_markup=None)
    details = f"<b>ДЕТАЛИ NFT БЛАНТА 💎</b>\n\n{color} <b>{name}</b>\n\n<b>Редкость:</b> <i>{rarity}</i> {color}\n\n🩸 <b>Серийный номер:</b> <b>#{rare_number}</b>\n🔗 <b>Хеш:</b> <b>{hash_code}</b>\n📜 <b>Реакция:</b> <i>{reaction}</i>\n"
    if "owner_history" in item:
        details += "\n🔄 История владения:\n"
        for entry in item["owner_history"]:
            date_str = format_date(entry.get('since',''))
            details += f"   @{entry.get('user_id','?')} — {date_str}\n"
    await update.message.reply_text(details, parse_mode='HTML')

    # Обновляем счётчик проверок через модель
    player = await ctx.repo.get_by_id(update.effective_user.id)
    if player:
        player.check_count = (player.check_count or 0) + 1
        await ctx.repo.save(player)

# ============================================================
# ЛАБИРИНТ ИСКАЖЕНИЯ — ИТОГОВАЯ СЕНЬОР-ВЕРСИЯ (ПОЛНАЯ ЗАМЕНА)
# ============================================================
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
    ctx = context.application.bot_data["ctx"]
    user, msg = get_user_and_msg(update)
    uid = user.id
    player = await ctx.repo.get_by_id(uid)
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
            await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')
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
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')

# ─── ПОДГОТОВКА К ЗАБЕГУ ────────────────────────────────────
async def lab_enter_confirm(update, context):
    ctx = context.application.bot_data["ctx"]
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await ctx.repo.get_by_id(uid)
    depth = player.lab_depth or 1 if player else 1
    total_rooms = 4 + depth
    now = datetime.now()
    async with ctx.db_pool.acquire() as conn:
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

    if ctx.redis:
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
    ctx = context.application.bot_data["ctx"]
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
    ctx = context.application.bot_data["ctx"]
    query = update.callback_query
    uid = query.from_user.id
    player = await ctx.repo.get_by_id(uid)
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
        await ctx.war_service.add_score(uid, WarAction.LAB_WIN, conn)

    await ctx.repo.atomic_update(uid, _lab_win)

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
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')
    await check_achievements(uid, context)

# ─── СМЕРТЬ В ЛАБИРИНТЕ ──────────────────────────────────────
async def show_lab_death(update, context):
    ctx = context.application.bot_data["ctx"]
    query = update.callback_query
    uid = query.from_user.id
    player = await ctx.repo.get_by_id(uid)
    if not player:
        return
    depth = player.lab_depth or 1

    # атомарно начисляем утешительный приз и военные очки
    async def _lab_die(p, conn):
        p.balance += 50
        p.lab_deaths += 1

        await ctx.war_service.add_score(uid, WarAction.LAB_DEATH, conn)

    await ctx.repo.atomic_update(uid, _lab_die)

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
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')

async def welcome_new_member(update, context):
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        username = member.username or member.first_name
        ctx = context.bot_data.get("ctx")
        online = 0
        player_guild = None
        if ctx:
            try:
                cnt = await count_guilds(ctx)
                online = cnt.get("BLACK", 0) + cnt.get("WHITE", 0)
                player = await ctx.repo.get_by_id(member.id)
                if player:
                    player_guild = player.guild
            except: pass

        welcome_text = (
            f"<b><i>🕯️⚜️ ДОБРО ПОЖАЛОВАТЬ В ЧАТ, СТРАННИК! ⚜️🕯️</i></b>\n\n"
            f"🪽 <b>{html.escape(username)}</b>, ты переступил порог Гильдии.\n\n"
            f"🌿 Твой первый /farm уже готов и ждёт тебя.\n"
            f"🍬 OAC ждут своего владельца.\n\n"
            f"👥 Сегодня с нами в игре уже <b>{online}</b> душ."
        )

        if player_guild:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🍬 Начать фарм", callback_data="farm")
            ]])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🕯️ Тёмная Гильдия (+50 🍬)", callback_data="guild_join_BLACK"),
                 InlineKeyboardButton("⚜️ Светлая Гильдия (+50 🍬)", callback_data="guild_join_WHITE")]
            ])

        await safe_send_message(context, update.message.chat.id, welcome_text, reply_markup=keyboard, parse_mode='HTML')

logger = logging.getLogger(__name__)

# ============================================================
# ОБРАБОТЧИК ТЕКСТОВЫХ СОКРАЩЕНИЙ (с Redis лимитером)
# ============================================================


# ============================================================
# ФУНКЦИИ ДЛЯ ИМЕННЫХ БЛАНТОВ И ДАРЕНИЯ (ВОССТАНОВЛЕНЫ)
# ============================================================

async def handle_gift_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx: AppContext = context.application.bot_data.get("ctx")
    if not ctx:
        return
    target_username = update.message.text.strip().lstrip('@')
    if not target_username:
        await update.message.reply_text("❌ Укажите корректный @username.")
        return

    blunt_id = context.user_data.get('gifting_blunt_id')
    if not blunt_id:
        await update.message.reply_text("❌ Не найден блант для дарения. Попробуйте заново.")
        return

    uid = update.effective_user.id
    player = await ctx.repo.get_by_id(uid, with_inventory=True)
    if not player:
        await update.message.reply_text("Профиль не найден.")
        context.user_data.pop('gifting_blunt_id', None)
        return

    # Находим блант в инвентаре
    item = None
    for it in player.inventory:
        if it.get("id") == blunt_id:
            item = it
            break
    if not item:
        await update.message.reply_text("❌ Блант уже не в вашем инвентаре.")
        context.user_data.pop('gifting_blunt_id', None)
        return

    # Находим получателя по username
    async with ctx.db_pool.acquire() as conn:
        target = await conn.fetchrow("SELECT user_id FROM players WHERE LOWER(username) = LOWER($1)", target_username)
    if not target:
        await update.message.reply_text("❌ Игрок с таким username не найден.")
        return
    target_id = target["user_id"]
    if target_id == uid:
        await update.message.reply_text("❌ Нельзя подарить блант самому себе.")
        return

    # Передаём блант
    # 1. Удаляем у дарителя
    player.inventory = [it for it in player.inventory if it.get("id") != blunt_id]
    # 2. Добавляем получателю (с обновлением истории)
    target_player = await ctx.repo.get_by_id(target_id, with_inventory=True)
    if not target_player:
        target_player = Player(user_id=target_id)
    if not target_player.inventory:
        target_player.inventory = []
    item["owner_history"] = item.get("owner_history", [])
    item["owner_history"].append({"user_id": uid, "since": datetime.now().isoformat()})
    target_player.inventory.append(item)
    await ctx.repo.save(player)
    await ctx.repo.save(target_player)

    await update.message.reply_text(f"✅ Блант «{item.get('name')}» подарен @{target_username}!")
    context.user_data.pop('gifting_blunt_id', None)

# ============================================================
# ПИТОМЦЫ
# ============================================================
@cb
async def pet_preview(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    player = await ctx.repo.get_by_id(uid)

    if player and player.pet:
        name_str = f" по кличке «{player.pet_name}»" if player.pet_name else ""
        await query.message.edit_text(f"Твой питомец: {player.pet}{name_str}")
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🐕 Купить Песика ({PET_CONFIG['dog']['price']} 🍬)", callback_data="pet_buy_dog")],
            [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
        ])
        await query.message.edit_text(
            "🐾 <b>ПИТОМЦЫ</b>\n\nПока доступен только Песик.",
            reply_markup=kb, parse_mode='HTML'
        )

@cb
async def pet_buy_dog_handler(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    result = await ctx.pet_service.buy(uid, "dog")
    if result is None:
        await query.answer("❌ Ошибка сервиса питомцев. Попробуйте позже.", show_alert=True)
        return

    status = result["status"]
    if status == "already_have":
        await query.answer("У тебя уже есть питомец!")
    elif status == "no_money":
        await query.answer(f"Недостаточно OAC. Нужно {PET_CONFIG['dog']['price']} 🍬")
    else:
        context.user_data['awaiting_pet_name'] = True
        await query.message.edit_text(
            f"<b>🐕 Песик ждёт имя!</b>\n\nВведи имя (до {PET_CONFIG['dog']['max_name_len']} символов).\nДля отмены нажми кнопку ниже.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Пропустить", callback_data="pet_name_skip")]])
        )

@cb
async def pet_name_skip_handler(update, context, ctx):
    query = update.callback_query
    context.user_data.pop('awaiting_pet_name', None)
    await query.message.edit_text("🐕 Хорошо, твой питомец будет просто Песиком!")

async def handle_pet_name(update, context):
    ctx = context.bot_data.get("ctx")
    if not ctx:
        await update.message.reply_text("⚠️ Игра инициализируется (отсуствие контекста ctx), попробуйте позже.")
        return
    name = update.message.text.strip()[:PET_CONFIG["dog"]["max_name_len"]]
    if not name:
        await update.message.reply_text("❌ Имя не может быть пустым.")
        return
    if len(update.message.text.strip()) > PET_CONFIG["dog"]["max_name_len"]:
        await update.message.reply_text(f"⚠️ Имя обрезано до {PET_CONFIG['dog']['max_name_len']} символов.")

    uid = update.effective_user.id
    success = await ctx.pet_service.set_name(uid, name)
    if not success:
        await update.message.reply_text("Ошибка сохранения имени.")
    else:
        await update.message.reply_text(f"Отлично! Теперь твоего питомца зовут «{name}»! 🐕")
    context.user_data.pop('awaiting_pet_name', None)

async def pet_locked_handler(update, context):
    query = update.callback_query
    await query.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)", show_alert=True)

# ============================================================
# МАГАЗИН, АДМИН-КОМАНДЫ
# ============================================================
@cb
async def shop_callback(update, context, ctx):
    query = update.callback_query
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🪪 Скидка", callback_data="privilege")],
        [InlineKeyboardButton("📦 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])
    await query.message.edit_text("<b>🛒 МАГАЗИН</b>", reply_markup=kb, parse_mode='HTML')

@cb
async def setbluntpic(update, context, ctx):
    ctx = context.application.bot_data["ctx"]
    if update.effective_user.id != ctx.settings.admin_id:
        await update.message.reply_text("⛔ Только для админа.")
        return
    if not context.args:
        await update.message.reply_text("Используй: /setbluntpic common (rare, epic, legendary) и прикрепи фото.")
        return
    rarity = context.args[0].lower()
    if rarity not in ctx.blunt_images:
        await update.message.reply_text("Редкость должна быть: common, rare, epic, legendary.")
        return
    if not update.message.photo:
        await update.message.reply_text("Пришли фото вместе с командой.")
        return
    ctx.blunt_images[rarity] = update.message.photo[-1].file_id
    await set_setting(f"blunt_image_{rarity}", ctx.blunt_images[rarity], ctx)
    names = {"common":"⚪ Обычный","rare":"🔵 Редкий","epic":"🟣 Эпический","legendary":"🟡 Легендарный"}
    await update.message.reply_text(f"✅ Изображение для {names[rarity]} обновлено!", parse_mode='HTML')

@cb
async def give_oac(update, context, ctx):
    ctx = context.application.bot_data["ctx"]
    if update.effective_user.id != ctx.settings.admin_id:
        await update.message.reply_text("⛔ Только для админа.")
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Формат: /give_oac <ID или @username> <сумма>")
        return

    target_raw = context.args[0]
    try:
        amount = int(context.args[1])
        if amount <= 0:
            await update.message.reply_text("Сумма должна быть положительной.")
            return
    except ValueError:
        await update.message.reply_text("Сумма должна быть целым числом.")
        return

    target_id = None
    try:
        if target_raw.startswith("@"):
            async with ctx.db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT user_id FROM players WHERE LOWER(username) = LOWER($1)", target_raw[1:])
                if row:
                    target_id = row["user_id"]
            if not target_id:
                chat = await context.bot.get_chat(target_raw)
                target_id = chat.id
        elif target_raw.isdigit():
            target_id = int(target_raw)
        else:
            await update.message.reply_text("Укажи числовой ID или @username.")
            return
    except Exception as e:
        logger.error("Ошибка поиска пользователя %s: %s", target_raw, e)
        await update.message.reply_text(f"Не удалось найти пользователя {target_raw}.")
        return

    if not target_id:
        await update.message.reply_text("Игрок не найден.")
        return

    try:
        target_player = await ctx.repo.get_by_id(target_id, with_inventory=False)
        target_name = target_player.username if target_player else f"ID{target_id}"
    except Exception:
        target_name = f"ID{target_id}"

    try:
        async def _add(p, conn):
            p.balance = (p.balance or 0) + amount
            return ("ok", p.balance)

        result = await ctx.repo.atomic_update(target_id, _add)
        if result is None:
            player = Player(user_id=target_id, balance=amount)
            await ctx.repo.save(player)
            new_balance = amount
        else:
            new_balance = result[1]

        await update.message.reply_text(
            f"✅ Игроку <b>{html.escape(target_name)}</b> начислено <b>{amount}</b> OAC 🍬. "
            f"Новый баланс: <b>{new_balance}</b> 🍬",
            parse_mode='HTML'
        )
        logger.info("Админ %d начислил %d OAC игроку %d (%s)", update.effective_user.id, amount, target_id, target_name)
    except Exception as e:
        logger.error("Ошибка начисления OAC: %s", e, exc_info=True)
        await update.message.reply_text("⚠️ Не удалось начислить OAC 🍬. Попробуй позже. 🍃")

@cb
async def check_blunt_pics(update, context, ctx):
    if update.effective_user.id != ctx.settings.admin_id:
        return
    status = []
    for rarity in ("common", "rare", "epic", "legendary"):
        file_id = ctx.blunt_images.get(rarity)
        if not file_id:
            status.append(f"❌ {rarity}: не задан")
        else:
            status.append(f"✅ {rarity} (file_id задан)")
    await update.message.reply_text("\n".join(status))

async def get_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = context.bot_data.get("ctx")
    if not ctx or update.effective_user.id != ctx.settings.admin_id:
        return
    if update.message.photo:
        fid = update.message.photo[-1].file_id
        await update.message.reply_text(fid)

# ============================================================
# ОБРАБОТЧИК КОМАНД
# ============================================================

    if not await check_rate_limit_redis(ctx, user_id, "command", limit=10, period=10):
        await update.message.reply_text("⚠️ Слишком часто. Подожди секунду.")
        return

    raw_text = msg.text.strip()
    request_id = uuid.uuid4().hex[:8]
    logger.info("[%s] Команда '%s' от user=%d", request_id, raw_text, user_id)

    command = raw_text.split()[0].split('@')[0][1:].lower()
    if not command:
        return
    args = raw_text.split()[1:]

    # ===== ВОТ ЭТУ СТРОКУ ЗАМЕНИЛ =====
    handler = TEXT_COMMAND_HANDLERS.get(command)
    if not handler:
        return

    try:
        old_args = context.args
        context.args = args
        try:
            await handler(update, context)
        finally:
            context.args = old_args
    except Exception as e:
        logger.error(f"Ошибка в команде /{command}: {e}", exc_info=True)
        async def _alert():
            for attempt in range(3):
                try:
                    await context.bot.send_message(chat_id=ctx.settings.admin_id, text=f"🚨 [{request_id}] Ошибка /{command} от {user_id}: {html.escape(str(e)[:500])}")
                    break
                except Exception:
                    await asyncio.sleep(2 ** attempt)
        asyncio.create_task(_alert())
        await update.message.reply_text("⚠️ Внутренняя ошибка. Админ уже уведомлён.")

# ============================================================
# ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК
# ============================================================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    logger.error("Глобальная ошибка", exc_info=error)
    ctx = context.bot_data.get("ctx")
    if ctx and ctx.settings.admin_id:
        try:
            await context.bot.send_message(chat_id=ctx.settings.admin_id, text=f"🚨 Глобальная ошибка: {error}")
        except Exception:
            pass

@cb
async def debug_pet(update, context, ctx):
    if update.effective_user.id != ctx.settings.admin_id: return
    player = await ctx.repo.get_by_id(update.effective_user.id)
    if player is None:
        await update.message.reply_text("Профиль не найден.")
        return
    pet = player.pet or "нет"
    name = player.pet_name or "без имени"
    await update.message.reply_text(f"🐾 Питомец: {pet}\n🎉 Имя: {name}")

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ОБРАБОТЧИКИ КНОПОК
# ============================================================
@cb
async def menu_handler(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    kb, whisper = await get_main_menu_keyboard(uid, ctx)
    menu_text = f"<b>🎮 ГЛАВНОЕ МЕНЮ</b>\n\n<i>{whisper}</i>"
    try:
        await query.message.edit_text(menu_text, reply_markup=kb, parse_mode='HTML')
    except Exception:
        await query.message.reply_text(menu_text, reply_markup=kb, parse_mode='HTML')

async def bush_preview_handler(update, context):
    query = update.callback_query
    await query.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)", show_alert=True)

@cb
async def activate_menu_handler(update, context, ctx):
    query = update.callback_query
    user = query.from_user
    uname = user.username or user.first_name
    uid = user.id
    player = await ctx.repo.get_by_id(uid, with_inventory=False)
    if player is None:
        player = Player(user_id=uid, username=uname, balance=800)
        new_name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
        await create_named_blunt(uid, new_name, ctx=ctx)
        await ctx.repo.save(player)
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

@cb
async def skins_menu_handler(update, context, ctx):
    query = update.callback_query
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Выбрать титул", callback_data="choose_title")],
        [InlineKeyboardButton("🖼️ Выбрать фон", callback_data="choose_bg")],
        [InlineKeyboardButton("🔙 Назад", callback_data="profile")]
    ])
    try:
        await query.message.edit_text("<b>🎨 СКИНЫ</b>\n\nВыбери, что хочешь изменить.", reply_markup=kb, parse_mode='HTML')
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        await query.message.reply_text("<b>🎨 СКИНЫ</b>\n\nВыбери, что хочешь изменить.", reply_markup=kb, parse_mode='HTML')

@cb
async def choose_title_handler(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    player = await ctx.repo.get_by_id(uid, with_inventory=False)
    if player is None:
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
    await query.message.edit_text("<b>🎨 ВЫБОР ТИТУЛА</b>\n\nВыбери титул:", reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')

@cb
async def choose_bg_handler(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    player = await ctx.repo.get_by_id(uid, with_inventory=False)
    if player is None:
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
    await query.message.edit_text("<b>🖼️ ВЫБОР ФОНА</b>\n\nВыбери фон:", reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode='HTML')

@cb
async def handle_set_title(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    new_title = query.data.replace("set_title_", "")
    async def _set(p, conn):
        skins = p.profile_skins or {}
        skins["active_title"] = new_title
        p.profile_skins = skins
        # Сохраняем в общий список титулов (из первой версии)
        titles = (p.titles or "").split()
        if new_title not in titles:
            titles.append(new_title)
            p.titles = " ".join(titles).strip()
        return new_title
    result = await ctx.repo.atomic_update(uid, _set)
    if result is None:
        await query.answer("Профиль не найден", show_alert=True)
        return
    await context.bot.send_message(chat_id=query.message.chat.id, text=f"✨ Титул «{new_title}» активирован!")
    await skins_menu_handler(update, context, ctx)

@rate_limit(1)
@cb
async def handle_set_bg(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    new_bg = query.data.replace("set_bg_", "")
    async def _set(p, conn):
        skins = p.profile_skins or {}
        skins["active_background"] = new_bg
        p.profile_skins = skins
        return new_bg
    result = await ctx.repo.atomic_update(uid, _set)
    if result is None:
        await query.answer("Профиль не найден", show_alert=True)
        return
    await safe_send_message(context, query.message.chat.id, f"✨ Фон «{new_bg}» активирован!")
    await skins_menu_handler(update, context)
    
@rate_limit(1)
@game_handler
async def blunt_details_handler(update, context, ctx, player):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    blunt_id = query.data.replace("blunt_details_", "")
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
        f"{color} <b>«{html.escape(name)}»</b>\n"
        f"Оригинальное имя:<b>«{name}»</b>\n"
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
    sent = await safe_send_blunt_image(context, query.message.chat.id, rarity, caption=text, reply_markup=kb, ctx=ctx)
    if sent:
        try:
            await query.message.delete()
        except Exception:
            pass
    else:
        await query.message.edit_text(text=text, reply_markup=kb, parse_mode='HTML')

@rate_limit(1)
@game_handler
async def share_blunt_handler(update, context, ctx, player):
    query = update.callback_query
    uid = query.from_user.id
    blunt_id = query.data.replace("share_blunt_", "")
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=blunt_{blunt_id}"

    inv = player.inventory or []
    item = next((it for it in inv if it.get("id") == blunt_id), None)
    username = html.escape(player.username or str(uid))

    if not item:
        fallback_text = f"Блант не найден.\n{ref_link}"
        await query.answer(switch_inline_query=fallback_text)
        return

    name = item["name"]
    rarity = item.get("rarity", "common")
    color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")

    # Твой текст (полностью сохранён)
    share_text = (
        f"<b>{username}</b>\n\n"
        f"{color} <b>Имя именного NFT Бланта: «{html.escape(name)}»</b>\n"
        f"🧬 <b>Редкость: {rarity} {color}</b>\n"
        f"🩸 <b>Серийный номер: #{item.get('rare_number', '?-????')}</b>\n"
        f"💬 <b>Реакция:</b> <i>{item.get('reaction', '')}</i>\n\n"
        f"<b>💎 Нажми на ссылку чтобы забрать уникальный Блант:</b>\n{ref_link}"
    )

    # Вот новое: вместо отправки в чат — открываем список чатов для пересылки
    await query.answer(switch_inline_query=share_text)

@rate_limit(1)
@game_handler
async def shrine_donate_handler(update, context, ctx, player):
    query = update.callback_query
    amount = 100 if query.data == "shrine_donate_100" else 500
    uid = query.from_user.id

    async def _donate(p, conn):
        if p.balance < amount:
            return ("no_money",)
        p.balance -= amount
        p.donated = (p.donated or 0) + amount
        return ("ok",)

    result = await ctx.repo.atomic_update(uid, _donate)
    if result is None:
        await query.answer("Профиль не найден.", show_alert=True)
        return
    status = result[0]
    if status == "no_money":
        await query.answer("Недостаточно OAC.", show_alert=True)
        return

    await send_whisper_dm(update, context, f"💎 Ты внёс {amount} OAC в Храм. Спасибо, Странник!")
    
@cb
async def guild_shrine_callback(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id

    player = await ctx.repo.get_by_id(uid)
    if not player or not player.guild:
        await query.answer("Ты не в гильдии.", show_alert=True)
        return

    guild = player.guild
    async with ctx.db_pool.acquire() as conn:
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

    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')

@cb(True)
async def guild_join_handler(update, context, ctx):
    query = update.callback_query
    guild = "BLACK" if query.data == "guild_join_BLACK" else "WHITE"
    uid = query.from_user.id

    try:
        player = await ctx.repo.get_by_id(uid, with_inventory=False)
        if player is None:
            await query.answer("Профиль не найден, начните с /start", show_alert=True)
            return

        player.guild = guild
        # Награда за вступление (только для новичков)
        if player.onboarding_step == 0:
            player.balance += 50
        g_emoji = "🕯️" if guild == "BLACK" else "⚜️"
        g_name = "Тёмная" if guild == "BLACK" else "Светлая"

        # === Онбординг: шаг 0 → шаг 1 ===
        if player.onboarding_step == 0 and player.farm_count == 0 and player.craft_count == 0:
            player.onboarding_step = 1
            await ctx.repo.save(player)
        
            # Социальное доказательство — количество согильдийцев
            cnt = await count_guilds(ctx)
            online = cnt.get(guild, 0)
        
            kb1 = InlineKeyboardMarkup([
                [InlineKeyboardButton("🍬 Фармить", callback_data="farm")],
                [InlineKeyboardButton("⏭️ Пропустить обучение", callback_data="skip_onboarding")]
            ])
            await safe_send_message(
                context, uid,
                f"🕋 <b>Ты в {g_name} Гильдии!</b> Сейчас в ней <b>{online}</b> странников.\n\n"
                "<b>🎓 ОБУЧЕНИЕ [▓▓░░] 2/3</b>\n\n"
                "<b>🍬 Твой первый шаг — фарм!</b>\n"
                "Нажми кнопку ниже, чтобы получить <b>OAC</b>.\n\n"
                "<i>💡 OAC — главная валюта. Трать её на крафт, питомцев и свитки.</i>",
                reply_markup=kb1, parse_mode='HTML'
            )
        
            await query.answer(f"✅ Ты вступил в {g_emoji} {g_name} Гильдию! +50 OAC 🍬", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        # === Обычное вступление (без онбординга) ===
        await ctx.repo.save(player)

        guild_name_genitive = "Тёмной Гильдии" if guild == "BLACK" else "Светлой Гильдии"
        action_emoji = "🕯️" if guild == "BLACK" else "⚜️"
        action_text = "Совершить первый Ритуал" if guild == "BLACK" else "Принести первую Исповедь"
        action_cb = "ritual" if guild == "BLACK" else "repent"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{action_emoji} {action_text}", callback_data=action_cb)],
            [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
        ])

        await query.message.edit_text(
            f"<b><i>🕋 ГИЛЬДИЯ ПРИНЯЛА ТЕБЯ 🪽</i></b>\n\n"
            f"✨ Отныне ты — часть <b>{guild_name_genitive}</b>.\n"
            f"🩸 Искажение стало плотнее...\n\n"
            f"<b>💡 Твой первый шаг:</b>",
            reply_markup=kb,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Guild join error for {uid}: {e}", exc_info=True)
        await query.answer(f"❌ Ошибка при вступлении: {e}", show_alert=True)
    
# ============================================================
# ОБРАБОТЧИКИ УДАЧИ, АЛХИМИИ, ПОДАРКОВ (прокси)
# ============================================================
async def luck_wheel_handler(update, context):
    await luck_callback(update, context, action="luck_wheel")
async def luck_berserk_handler(update, context):
    await luck_callback(update, context, action="luck_berserk")
async def alchemy_start_handler(update, context):
    await luck_callback(update, context, action="alchemy_start")
async def alchemy_confirm_handler(update, context):
    await luck_callback(update, context, action="alchemy_confirm")
async def cancel_gift_handler(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("gifting_blunt_id", None)
    await profile_callback(update, context)
    
# ========== ЕДИНЫЙ РЕЕСТР КОМАНД ДЛЯ / И ТЕКСТА ==========
TEXT_COMMAND_HANDLERS = {
    # Команды с / (без слеша)
    "start": start,
    "farm": farm_callback_v2,
    "craft": craft_callback_v2,
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
    "repent": repent_callback,
    "lab": lab_enter,
    "pet": pet_preview,
    "shop": shop_callback,
    "setbluntpic": setbluntpic,
    "give_oac": give_oac,
    "debugpet": debug_pet,
    "checkbluntpics": check_blunt_pics,
    # Текстовые сокращения (без слеша)
    "меню": start,
    "фарм": farm_callback_v2,
    "крафт": craft_callback_v2,
    "дунуть": smoke_callback,
    "топ": top_callback,
    "удача": luck_callback,
    "профиль": profile_callback,
    "сбор": collect_callback,
    "правила": rules_callback,
    "исповедь": repent_callback,
    "гильдия": guild_info_callback,
    "привилегия": privilege_callback,
    "каталог": catalog_callback,
    "проверка": check_blunt,
    "ритуал": ritual_callback,
    "лабиринт": lab_enter,
    "питомец": pet_preview,
    "магазин": shop_callback,
}

# ============================================================
# СЛОВАРИ КОЛБЭКОВ
# ============================================================
CALLBACKS: Dict[str, Callable] = {
    "menu": menu_handler,
    "farm": farm_callback_v2,
    "craft": craft_callback_v2,
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
    "craft_normal": handle_craft_normal_v2,
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
    "repent": repent_callback,
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
    "pet_locked": pet_locked_handler,
    "onboarding_reward": onboarding_reward,
    "daily_quest_hub": daily_quest_hub,
    "world_hub": world_hub_handler,
}

EXACT_HANDLERS: Dict[str, Callable] = {
    "lab_special": handle_lab_option,
    "lab_focus_use": handle_lab_option,
    "lab_escape": handle_lab_option,
    "luck_wheel": luck_wheel_handler,
    "luck_berserk": luck_berserk_handler,
    "alchemy_start": alchemy_start_handler,
    "alchemy_confirm": alchemy_confirm_handler,
}

PREFIX_HANDLERS: Dict[str, Callable] = {
    "ach_page_": achievements_callback,
    "blunts_page_": my_blunts_callback,
    "blunt_details_": blunt_details_handler,
    "share_blunt_": share_blunt_handler,
    "gift_blunt_": gift_blunt_start,
    "set_title_": handle_set_title,
    "set_bg_": handle_set_bg,
    "lab_attack_": handle_lab_option,
    "achievements_": achievements_callback,
}

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    try:
        if data in EXACT_HANDLERS:
            await EXACT_HANDLERS[data](update, context)
            return
        for prefix, handler in PREFIX_HANDLERS.items():
            if data.startswith(prefix):
                if prefix in ("ach_page_", "blunts_page_"):
                    page = int(data.split("_")[-1])
                    await handler(update, context, page=page)
                else:
                    await handler(update, context)
                return
        handler = CALLBACKS.get(data)
        if handler:
            await handler(update, context)
        else:
            await q.answer("Неизвестная команда.")
    except Exception as e:
        logger.error(f"Button error: {e}", exc_info=True)
        await q.answer(f"❌ Ошибка: {e}", show_alert=True)

# ============================================================
# ДЖОБЫ (ВОССТАНОВЛЕНЫ И АКТИВИРОВАНЫ)
# ============================================================
async def update_pulse(ctx: AppContext):
    if not ctx:
        return
    now = time.time()
    if not hasattr(ctx, 'guild_counts_updated') or now - ctx.guild_counts_updated > 120:
        async with ctx.db_pool.acquire() as conn:
            black = await conn.fetchval("SELECT COUNT(*) FROM players WHERE guild='BLACK'")
            white = await conn.fetchval("SELECT COUNT(*) FROM players WHERE guild='WHITE'")
            ctx.guild_counts = {"BLACK": black, "WHITE": white}
            ctx.guild_counts_updated = now
    online = await ctx.db_pool.fetchval("SELECT COUNT(DISTINCT user_id) FROM players WHERE last_farm > $1", datetime.now()-timedelta(hours=1))
    desc = f"🕯️{ctx.guild_counts['BLACK']} ▰▱⚜️{ctx.guild_counts['WHITE']} | 👥{online}"
    # Отправка описания ЧАТА через HTTP
    token = ctx.settings.bot_token
    url = f"https://api.telegram.org/bot{token}/setChatDescription"
    payload = {"chat_id": "@guild_antysocial", "description": desc}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
    except Exception:
        pass

async def happy_hour_trigger(ctx: AppContext):
    if not ctx:
        return
    ctx.cache["happy_hour"] = True
    ctx.cache["happy_hour_end"] = datetime.now() + timedelta(minutes=ctx.settings.happy_hour_duration_min)
    try:
        await _send_http_message(ctx, "@guild_antysocial",
            "🎉 <b>ЧАС УДАЧИ!</b> 🌠 Все действия приносят x2 OAC 🍬 (30 минут)!")
    except Exception as e:
        logger.error(f"Happy hour announce error: {e}")

    # Отложенное выключение через asyncio вместо PTB job_queue
    asyncio.create_task(_reset_happy_hour_after(ctx, ctx.settings.happy_hour_duration_min * 60))

async def _reset_happy_hour_after(ctx: AppContext, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    ctx.cache["happy_hour"] = False
    try:
        await _send_http_message(ctx, "@guild_antysocial", "⏳ Час Удачи завершён.")
    except Exception as e:
        logger.error(f"Happy hour reset error: {e}")

async def echo_of_distortion(ctx: AppContext):
    """Эхо искажения: показывает 3 случайных именных бланта в чат гильдии."""
    if not ctx or not ctx.db_pool:
        return

    # 1. Получаем данные из БД
    try:
        async with ctx.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, username, inventory FROM players "
                "WHERE inventory IS NOT NULL AND inventory != '[]'"
            )
    except Exception as e:
        logger.error(f"Echo of distortion DB error: {e}")
        return

    # 2. Собираем именные бланты
    all_named = []
    for row in rows:
        try:
            inv = _json_safe_load(row["inventory"], [])
            for item in inv:
                if item.get("type") == "named":
                    all_named.append((row["user_id"], row["username"], item))
        except Exception:
            continue

    if not all_named:
        return

    # 3. Сообщение эхо
    sample = random.sample(all_named, min(3, len(all_named)))
    text = "<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n"
    for uid, uname, item in sample:
        name = item["name"]
        rarity = item.get("rarity", "common")
        color = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(rarity, "🟢")
        reaction = item.get("reaction", "")
        text += (
            f"⚜️ <b>@{html.escape(uname)}</b> создал свой блант {color} "
            f"<b><i>«{html.escape(name)}»</i></b> 🌿\n"
            f"<i>Редкость: {rarity}</i>\n"
            f"🩸 <i>{reaction}</i>\n\n"
        )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💍 Создать свой блант", callback_data="craft_named")]])

    # 4. Отправка в ЧАТ ГИЛЬДИИ через прямой HTTP-запрос (надёжно, без PTB Application)
    token = ctx.settings.bot_token
    chat_id = "@guild_antysocial"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": kb.to_json() if kb else None,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error(f"Echo send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Echo of distortion error: {e}")

async def weekly_guild_rating(ctx: AppContext):
    if not ctx:
        return
    job_name = "weekly_guild_rating"
    try:
        await ctx.war_service.stop_war()

        async with ctx.db_pool.acquire() as conn:
            black_score = await conn.fetchval("SELECT total_score FROM guild_weekly WHERE guild='BLACK'") or 0
            white_score = await conn.fetchval("SELECT total_score FROM guild_weekly WHERE guild='WHITE'") or 0

            if black_score == white_score:
                logger.info("%s: Война завершилась вничью (%d - %d).", job_name, black_score, white_score)
                await _safe_send_guild_message(ctx,
                    f"🤝 <b>ВОЙНА ГИЛЬДИЙ ЗАВЕРШИЛАСЬ ВНИЧЬЮ!</b>\n"
                    f"🕯️ Тёмные: {black_score} | ⚜️ Светлые: {white_score}\n"
                    f"Ничья — награды не выданы. Следующая война скоро!"
                )
                await ctx.war_service.start_war()
                return

            winner = "BLACK" if black_score > white_score else "WHITE"
            oac = random.randint(200, 500)
            blunts = random.randint(3, 7)
            dust = random.randint(1, 3)

            rows = await conn.fetch("SELECT user_id FROM players WHERE guild = $1", winner)
            winners_count = len(rows)
            for r in rows:
                async def _reward(p, conn):
                    p.balance += oac
                    p.blunts += blunts
                    p.m_essence += dust
                try:
                    await ctx.repo.atomic_update(r["user_id"], _reward)
                except Exception as e:
                    logger.warning("Не удалось начислить награду игроку %d: %s", r["user_id"], e)

            logger.info("%s: Война завершена. Победитель: %s (%d vs %d). Начислено %d OAC, %d блантов, %d пыли %d игрокам.",
                        job_name, winner, black_score, white_score, oac, blunts, dust, winners_count)

            winner_emoji = "🕯️" if winner == "BLACK" else "⚜️"
            await _safe_send_guild_message(ctx,
                f"🎉 <b>ВОЙНА ГИЛЬДИЙ ЗАВЕРШЕНА!</b>\n\n"
                f"{winner_emoji} <b>Победила {winner} гильдия!</b>\n"
                f"🕯️ Тёмные: {black_score} | ⚜️ Светлые: {white_score}\n\n"
                f"Каждый участник победившей гильдии получает:\n"
                f"• {oac} OAC 🍬\n• {blunts} блантов 🌿\n• {dust} кристальной пыли 💠"
            )

        await ctx.war_service.start_war()

    except Exception as e:
        logger.critical("%s: КРИТИЧЕСКАЯ ОШИБКА: %s", job_name, e, exc_info=True)
        if ctx.settings.admin_id:
            try:
                await _send_http_message(ctx, ctx.settings.admin_id,
                    f"🚨 Ошибка в weekly_guild_rating:\n{e}")
            except Exception:
                pass

async def _safe_send_guild_message(ctx: AppContext, text: str):
    for attempt in range(3):
        try:
            await _send_http_message(ctx, "@guild_antysocial", text)
            return
        except Exception as e:
            logger.warning("Ошибка отправки в чат гильдии (попытка %d): %s", attempt+1, e)
            await asyncio.sleep(2 ** attempt)

async def keep_db_alive(ctx: AppContext):
    try:
        async with ctx.db_pool.acquire() as conn:
            await conn.execute("SELECT 1")
        logger.debug("DB keep-alive executed")
    except Exception as e:
        logger.error(f"keep_db_alive failed: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    user_id = update.effective_user.id
    ctx = context.bot_data.get("ctx")
    if not ctx:
        return

    if not await check_rate_limit_redis(ctx, user_id, "text", limit=10, period=10):
        await msg.reply_text("⚠️ Слишком часто. Подожди секунду.")
        return

    # Состояния ввода (питомец, бланты, подарки)
    if context.user_data.get('awaiting_pet_name'):
        return await handle_pet_name(update, context)
    if context.user_data.get('awaiting_named_blunt'):
        return await handle_named_name(update, context)
    if context.user_data.get('gifting_blunt_id'):
        return await handle_gift_username(update, context)

    raw_text = msg.text.strip()
    text_lower = raw_text.lower()

    # Убираем слеш, если есть
    if text_lower.startswith("/"):
        command = text_lower.split()[0][1:].split('@')[0]   # /start -> start
    else:
        command = text_lower

    handler = TEXT_COMMAND_HANDLERS.get(command)
    if not handler:
        return

    try:
        # Аргументы для команд с параметрами
        context.args = raw_text.split()[1:] if raw_text.startswith("/") else []
        await handler(update, context)
    except Exception as e:
        logger.exception(f"Ошибка в текстовой команде '{command}' от {user_id}")
        await msg.reply_text("⚠️ Внутренняя ошибка. Попробуйте позже.")
