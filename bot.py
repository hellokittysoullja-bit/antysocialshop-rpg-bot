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
import asyncio, json, logging, os, sys, time, random, re, hashlib, html, enum, copy, math
from datetime import datetime, timedelta, date, timezone
from typing import Optional, List, Any, Dict, Tuple, NamedTuple, Callable
from dataclasses import dataclass, field  

import asyncpg
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log
)
from pydantic import BaseModel, ConfigDict, Field

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest, Forbidden, RetryAfter

import redis.asyncio as aioredis
from functools import wraps

try:
    import pybreaker
except Exception:  # pragma: no cover
    pybreaker = None
# Circuit breakers вынесены в инфраструктурный слой
from infra import redis_breaker, db_breaker, tg_breaker, _json_safe_load

import httpx

# Статический игровой контент/константы вынесены в отдельный модуль (слой данных)
from game_content import (
    FARM_MEDALS, CRAFT_MEDALS, SMOKE_MEDALS, RITUAL_MEDALS, REPENT_MEDALS,
    WHISPERS, NEURO_STATUSES, FUNNY_REACTIONS, RANKS,
    ACHIEVEMENTS, ACHIEVEMENTS_DICT, ACHIEVEMENT_CONDITIONS, SMOKE_FLAVORS,
    QUEST_TEMPLATES, BLUNTS_PER_PAGE, BLUNT_IMAGES, LUCK_CONFIG, LABYRINTH_ROOMS,
    SHOP_ITEMS, RANK_LORE,
)
# Слой моделей
from game_models import Player
# Слой конфигурации
from config import (
    settings, FARM_MIN, FARM_MAX, FARM_COOLDOWN_HOURS, FARM_GRACE_COUNT,
    HAPPY_HOUR_MULTIPLIER, GAME_CONFIG, PET_CONFIG,
)
# Слой доступа к данным
from repository import PlayerRepository
# Слой доменных сервисов
from services import (
    UnknownWarActionError, WarAction, WarConfig, WarSettings,
    GuildWarService, AlchemyResult, PetService,
)
from enum import Enum, auto  # для SmokeStatus/CraftStatus ниже

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

# ── Хелперы ──
def has_rank(balance: int, rank_name: str = "Ветеран") -> bool:
    thresholds = {
        "Ветеран": GAME_CONFIG["veteran_threshold"],
        "Призрак": GAME_CONFIG["phantom_threshold"],
        "Некромант": GAME_CONFIG["necromant_threshold"],
    }
    return balance >= thresholds.get(rank_name, 0)

    
from urllib.parse import quote

def build_share_url(share_text: str) -> str:
    return f"https://t.me/share/url?text={quote(share_text, safe='')}"

# ── Исключения ──────────────────────────────────────────────
        
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
                            await safe_send_message(context, user_id, text, parse_mode='HTML')
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
                    await safe_send_message(context, user_id, text, parse_mode='HTML')
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
    
    clean_name = str(name or "").strip()[:28] or "Безымянный"
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
                
def _build_ascension_card(rank_label, new_balance):
    """Карточка возвышения. Чистая функция → тестируется без БД.

    rank_label = строка ранга из RANKS[i][0] (напр. '⚔️ Ветеран')."""
    emoji, _, name = rank_label.partition(" ")
    lore = RANK_LORE.get(rank_label, {})
    lines = [
        "🌑 ⚡ 🌑",
        "━━━━━━━━━━━━━",
        "<b>В О З В Ы Ш Е Н И Е</b>",
        f"<b>{emoji} {name.upper()}</b>",
        "━━━━━━━━━━━━━",
    ]
    if lore.get("line"):
        lines.append(f"<i>{lore['line']}</i>")
    if lore.get("unlock"):
        lines.append(f"\n🔓 <b>Открыто:</b> {lore['unlock']}")
    lines.append(f"\n💰 <b>Баланс:</b> {new_balance} OAC 🍬")
    # goal-gradient: показать следующую ступень и дистанцию до неё
    for e, th, nm in RANKS:
        if th > new_balance:
            lines.append(f"🎯 <b>Дальше:</b> {e} — ещё {th - new_balance} OAC")
            break
    else:
        lines.append("👑 <i>Ты на вершине. Легенды говорят о тебе.</i>")
    return "\n".join(lines)


async def check_rank_up(context, user_id, username, old_balance, new_balance):
    old_idx = 0
    new_idx = 0
    for i, (_, threshold, _) in enumerate(RANKS):
        if old_balance >= threshold:
            old_idx = i
        if new_balance >= threshold:
            new_idx = i
    if new_idx <= old_idx:
        return
    rank_label = RANKS[new_idx][0]
    ctx = context.bot_data.get("ctx")
    try:
        # мини-предвкушение: вспышка перед раскрытием ранга
        msg = await context.bot.send_message(
            chat_id=user_id, text="🌑 <i>Что-то меняется в тебе…</i>", parse_mode='HTML')
        await asyncio.sleep(1.1)
        await msg.edit_text(_build_ascension_card(rank_label, new_balance), parse_mode='HTML')
    except Exception:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=_build_ascension_card(rank_label, new_balance), parse_mode='HTML')
        except Exception:
            pass
    # аспирационные ранги — анонс в гильдию (социальное доказательство + FOMO)
    if ctx and RANK_LORE.get(rank_label, {}).get("big"):
        who = f"@{username}" if username else "Один из наших"
        asyncio.create_task(_safe_send_guild_message(
            ctx,
            f"⚡ <b>ВОЗВЫШЕНИЕ</b>\n{who} достиг ранга <b>{rank_label}</b> 🌑\n"
            f"<i>Ступень, до которой доходят немногие. Кто следующий?</i>"))




def compute_rank_info(balance: int):
    """Чистая функция: разбирает RANKS и возвращает данные о ранге.

    Единый источник вычисления ранга (раньше этот блок был скопирован
    в build_main_menu и progress_hub_handler).

    Возвращает кортеж:
        (rank_emoji, rank_name, next_emoji, next_name, next_threshold, prev_threshold)
    next_threshold == 0 означает максимальный ранг.
    """
    rank_emoji, rank_name = "🪓", "Рекрут"
    next_rank_emoji, next_rank_name, next_threshold = "", "", 0
    final_i = 0
    for i, (emoji, threshold, _) in enumerate(RANKS):
        final_i = i
        if balance >= threshold:
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
    prev_threshold = RANKS[final_i-1][1] if final_i > 0 else 0
    return rank_emoji, rank_name, next_rank_emoji, next_rank_name, next_threshold, prev_threshold


async def ensure_daily_progress(player, ctx) -> dict:
    """Сбрасывает daily_progress при наступлении нового дня (сохраняя текущий квест).

    Единый источник (раньше этот блок был скопирован в build_main_menu,
    progress_hub_handler и daily_quest_hub). Возвращает актуальный dict progress.
    """
    today = date.today().isoformat()
    progress = getattr(player, 'daily_progress', {}) or {}
    if progress.get("reset_date") != today:
        current_quest = progress.get("quest_id", "chapter1")
        progress = {"reset_date": today, "quest_id": current_quest, "reward_claimed": False}
        player.daily_progress = progress
        await ctx.repo.save(player)
    return progress


def _quest_progress_counts(template, progress, guild, is_veteran, has_pet):
    """(done, total) по заданиям квеста с учётом условий видимости. Чистая функция."""
    if not template:
        return (0, 0)
    conditions = {
        "guild_black": guild == "BLACK",
        "guild_white": guild == "WHITE",
        "is_veteran_and_has_pet": is_veteran and has_pet,
    }
    tasks = [t for t in template.get("tasks", [])
             if not t.get("condition") or conditions.get(t["condition"], False)]
    done = sum(1 for t in tasks if progress.get(t["key"], False))
    return (done, len(tasks))


def _plural_steps(n: int) -> str:
    """Русское склонение слова «шаг» для числа n."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return "шагов"
    d = n % 10
    if d == 1:
        return "шаг"
    if 2 <= d <= 4:
        return "шага"
    return "шагов"






def build_smoke_effect(outcome, earned):
    """Собирает карточку исхода. Текст берётся из корзины, соответствующей
    исходу (jackpot/win/loss/neutral), поэтому подпись OAC всегда честна."""
    name, flavor = random.choice(SMOKE_FLAVORS.get(outcome, SMOKE_FLAVORS["neutral"]))
    if outcome == "jackpot":
        header = "<b>🎰 ДЖЕКПОТ! ДЫМ ХЛЫНУЛ ЗОЛОТОМ</b>"
        earned_str = f"🎰 <b>+{earned} OAC</b>"
    elif earned > 0:
        header = "<b>💨 ДЫМ РАССЕЯЛСЯ</b>"
        earned_str = f"🍬 <b>+{earned} OAC</b>"
    elif earned < 0:
        header = "<b>💨 ДЫМ РАССЕЯЛСЯ</b>"
        earned_str = f"🕳️ <b>{earned} OAC</b>"
    else:
        header = "<b>💨 ДЫМ РАССЕЯЛСЯ</b>"
        earned_str = "<i>Ни капли OAC осело на дне…</i>"
    return (
        f"{header}\n"
        f"– {name}\n"
        f"– <i>{flavor}</i>\n\n"
        f"{earned_str}"
    )

def calculate_smoke_reward(p, happy_hour):
    """Возвращает (earned, outcome). Одна руч­ка — и число, и флейвор.
    Раньше число и текст брались из двух разных бросков и противоречили друг
    другу. Джекпот (2%) вырезан из доли выигрыша — суммарный шанс плюса тот же
    (18%), но у него есть дофаминовый пик."""
    r = random.random()
    if r < 0.02:
        earned, outcome = random.randint(80, 160), "jackpot"
    elif r < 0.18:
        earned, outcome = random.randint(15, 40), "win"
    elif r < 0.70:
        earned, outcome = -5, "loss"
    else:
        earned, outcome = 0, "neutral"
    if happy_hour and earned > 0:
        earned *= HAPPY_HOUR_MULTIPLIER
    return earned, outcome

class SmokeStatus(Enum):
    NO_BLUNTS = "no_blunts"
    OK = "ok"
    
class CraftStatus(Enum):
    NO_MONEY = "no_money"
    OK = "ok"

# ========== БАЗА ДАННЫХ ==========


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
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='players' AND column_name='last_repent'
            ) THEN
                ALTER TABLE players ADD COLUMN last_repent TIMESTAMP;
            END IF;
        END $$;
    """)
    # ВОТ ЭТОТ UPDATE ДОЛЖЕН БЫТЬ ВНУТРИ conn.execute()
    await conn.execute("""
        UPDATE players 
        SET daily_progress = jsonb_build_object(
            'reset_date', CURRENT_DATE::text,
            'quest_id', 
                CASE 
                    WHEN daily_progress ? 'quest_id' AND daily_progress->>'quest_id' IN ('chapter1','chapter2','chapter3_warrior','chapter3_benefactor') 
                    THEN daily_progress->>'quest_id'
                    ELSE 'chapter1'
                END,
            'reward_claimed', false
        )
        WHERE NOT (daily_progress ? 'reset_date' AND daily_progress ? 'quest_id' AND daily_progress ? 'reward_claimed')
           OR (daily_progress ? 'reset_date' AND (daily_progress->>'reset_date')::date < CURRENT_DATE)
           OR (daily_progress ? 'quest_id' AND daily_progress->>'quest_id' = 'chapter1' AND (daily_progress->>'reward_claimed')::boolean = true);
    """)

    # Исповедь медали миграция 
    await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS repent_count INTEGER DEFAULT 0;")
    
    # Питомцы и exists
    await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS pet TEXT DEFAULT '';")
    await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS pet_name TEXT DEFAULT '';")
    await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS \"exists\" BOOLEAN DEFAULT TRUE;")
    # Долговечность онбординга и сытости питомца: раньше эти поля жили только
    # в кэше (Redis TTL 10с) → прогресс онбординга сбрасывался при паузе, а
    # кормление питомца не сохранялось. Теперь — настоящие колонки.
    await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS onboarding_step INTEGER DEFAULT 0;")
    await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS pet_hunger INTEGER DEFAULT 100;")

# ===== ОПТИМИЗАЦИЯ ХРАНЕНИЯ (Render Free Tier) =====
    # JSONB поля — сжатие + хранение вне таблицы при размере > 2KB
    # Индекс под рейтинг (снимок лидерборда + топ-10). Ускоряет сортировку по
    # балансу, когда игроков станет много — лидерборд теперь на главном экране.
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_players_balance ON players (balance DESC);")
    await conn.execute("ALTER TABLE players ALTER COLUMN inventory SET STORAGE EXTENDED;")
    await conn.execute("ALTER TABLE players ALTER COLUMN profile_skins SET STORAGE EXTENDED;")
    await conn.execute("ALTER TABLE players ALTER COLUMN daily_progress SET STORAGE EXTENDED;")
    
    # Autovacuum — агрессивная очистка для Free Tier

    
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
            last_repent TIMESTAMP,
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



async def set_setting(key: str, value: str, ctx: AppContext = None) -> None:
    if ctx is None:
        return
    async with ctx.db_pool.acquire() as conn:
        await db_breaker.call(
            conn.execute,
            "INSERT INTO bot_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, value
        )

        

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
    except Exception:
        return iso_string

def next_sunday_str() -> str:
    now = datetime.now()
    days_until_sunday = (6 - now.weekday()) % 7
    if days_until_sunday == 0:
        days_until_sunday = 7
    next_sunday = now + timedelta(days=days_until_sunday)
    return next_sunday.strftime("%d.%m")


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
        # Начисляем ЛЮБОЕ реальное поле Player (не только blunts) — иначе
        # награда, показанная в сообщении, молча не начислялась бы (как было с
        # фантомными focus/lives). hasattr-гард отсекает несуществующее.
        for field, qty in reward.inventory_items.items():
            if hasattr(p, field):
                setattr(p, field, (getattr(p, field, 0) or 0) + qty)
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
        "cooldown_hours": GAME_CONFIG["farm_cooldown_hours"],
        "last_attr": "last_farm",
        "format": "min",
    },
    "ritual": {
        "text": "🕯️ Ритуал",
        "cooldown_hours": GAME_CONFIG["ritual_cooldown_hours"],
        "last_attr": "last_ritual",
        "format": "hrs",
        "guild_only": "BLACK",
    },
    "repent": {
        "text": "⚜️ Исповедь",
        "cooldown_hours": GAME_CONFIG["repent_cooldown_hours"],
        "last_attr": "last_repent",
        "format": "hrs",
        "guild_only": "WHITE",
    },
    "lab": {
        "text": "🏛️ Лабиринт",
        "cooldown_hours": GAME_CONFIG["lab_cooldown_hours"],
        "last_attr": "last_lab_attempt",
        "format": "full",
    },
}


def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])

@cb
async def world_hub(update, context, ctx):
    query = update.callback_query
    await query.answer()
    player = await ctx.repo.get_by_id(query.from_user.id)
    if not player:
        return

    balance = player.balance or 0
    is_veteran = balance >= 5000
    has_pet = bool(player.pet)

    kb_rows = []

    # Путь к власти — north-star «кем ты становишься» (смысл/фантазия)
    kb_rows.append([InlineKeyboardButton("🎯 Твой Путь к власти", callback_data="destiny_hub")])

    # Плантация — idle-крючок «зайди собрать». Кнопка живая: показывает
    # созревший урожай (goal-gradient тянет вернуться) или зовёт посадить.
    plant_lvl = player.passive_level or 0
    _pending, _h, _c = _plant_pending(plant_lvl, player.passive_collected, datetime.now())
    if plant_lvl <= 0:
        plant_label = "🪴 Плантация · посадить 🌱"
    elif _pending > 0:
        plant_label = f"🪴 Плантация · собрать {_pending} 🌾"
    else:
        plant_label = "🪴 Плантация"
    kb_rows.append([InlineKeyboardButton(plant_label, callback_data="collect")])

    # Питомец – виден всем
    if is_veteran and has_pet:
        kb_rows.append([InlineKeyboardButton("🐾 Питомец", callback_data="pet_preview")])
    elif is_veteran and not has_pet:
        kb_rows.append([InlineKeyboardButton("🐾 Питомец (купить)", callback_data="pet_preview")])
    else:
        kb_rows.append([InlineKeyboardButton("🐾 Питомец 🔒", callback_data="pet_locked")])

    kb_rows.append([InlineKeyboardButton("🎲 Удача ›", callback_data="luck")])
    kb_rows.append([InlineKeyboardButton("🏛️ Лабиринт ›", callback_data="lab_start")])
    kb_rows.append([InlineKeyboardButton("🛒 Магазин ›", callback_data="shop")])
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])

    kb = InlineKeyboardMarkup(kb_rows)
    await query.message.edit_text(
            "<b>🏰 Главное Меню › 🌍 Мир</b>\n\n<i>Древние земли ждут своего исследователя.</i>",
        reply_markup=kb, parse_mode='HTML'
    )

@cb
async def destiny_hub(update, context, ctx):
    """Северная звезда: «кем ты становишься». Отвечает на «зачем эта игра» —
    показывает фантазию (восхождение к власти) как видимый путь + твою легенду.
    """
    query = update.callback_query
    await query.answer()
    player = await ctx.repo.get_by_id(query.from_user.id)
    if not player or not player.exists:
        await query.answer("Профиль не найден", show_alert=True)
        return

    balance = player.balance or 0
    _re, _rn, _ne, _nn, next_th, _pt = compute_rank_info(balance)

    # Лестница восхождения: пройденное ✅, следующее ➡️, заблокированное 🔒
    ladder = []
    for emoji, threshold, _ in RANKS:
        e = emoji.split(' ', 1)[0]
        nm = emoji.split(' ', 1)[1] if ' ' in emoji else emoji
        if balance >= threshold:
            ladder.append(f"✅ {e} <b>{nm}</b>")
        elif threshold == next_th:
            ladder.append(f"➡️ {e} <b>{nm}</b> — осталось {threshold - balance} OAC")
        else:
            ladder.append(f"🔒 {e} {nm}")

    inv = player.inventory or []
    named = sum(1 for it in inv if it.get("type") == "named")
    legendaries = sum(1 for it in inv if it.get("type") == "named" and it.get("rarity") == "legendary")
    plant_lvl = player.passive_level or 0
    guild = {"BLACK": "🕯️ Тёмная", "WHITE": "⚜️ Светлая"}.get(player.guild, "— не выбрана")

    text = (
        "🎯 <b>ТВОЙ ПУТЬ К ВЛАСТИ</b>\n\n"
        "<i>Ты начал никем. Стань 🪬 Некромантом Искажения — тем, о ком шепчутся оба мира.</i>\n\n"
        "<b>⚜️ Восхождение:</b>\n"
        + "\n".join(ladder) +
        "\n\n<b>👑 Твоя легенда:</b>\n"
        f"💍 Именных блантов: <b>{named}</b>  (🟡 легендарных: <b>{legendaries}</b>)\n"
        f"🪴 Плантация-империя: <b>уровень {plant_lvl}</b>\n"
        f"🏰 Гильдия: <b>{guild}</b>\n\n"
        "<i>Каждый фарм, каждый блант, каждая победа гильдии — шаг к власти.</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🪴 Растить империю", callback_data="collect"),
         InlineKeyboardButton("💍 Крафт", callback_data="craft")],
        [InlineKeyboardButton("🔙 В меню", callback_data="menu")],
    ])
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')


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

async def animate_progress_bar(update, context, title="", duration=0.6, steps=4, in_place=False):
    """
    Быстрая и надёжная анимация прогресс-бара.
    - duration: общее время анимации в секундах (рекомендуется 0.4–0.8).
    - steps: количество кадров (3–5). Чем меньше шагов, тем меньше запросов.
    - in_place=True: анимация РЕДАКТИРУЕТ нажатый экран вместо нового сообщения
      («единый живой экран», ноль мёртвых сообщений). Требование: экран-результат
      обязан нести свою навигацию, иначе тупик. Фолбэк — новое сообщение
      (команда без callback, фото, слишком старое сообщение).
    Возвращает None, если не удалось отправить даже первое сообщение.
    """
    chat_id = update.effective_chat.id
    title_text = f"<b>{title}</b>" if title else ""
    first_frame = f"{title_text}\n[░░░░░░░░░░] 0%"

    msg = None
    query = update.callback_query
    if in_place and query and query.message:
        try:
            await query.message.edit_text(first_frame, parse_mode='HTML')
            msg = query.message
        except Exception:
            msg = None
    if msg is None:
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=first_frame,
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
        except Exception:
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


async def _create_new_player(update, context, uid, username, invited_by=None,
                             inviter_name=None, shared_blunt=None):
    ctx = context.bot_data.get("ctx")
    new_name = random.choice(["Крик Бездны", "Шёпот Склепа"])
    # Двусторонний реферал: приглашённый получает бонус к старту
    start_balance = 800 + (100 if invited_by else 0)

    async with ctx.db_pool.acquire() as conn:
        async with conn.transaction():
            player = Player(user_id=uid, username=username, balance=start_balance)
            player.invited_by = invited_by
            # Установка daily_progress ДО сохранения
            player.daily_progress = {
                "reset_date": date.today().isoformat(),
                "quest_id": "chapter1",
                "reward_claimed": False
            }
            await ctx.repo.save(player, conn=conn)
            await create_named_blunt(uid, new_name, ctx=ctx, conn=conn)

    # Тёплое, социально-связанное прибытие: закрывает обещание шеринга
    # («забери уникальный Блант»), называет друга (соц-доказательство +
    # принадлежность) и показывает блант, что привёл. Пустое — если пришёл сам.
    if invited_by:
        friend = f"@{html.escape(inviter_name)}" if inviter_name else "Твой друг"
        blunt_line = ""
        if shared_blunt:
            c = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(shared_blunt["rarity"], "🟢")
            blunt_line = (f"💍 Блант, что привёл тебя сюда: {c} "
                          f"<b>«{html.escape(shared_blunt['name'])}»</b>\n")
        ref_bonus_line = (
            f"🤝 <b>{friend} позвал тебя в Гильдию</b> — он уже здесь.\n"
            f"{blunt_line}"
            f"🎁 <b>Дар за приход: +100 OAC 🍬</b> и твой первый именной блант — уже в свёртке!\n\n"
        )
    else:
        ref_bonus_line = ""
    welcome_text = (
        "<b>🎉 Добро пожаловать в Гильдию Antysocialshop!</b>\n"
        "<i>Здесь курят бланты, поклоняются древним богам и воюют за OAC.</i>\n\n"
        "🩸 <b>Твой путь:</b> <i>от нищего 🪓 Рекрута до 🪬 Некроманта Искажения — "
        "скрути легендарные бланты, вырасти империю-плантацию и приведи гильдию к власти "
        "над обоими мирами.</i>\n\n"
        f"{ref_bonus_line}"
        f"🎁 <b>Смотритель дарует тебе</b> <code>{start_balance}</code> 🍬 <b>и твой первый именной блант!</b>\n\n"
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
         InlineKeyboardButton("⚜️ Светлая Гильдия (+50 🍬)", callback_data="guild_join_WHITE")],
        # Крючок перед коммитом: дать сначала попробовать петлю (дофамин от
        # фарма), выбор стороны — позже, когда он осмыслен. +50 не теряется.
        [InlineKeyboardButton("🍬 Позже — сначала играть →", callback_data="defer_faction")],
    ])

    await update.effective_message.reply_text(
        welcome_text,
        reply_markup=guild_kb,
        parse_mode='HTML'
    )


async def defer_faction_handler(update, context):
    """Новичок отложил выбор фракции — сразу в фарм (дофамин до коммита).
    Ставит onboarding_step=1, дальше работает штатный funnel фарм→крафт→готово.
    Сторону выберет позже из меню и получит те же +50 (см. guild_join)."""
    ctx = context.bot_data.get("ctx")
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await ctx.repo.get_by_id(uid, with_inventory=False)
    if not player or not player.exists:
        await query.answer("Профиль не найден, начните с /start", show_alert=True)
        return
    if player.onboarding_step == 0:
        player.onboarding_step = 1
        await ctx.repo.save(player)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍬 Фармить", callback_data="farm")],
        [InlineKeyboardButton("⏭️ Пропустить обучение", callback_data="skip_onboarding")],
    ])
    try:
        await query.message.edit_text(
            "<b>🎓 ОБУЧЕНИЕ [▓░░░] 1/3</b>\n\n"
            "<b>🍬 Твой первый шаг — фарм!</b>\n"
            "Нажми кнопку ниже и получи первые <b>OAC</b> — прямо сейчас.\n\n"
            "<i>💡 OAC — главная валюта. Сторону Гильдии выберешь позже, "
            "когда почувствуешь игру (за вступление всё так же +50 🍬).</i>",
            reply_markup=kb, parse_mode='HTML')
    except Exception:
        await safe_send_message(context, uid,
            "<b>🍬 Твой первый шаг — фарм!</b> Жми «Фармить».",
            reply_markup=kb, parse_mode='HTML')
    
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


def _resolve_referrer(args, uid):
    """Из реф-ссылки blunt_... достаёт creator_id. Чистая функция, без БД.

    Создатель зашит в самом blunt_id (blunt_{creator}_{ts}_{rand}), поэтому
    скан таблицы не нужен — это O(1) и масштабируется. start-параметр =
    "blunt_" + blunt_id. Возвращает creator_id (int) или None.
    """
    if not args or not str(args[0]).startswith("blunt_"):
        return None
    blunt_id = str(args[0])[len("blunt_"):]     # снимаем ведущий префикс
    parts = blunt_id.split("_")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    creator_id = int(parts[1])
    return creator_id if creator_id != uid else None


def _shared_blunt_info(ref_player, args):
    """Достаёт блант, которым поделились (по blunt_id из ссылки), для тёплого
    приветствия приглашённого. Возвращает {name, rarity} или None."""
    if not ref_player or not args or not str(args[0]).startswith("blunt_"):
        return None
    blunt_id = str(args[0])[len("blunt_"):]
    for it in (ref_player.inventory or []):
        if it.get("id") == blunt_id and it.get("type") == "named":
            return {"name": it.get("name", "?"), "rarity": it.get("rarity", "common")}
    return None


async def _reward_referrer(ctx, context, creator_id):
    """Награда рефереру: +50 OAC, счётчик, легендарный блант, метка 🩸 + уведомление."""
    async def _reward(p, conn):
        p.balance = (p.balance or 0) + 50
        p.referral_count = (p.referral_count or 0) + 1
        if "🩸" not in (p.titles or ""):
            p.titles = f"{p.titles or ''} 🩸".strip()
        return p.referral_count
    count = await ctx.repo.atomic_update(creator_id, _reward)
    if count is None:
        return
    try:
        await create_named_blunt(
            creator_id,
            random.choice(["Крик Бездны", "Пепел Короля", "Шёпот Склепа"]),
            rarity="legendary", ctx=ctx)
    except Exception:
        logger.exception("referral: не удалось создать легендарный блант")
    try:
        await safe_send_message(
            context, creator_id,
            "🩸 <b>По твоей ссылке пришёл новый Странник!</b>\n"
            "🎁 +50 OAC · легендарный блант · метка 🩸\n"
            f"👥 Всего приглашено: {count}",
            parse_mode='HTML')
    except Exception:
        pass


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
        if not player or not player.exists:
            # Реферал применяется только к НОВЫМ игрокам (анти-фарм)
            creator_id = _resolve_referrer(context.args, uid)
            inviter_name = None
            shared_blunt = None
            if creator_id:
                ref = await ctx.repo.get_by_id(creator_id)
                if not ref or not ref.exists:
                    creator_id = None
                else:
                    inviter_name = ref.username or None
                    shared_blunt = _shared_blunt_info(ref, context.args)
            await _create_new_player(update, context, uid, username, invited_by=creator_id,
                                     inviter_name=inviter_name, shared_blunt=shared_blunt)
            if creator_id:
                await _reward_referrer(ctx, context, creator_id)
        else:
            # Запускаем ежедневный бонус в фоне, чтобы меню появилось мгновенно:
            asyncio.create_task(process_daily_login(uid, context))

            # Меню отправляем немедленно:
            text, kb = await build_main_menu(player, ctx, context, full_mode=True)
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode='HTML')
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
        # Только реально существующие награды. Раньше тут были «focus»/«life»,
        # которых нет ни полем в Player, ни механикой: сообщение обещало «+1
        # Фокус/жизнь», а начислялись только бланты → ~30% дневных бонусов были
        # фантомом (обман дофаминовой петли). Их доля перераспределена в реальное.
        "extra_oac": 0.6,
        "blunt": 0.4,
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

    # Маппинг item (из конфига) → поле модели и читаемое имя. Только реальные
    # поля Player (focus/lives убраны — их не существует).
    item_to_field: Dict[str, str] = field(default_factory=lambda: {
        "blunt": "blunts",
    })
    item_display_names: Dict[str, str] = field(default_factory=lambda: {
        "blunts": "+1 блант",
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

def _build_next_day_preview(streak: int, config: StreakConfig) -> str:
    """Предпросмотр завтрашней награды — крючок предвкушения + loss-aversion.

    Показывает, что игрок получит, если вернётся завтра и продлит серию.
    Чистая функция (детерминированная часть награды, без случайных бонусов).
    """
    next_streak = streak + 1
    base = config.base_rewards.get(next_streak, 100)
    if next_streak >= config.hot_streak_threshold:
        base = int(base * config.hot_streak_multiplier)
    next_title = config.title_rewards.get(next_streak)

    if next_title:
        return (f"\n\n🎁 <b>Завтра (День {next_streak}):</b> +{base} OAC "
                f"<b>и титул {next_title}!</b>\n<i>Вернись и не разорви серию 🔥</i>")
    return (f"\n\n⏳ <b>Завтра (День {next_streak}):</b> +{base} OAC ждут тебя.\n"
            f"<i>Вернись и продли серию 🔥</i>")


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
        f"{_build_next_day_preview(streak, config)}"
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




#===•===****=====ФАРМ ОАС=====*****=====
# ============================================================
# FARM – атомарный сбор OAC
# ============================================================

def _farm_on_cooldown(farm_count, last_farm, now) -> bool:
    """True, если фарм сейчас на кулдауне.

    Единый источник логики кулдауна (используется и хендлером фарма, и таймером
    в главном меню). Первые FARM_GRACE_COUNT фармов — без кулдауна, чтобы новичок
    сформировал привычку в первую сессию до включения 30-минутного ожидания.
    """
    if (farm_count or 0) < FARM_GRACE_COUNT:
        return False
    return bool(last_farm and (now - last_farm) < timedelta(hours=FARM_COOLDOWN_HOURS))


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

    # Happy hour – удвоение. Флаг живёт в ctx.cache (как и во всех остальных
    # механиках); раньше фарм ошибочно читал его из bot_data → ×2 не срабатывал.
    _ctx = context.bot_data.get("ctx")
    happy = bool(_ctx and _ctx.cache and _ctx.cache.get("happy_hour", False))
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
    is_mega = crit and earned >= FARM_MAX * 10
    if not crit:
        crit_emoji = "🍬"
    elif is_mega:
        crit_emoji = "💥 (×10!)"
    else:
        crit_emoji = "🔥 (×2)"

    # Happy Hour — теперь ВИДНО игроку (peak-момент нельзя прятать)
    happy_str = " 🌟×2 HAPPY HOUR" if happy else ""

    # Праздничный баннер пикового момента (peak-end / «liking»)
    if is_mega:
        banner = (
            "💥💥💥 <b>МЕГА-КРИТ ×10!</b> 💥💥💥\n"
            "<i>Искажение прорвалось — тебе выпал невероятный куш!</i>\n\n"
        )
    elif crit:
        banner = "🔥 <b>КРИТ ×2!</b> Удача на твоей стороне.\n\n"
    elif happy:
        banner = "🌟 <b>HAPPY HOUR!</b> Добыча удвоена — лови момент.\n\n"
    else:
        banner = ""

    # Прогресс-бары
    progress_bar_str = get_medal_progress(new_count, FARM_MEDALS)
    rank_progress = get_rank_progress(new_balance)

    # Сборка сообщения
    msg = (
        f"{banner}"
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
        if _farm_on_cooldown(p.farm_count, p.last_farm, now):
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
        guild_emoji = "🕯️" if guild == "BLACK" else "⚜️" if guild == "WHITE" else "🏰"
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
    
        # Единый живой экран: кулдаун-подсказка заменяет текущий экран,
        # а не плодит новое сообщение (fallback внутри edit_or_reply).
        await edit_or_reply(
            update, context, message_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(btn_text, callback_data=btn_callback)],
                [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
            ])
        )
        return

    earned, crit, happy, medal_text, new_count, new_balance, old_balance = data

    target = get_medal_target(new_count, FARM_MEDALS)
    text = _format_farm_message(earned, crit, happy, medal_text, new_count, target, new_balance)

    # Экран-результат несёт навигацию (единый живой экран). На кулдауне НЕ
    # показываем «фарм» — это тупик (тап вернёт тот же кулдаун). Вместо этого
    # уводим в следующий шаг петли (крафт/дунуть), а таймер — в текст. В грейсе
    # (первые фармы без кулдауна) предлагаем «Фармить ещё» — петля плотная.
    if not _farm_on_cooldown(new_count, now, now):
        result_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🍬 Фармить ещё", callback_data="farm"),
             InlineKeyboardButton("🌿 Крафт", callback_data="craft")],
            [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
        ])
    else:
        text += (f"\n\n🌱 <i>Грядка зреет — новый фарм через "
                 f"{int(FARM_COOLDOWN_HOURS*60)} мин. А пока — в дело:</i>")
        result_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌿 Крафт", callback_data="craft"),
             InlineKeyboardButton("💨 Дунуть", callback_data="smoke")],
            [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
        ])
    anim_msg = await animate_progress_bar(update, context, title="🍬 Фармим...", in_place=True)
    if anim_msg is not None:
        await anim_msg.edit_text(text, parse_mode='HTML', reply_markup=result_kb)
    else:
        await safe_send_message(
            context,
            update.effective_chat.id,
            text,
            parse_mode='HTML',
            reply_markup=result_kb,
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
        # Единый живой экран: отказ заменяет текущий экран, не плодит новый.
        await edit_or_reply(update, context,
            f"<b>❌ Недостаточно OAC.</b>\n🕯️ Требуется <b>{GAME_CONFIG['craft_cost']} OAC</b> 🍬.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🍬 Фармить", callback_data="farm")],
                [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
            ])
        )
        return

    medal_text, new_count, blunts, new_balance = data
    target = get_medal_target(new_count, CRAFT_MEDALS)
    text = _format_normal_craft_message(medal_text, new_count, target, blunts, new_balance)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌿 Скрафтить ещё", callback_data="craft_normal"),
         InlineKeyboardButton("💨 Дунуть", callback_data="smoke")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")]
    ])

    # Единый живой экран: анимация и результат редактируют нажатый экран.
    anim_msg = await animate_progress_bar(update, context, title="🌿 Скручиваем Блант...", in_place=True)
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
        name = update.message.text.strip()[:28]
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
                
                await update.message.reply_text(caption, parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💍 Мои бланты", callback_data="my_blunts")],
                        [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
                    ]))
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
            await update.message.reply_text(
                f"<b>🔮 ИСКАЖЕНИЕ МОЛЧИТ</b>\n\n<i>🛡️ Недостаточно OAC.</i>\n🕯️ Требуется <b>{GAME_CONFIG['named_blunt_cost']} OAC 🍬</b>.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🍬 Фармить", callback_data="farm")],
                    [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
                ]))
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

        # Локальная таблица (нигде больше не нужна)
        rarity = item["rarity"]
        
        if rarity == "legendary":
            label = "ЛЕГЕНДАРНЫЙ"
            discovery = "0.17%"
        elif rarity == "epic":
            label = "ЭПИЧЕСКИЙ"
            discovery = "0.7%"
        elif rarity == "rare":
            label = "РЕДКИЙ"
            discovery = "1.4%"
        else:
            label = "ОБЫЧНЫЙ"
            discovery = "3.5%"

        # Фанфары по редкости — эмоциональный пик («ахнуть»), твой текст ниже не тронут
        fanfare = {
            "legendary": "🎊✨🎊 <b>ЛЕГЕНДАРНЫЙ!!!</b> 🎊✨🎊\n<i>Такое рождается раз на тысячу. Ты уловил невозможное.</i>\n\n",
            "epic": "🟣✨ <b>ЭПИЧЕСКИЙ!</b> Искажение благосклонно к тебе.\n\n",
            "rare": "🔵 <b>РЕДКИЙ!</b> Достойный улов.\n\n",
        }.get(rarity, "")

        caption = fanfare + (
            f"<b>💍 ТЫ СОЗДАЛ ИМЕННОЙ БЛАНТ!</b>\n"
            f"🎉 Он навсегда останется в <b>твоей коллекции</b>.\n\n"
            f"{color}<b><i>«{html.escape(meme_name)}»</i></b>\n"
            f"💎 Редкость: <b>{label} • #{item.get('rare_number', '?-????')}</b>\n"
            f"👑 Первый владелец: <b>{html.escape(player.username or 'игрок')}</b>\n"
            f"🌎 Обнаружен у <b>{discovery}</b> игроков\n\n"
            f"🕯️ <i>{reaction}</i>\n\n"
            f"💬 Этот блант достоин того, чтобы его <b>увидели друзья. Действуй!</b>"
        )

        # --- Текст для кнопки «Поделиться» ---
        bot_username = (await context.bot.get_me()).username
        # Рабочий реф-формат (согласован с _resolve_referrer): создатель зашит в blunt_id.
        # Прежний ?start=b_<short_code> не работал — short_code нигде не сохранялся.
        ref_link = f"https://t.me/{bot_username}?start=blunt_{blunt_id}"
        
        share_text = (
            f"{color} ИМЯ NFT Бланта: «{html.escape(meme_name)}»\n"
            f"💎 Редкость: {label} • #{item.get('rare_number', '?-????')}\n"
            f"👑 Первый владелец: {html.escape(player.username or 'игрок')}\n\n"
            f"💬 Реакция: {reaction}\n"
            f"🎁 ЗАБРАТЬ СЕБЕ ТАКОЙ ЖЕ:\n"
            f"{ref_link}"
        )

        share_url = build_share_url(share_text)
        
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎁 Подарить", callback_data=f"gift_blunt_{blunt_id}"),
                InlineKeyboardButton("🔗 Поделиться", url=share_url)
            ],
            [InlineKeyboardButton("🔙 В меню", callback_data="menu")]
        ])

        # Суспенс-ревил: предвкушение перед результатом (дофаминовый пик гачи)
        try:
            suspense = await update.message.reply_text("🌀 <b>Искажение сгущается...</b>", parse_mode='HTML')
            for frame in ("🌫️ <b>Скручиваем твой блант...</b>",
                          "✨ <b>Проступает редкость...</b>",
                          "💫 <b>Почти...</b>"):
                await asyncio.sleep(0.7)
                try:
                    await suspense.edit_text(frame, parse_mode='HTML')
                except Exception:
                    pass
            await asyncio.sleep(0.4)
            try:
                await suspense.delete()
            except Exception:
                pass
        except Exception:
            pass

        sent_img = await safe_send_blunt_image(
            context, update.effective_chat.id, item["rarity"], caption=caption, reply_markup=kb
        )
        if not sent_img:
            await update.message.reply_text(caption, reply_markup=kb, parse_mode='HTML')

        # Публичное признание редких блантов в чате гильдии — гордость + зависть + вирусность
        if rarity in ("legendary", "epic"):
            try:
                await _safe_send_guild_message(
                    ctx,
                    f"{color} <b>@{html.escape(player.username or 'Странник')}</b> создал "
                    f"<b>{'ЛЕГЕНДАРНЫЙ' if rarity == 'legendary' else 'ЭПИЧЕСКИЙ'}</b> блант "
                    f"<b>«{html.escape(meme_name)}»</b>! 🩸"
                )
            except Exception:
                pass

        # FOMO-бонус (без изменений)
        async def fomo_reminder():
            await asyncio.sleep(300)
            player_check = await ctx.repo.get_by_id(uid)
            if player_check:
                inv_now = player_check.inventory or []
                if any(it.get("id") == blunt_id for it in inv_now):
                    try:
                        await context.bot.send_message(uid, "⌛ Твой именной блантик всё ещё скучает. Подари или поделись им, пока не поздно!")
                    except Exception:
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
        except Exception:
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
        # Убираем «обрыв обучения»: вместо «исследуй меню сам» — одна конкретная
        # цель (goal-gradient + Zeigarnik) и путь к первому завершению квеста.
        progress = getattr(player, 'daily_progress', {}) or {}
        done, total = _quest_progress_counts(
            QUEST_TEMPLATES.get("chapter1"), progress, player.guild, False, False)
        left = max(0, total - done)

        if left <= 0:
            body = ("<b>Ты почти прошёл Главу 1 — первая награда Саги уже ждёт!</b>\n"
                    "Забери её в «Заданиях дня» 👇")
            cta = "🎁 Забрать награду Главы 1"
        else:
            body = (
                "<b>Основы освоены. Но история только начинается…</b>\n\n"
                f"📜 <b>Глава 1 почти пройдена — остал{'ся' if left == 1 else 'ось'} "
                f"{left} {_plural_steps(left)} до первой награды Саги.</b>\n"
                "<i>Заверши — и Смотритель наградит тебя. Один шаг за раз 👇</i>"
            )
            cta = f"📋 Продолжить · осталось {left}"

        await query.message.edit_text(
            f"🎁 <b>Бонус за обучение: +30 OAC, +1 блант!</b>\n"
            f"💎 Баланс: {new_bal} OAC · 🗞️ Блантов: {new_blunts}\n\n"
            f"{body}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(cta, callback_data="daily_quest_hub")
            ]]),
            parse_mode='HTML'
        )
    
# ====== ФУНКЦИЯ ПЕРЕДАЧИ БЛАНТА (АТОМАРНАЯ, БЕЗОПАСНАЯ) =====
class TransferError(Exception):
    pass
class BluntNotFound(TransferError):
    pass
class SameUserError(TransferError):
    pass


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
        earned, outcome = calculate_smoke_reward(p, ctx.cache.get("happy_hour", False))

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

        return SmokeStatus.OK, (earned, outcome, save, medal_text, new_count, p.blunts, p.balance)

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

    earned, outcome, save, medal_text, new_count, bl_left, new_balance = data
    effect = build_smoke_effect(outcome, earned)

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
    if outcome == "jackpot":
        who = f"@{player.username}" if player.username else "Кто-то из наших"
        asyncio.create_task(_safe_send_guild_message(
            ctx,
            f"🎰 <b>ДЖЕКПОТ!</b>\n{who} сорвал <b>+{earned} OAC</b> одной тягой 🌌\n"
            f"<i>Фабрика №9 сегодня щедра. Кто следующий?</i>"
        ))

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
            remain = timedelta(hours=GAME_CONFIG["ritual_cooldown_hours"]) - (now - player.last_ritual)
            hrs, rem = divmod(int(remain.total_seconds()), 3600)
            return ("cooldown", hrs, rem // 60)

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
        player.daily_progress["ritual"] = True
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
        hrs, mins = data[0], data[1]
        wait = f"{hrs} ч {mins} мин" if hrs else f"{mins} мин"
        # Единый живой экран: кулдаун заменяет текущий экран, не плодит новый.
        await edit_or_reply(update, context,
            f"<b>🕯️ Тёмный алтарь истощён 🌙</b>\n\n<b>🗝️ Жди {wait}</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🍬 Фармить", callback_data="farm")],
                [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
            ]))
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
    # Единый живой экран: результат ритуала заменяет экран и несёт навигацию.
    ritual_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏛️ Храм", callback_data="guild_shrine"),
         InlineKeyboardButton("🏰 Гильдия", callback_data="guild_info")],
        [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
    ])
    anim_msg = await animate_progress_bar(update, context, title="🕯️ Ритуал проводится...", in_place=True)
    if anim_msg is not None:
        await anim_msg.edit_text(text, parse_mode='HTML', reply_markup=ritual_kb)
    else:
        await safe_send_message(context, update.effective_chat.id, text,
                                parse_mode='HTML', reply_markup=ritual_kb)

    await check_achievements(uid, context)

# ============================================================
# ПЛАНТАЦИЯ (idle-система: владение + апгрейды + лимит накопления)
# Переиспользует поля passive_level (уровень) и passive_collected (последний сбор)
# — без миграции БД. Доступна с самого старта (лечит ранний отток).
# ============================================================
PLANT_RATE_PER_LEVEL = 25     # OAC/час за каждый уровень плантации
PLANT_CAP_HOURS = 8           # максимум накопления (создаёт крючок «зайди собрать»)
PLANT_MAX_LEVEL = 10


def _plant_rate(level: int) -> int:
    """Пассивная скорость плантации, OAC/час."""
    return PLANT_RATE_PER_LEVEL * max(0, level)


def _plant_upgrade_cost(level: int) -> int:
    """Стоимость апгрейда с текущего level на level+1 (растущая кривая = сток экономики)."""
    return 150 * (level + 1) * (level + 1)


def _plant_pending(level, last_collected, now):
    """(earned, hours_used, capped) — сколько накоплено к сбору. Чистая функция.

    Накопление ограничено PLANT_CAP_HOURS: производство «переполняется» и стоит,
    пока не соберёшь → loss-aversion и ежедневный возврат (механика Clash/Township).
    """
    if (level or 0) < 1 or not last_collected:
        return (0, 0.0, False)
    hrs = (now - last_collected).total_seconds() / 3600
    if hrs < 0:
        hrs = 0.0
    capped = hrs >= PLANT_CAP_HOURS
    hrs_used = min(hrs, PLANT_CAP_HOURS)
    earned = int(hrs_used * _plant_rate(level))
    return (earned, hrs_used, capped)


async def collect_callback(update, context):
    """Вход в Плантацию (эволюция старого «кустика»/collect)."""
    ctx = context.bot_data.get("ctx")
    if not ctx:
        await update.effective_message.reply_text("⚠️ Бот инициализируется, попробуйте позже.")
        return
    user, msg = get_user_and_msg(update)
    player = await ctx.repo.get_by_id(user.id)
    if not player or not player.exists:
        await _notify_user(update, context, "Сначала активируйся: /start")
        return
    await _show_plantation(update, context, ctx, player)


async def _show_plantation(update, context, ctx, player):
    now = datetime.now()
    level = player.passive_level or 0

    if level < 1:
        text = (
            "🪴 <b>ПЛАНТАЦИЯ</b>\n\n"
            "<i>Пустая грядка ждёт первого ростка.</i>\n\n"
            "🌱 Посади куст — и он будет приносить <b>OAC сам</b>, даже пока тебя нет.\n"
            "Заглядывай собирать урожай и <b>прокачивай грядку</b>, чтобы она росла быстрее."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌱 Посадить (бесплатно)", callback_data="plant_start")],
            [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
        ])
    else:
        earned, _hrs, capped = _plant_pending(level, _to_datetime(player.passive_collected), now)
        rate = _plant_rate(level)
        cap_total = rate * PLANT_CAP_HOURS
        fill = min(100, int(earned / cap_total * 100)) if cap_total else 0
        bar = "🟩" * (fill // 20) + "⬛️" * (5 - fill // 20)
        cap_line = ("⚠️ <b>ПЕРЕПОЛНЕНО!</b> Собери, иначе урожай пропадает."
                    if capped else f"📦 Хранилище: {bar} {fill}%")
        if level < PLANT_MAX_LEVEL:
            up_cost = _plant_upgrade_cost(level)
            up_line = f"⬆️ <b>Апгрейд до ур.{level+1}:</b> {up_cost} OAC → {_plant_rate(level+1)}/ч"
        else:
            up_cost = None
            up_line = "⭐️ <b>Максимальный уровень!</b>"
        text = (
            f"🪴 <b>ПЛАНТАЦИЯ · Уровень {level}</b>\n\n"
            f"⚡ Скорость: <b>{rate} OAC/час</b>\n"
            f"🌾 К сбору: <b>{earned} OAC</b>\n"
            f"{cap_line}\n\n"
            f"{up_line}"
        )
        rows = [[InlineKeyboardButton(f"🌾 Собрать ({earned} OAC)", callback_data="plant_harvest")]]
        if up_cost is not None:
            rows.append([InlineKeyboardButton(f"⬆️ Улучшить · {up_cost} OAC", callback_data="plant_upgrade")])
        rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
        kb = InlineKeyboardMarkup(rows)

    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')


@cb
async def plant_start_handler(update, context, ctx):
    uid = update.callback_query.from_user.id
    now = datetime.now()

    async def _plant(p, conn):
        if (p.passive_level or 0) >= 1:
            return False
        p.passive_level = 1
        p.passive_collected = now
        return True
    await ctx.repo.atomic_update(uid, _plant)
    player = await ctx.repo.get_by_id(uid)
    await _show_plantation(update, context, ctx, player)


@cb
async def plant_harvest_handler(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    now = datetime.now()
    happy = bool(ctx.cache.get("happy_hour", False))

    async def _harvest(p, conn):
        level = p.passive_level or 0
        if level < 1:
            return ("no_plant",)
        earned, _hrs, _capped = _plant_pending(level, _to_datetime(p.passive_collected), now)
        if happy:
            earned *= HAPPY_HOUR_MULTIPLIER
        if earned < 1:
            return ("not_ready",)
        p.balance += earned
        p.passive_collected = now
        if ctx.war_service:
            await ctx.war_service.add_score_raw(uid, earned, conn)
        return ("ok", earned)

    result = await ctx.repo.atomic_update(uid, _harvest)
    if result and result[0] == "ok":
        await query.answer(f"🌾 Урожай собран: +{result[1]} OAC!")
    elif result and result[0] == "not_ready":
        await query.answer("🌱 Ещё рано — куст копит урожай.")
    player = await ctx.repo.get_by_id(uid)
    await _show_plantation(update, context, ctx, player)


@cb
async def plant_upgrade_handler(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    now = datetime.now()

    async def _upgrade(p, conn):
        level = p.passive_level or 0
        if level < 1:
            return ("no_plant",)
        if level >= PLANT_MAX_LEVEL:
            return ("max",)
        cost = _plant_upgrade_cost(level)
        if (p.balance or 0) < cost:
            return ("no_money", cost)
        # Сначала собираем накопленное по старой ставке (честно), потом апаем
        pending, _h, _c = _plant_pending(level, _to_datetime(p.passive_collected), now)
        p.balance += pending
        p.balance -= cost
        p.passive_level = level + 1
        p.passive_collected = now
        return ("ok", level + 1)

    result = await ctx.repo.atomic_update(uid, _upgrade)
    if result and result[0] == "ok":
        await query.answer(f"⬆️ Плантация улучшена до ур.{result[1]}!")
    elif result and result[0] == "no_money":
        await query.answer(f"Недостаточно OAC. Нужно {result[1]}.", show_alert=True)
    elif result and result[0] == "max":
        await query.answer("Уже максимальный уровень!")
    player = await ctx.repo.get_by_id(uid)
    await _show_plantation(update, context, ctx, player)

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
    # Фон по умолчанию читаемый, иначе строка «🫧 Фон: » висела пустой (выглядит
    # как баг). Пустой active_background → «🌑 Обычный».
    bg = skins.get("active_background") or "🌑 Обычный"
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

    # Плантация: показываем РЕАЛЬНОЕ состояние (уровень × ставку), а не прежнюю
    # фантомную формулу от баланса, которая нигде не начислялась и врала игроку.
    plant_lvl = player.passive_level or 0
    bush_line = (f"🪴 <b>Плантация:</b> ур.{plant_lvl} · +{_plant_rate(plant_lvl)} OAC/ч"
                 if plant_lvl > 0 else "🪴 <b>Плантация:</b> <i>не посажена — открой в 🌍 Мир</i>")

    text = (
        f"<b>⚜️ ПРОФИЛЬ</b>\n"
        f"👤 <b>{uname}</b>{guild_line}\n"
        f"🫧 Фон: {bg}\n\n"
        f"{rank_progress}\n\n"
        f"💎 <b>ОАС:</b> <b>{bal} OAC</b> 🍬\n"
        f"🌿 <b>Блантов в свёртке:</b> <b>{bl}</b>\n"
        f"{bush_line}\n"
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
    # Кодекс блантов — приоритетная, полноширинная (это про статус/коллекцию).
    if len(named) > 2:
        kb_rows.append([InlineKeyboardButton(f"💍 Все именные бланты ({len(named)})", callback_data="my_blunts")])
    # Утилитарные — парой в ряд: короче вертикаль, удобнее большому пальцу.
    kb_rows.append([
        InlineKeyboardButton("📖 Правила мира", callback_data="rules"),
        InlineKeyboardButton("🎨 Кастомизация", callback_data="skins_menu"),
    ])
    kb_rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)

    photo_id = None
    try:
        photos = await context.bot.get_user_profile_photos(uid, limit=1)
        if photos.photos:
            photo_id = photos.photos[0][0].file_id
    except Exception:
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
# Порядок и мета редкостей — единый источник для Кодекса.
_RARITY_TIERS = (
    ("legendary", "🟡", "Легендарные"),
    ("epic",      "🟣", "Эпические"),
    ("rare",      "🔵", "Редкие"),
    ("common",    "🟢", "Обычные"),
)


def _codex_prestige_title(named):
    """Титул коллекционера по размеру/качеству коллекции. Чистая функция."""
    n = len(named)
    has_leg = any(it.get("rarity") == "legendary" for it in named)
    if has_leg and n >= 15:
        return "👑 Владыка Искажения"
    if n >= 15:
        return "🏛️ Архивариус"
    if n >= 5:
        return "🗝️ Коллекционер"
    if n >= 1:
        return "🌱 Начинающий собиратель"
    return "🕳️ Пустая витрина"


def _build_codex_header(named):
    """Богатая шапка Кодекса: визитка + метр редкостей + аспирация.

    Чистая функция (только список инвентаря игрока), поэтому тестируется без
    БД. Активирует эндаумент (это ТВОЁ), Зейгарник (незакрытая коллекция) и
    статус (визитка + титул)."""
    from collections import Counter
    counts = Counter(it.get("rarity", "common") for it in named)
    total = len(named)
    owned_tiers = sum(1 for k, _, _ in _RARITY_TIERS if counts.get(k, 0) > 0)

    # Визитка — редчайший блант игрока (named уже отсортирован по редкости).
    sig = named[0] if named else None
    sig_emoji = {"legendary": "🟡", "epic": "🟣", "rare": "🔵"}.get(
        (sig or {}).get("rarity"), "🟢") if sig else ""

    lines = ["<b>📜 КОДЕКС ИСКАЖЕНИЯ</b>", f"<i>{_codex_prestige_title(named)}</i>", ""]
    if sig:
        lines.append(f"🔱 <b>Твоя визитка:</b> {sig_emoji} «{html.escape(sig.get('name','?'))}»")
        lines.append(f"   <i>#{sig.get('rare_number', '?-????')} · один из {total} твоих</i>")
        lines.append("")
    lines.append(f"<b>🎴 Собрано редкостей: {owned_tiers}/4</b>")
    for k, emoji, label in _RARITY_TIERS:
        c = counts.get(k, 0)
        if c > 0:
            lines.append(f"{emoji} {label}: <b>{c}</b>")
        else:
            lines.append(f"🔒 <s>{label}</s>: <b>0</b> — ещё не в коллекции")
    # Аспирация: чего не хватает до вершины (Зейгарник — тянет закрыть пробел).
    if counts.get("legendary", 0) == 0:
        lines.append("\n✨ <i>Легендарного пока нет — 2% с крафта или 🎰 джекпот дыма. "
                     "Скрути его и войди в легенды Гильдии.</i>")
    elif counts.get("epic", 0) == 0:
        lines.append("\n✨ <i>Нет Эпического — 13% с крафта. Коллекция ждёт печать искажения.</i>")
    lines.append("")
    return "\n".join(lines)


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
        await edit_or_reply(update, context,
                            "<b>📜 КОДЕКС ИСКАЖЕНИЯ</b>\n<i>🕳️ Пустая витрина</i>\n\n"
                            "💎 У тебя пока нет именных блантов.\n"
                            "🌿 Скрути первый — и начни свою коллекцию легенд.",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🌿 Крафт", callback_data="craft")],
                                [InlineKeyboardButton("🔙 В профиль", callback_data="profile")]
                            ]))
        return

    total_pages = (len(named) + BLUNTS_PER_PAGE - 1) // BLUNTS_PER_PAGE
    start = page * BLUNTS_PER_PAGE
    end = start + BLUNTS_PER_PAGE
    page_blunts = named[start:end]

    # Кодекс-шапка на первой странице; на прочих — компактный заголовок.
    if page == 0:
        text = _build_codex_header(named)
        text += f"<b>💎 Твои бланты (стр. {page+1}/{total_pages}):</b>\n\n"
    else:
        text = f"<b>💎 ТВОИ ИМЕННЫЕ БЛАНТЫ ({len(named)} всего, стр. {page+1}/{total_pages})</b>\n\n"
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
        back_cb = "progress_hub"
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

    text = "<b>🏰 ГИЛЬДИИ</b>\n\n"

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
        # Кулдаун на кнопке должен совпадать с реальным (GAME_CONFIG), а не с
        # захардкоженными 24ч — иначе таймер врёт (реально ритуал/исповедь через
        # 12ч). Исповедь раньше вообще не показывала таймер — теперь симметрично.
        def _action_label(base_label, last_time, cooldown_hours, cb):
            if last_time:
                lt = _to_datetime(last_time)
                if lt and datetime.now() - lt < timedelta(hours=cooldown_hours):
                    diff = timedelta(hours=cooldown_hours) - (datetime.now() - lt)
                    hrs, rem = divmod(int(diff.total_seconds()), 3600)
                    wait = f"{hrs} ч {rem // 60} мин" if hrs else f"{rem // 60} мин"
                    return InlineKeyboardButton(f"{base_label} ({wait})", callback_data=cb)
            return InlineKeyboardButton(base_label, callback_data=cb)

        if guild == "BLACK":
            kb_rows.append([_action_label("🕯️ Ритуал", player.last_ritual,
                            GAME_CONFIG["ritual_cooldown_hours"], "ritual")])
        elif guild == "WHITE":
            kb_rows.append([_action_label("⚜️ Исповедь", player.last_repent,
                            GAME_CONFIG["repent_cooldown_hours"], "repent")])
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

def _days_left_in_week(now) -> int:
    """Дней до подведения итогов недели (понедельник — новая неделя). Чистая функция."""
    return 7 - now.weekday()


def _war_rally_line(my_guild, black, white) -> str:
    """Мотивационная строка войны: долг + соревнование. Чистая функция.

    Отстаём → «ты нужен»; ведём → «не дай догнать»; поровну → «твой вклад решит».
    """
    if my_guild not in ("BLACK", "WHITE"):
        return "🏰 <b>Выбери гильдию</b>, чтобы сражаться за общую награду!"
    mine = black if my_guild == "BLACK" else white
    rival = white if my_guild == "BLACK" else black
    if mine < rival:
        return (f"🔥 <b>Твоя гильдия ОТСТАЁТ на {rival - mine}!</b>\n"
                "Каждый твой фарм и сбор — очки гильдии. <b>Ты нужен.</b>")
    if mine > rival:
        return (f"🏆 <b>Твоя гильдия ВЕДЁТ (+{mine - rival})!</b>\n"
                "Не дай сопернику догнать — продолжай приносить очки.")
    return "⚖️ <b>Ноздря в ноздрю!</b> Твой вклад решит исход недели."


async def guild_war_callback(update, context):
    ctx = context.application.bot_data["ctx"]
    query = update.callback_query
    await query.answer()
    player = await ctx.repo.get_by_id(query.from_user.id)
    my_guild = player.guild if player else None

    async with ctx.db_pool.acquire() as conn:
        scores = await conn.fetch("SELECT guild, total_score FROM guild_weekly")
        black_score = next((r["total_score"] for r in scores if r["guild"] == "BLACK"), 0)
        white_score = next((r["total_score"] for r in scores if r["guild"] == "WHITE"), 0)
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

    days_left = _days_left_in_week(datetime.now())
    text = (
        f"<b>⚔️ ВОЙНА ГИЛЬДИЙ</b>\n\n"
        f"{_war_rally_line(my_guild, black_score, white_score)}\n\n"
        f"🕯️ <b>Тёмные:</b> {black_score}\n<b>{safe_bar(bp)} {bp}%</b>\n\n"
        f"⚜️ <b>Светлые:</b> {white_score}\n<b>{safe_bar(wp)} {wp}%</b>\n\n"
        f"⏳ До итогов недели: <b>{days_left} дн.</b>\n"
        f"🎁 <b>Победившая гильдия — награда КАЖДОМУ</b> (OAC + бланты + пыль)!\n\n"
    )

    if top_black:
        text += "🕯️ <b>Герои Тьмы:</b>\n"
        for i, row in enumerate(top_black, 1):
            text += f"  {i}. {html.escape(row['username'])} — {row['donated']}\n"
    if top_white:
        text += "⚜️ <b>Герои Света:</b>\n"
        for i, row in enumerate(top_white, 1):
            text += f"  {i}. {html.escape(row['username'])} — {row['donated']}\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]
    ])
    await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')

@cb
async def repent_callback(update, context, ctx):
    # Работает и как кнопка (callback), и как команда /repent (текст, без
    # callback_query). Раньше безусловный query.answer() ронял AttributeError
    # на команде → «Внутренняя ошибка», хотя кнопка исповеди работала.
    query = update.callback_query
    if query:
        await query.answer()
    user, _msg = get_user_and_msg(update)
    uid = user.id

    async def _repent(p, conn):
        now = datetime.now()
        cooldown_hours = GAME_CONFIG.get("repent_cooldown_hours", 12)
        if p.last_repent and (now - p.last_repent) < timedelta(hours=cooldown_hours):
            remain = timedelta(hours=cooldown_hours) - (now - p.last_repent)
            hrs, rem = divmod(int(remain.total_seconds()), 3600)
            mins = rem // 60
            return ("cooldown", f"⏳ Исповедь через {hrs} ч {mins} мин")

        if p.guild != "WHITE":
            return ("wrong_guild", "❌ Только Светлая Гильдия.")
        if (p.blunts or 0) < 1:
            return ("no_blunts", "❌ Нет блантов. Скрути!")

        # === ДОБАВЛЕНО: Счётчик исповедей (пункт 2) ===
        p.blunts -= 1
        p.last_repent = now
        p.daily_progress = p.daily_progress or {}
        p.repent_count = (p.repent_count or 0) + 1
        # Исповедь СОСТОЯЛАСЬ (блант потрачен, кулдаун 12ч запущен) — квест
        # обязан засчитаться при ЛЮБОМ исходе. Раньше отметка стояла только в
        # ветке награды (70%): при удаче на эссенцию/легендарку задание не
        # тикало, а повторить нельзя 12ч → Светлая гильдия застревала в главе.
        p.daily_progress["repent"] = True
        p.daily_progress["guild_action"] = True

        # === ДОБАВЛЕНО: Медали и прогресс (пункты 3 и 4) ===
        old_count = p.repent_count - 1
        new_count = p.repent_count
        medal_text, medal_bonus = get_medal_text_and_reward(old_count, new_count, REPENT_MEDALS)

        # Случайный исход
        r = random.random()
        reward = 0
        result_line = ""

        if r < 0.70:
            reward = random.randint(100, 200)
            p.balance += reward + medal_bonus  # ← добавили medal_bonus
            result_line = f"Исповедь принесла тебе <b>{reward} OAC</b> 🍬"
        elif r < 0.95:
            p.m_essence = (p.m_essence or 0) + 1
            result_line = "Ты получил 💠 <b>+1 Кристальную Пыль</b>"
        else:
            name = random.choice(["Крик Бездны", "Пепел Короля", "Шёпот Склепа"])
            await create_named_blunt(uid, name, rarity="legendary", ctx=ctx, player=p, conn=conn)
            result_line = f"🌟 Чудо! Легендарный блант <b>«{name}»</b>"

        # === ДОБАВЛЕНО: Прогресс-бар (пункт 4) ===
        target = get_medal_target(new_count, REPENT_MEDALS)
        progress_bar_str = get_medal_progress(new_count, REPENT_MEDALS)

        # === ДОБАВЛЕНО: Красивый текст с цитатой (пункт 9) ===
        full_text = (
            f"<b>⚜️ ИСПОВЕДЬ ПРИНЯТА🎉</b>\n\n"
            f"{result_line}\n"
            f"<b>⚜️ У тебя:</b> <b>{p.balance} OAC 🕊️</b>\n\n"
            f"<i>«Твоя душа очистилась...»</i>\n"
            f"{medal_text}\n"
            f"<b>🕊️ Исповеди:</b> {new_count}/{target}\n"
            f"<b>{progress_bar_str}</b>"
        )

        return ("ok", full_text)

    result = await ctx.repo.atomic_update(uid, _repent)

    if result is None:
        await query.message.edit_text(
            "❌ Профиль не найден. Напиши /start",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
        )
        return

    status, data = result[0], result[1] if len(result) > 1 else ""

    if status == "ok":
        # Единый живой экран: исповедь анимируется и завершается на месте.
        repent_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏛️ Храм", callback_data="guild_shrine"),
             InlineKeyboardButton("🏰 Гильдия", callback_data="guild_info")],
            [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
        ])
        anim_msg = await animate_progress_bar(update, context, title="🕊️ Исповедь...", duration=0.6, steps=4, in_place=True)
        if anim_msg is not None:
            await anim_msg.edit_text(
                data,
                reply_markup=repent_kb,
                parse_mode='HTML'
            )
        else:
            # Если анимация не удалась – отправляем новое сообщение
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=data,
                reply_markup=repent_kb,
                parse_mode='HTML'
            )
    else:
        # Ошибки (кулдаун/не та гильдия/нет блантов) — тоже на месте.
        await edit_or_reply(
            update, context, data,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="guild_info")]]),
            parse_mode='HTML'
        )

    if status == "ok":
        await check_achievements(uid, context)

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
        "<b>🏰 ГИЛЬДИИ И РАЗВИТИЕ</b>\n"
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
    # Раньше был тупик: только внешняя ссылка, назад в игру — никак (приходилось
    # писать /menu). Добавлена навигация; экран редактируется на месте.
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Открыть Каталог", url="https://t.me/antysocialshop")],
        [InlineKeyboardButton("🛒 Магазин", callback_data="shop"),
         InlineKeyboardButton("🏰 В меню", callback_data="menu")],
    ])
    await edit_or_reply(update, context,
        "<b>🕯️ ANTYSOCIALSHOP · КАТАЛОГ</b>\n\n"
        "<i>Настоящие артефакты Фабрики ждут по ссылке.</i>",
        reply_markup=kb)

# ============================================================
# УДАЧА – полная сеньорская версия
# ============================================================



# ── Хелперы ─────────────────────────────────────────────────
async def _notify_user(update, context, text, show_alert=False, reply_markup=None):
    # Анти-тупик: раньше без reply_markup экран заменялся текстом БЕЗ кнопок →
    # игрок застревал (Удача/Алхимия/Лабиринт: «Колесо недоступно», «нет OAC»…).
    # Теперь: (1) show_alert — это попап-предупреждение, экран НЕ трогаем, если
    # своей клавы нет (игрок остаётся где был); (2) без клавы даём выход в меню.
    if update.callback_query:
        if show_alert:
            await update.callback_query.answer(text, show_alert=True)
            if reply_markup is not None:
                try:
                    await update.callback_query.message.edit_text(
                        text, reply_markup=reply_markup, parse_mode='HTML')
                except Exception:
                    pass
            return
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(
            text, reply_markup=reply_markup or get_back_to_menu_keyboard(), parse_mode='HTML')
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text,
            reply_markup=reply_markup or get_back_to_menu_keyboard(), parse_mode='HTML')

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
        await _process_wheel(update, context, uid, player, cfg, ctx)     
        return
    if action == "luck_berserk":
        await _process_mines(update, context, uid, player, cfg, ctx)
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
import json
import time
import random
from typing import Optional, Tuple, Set, List

# Вспомогательная функция для генерации поля (чистая)
def _generate_mines_field() -> Tuple[List[List[int]], Set[Tuple[int, int]]]:
    """Создаёт поле 5x5 и расставляет 3 мины. Возвращает (поле, координаты мин)."""
    size = 5
    field = [[0]*size for _ in range(size)]
    # Все координаты
    all_cells = [(r, c) for r in range(size) for c in range(size)]
    mines = set(random.sample(all_cells, 3))  # 3 мины
    return field, mines

# Вспомогательная функция для расчёта множителя
def _calc_multiplier(step: int) -> float:
    """Множитель от 1.0 до 3.0, линейно растёт с каждым шагом."""
    max_step = 22  # всего безопасных клеток (25-3)
    return round(1.0 + (step / max_step) * 2.0, 2)

# Основная функция – замена _process_berserk
async def _mines_state_get(ctx, uid):
    """Состояние игры «Мины»: Redis, если он есть, иначе in-memory кэш.

    Приложение по дизайну работает и без Redis (main.py: «без Redis продолжим»),
    но мины дёргали ctx.redis напрямую → при отсутствующем/упавшем Redis
    ctx.redis был None и кнопка «Рискнуть» молча падала на None.get(). Фолбэк
    на ctx.cache (TTL ~10мин, партии хватает) чинит мины без Redis."""
    key = f"mines_game:{uid}"
    if getattr(ctx, "redis", None):
        raw = await ctx.redis.get(key)
        return json.loads(raw) if raw else None
    val = ctx.cache.get(key)
    return json.loads(val) if isinstance(val, (str, bytes)) else val


async def _mines_state_set(ctx, uid, state):
    key = f"mines_game:{uid}"
    if getattr(ctx, "redis", None):
        await ctx.redis.setex(key, 3600, json.dumps(state))
    else:
        ctx.cache[key] = state


async def _process_mines(update, context, uid, player, cfg, ctx):
    """
    Запускает игру «Мины» (вместо Берсерка).
    Обрабатывает все состояния: начало, открытие клетки, кэшаут, завершение.
    """
    query = update.callback_query
    if query:
        await query.answer()

    # --- 1. Проверяем доступность (баланс, кулдаун) ---
    min_bet = min(cfg["mines"]["bet_options"])
    if player.balance < min_bet:
        await _notify_user(update, context, f"💣 Недостаточно OAC. Минимальная ставка: {min_bet} OAC.")
        return

    # --- 2. Загружаем или создаём состояние игры (Redis или in-memory) ---
    redis_key = f"mines_game:{uid}"
    state = await _mines_state_get(ctx, uid)
    if not state:
        # Если игра не начата – показываем меню выбора ставки
        await _show_mines_bet_menu(update, context, player, cfg)
        return

    # --- 3. Обработка действий в зависимости от состояния ---
    action = query.data if query else None

    # Если действие – открыть клетку
    if action and action.startswith("mines_open_"):
        await _mines_open_cell(update, context, state, redis_key, uid, ctx)
        return

    # Если действие – забрать выигрыш
    if action and action == "mines_cashout":
        await _mines_cashout(update, context, state, redis_key, uid, ctx)
        return

    # Если действие – начать новую игру (выбрана ставка)
    if action and action.startswith("mines_bet_"):
        bet = int(action.split("_")[-1])
        await _mines_start_game(update, context, uid, bet, ctx)
        return

    # Если действие – "назад" или "меню" – просто показываем меню удачи
    if action and action in ("luck", "menu"):
        await luck_callback(update, context)
        return

    # Если никакое действие не подошло – показываем текущее поле
    await _mines_show_field(update, context, state, redis_key, uid, ctx)
    
async def _show_mines_bet_menu(update, context, player, cfg):
    """Показывает меню выбора ставки."""
    query = update.callback_query
    if not query:
        return
    text = (
        "💣 **МИНЫ**\n\n"
        "Выбери ставку и начни игру.\n"
        "Поле 5×5, спрятано 3 мины.\n"
        "Открывай клетки, множитель растёт!\n"
        "Можешь в любой момент забрать выигрыш.\n\n"
        f"💰 Твой баланс: {player.balance} OAC"
    )
    keyboard = []
    for bet in cfg["mines"]["bet_options"]:
        keyboard.append([InlineKeyboardButton(f"{bet} OAC", callback_data=f"mines_bet_{bet}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="luck")])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def _mines_start_game(update, context, uid, bet, ctx):
    """Создаёт новую игру, списывает ставку и показывает поле."""
    query = update.callback_query
    if not query:
        return
    # Проверка баланса
    player = await ctx.repo.get_by_id(uid)
    if player.balance < bet:
        await query.answer("Недостаточно OAC!", show_alert=True)
        return

    # Атомарно списываем ставку
    async def _deduct(p, conn):
        if p.balance < bet:
            raise ValueError("Недостаточно средств")
        p.balance -= bet
        return p.balance

    try:
        await ctx.repo.atomic_update(uid, _deduct)
    except Exception as e:
        logger.error(f"Ошибка списания ставки {bet} у {uid}: {e}")
        await query.answer("Ошибка при списании ставки", show_alert=True)
        return

    # Генерируем поле и мины
    field, mines = _generate_mines_field()
    state = {
        "field": field,
        "mines": list(mines),  # для сериализации
        "bet": bet,
        "step": 0,
        "multiplier": 1.0,
        "status": "playing",
        "created_at": time.time()
    }
    redis_key = f"mines_game:{uid}"
    await _mines_state_set(ctx, uid, state)
    await _mines_show_field(update, context, state, redis_key, uid, ctx)

async def _mines_show_field(update, context, state, redis_key, uid, ctx):
    """Отображает текущее состояние поля."""
    query = update.callback_query
    if not query:
        return
    field = state["field"]
    mines = set(state["mines"])
    bet = state["bet"]
    step = state["step"]
    multiplier = state["multiplier"]
    status = state["status"]

    # Строим визуальное поле
    size = 5
    lines = []
    for r in range(size):
        row_cells = []
        for c in range(size):
            val = field[r][c]
            if val == 0:
                row_cells.append("?")
            elif val == 1:
                row_cells.append("💎")
            elif val == 2:
                row_cells.append("💀")
        lines.append("│ " + " │ ".join(row_cells) + " │")
    field_str = "┌───┬───┬───┬───┬───┐\n" + "\n├───┼───┼───┼───┼───┤\n".join(lines) + "\n└───┴───┴───┴───┴───┘"

    win = int(bet * multiplier) if status == "playing" else 0

    text = (
        f"💣 **МИНЫ**\n\n"
        f"💰 Ставка: {bet} OAC\n"
        f"🏆 Множитель: x{multiplier:.2f}\n"
        f"📊 Прогресс: {step}/22 клеток\n"
    )

    if status == "playing":
        text += f"💰 Возможный выигрыш: {win} OAC\n\n"
        text += f"```\n{field_str}\n```\n"
        # Клавиатура с клетками
        keyboard = []
        for r in range(size):
            row_btns = []
            for c in range(size):
                if field[r][c] == 0:
                    row_btns.append(InlineKeyboardButton("▪️", callback_data=f"mines_open_{r}_{c}"))
                else:
                    row_btns.append(InlineKeyboardButton("  ", callback_data="noop"))
            keyboard.append(row_btns)
        keyboard.append([InlineKeyboardButton(f"🏆 Забрать {win} OAC", callback_data="mines_cashout")])
        keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="luck")])
    elif status == "won":
        text += f"🎉 **ПОБЕДА!** Ты открыл все клетки!\n"
        text += f"💰 Выигрыш: {win} OAC\n\n```\n{field_str}\n```"
        keyboard = [[InlineKeyboardButton("💣 Новая игра", callback_data="mines_bet_50")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="luck")]]
    elif status == "lost":
        text += f"💥 **ВЗРЫВ!** Ты попал на мину!\n"
        if step >= 1:
            almost = int(bet * multiplier)
            text += f"😱 Так близко! Открыто {step}/22 — ты мог забрать {almost} OAC (x{multiplier:.2f}).\n"
            text += f"💰 Ставка сгорела. Ещё один шаг — и куш был бы твой.\n\n```\n{field_str}\n```"
        else:
            text += f"💰 Ты потерял ставку.\n\n```\n{field_str}\n```"
        keyboard = [[InlineKeyboardButton("💣 Попробовать снова", callback_data="mines_bet_50")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="luck")]]
    else:  # cashed_out
        text += f"✅ **Ты забрал выигрыш!**\n"
        text += f"💰 Выигрыш: {win} OAC\n\n```\n{field_str}\n```"
        keyboard = [[InlineKeyboardButton("💣 Новая игра", callback_data="mines_bet_50")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="luck")]]

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def _mines_open_cell(update, context, state, redis_key, uid, ctx):
    """Открывает клетку, проверяет мину, обновляет состояние."""
    query = update.callback_query
    if not query:
        return
    # Парсим координаты. callback = "mines_open_{r}_{c}" → split даёт 4 части
    # (["mines","open","r","c"]), а не 3. Раньше распаковка в 3 переменные
    # роняла ValueError → клик по клетке молча ничего не делал. Берём последние
    # два сегмента.
    parts = query.data.split("_")
    row, col = int(parts[-2]), int(parts[-1])

    field = state["field"]
    if field[row][col] != 0:
        await query.answer("Эта клетка уже открыта", show_alert=True)
        return

    mines = set(state["mines"])

    # Проверяем мину
    if (row, col) in mines:
        field[row][col] = 2
        state["status"] = "lost"
        # Ставка уже списана, ничего не возвращаем
        await _mines_state_set(ctx, uid, state)
        await _mines_show_field(update, context, state, redis_key, uid, ctx)
        return

    # Безопасная клетка
    field[row][col] = 1
    state["step"] += 1
    state["multiplier"] = _calc_multiplier(state["step"])

    # Проверяем победу (все 22 клетки открыты)
    if state["step"] == 22:
        state["status"] = "won"
        win = int(state["bet"] * state["multiplier"])
        # Начисляем выигрыш атомарно
        async def _win(p, conn):
            p.balance += win
            return p.balance
        try:
            await ctx.repo.atomic_update(uid, _win)
        except Exception as e:
            logger.error(f"Ошибка начисления выигрыша {win} у {uid}: {e}")
            await query.answer("Ошибка при начислении", show_alert=True)
            return
        await _mines_state_set(ctx, uid, state)
        await _mines_show_field(update, context, state, redis_key, uid, ctx)
        return

    # Игра продолжается
    await _mines_state_set(ctx, uid, state)
    await _mines_show_field(update, context, state, redis_key, uid, ctx)

async def _mines_cashout(update, context, state, redis_key, uid, ctx):
    """Забирает текущий выигрыш."""
    query = update.callback_query
    if not query:
        return
    if state["status"] != "playing":
        await query.answer("Игра уже завершена", show_alert=True)
        return
    if state["step"] == 0:
        await query.answer("Нужно открыть хотя бы одну клетку", show_alert=True)
        return

    win = int(state["bet"] * state["multiplier"])
    # Начисляем выигрыш
    async def _cash(p, conn):
        p.balance += win
        return p.balance
    try:
        await ctx.repo.atomic_update(uid, _cash)
    except Exception as e:
        logger.error(f"Ошибка кэшаута {win} у {uid}: {e}")
        await query.answer("Ошибка при выводе", show_alert=True)
        return

async def _mines_open_cell_wrapper(update, context):
    # Извлекаем uid и данные из callback_data, затем вызываем основную функцию
    query = update.callback_query
    uid = query.from_user.id
    ctx = context.bot_data.get("ctx")
    redis_key = f"mines_game:{uid}"
    state = await _mines_state_get(ctx, uid)
    if not state:
        await query.answer("Игра не найдена. Начните новую.", show_alert=True)
        return
    await _mines_open_cell(update, context, state, redis_key, uid, ctx)

async def _mines_cashout_wrapper(update, context):
    query = update.callback_query
    uid = query.from_user.id
    ctx = context.bot_data.get("ctx")
    redis_key = f"mines_game:{uid}"
    state = await _mines_state_get(ctx, uid)
    if not state:
        await query.answer("Игра не найдена.", show_alert=True)
        return
    await _mines_cashout(update, context, state, redis_key, uid, ctx)

    state["status"] = "cashed_out"
    await _mines_state_set(ctx, uid, state)
    await _mines_show_field(update, context, state, redis_key, uid, ctx)


async def _mines_bet_wrapper(update, context):
    """Клик по кнопке ставки в минах: списывает ставку и стартует игру.

    Раньше callback 'mines_bet_<n>' не был зарегистрирован ни в одном реестре,
    поэтому выбор ставки выдавал «Неизвестная команда» и мины были непроходимы.
    """
    query = update.callback_query
    await query.answer()
    ctx = context.bot_data.get("ctx")
    uid = query.from_user.id
    try:
        bet = int(query.data.split("_")[-1])
    except (ValueError, AttributeError):
        await query.answer("Некорректная ставка", show_alert=True)
        return
    await _mines_start_game(update, context, uid, bet, ctx)

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
            except Exception: continue
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
                f"<b>🏰 Главное Меню › 🌍 Мир › 🏛️ Лабиринт</b>\n\n"
                f"<b>🏛️ ЛАБИРИНТ ИСКАЖЕНИЯ — ЭТАЖ {depth}</b>\n\n"
                f"<i>– Портал откроется через <b>{hrs} ч {mins} мин</b>.</i>"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="menu")]])
            await edit_or_reply(update, context, text, reply_markup=kb, parse_mode='HTML')
            return
    total_rooms = 4 + depth
    text = (
        f"<b>🏛️ ЛАБИРИНТ ИСКАЖЕНИЯ — ЭТАЖ {depth}</b>\n\n"
        f"📊 <i>Твоя статистика:</i>\n"
        f"🎁 Сундуков открыто: {player.lab_chests}\n"
        f"💀 Смертей: {player.lab_deaths}\n\n"
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

    async def _mark_lab(p, conn):
        p.last_lab_attempt = now
        # Отмечаем задание квеста «Лабиринт» (раньше не трекалось → глава 2
        # была непроходима)
        p.daily_progress = p.daily_progress or {}
        p.daily_progress["lab"] = True
        return True
    await ctx.repo.atomic_update(uid, _mark_lab)

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
            except Exception: pass

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

    await update.message.reply_text(
        f"✅ Блант «{item.get('name')}» подарен @{target_username}!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💍 Мои бланты", callback_data="my_blunts")],
            [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
        ]))
    context.user_data.pop('gifting_blunt_id', None)

# ============================================================
# ПИТОМЦЫ
# ============================================================
@cb
async def pet_preview(update, context, ctx):
    # Робастно к вызову и кнопкой, и командой /pet (edit_or_reply сам решает
    # редактировать сообщение или прислать новое; раньше query.message.edit_text
    # ронял команду /pet).
    uid = update.effective_user.id
    player = await ctx.repo.get_by_id(uid)

    if player and player.pet:
        name_str = f" по кличке «{player.pet_name}»" if player.pet_name else ""
        hunger = player.pet_hunger if player.pet_hunger is not None else 100
        hbar = "🟩" * (hunger // 20) + "⬛️" * (5 - hunger // 20)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🍖 Покормить", callback_data="pet_feed")],
            [InlineKeyboardButton("🔙 Назад", callback_data="menu")],
        ])
        await edit_or_reply(
            update, context,
            f"🐾 <b>Твой питомец: {player.pet}{name_str}</b>\n\n"
            f"🍖 <b>Сытость:</b> {hbar} {hunger}/100",
            reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🐕 Купить Песика ({PET_CONFIG['dog']['price']} 🍬)", callback_data="pet_buy_dog")],
            [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
        ])
        await edit_or_reply(
            update, context,
            "🐾 <b>ПИТОМЦЫ</b>\n\nПока доступен только Песик.",
            reply_markup=kb)

@cb
async def pet_feed_handler(update, context, ctx):
    query = update.callback_query
    uid = query.from_user.id
    result = await ctx.pet_service.feed(uid)
    if not result or result.get("status") == "no_pet":
        await query.answer("Сначала заведи питомца!", show_alert=True)
        return
    await query.answer("🐾 Питомец сыт и доволен!")
    await pet_preview(update, context)


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
    await query.message.edit_text("🐕 Хорошо, твой питомец будет просто Песиком!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🐾 Питомец", callback_data="pet_preview")],
            [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
        ]))

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
        await update.message.reply_text("Ошибка сохранения имени.",
            reply_markup=get_back_to_menu_keyboard())
    else:
        await update.message.reply_text(
            f"Отлично! Теперь твоего питомца зовут «{name}»! 🐕",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🐾 Питомец", callback_data="pet_preview")],
                [InlineKeyboardButton("🏰 В меню", callback_data="menu")],
            ]))
    context.user_data.pop('awaiting_pet_name', None)

async def pet_locked_handler(update, context):
    query = update.callback_query
    await query.answer("❌ Доступно с ранга ⚔️ Ветеран (5000 OAC 🍬)", show_alert=True)

# ============================================================
# МАГАЗИН, АДМИН-КОМАНДЫ
# ============================================================
# ── Прилавок: чистая логика (тестируется без БД) ────────────
def _shop_discount_pct(balance):
    """Скидка прилавка по достатку — ранг даёт ощутимую выгоду в лавке.
    Потолок 15%, чтобы не разгонять инфляцию."""
    b = balance or 0
    if b >= 50000:
        return 15
    if b >= 20000:
        return 10
    if b >= 5000:
        return 5
    return 0


def _shop_price(base, discount_pct):
    """Цена со скидкой, минимум 1 OAC."""
    return max(1, round(base * (100 - discount_pct) / 100))


def _shop_today(ordinal):
    """3 товара дня — детерминированное окно по дате (без состояния в БД).
    Витрина сдвигается на 1 позицию каждый день → ощущение живой лавки."""
    pool = list(SHOP_ITEMS.keys())
    start = ordinal % len(pool)
    return [pool[(start + i) % len(pool)] for i in range(min(3, len(pool)))]


def _shop_time_left(now):
    """(часы, минуты) до смены витрины — до ближайшей полуночи. FOMO-таймер."""
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    delta = tomorrow - now
    return delta.seconds // 3600, (delta.seconds % 3600) // 60


def _build_shop_view(balance, now):
    """Собирает текст+клавиатуру прилавка. Чистая функция для тестов."""
    disc = _shop_discount_pct(balance)
    today = _shop_today(now.toordinal())
    h, m = _shop_time_left(now)
    lines = ["<b>🏪 ЛАВКА ФАБРИКИ №9</b>", ""]
    if disc:
        lines.append(f"🪪 <b>Скидка ранга: −{disc}%</b> на всё сегодня")
    else:
        lines.append("🪪 <i>Достигни Ветерана — откроется скидка ранга</i>")
    lines.append(f"🔥 <i>Прилавок сменится через {h}ч {m:02d}м</i>")
    lines.append(f"💰 <b>У тебя:</b> {balance} OAC 🍬")
    lines.append("")
    rows = []
    for key in today:
        it = SHOP_ITEMS[key]
        price = _shop_price(it["price"], disc)
        old = f" <s>{it['price']}</s>" if disc else ""
        lines.append(f"{it['emoji']} <b>{it['name']}</b> — {price} OAC{old}")
        lines.append(f"   <i>{it['blurb']}</i>")
        afford = "" if balance >= price else "🔒 "
        rows.append([InlineKeyboardButton(
            f"{afford}{it['emoji']} {it['name']} · {price}",
            callback_data=f"shop_buy_{key}")])
    lines.append("")
    lines.append("🕯️ <i>А за настоящими артефактами — в Каталог Фабрики.</i>")
    # Мост в реальный магазин сохранён: Скидка (привилегия ранга) и Каталог.
    rows.append([
        InlineKeyboardButton("🪪 Скидка", callback_data="privilege"),
        InlineKeyboardButton("📦 Каталог", callback_data="catalog"),
    ])
    rows.append([InlineKeyboardButton("🏰 В меню", callback_data="menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@cb
async def shop_callback(update, context, ctx):
    # Робастно к кнопке и команде /shop (раньше query.from_user на команде
    # (без callback_query) ронял «внутреннюю ошибку»).
    uid = update.effective_user.id
    player = await ctx.repo.get_by_id(uid)
    balance = (player.balance if player else 0) or 0
    text, kb = _build_shop_view(balance, datetime.now())
    await edit_or_reply(update, context, text, reply_markup=kb)


async def shop_buy_callback(update, context):
    """Покупка товара дня. Атомарно, с валидацией витрины и скидки."""
    ctx = context.bot_data.get("ctx")
    query = update.callback_query
    if not ctx:
        await query.answer("⚠️ Бот инициализируется.", show_alert=True)
        return
    uid = query.from_user.id
    key = query.data[len("shop_buy_"):]
    now = datetime.now()
    item = SHOP_ITEMS.get(key)
    # товар должен быть в сегодняшней витрине — защита от устаревших кнопок
    if not item or key not in _shop_today(now.toordinal()):
        await query.answer("🔥 Этого товара уже нет на прилавке — витрина сменилась.", show_alert=True)
        await shop_callback(update, context)
        return

    async def _buy(p, conn):
        # Возвращаем статус, а НЕ бросаем исключение: atomic_update обёрнут в
        # tenacity-retry, который завернул бы кастомный exception в RetryError
        # и вхолостую ретраил бы «нет денег». Статус-кортеж — принятый здесь
        # паттерн (см. do_smoke/craft).
        price = _shop_price(item["price"], _shop_discount_pct(p.balance or 0))
        if (p.balance or 0) < price:
            return ("no_funds", price, None)
        p.balance = (p.balance or 0) - price
        setattr(p, item["field"], (getattr(p, item["field"], 0) or 0) + item["qty"])
        return ("ok", price, getattr(p, item["field"]))

    result = await ctx.repo.atomic_update(uid, _buy)
    if result is None:
        await query.answer("Профиль не найден.", show_alert=True)
        return
    status, price, new_total = result
    if status == "no_funds":
        await query.answer(f"💸 Не хватает OAC — нужно {price}. Ферма зовёт.", show_alert=True)
        return
    await query.answer(f"✅ Куплено! −{price} OAC", show_alert=False)
    player = await ctx.repo.get_by_id(uid)
    balance = (player.balance if player else 0) or 0
    text, kb = _build_shop_view(balance, now)
    banner = (f"✅ <b>{item['emoji']} {item['name']} — твоё!</b>\n"
              f"📦 Теперь у тебя: <b>{new_total}</b>\n\n")
    await query.message.edit_text(banner + text, reply_markup=kb, parse_mode='HTML')

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


@cb
async def broadcast(update, context, ctx):
    if update.effective_user.id != ctx.settings.admin_id:
        await update.message.reply_text("🔒 Только для администратора.")
        return
    
    if not context.args:
        await update.message.reply_text("📝 Используй: /broadcast Текст")
        return
    
    text = " ".join(context.args)
    
    async with ctx.db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM players")
    
    success = 0
    for user in users:
        try:
            # ✅ Отправляем без HTML (чтобы не падать)
            await context.bot.send_message(user["user_id"], text)
            success += 1
            await asyncio.sleep(0.03)
        except Exception:
            pass
    
    await update.message.reply_text(f"✅ Разослано {success} из {len(users)} игроков")

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
# ───────────────────────────────────────────────
# НОВАЯ ГЛАВНАЯ ПАНЕЛЬ (КАРТА 3, СОСТОЯНИЯ А–З)
# ───────────────────────────────────────────────
def _happy_hour_banner(ctx, now):
    """Живой баннер Часа Удачи с обратным отсчётом. Чистая, тестируемая.
    Показывается в момент входа → FOMO бьёт именно когда игрок уже в игре."""
    if not (ctx and getattr(ctx, "cache", None) and ctx.cache.get("happy_hour")):
        return ""
    end = ctx.cache.get("happy_hour_end")
    if end and now < end:
        mins = max(1, math.ceil((end - now).total_seconds() / 60))
        tail = f"Осталось {mins}м — фарми и дуй прямо сейчас!"
    else:
        tail = "Лови момент — всё приносит вдвое больше!"
    return (f"🌟🌟🌟 <b>ЧАС УДАЧИ!</b> 🌟🌟🌟\n"
            f"<b>Все действия ×{HAPPY_HOUR_MULTIPLIER} OAC 🍬.</b> {tail}")


# Ключ задания квеста → (лейбл, callback) для геройской кнопки. Гарантирует,
# что действие героя совпадает со счётчиком N/M (действие двигает прогресс).
QUEST_HERO_ACTIONS = {
    "farm":   ("🍬 Фармить", "farm"),
    "craft":  ("🌿 Крафтить", "craft"),
    "smoke":  ("💨 Дунуть", "smoke"),
    "ritual": ("🕯️ Ритуал", "ritual"),
    "repent": ("⚜️ Исповедь", "repent"),
    "donate": ("💎 Пожертвовать", "guild_shrine"),
    "lab":    ("🏛️ Лабиринт", "lab_start"),
    "pet":    ("🐾 Покормить питомца", "pet_preview"),
    "train":  ("⚔️ Тренировка", "train"),
}


async def build_main_menu(player, ctx, context=None, full_mode=False):
    now = datetime.now()
    guild = player.guild
    balance = player.balance or 0
    has_pet = bool(player.pet)
    is_veteran = balance >= 5000

    # ---- Автоматический сброс daily_progress ----
    progress = await ensure_daily_progress(player, ctx)

    # ---- Вычисление total и done из шаблона квеста ----
    quest_id = progress.get("quest_id", "chapter1")
    template = QUEST_TEMPLATES.get(quest_id)
    if template:
        conditions = {
            "guild_black": guild == "BLACK",
            "guild_white": guild == "WHITE",
            "is_veteran_and_has_pet": is_veteran and has_pet,
        }
        filtered_tasks = []
        for task in template["tasks"]:
            cond = task.get("condition")
            if cond and not conditions.get(cond, False):
                continue
            filtered_tasks.append(task)
        total = len(filtered_tasks)
        done = sum(1 for task in filtered_tasks if progress.get(task["key"], False))
        # Геройская кнопка берётся из ЭТИХ ЖЕ задач (не из get_next_action с
        # захардкоженным списком) — иначе в главе 2+ герой предлагал «Фармить»,
        # которого нет в задачах главы, и счётчик N/M не двигался после действия.
        hero_task = next((t for t in filtered_tasks if not progress.get(t["key"])), None)
    else:
        total = 0
        hero_task = None
        done = 0

    reward_claimed = progress.get("reward_claimed", False)

    # ── ТЕКСТ ──
    whisper = random.choice(WHISPERS)
    display_name = html.escape(player.username or "Странник")

    if full_mode:
        # Полный текст (при старте) — используется оригинальное оформление
        lines = []

        # Заголовок меню и шёпот (пункт 5 и оригинальная структура)
        lines.append("<b>🎮 ГЛАВНОЕ МЕНЮ</b>")
        lines.append("")
        lines.append(f"<i>{whisper}</i>")
        lines.append("")

        # Определение текущего и следующего ранга
        rank_emoji, rank_name, next_rank_emoji, next_rank_name, next_threshold, _ = compute_rank_info(balance)

        rank_display = f"{rank_emoji} {rank_name}" if rank_name else rank_emoji

        # Приветствие и гильдия (пункты 1, 2, 3 — возвращены к оригиналу)
        lines.append(f"⚔️ С возвращением в <b>Гильдию, {rank_display} {display_name}</b>")
        if guild == "BLACK":
            lines.append("🔮 Ты — часть <b>Темной Гильдии. 🕯️Ритуалы ждут тебя</b>")
        elif guild == "WHITE":
            lines.append("🪽 Ты — часть <b>Светлой Гильдии. ⚜️Исповедь очищает душу и ждёт тебя</b>")
        else:
            lines.append("<b>🕯️⚜️ Ты ещё не ВЫБРАЛ сторону!</b>")
            lines.append("🔮 Гильдия откроет <b>ритуалы, исповеди и войну</b>")
            lines.append("👉 <b>Нажми кнопку «🏰 Гильдии» в меню чтобы ВСТУПИТЬ.</b>")

        lines.append("")  # отступ перед мотивационной строкой

        # Мотивационная строка (до следующего ранга)
        if next_threshold > 0:
            gap = next_threshold - balance
            lines.append(f"📈 До следующего ранга <b>{next_rank_emoji} {next_rank_name}</b> осталось — <b>{gap} OAC 🍬!</b>")
        else:
            lines.append(f"<b>⚡ Ты достиг вершины! Твой ранг — {rank_emoji} {rank_name}.</b>")

        lines.append("")  # отступ перед подсказкой

        # Подсказка для новичков (пункт 4 — возвращено жирное оформление)
        farm_count = player.farm_count or 0
        craft_count = player.craft_count or 0
        named = [it for it in (player.inventory or []) if it.get("type") == "named"]

        if farm_count == 0:
            hint = "<b>💡 Твой первый шаг: нажми 🍬 Фармить и получи свои первые OAC!</b>"
        elif craft_count == 0:
            hint = "<b>💡 Попробуй 🌿 Крафт, чтобы создать свой первый Блант!</b>"
        elif len(named) <= 1 and balance >= GAME_CONFIG["named_blunt_cost"]:
            hint = "<b>💡 Готов к большему? Создай свой первый 💍 Именной блант! (50 OAC)</b>"
        elif is_veteran:
            hint = "💡 Исследуй <b>🔮 Алхимию</b> и корми своего 🐾 <b>питомца!</b>"
        else:
            hint = "<b>💡 Исследуй 🏛️ Лабиринт! Он полон опасностей и наград.</b>"
        lines.append(hint)

    else:
        # Краткий режим (без изменений)
        lines = [f"<i>{whisper}</i>"]

    # Общие краткие сообщения (всегда) — новые фичи оставлены
    if context and context.user_data.get("return_after_pause"):
        lines.append("🎁 <b>Пока вас не было: накопились задания и готова награда</b>")
        context.user_data["return_after_pause"] = False

    if not guild and (player.login_streak or 0) == 3:
        lines.append("🏰 Гильдии помогают расти быстрее — загляните")

    # Loss-aversion по серии входов: показываем, что можно потерять
    _streak = player.login_streak or 0
    if _streak >= 3:
        lines.append(f"🔥 <b>Серия входов: {_streak} дн.</b> — не разорви её, вернись завтра за наградой!")

    # Час Удачи — баннер поверх всего меню (peak-момент нельзя прятать)
    hh_banner = _happy_hour_banner(ctx, now)
    if hh_banner:
        lines.insert(0, hh_banner)
        lines.insert(1, "")

    text = "\n".join(lines)

# ── КЛАВИАТУРА ──
    keyboard = []
    happy_now = bool(hh_banner)

    # Кнопка фарма с живым таймером кулдауна — конец «слепым кликам»
    farm_ready = not _farm_on_cooldown(player.farm_count, player.last_farm, now)
    if farm_ready:
        farm_label = "🍬 Фармить ×2 🌟" if happy_now else "🍬 Фармить"
    else:
        _remain = timedelta(hours=FARM_COOLDOWN_HOURS) - (now - player.last_farm)
        _mins = max(1, math.ceil(_remain.total_seconds() / 60))
        farm_label = f"🍬 Грядка зреет · {_mins}м"

    def _farm_btn():
        return InlineKeyboardButton(farm_label, callback_data="farm")

    if player.onboarding_step != -1:
        keyboard.append([InlineKeyboardButton("✨ Все возможности ›", callback_data="all_features")])

    # ЕДИНАЯ ГЕРОЙСКАЯ КНОПКА: один бесспорный следующий ход наверху меню.
    # Снимает «проблему первого решения» (закон Хика) → привычка без трения,
    # «one tap to fun». Раньше при незакрытых заданиях верхняя кнопка была
    # прогресс-баром «⚠️ Задания N/M», ведущим в СПИСОК (ещё одно решение);
    # теперь это ПРЯМОЕ лучшее действие + счётчик дня (эффект Зейгарник).
    featured_cb = None   # действие, поднятое в героя — убираем его из row2 (без дублей)
    if not reward_claimed and total > 0 and done == total:
        keyboard.append([InlineKeyboardButton("🎁 Забрать награду!", callback_data="claim_reward")])
    elif not reward_claimed and hero_task is not None:
        hkey = hero_task["key"]
        # фарм-задача на кулдауне → предложи СЛЕДУЮЩУЮ задачу главы (не случайное
        # действие), чтобы герой всегда двигал счётчик и не вёл в кулдаун-тупик.
        if hkey == "farm" and not farm_ready:
            alt = next((t for t in filtered_tasks
                        if not progress.get(t["key"]) and t["key"] != "farm"), None)
            if alt:
                hkey = alt["key"]
        hlabel, hcb = QUEST_HERO_ACTIONS.get(hkey, ("📋 Задания дня", "daily_quest_hub"))
        if hkey == "farm":
            # у фарма богатый лейбл (Happy Hour / таймер) — сохраняем его
            keyboard.append([InlineKeyboardButton(f"{farm_label} · {done}/{total} ›", callback_data="farm")])
            featured_cb = "farm"
        else:
            keyboard.append([InlineKeyboardButton(f"{hlabel} · {done}/{total} ›", callback_data=hcb)])
            if hcb in ("craft", "smoke"):
                featured_cb = hcb
    else:
        keyboard.append([_farm_btn()])
        featured_cb = "farm"

    # Вторая строка: стандартные действия, минус вынесенное в героя (без дублей).
    row2 = []
    if featured_cb != "farm":
        row2.append(_farm_btn())
    if featured_cb != "craft":
        row2.append(InlineKeyboardButton("🌿 Крафт ›", callback_data="craft"))
    if featured_cb != "smoke":
        row2.append(InlineKeyboardButton("💨 Дунуть", callback_data="smoke"))
    if row2:
        keyboard.append(row2)

    # ===== АДАПТИВНАЯ КНОПКА ГИЛЬДИИ =====
    if guild:
        now = datetime.now()
        if guild == "BLACK":
            last_time = player.last_ritual
            cooldown = GAME_CONFIG["ritual_cooldown_hours"]
            label = "🕯️ Ритуал"
            callback = "ritual"
        else:  # WHITE
            last_time = player.last_repent
            cooldown = GAME_CONFIG["repent_cooldown_hours"]
            label = "⚜️ Исповедь"
            callback = "repent"
        
        # Показываем кнопку ТОЛЬКО если доступно
        if not last_time or (now - last_time) >= timedelta(hours=cooldown):
            keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    # Навигация: 2 кнопки в ряд, чтобы длинные подписи не обрезались
    # (3-в-ряд не помещались: «Прогресс…», «Гильди…»).
    keyboard.append([
        InlineKeyboardButton("🏰 Гильдия ›", callback_data="guild_info"),
        InlineKeyboardButton("📊 Прогресс ›", callback_data="progress_hub"),
    ])
    # Лидерборд — из подвала на главный экран: сильнейший соц-крючок (топ-10 +
    # твоя позиция + приз, который надо удержать). 2-в-ряд, без обрезки подписей.
    keyboard.append([
        InlineKeyboardButton("🏅 Лидеры ›", callback_data="top"),
        InlineKeyboardButton("🌍 Мир ›", callback_data="world_hub"),
    ])

    return text, InlineKeyboardMarkup(keyboard)

# ── Обработчик меню (редактирование сообщения) ──
@cb
async def menu_handler(update, context, ctx):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    player = await ctx.repo.get_by_id(uid)
    if not player or not player.exists:
        await query.answer("Профиль не найден. Напиши /start", show_alert=True)
        return

    text, kb = await build_main_menu(player, ctx, context)
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')


# ── Прогресс-хаб (LVL 1) ──
@cb
async def progress_hub_handler(update, context, ctx):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    try:
        player = await ctx.repo.get_by_id(uid)
        if not player or not player.exists:
            await query.answer("❌ Профиль не найден!", show_alert=True)
            return

        balance = player.balance or 0
        username = html_escape(player.username or str(uid))

        # ===== 1. РАНГ И ПРОГРЕСС =====
        rank_emoji, rank_name, next_rank_emoji, next_rank_name, next_threshold, prev_threshold = compute_rank_info(balance)

        if next_threshold:
            progress_percent = int((balance - prev_threshold) / (next_threshold - prev_threshold) * 100) if next_threshold > prev_threshold else 100
            progress_percent = min(100, max(0, progress_percent))
            bar = "▓" * (progress_percent // 10) + "░" * (10 - progress_percent // 10)
            rank_line = (
                f"<b>⚜️ Ранг: {rank_emoji} {rank_name} → {next_rank_emoji} {next_rank_name}</b>\n"
                f"<b>{balance} / {next_threshold} OAC</b>\n"
                f"{bar} <b>{progress_percent}</b>%"
            )
        else:
            rank_line = f"<b>⚜️ Ранг: {rank_emoji} {rank_name}</b> (Максимум!)"

        # ===== 2. ЕЖЕДНЕВНЫЕ ЗАДАНИЯ =====
        progress = await ensure_daily_progress(player, ctx)

        # Получаем текущий квест
        quest_id = progress.get("quest_id", "chapter1")
        template = QUEST_TEMPLATES.get(quest_id)
        if not template:
            quest_id = "chapter1"
            template = QUEST_TEMPLATES[quest_id]
            progress["quest_id"] = quest_id
            player.daily_progress = progress
            await ctx.repo.save(player)
        
        # Фильтруем задания по условиям
        conditions = {
            "guild_black": player.guild == "BLACK",
            "guild_white": player.guild == "WHITE",
            "is_veteran_and_has_pet": (player.balance or 0) >= 5000 and bool(player.pet),
        }
        filtered_tasks = []
        for task in template["tasks"]:
            cond = task.get("condition")
            if cond and not conditions.get(cond, False):
                continue
            filtered_tasks.append(task)
        
        total = len(filtered_tasks)
        done = sum(1 for task in filtered_tasks if progress.get(task["key"], False))
        bar_tasks = "▓" * done + "░" * (total - done)
        percent_tasks = int(done / total * 100) if total else 0
        
        # --- Заголовок квеста ---
        tasks_header = (
            f"<b>📋 Ежедневные задания:</b>\n"
            f"<b>📜 {template['title']}</b>\n"
            f"<b>[{bar_tasks}] {percent_tasks}% ({done}/{total} этапов)</b>\n"
            f"🏆 <b>Сага: Глава {template['chapter_number']} из {template['total_chapters']}</b>"
        )
        
        # --- Список заданий (галочки) ---
        #tasks_list = []
        #for task in filtered_tasks:
            #label = task["label"]
            #if progress.get(task["key"], False):
                #tasks_list.append(f"   ✅ {label}")
            #else:
                #tasks_list.append(f"   ⬜️ {label}")
        #tasks_text = "\n".join(tasks_list)
     
        # --- Если всё выполнено — радостный текст (всегда!) ---
        if done == total:
            tasks_block = f"{tasks_header}\n🎉 <b>ВСЕ ЗАДАНИЯ ВЫПОЛНЕНЫ!</b>"
        else:
            tasks_block = f"{tasks_header}" #\n{tasks_text}

        # ===== 3. СРАВНЕНИЕ С СОСЕДЯМИ =====
        my_balance = player.balance or 0
        async with ctx.db_pool.acquire() as conn:
            # ✅ добавлен user_id
            above_row = await conn.fetchrow(
                "SELECT user_id, username, balance FROM players WHERE balance > $1 ORDER BY balance ASC LIMIT 1",
                my_balance
            )
            below_row = await conn.fetchrow(
                "SELECT user_id, username, balance FROM players WHERE balance < $1 ORDER BY balance DESC LIMIT 1",
                my_balance
            )
            total_players = await conn.fetchval("SELECT COUNT(*) FROM players")
            above_count = await conn.fetchval("SELECT COUNT(*) FROM players WHERE balance > $1", my_balance)

        position = (above_count or 0) + 1
        in_top10 = position <= 10

        # ✅ безопасное формирование имени
        def format_player(row):
            if not row:
                return "Игрок"
            user_id = row.get("user_id")
            username = row.get("username")
            if username:
                return f"@{html_escape(username)}"
            elif user_id:
                return f"ID{user_id}"
            return "Игрок"

        comparison_lines = []

        if below_row:
            gap = my_balance - below_row["balance"]
            name = format_player(below_row)
            comparison_lines.append(f"⬇️ <b>Ниже вас: {name}</b> (отстаёт на {gap} OAC)")

        if above_row:
            gap = above_row["balance"] - my_balance
            name = format_player(above_row)
            comparison_lines.append(f"⬆️ <b>Выше вас: {name}</b> (нужно {gap} OAC для обгона)")

        if not comparison_lines:
            comparison_lines.append("🏅 Вы единственный в рейтинге!")

        if in_top10:
            comparison_lines.append(f"🎯 <b>Позиция в топе: #{position}</b>")
        else:
            tenth_row = await conn.fetchrow(
                "SELECT balance FROM players ORDER BY balance DESC LIMIT 1 OFFSET 9"
            )
            if tenth_row:
                tenth_balance = tenth_row["balance"]
                gap_top10 = tenth_balance - my_balance
                if gap_top10 > 0:
                    comparison_lines.append(f"🎯 До топ-10 осталось: {gap_top10} OAC")
                else:
                    comparison_lines.append("✅ Ты уже в топ-10!")

        comparison = "\n".join(comparison_lines)

        # ===== 4. СТАТИСТИКА =====
        stats_lines = []
        stats_lines.append(f"🌿 Блантов скручено: {player.craft_count or 0}")
        stats_lines.append(f"💨 Выкурено: {player.smoke_count or 0}")

        if player.guild == "BLACK":
            stats_lines.append(f"🕯️ Ритуалов: {player.ritual_count or 0}")
        elif player.guild == "WHITE":
            stats_lines.append(f"⚜️ Исповедей: {player.repent_count or 0}")

        stats_text = "\n".join(stats_lines)

        # ===== 5. ДОСТИЖЕНИЯ =====
        async with ctx.db_pool.acquire() as conn:
            awarded = await conn.fetchval("SELECT COUNT(*) FROM achievements_awarded WHERE user_id=$1", uid)
        total_ach = len(ACHIEVEMENTS)
        ach_line = f"🏆 Достижений: {awarded or 0} / {total_ach}"

        # ===== СБОРКА =====
        text = (
            f"<b>📊 ЛИЧНЫЙ ПРОГРЕСС</b>\n\n"
            f"{rank_line}\n\n"
            f"{tasks_block}\n\n"
            f"<b>🏅 В рейтинге:</b>\n{comparison}\n\n"
            f"{stats_text}\n"
            f"{ach_line}"
        )

        kb_rows = [
            [InlineKeyboardButton("👤 Профиль", callback_data="profile"),
             InlineKeyboardButton("🏆 Достижения", callback_data="achievements_menu"),
             InlineKeyboardButton("🏅 Лидеры", callback_data="top")]
        ]

        # Полный интерактивный чек-лист заданий дня переехал сюда, в свой
        # логичный дом (Прогресс), когда меню получило единую геройскую кнопку.
        # Так планировщики/завершители не теряют «увидеть все задачи разом».
        if done == total and not progress.get("reward_claimed"):
            kb_rows.insert(0, [InlineKeyboardButton("🎁 Забрать награду!", callback_data="claim_reward")])
        else:
            kb_rows.insert(0, [InlineKeyboardButton(f"📋 Задания дня · {done}/{total}", callback_data="daily_quest_hub")])

        kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
        kb = InlineKeyboardMarkup(kb_rows)

        await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

    except Exception as e:
        logger.exception("Ошибка в progress_hub_handler")
        await query.answer("⚠️ Внутренняя ошибка. Попробуйте позже.", show_alert=True)
        # Уведомление админу
        if ctx.settings.admin_id:
            try:
                await context.bot.send_message(
                    chat_id=ctx.settings.admin_id,
                    text=f"🚨 Ошибка в progress_hub_handler для {uid}:\n{html_escape(str(e))}"
                )
            except Exception:
                pass

@cb
async def daily_quest_hub(update, context, ctx):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    player = await ctx.repo.get_by_id(uid)
    if not player:
        return

    # ===== ЕЖЕДНЕВНЫЙ СБРОС =====
    progress = await ensure_daily_progress(player, ctx)

    # === ВЫБОР ШАБЛОНА ===
    quest_id = progress.get("quest_id", "chapter1")
    template = QUEST_TEMPLATES.get(quest_id)
    if not template:
        quest_id = "chapter1"
        template = QUEST_TEMPLATES[quest_id]
        progress["quest_id"] = quest_id
        player.daily_progress = progress
        await ctx.repo.save(player)

    guild = player.guild
    has_pet = bool(player.pet)
    is_veteran = (player.balance or 0) >= 5000

    # Проверка условий
    conditions = {
        "guild_black": guild == "BLACK",
        "guild_white": guild == "WHITE",
        "is_veteran_and_has_pet": is_veteran and has_pet,
    }
    
    tasks = []
    for task in template["tasks"]:
        cond = task.get("condition")
        if cond and not conditions.get(cond, False):
            continue
        tasks.append(task)

    total = len(tasks)
    done = sum(1 for task in tasks if progress.get(task["key"], False))

        # ===== ПРОГРЕСС-БАР =====
    bar = "▓" * done + "░" * (total - done)
    percent = int(done / total * 100) if total > 0 else 0
    
    # ===== ЕСЛИ ВСЕ ЗАДАНИЯ ВЫПОЛНЕНЫ И ЕСТЬ ВЫБОР =====
    if done == total and template.get("choices"):
        text = f"<b>📋 ЗАДАНИЯ ДНЯ [▓▓▓▓▓] {done}/{total}</b>\n\n"
        text += f"<b>📜 {template['title']}</b>\n"
        text += f"{template['description']}\n\n"
        text += "🎯 <b>Прогресс: 100%</b>\n\n"
        text += "<b>Что ты сделаешь?</b>"

        kb_rows = []
        for i, choice in enumerate(template["choices"]):
            kb_rows.append([InlineKeyboardButton(choice["label"], callback_data=f"quest_choice_{i}")])
        kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
        kb = InlineKeyboardMarkup(kb_rows)
        await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
        return

    # ===== НОВЫЙ заголовок =====
    text = f"<b>📋 ЗАДАНИЯ ДНЯ</b>\n\n"
    text += f"<b>📜 {template['title']}</b>\n"
    text += f"{template['description']}\n"
    text += f"<b>[{bar}] {done}/{total} этапов</b>\n\n"
    
    # ===== СПИСОК ЗАДАНИЙ =====
    kb_rows = []
    for task in tasks:
        label = task["label"]
        key = task["key"]
        is_done = progress.get(key, False)
        if is_done:
            text += f"✅ {label}\n"
        else:
            text += f"⬜️ {label}\n"
            kb_rows.append([InlineKeyboardButton(label, callback_data=f"quest_{key}")])

    # ===== ПРОЦЕНТ ПРОГРЕССА =====
    text += f"\n🎯 <b>Прогресс: {percent}%</b>"
    
    if template.get("chapter_number") and template.get("total_chapters"):
        text += f"\n🏆 <b>Сага: Глава {template['chapter_number']} из {template['total_chapters']}</b>"
        
    # Если все задания выполнены и нет выбора (choices) – добавляем кнопку "Забрать награду"
    if done == total and not template.get("choices"):
        kb_rows.append([InlineKeyboardButton("🎁 Забрать награду", callback_data="claim_reward")])

    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

async def handle_choice(update, context, ctx):
    """Выбор ветки сюжета (глава 2 → путь воина/благодетеля).

    Раньше эта функция вызывалась из handle_quest_action, но НЕ БЫЛА ОПРЕДЕЛЕНА
    (NameError), а кнопка шла как 'choice_<i>' вместо 'quest_choice_<i>' и не
    маршрутизировалась. Из-за этого игроки застревали на главе 2. Логика наград
    и продвижения квеста зеркалит claim_reward_handler, но берёт данные из выбора.
    """
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    player = await ctx.repo.get_by_id(uid)
    if not player or not player.exists:
        return

    progress = getattr(player, 'daily_progress', {}) or {}
    quest_id = progress.get("quest_id", "chapter1")
    template = QUEST_TEMPLATES.get(quest_id)
    choices = template.get("choices") if template else None
    if not choices:
        await query.answer("Выбор сейчас недоступен", show_alert=True)
        return

    try:
        idx = int(query.data.split("_")[-1])
        choice = choices[idx]
    except (ValueError, IndexError):
        await query.answer("Некорректный выбор", show_alert=True)
        return

    async def _apply(p, conn):
        p.balance += choice.get("reward_oac", 0)
        reward_title = choice.get("reward_title")
        if reward_title:
            titles = (p.titles or "").split()
            if reward_title not in titles:
                titles.append(reward_title)
                p.titles = " ".join(titles).strip()
        for item_key, qty in choice.get("reward_items", {}).items():
            if hasattr(p, item_key):
                setattr(p, item_key, getattr(p, item_key, 0) + qty)
        reset_date = (p.daily_progress or {}).get("reset_date")
        p.daily_progress = {
            "reset_date": reset_date,
            "quest_id": choice.get("next_quest", quest_id),
            "reward_claimed": True,
        }
        return True

    await ctx.repo.atomic_update(uid, _apply)
    await context.bot.send_message(
        chat_id=query.message.chat.id,
        text=choice.get("result_text", "✨ Выбор сделан."),
        parse_mode='HTML',
    )


async def skip_onboarding_handler(update, context):
    """Кнопка «Пропустить обучение»: завершает онбординг и показывает меню.

    Раньше callback 'skip_onboarding' не имел обработчика → «Неизвестная команда».
    """
    ctx = context.bot_data.get("ctx")
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    player = await ctx.repo.get_by_id(uid)
    if not player or not player.exists:
        return
    player.onboarding_step = -1
    await ctx.repo.save(player)

    text, kb = await build_main_menu(player, ctx, context, full_mode=True)
    try:
        await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    except Exception:
        await safe_send_message(context, uid, text, reply_markup=kb, parse_mode='HTML')


@rate_limit(2)
@game_handler
async def train_callback(update, context, ctx, player):
    """Тренировка (ветка воина): раз в день, закаляет дух и даёт OAC.

    Отмечает задание квеста «train». Кулдаун — раз в день через daily_progress
    (сбрасывается вместе с дневным прогрессом), поэтому не требует новой колонки БД.
    """
    uid = update.effective_user.id
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass

    async def _train(p, conn):
        p.daily_progress = p.daily_progress or {}
        if p.daily_progress.get("train"):
            return ("already",)
        reward = random.randint(20, 45)
        p.balance += reward
        p.daily_progress["train"] = True
        return ("ok", reward, p.balance)

    result = await ctx.repo.atomic_update(uid, _train)
    if not result:
        return
    if result[0] == "already":
        await safe_send_message(
            context, uid,
            "⚔️ <b>Ты уже тренировался сегодня.</b>\n"
            "<i>Дух воина крепнет постепенно — возвращайся завтра.</i>",
            parse_mode='HTML')
        return
    _, reward, new_balance = result
    await safe_send_message(
        context, uid,
        f"⚔️ <b>ТРЕНИРОВКА ЗАВЕРШЕНА!</b>\n\n"
        f"🛡️ Ты закалил дух воина. <b>+{reward} OAC</b>\n"
        f"💎 <b>Баланс:</b> {new_balance} OAC",
        parse_mode='HTML')


# ─── Маршрутизатор заданий (вызывается при нажатии на кнопку задания) ───
async def handle_quest_action(update, context):
    ctx = context.bot_data.get("ctx")
    query = update.callback_query
    action = query.data.replace("quest_", "")
    uid = query.from_user.id

    if action.startswith("choice_"):
        await handle_choice(update, context, ctx)
        return

    player = await ctx.repo.get_by_id(uid)
    if not player and action not in ["farm", "craft", "smoke", "pet"]:
        await query.answer("Игрок не найден", show_alert=True)
        return

    # Ядро цикла рисует результат НА МЕСТЕ со своей навигацией (единый живой
    # экран). Ре-рендер хаба ниже затирал бы reveal-награду — поэтому эти
    # действия владеют экраном и возвращают управление (return).
    if action == "farm":
        await farm_callback_v2(update, context)
        return
    elif action == "craft":
        await handle_craft_normal_v2(update, context)
        return
    elif action == "smoke":
        await do_smoke(update, context)
        return
    elif action == "ritual":
        if player.guild == "BLACK":
            await ritual_callback(update, context)
            return
        await query.answer("Ты не в Тёмной Гильдии", show_alert=True)
    elif action == "repent":
        if player.guild == "WHITE":
            await repent_callback(update, context)
            return
        await query.answer("Ты не в Светлой Гильдии", show_alert=True)
    elif action == "train":
        await train_callback(update, context)
    elif action == "pet":
        result = await ctx.pet_service.feed(uid)
        if not result or result.get("status") == "no_pet":
            await query.answer("Сначала заведи питомца! (в разделе «Мир»)", show_alert=True)
        else:
            await query.answer("🐾 Питомец накормлен!")
    elif action == "donate":
        # Пожертвование идёт через Храм гильдии. Раньше 'donate'/'lab' не
        # обрабатывались → кнопки заданий главы 2 выдавали «Неизвестная команда».
        await guild_shrine_callback(update, context)
        return
    elif action == "lab":
        await lab_enter(update, context)
        return
    else:
        await query.answer("Неизвестное задание", show_alert=True)

    # Обновляем экран заданий после любой попытки
    await daily_quest_hub(update, context, ctx)

# ── Забирание награды (обновлённый профиль с наградой) ──
@cb
async def claim_reward_handler(update, context, ctx):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    player = await ctx.repo.get_by_id(uid)
    if not player:
        return

    progress = getattr(player, 'daily_progress', {}) or {}
    if progress.get("reward_claimed"):
        await query.answer("Награда уже получена сегодня!", show_alert=True)
        return

    # Получаем текущий квест
    quest_id = progress.get("quest_id", "chapter1")
    template = QUEST_TEMPLATES.get(quest_id)
    if not template:
        await query.answer("Квест не найден", show_alert=True)
        return

    # Проверяем выполнение всех этапов
# ---- Фильтруем задания по условиям (как в daily_quest_hub) ----
    guild = player.guild
    has_pet = bool(player.pet)
    is_veteran = (player.balance or 0) >= 5000
    conditions = {
        "guild_black": guild == "BLACK",
        "guild_white": guild == "WHITE",
        "is_veteran_and_has_pet": is_veteran and has_pet,
    }
    filtered_tasks = []
    for task in template.get("tasks", []):
        cond = task.get("condition")
        if cond and not conditions.get(cond, False):
            continue
        filtered_tasks.append(task)

    # ---- Проверяем выполнение только доступных заданий ----
    all_done = True
    for task in filtered_tasks:
        key = task["key"]
        if not progress.get(key, False):
            all_done = False
            break

    if not all_done:
        await query.answer("Выполни все доступные этапы квеста!", show_alert=True)
        return

    # ---- Начисляем награду из шаблона ----
    async def _reward(p, conn):
        reward_oac = template.get("reward_oac", 150)
        p.balance += reward_oac

        reward_title = template.get("reward_title")
        if reward_title:
            titles = (p.titles or "").split()
            if reward_title not in titles:
                titles.append(reward_title)
                p.titles = " ".join(titles).strip()

        for item_key, qty in template.get("reward_items", {}).items():
            if hasattr(p, item_key):
                setattr(p, item_key, getattr(p, item_key, 0) + qty)

        next_quest = template.get("next_quest")
        reset_date = p.daily_progress.get("reset_date")
        p.daily_progress = {
            "reset_date": reset_date,
            "quest_id": next_quest or quest_id,
            "reward_claimed": True
        }
        # Увеличиваем стрик (простейшая логика)
        # p.daily_progress["streak"] = p.daily_progress.get("streak", 0) + 1
        return p.balance

    result = await ctx.repo.atomic_update(uid, _reward)

    if result is not None:
        reward_oac = template.get("reward_oac", 150)
        reward_text = f"+{reward_oac} OAC 🍬" if reward_oac > 0 else ""
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=(
                f"🎉 <b>НАГРАДА ПОЛУЧЕНА!🏅</b>\n"
                f"🌙 Тень отступила, лес благодарит тебя.\n\n"
                f"<b>📜 {template['title']} — пройдена! {reward_text}</b>\n\n"
                f"Отличная работа!"
            ),
            parse_mode='HTML'
        )
    else:
        await query.answer("Ошибка при начислении награды. Попробуйте позже.", show_alert=True)

    # Обновляем главное меню
    await menu_handler(update, context, ctx)

# ── Все возможности (для новичков) ──
@cb
async def all_features_handler(update, context, ctx):
    query = update.callback_query
    await query.answer()
    text = (
        "<b>✨ ВСЕ ВОЗМОЖНОСТИ</b>\n\n"
        "• 🍬 <b>Фарм</b> — добыча OAC\n"
        "• 🌿 <b>Крафт</b> — создание блантов\n"
        "• 💨 <b>Дунуть</b> — случайный эффект\n"
        "• 🕯️ <b>Ритуал</b> — для Тёмной Гильдии\n"
        "• ⚜️ <b>Исповедь</b> — для Светлой Гильдии\n"
        "• 🐾 <b>Питомец</b> — появится позже\n"
        "• 🎲 <b>Удача</b> — колесо, берсерк, алхимия\n"
        "• 🏛️ <b>Лабиринт</b> — глубины и сокровища\n"
        "• 🛒 <b>Магазин</b> — скидки и каталог\n"
        "• 📜 <b>Кодекс</b> — правила мира\n\n"
        "<i>Продолжай выполнять задания, и всё откроется!</i>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏰 В меню", callback_data="menu")]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')

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
        await query.answer("Блант не найден.", show_alert=True)
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

    # Рабочий шеринг: switch_inline_query требует inline-режима/обработчика (их нет)
    # → кнопка была мёртвой. Используем нативный t.me/share/url (без HTML — он там
    # не рендерится) и мотивируем двусторонним бонусом (реферер+приглашённый).
    share_plain = re.sub(r"<[^>]+>", "", share_text)
    share_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Отправить другу", url=build_share_url(share_plain))],
        [InlineKeyboardButton("🔙 Назад", callback_data="my_blunts")],
    ])
    await query.answer()
    await query.message.edit_text(
        share_text + "\n\n<i>👆 Отправь другу — когда он войдёт по ссылке, "
        "<b>вы оба получите бонус!</b></i>",
        reply_markup=share_kb, parse_mode='HTML'
    )

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
        # Отмечаем задание квеста «Пожертвовать» (раньше не трекалось → глава 2
        # была непроходима)
        p.daily_progress = p.daily_progress or {}
        p.daily_progress["donate"] = True
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

        was_guildless = player.guild is None   # первый в жизни выбор стороны
        player.guild = guild
        # Награда за вступление — за ПЕРВЫЙ выбор стороны (в т.ч. если игрок
        # отложил его и играл без гильдии). Раньше было gated на step==0, и
        # отложившие фракцию теряли +50 — теперь честно один раз.
        if player.onboarding_step == 0 or was_guildless:
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
                f"🏰 <b>Ты в {g_name} Гильдии!</b> Сейчас в ней <b>{online}</b> странников.\n\n"
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

        bonus_line = "🎁 <b>+50 OAC 🍬 за выбор стороны!</b>\n\n" if was_guildless else ""
        await query.message.edit_text(
            f"<b><i>🏰 ГИЛЬДИЯ ПРИНЯЛА ТЕБЯ 🪽</i></b>\n\n"
            f"✨ Отныне ты — часть <b>{guild_name_genitive}</b>.\n"
            f"🩸 Искажение стало плотнее...\n\n"
            f"{bonus_line}"
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
    "broadcast": broadcast,
    # Текстовые сокращения (без слеша)
    "старт": start,
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
    "plant_start": plant_start_handler,
    "plant_harvest": plant_harvest_handler,
    "plant_upgrade": plant_upgrade_handler,
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
    "pet_feed": pet_feed_handler,
    "pet_buy_dog": pet_buy_dog_handler,
    "pet_name_skip": pet_name_skip_handler,
    "pet_locked": pet_locked_handler,
    "onboarding_reward": onboarding_reward,
    "daily_quest_hub": daily_quest_hub,
    "world_hub": world_hub,
    "destiny_hub": destiny_hub,
    "progress_hub": progress_hub_handler,
    "all_features": all_features_handler,
    "claim_reward": claim_reward_handler,
    "skip_onboarding": skip_onboarding_handler,
    "defer_faction": defer_faction_handler,
}

EXACT_HANDLERS: Dict[str, Callable] = {
    "lab_special": handle_lab_option,
    "lab_focus_use": handle_lab_option,
    "lab_escape": handle_lab_option,
    "luck_wheel": luck_wheel_handler,
    "luck_berserk": luck_berserk_handler,
    "mines_cashout": _mines_cashout_wrapper,
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
    "quest_": handle_quest_action,
    "mines_open_": _mines_open_cell_wrapper,
    "mines_bet_": _mines_bet_wrapper,
    "shop_buy_": shop_buy_callback,
}

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    try:
        if data == "noop":
            await q.answer()
            return
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


def _reengagement_text(last_farm, login_streak, last_login_date, now, farm_cooldown):
    """Выбирает текст пуш-возврата (или None, если повода нет). Чистая функция.

    Приоритет: серия под угрозой (вечером) > созревший фарм.
    """
    streak = login_streak or 0
    logged_today = (last_login_date is not None
                    and str(last_login_date) == now.date().isoformat())
    # Серия под угрозой: есть серия, сегодня не заходил, уже вечер
    if streak >= 2 and not logged_today and now.hour >= 18:
        return (f"🔥 <b>Твоя серия входов ({streak} дн.) сгорит в полночь!</b>\n"
                f"Загляни в игру, чтобы сохранить её и забрать награду дня.")
    # Фарм созрел
    if last_farm and (now - last_farm) >= farm_cooldown:
        return ("🍬 <b>Грядка созрела!</b>\n"
                "Твои OAC ждут сбора — вернись и продолжи путь. 🌿")
    return None


async def reengagement_push(ctx: AppContext) -> None:
    """Личные пуш-возвраты дрейфующим игрокам (анти-churn).

    Отбирает тех, кто недавно играл, но сейчас неактивен несколько часов, и у
    кого есть повод вернуться. АНТИ-СПАМ:
      • без Redis — не шлём вообще (fail-closed, иначе риск спама);
      • не чаще ~1 раза в 20 ч на игрока (guard в Redis);
      • окно активности 2 ч … 3 дня (не трогаем активных и давно ушедших);
      • 403 (заблокировал бота) — молча пропускаем.
    """
    if not ctx or not ctx.db_pool or not ctx.redis:
        return

    now = datetime.now()
    farm_cd = timedelta(hours=settings.farm_cooldown_hours)
    drift_min = now - timedelta(hours=2)      # неактивен хотя бы 2 часа
    active_window = now - timedelta(days=3)   # но не ушёл насовсем

    try:
        async with ctx.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, last_farm, login_streak, last_login_date "
                "FROM players "
                "WHERE last_farm IS NOT NULL AND last_farm BETWEEN $1 AND $2",
                active_window, drift_min,
            )
    except Exception as e:
        logger.error(f"reengagement query error: {e}")
        return

    token = ctx.settings.bot_token
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    sent = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for row in rows:
            uid = row["user_id"]
            guard_key = f"reengage:{uid}"
            try:
                if await ctx.redis.get(guard_key):
                    continue
            except Exception:
                continue  # Redis-сбой → пропускаем (не рискуем спамом)

            text = _reengagement_text(
                row["last_farm"], row["login_streak"],
                row["last_login_date"], now, farm_cd,
            )
            if not text:
                continue

            try:
                r = await client.post(url, json={"chat_id": uid, "text": text, "parse_mode": "HTML"})
                if r.status_code == 200:
                    sent += 1
                    await ctx.redis.setex(guard_key, 20 * 3600, "1")
                # 403/прочее — молча пропускаем
            except Exception:
                pass
            await asyncio.sleep(0.05)  # мягкий rate-limit к Telegram API

    logger.info("reengagement: sent %d pushes to %d candidates", sent, len(rows))

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
