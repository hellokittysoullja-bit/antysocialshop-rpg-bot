# ИМПОРТЫ
# ============================================================
import asyncio, logging, sys, os, signal, time, traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple, Set, Optional

import asyncpg
import redis.asyncio as aioredis
from aiohttp import web
from cachetools import TTLCache

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder,
    MessageHandler, CallbackQueryHandler, ContextTypes,
    AIORateLimiter, filters
)
from telegram.request import HTTPXRequest

# === Импорт реальных классов и функций из bot.py ===
from bot import (
    settings,
    AppContext,
    PlayerRepository,
    WarConfig,
    WarSettings,
    GuildWarService,
    PetService,
    AchievementService,
    TEXT_COMMAND_HANDLERS,
    CALLBACKS,
    EXACT_HANDLERS,
    PREFIX_HANDLERS,
    handle_text,
    button_handler,
    global_error_handler,
    welcome_new_member,
    create_tables,
    _run_migrations,
    keep_db_alive,
    update_pulse,
    echo_of_distortion,
    happy_hour_trigger,
    weekly_guild_rating,
    _safe_send_guild_message,
    BLUNT_IMAGES,
    clean_old_data,   # если используется
)

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ============================================================
BLUNT_IMAGES: Dict[str, str] = {}
TEXT_COMMAND_HANDLERS: Dict[str, callable] = {}

background_tasks: Set[asyncio.Task] = set()
active_updates: Set[asyncio.Task] = set()

# ============================================================
# УТИЛИТЫ ДЛЯ НАДЁЖНЫХ ФОНОВЫХ ЗАДАЧ
# ============================================================
def resilient_task(func):
    """Декоратор, перезапускающий задачу при любом исключении."""
    async def wrapper(*args, **kwargs):
        while True:
            try:
                await func(*args, **kwargs)
            except asyncio.CancelledError:
                logger.info(f"Задача {func.__name__} отменена")
                break
            except Exception:
                logger.exception(f"Задача {func.__name__} упала, перезапуск через 5с")
                await asyncio.sleep(5)
    return wrapper

# ============================================================
# ЗАГРУЗКА БЛАНТОВ (с защитой от падения Redis)
# ============================================================
async def load_blunt_images(ctx: AppContext):
    """Прогрев кэша изображений, устойчивый к недоступности Redis."""
    try:
        if not ctx.redis:
            logger.warning("Redis не доступен, изображения не кэшируются")
            return
        for rarity in ("common", "rare", "epic", "legendary"):
            try:
                cached = await ctx.redis.get(f"blunt_image:{rarity}")
                if cached:
                    BLUNT_IMAGES[rarity] = cached.decode() if isinstance(cached, bytes) else cached
                    continue
                # Замените на реальный вызов получения из БД
                saved = None  # await get_setting(f"blunt_image_{rarity}", ctx=ctx)
                if saved:
                    BLUNT_IMAGES[rarity] = saved
                    await ctx.redis.setex(f"blunt_image:{rarity}", 86400, saved)
            except Exception:
                logger.warning(f"Ошибка загрузки изображения {rarity} из Redis")
    except Exception as e:
        logger.warning(f"Критическая ошибка прогрева блантов: {e}")

# ============================================================
# ФОНОВЫЕ ДЖОБЫ (с автоматическим перезапуском)
# ============================================================
async def background_jobs(ctx: AppContext):
    """Создаёт периодические задачи, все с декоратором @resilient_task."""
    async def update_pulse(ctx): pass
    async def echo_of_distortion(ctx): pass
    async def happy_hour_trigger(ctx): pass
    async def weekly_guild_rating(ctx): pass

    @resilient_task
    async def job_keep_db_alive():
        while True:
            try:
                await keep_db_alive(ctx)
            except Exception:
                logger.exception("keep_db_alive error")
            await asyncio.sleep(180)

    @resilient_task
    async def job_update_pulse():
        while True:
            try:
                await update_pulse(ctx)
            except Exception:
                logger.exception("update_pulse error")
            await asyncio.sleep(3600)

    @resilient_task
    async def job_echo_of_distortion():
        while True:
            try:
                await echo_of_distortion(ctx)
            except Exception:
                logger.exception("echo_of_distortion error")
            await asyncio.sleep(21600)

    @resilient_task
    async def job_happy_hour():
        while True:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            await asyncio.sleep(wait)
            try:
                await happy_hour_trigger(ctx)
            except Exception:
                logger.exception("happy_hour_trigger error")

    @resilient_task
    async def job_weekly_guild_rating():
        while True:
            now = datetime.now(timezone.utc)
            # Воскресенье 00:00 UTC (weekday=6)
            target = now.replace(hour=0, minute=0, second=0, microsecond=0)
            days_until_sunday = (6 - now.weekday()) % 7
            target += timedelta(days=days_until_sunday)
            wait = (target - now).total_seconds()
            if wait <= 0:
                wait += 7 * 86400
            await asyncio.sleep(wait)
            try:
                await weekly_guild_rating(ctx)
            except Exception:
                logger.exception("weekly_guild_rating error")

    # Создаём задачи и сохраняем их
    for coro in (job_keep_db_alive, job_update_pulse, job_echo_of_distortion,
                 job_happy_hour, job_weekly_guild_rating):
        t = asyncio.create_task(coro())
        background_tasks.add(t)
        t.add_done_callback(background_tasks.discard)

    logger.info("✅ Фоновые джобы запущены (с авто-перезапуском)")

# ============================================================
# ИНИЦИАЛИЗАЦИЯ – единый контекст + прогрев + джобы
# ============================================================
async def on_startup(app: Application):
    logger.info("=== ON_STARTUP CALLED ===")
    try:
        # 1. Пул БД с keepalive и ограничением жизни соединений
        pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1, max_size=3, command_timeout=15,
            max_inactive_connection_lifetime=120.0,
            max_queries=50000,
            server_settings={
                'keepalives_idle': '60',
                'keepalives_interval': '10',
                'keepalives_count': '5'
            }
        )
        app.bot_data["db_pool"] = pool

        # 2. Миграции (вставьте свои)
        async with pool.acquire() as conn:
            pass  # await create_tables(conn); await _run_migrations(conn)

        # 3. Redis с повторными попытками подключения
        redis_client = None
        if settings.redis_url:
            try:
                redis_client = await aioredis.from_url(
                    settings.redis_url,
                    retry_on_timeout=True,
                    health_check_interval=30
                )
                await redis_client.ping()
                logger.info("✅ Redis подключён")
            except Exception as e:
                logger.warning(f"Redis недоступен при старте: {e}")

        # 4. Сервисы (полные)
        cache = TTLCache(maxsize=1000, ttl=600)  # 1000 записей для 100 игроков
        repo = PlayerRepository(pool, redis_client, cache)
        war_service = GuildWarService(pool, redis_client, WarConfig(), WarSettings())
        pet_service = PetService(repo, {"dog": {"name": "🐕 Песик", "price": 3000, "max_name_len": 15}})
        achievement_service = AchievementService(pool, redis_client, repo)

        # 5. Единый контекст
        ctx = AppContext(
            db_pool=pool,
            redis_client=redis_client,
            cache=cache,
            settings=settings,
            repo=repo,
            war_service=war_service,
            pet_service=pet_service,
            achievement_service=achievement_service,
        )
        app.bot_data["ctx"] = ctx
        logger.info("✅ Контекст сохранён")

        # 6. Прогрев кэша (безопасный)
        await load_blunt_images(ctx)

    except Exception:
        logger.exception("Критическая ошибка инициализации")
        raise

    # 7. Запускаем фоновые задачи (с сохранением для отмены)
    await background_jobs(app.bot_data["ctx"])

    logger.info("🚀 Бот готов к работе")

# ============================================================
# GRACEFUL SHUTDOWN (с отменой задач и ожиданием вебхуков)
# ============================================================
async def on_shutdown(app: Application):
    logger.info("🛑 Завершение работы...")

    # 1. Отменяем фоновые задачи
    for task in list(background_tasks):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    logger.info("Фоновые задачи остановлены")

    # 2. Ждём завершения активных вебхук-обновлений (с таймаутом 10с)
    if active_updates:
        try:
            await asyncio.wait_for(
                asyncio.gather(*active_updates, return_exceptions=True),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.warning("Не все вебхук-задачи завершились вовремя")

    # 3. Закрываем пул и Redis
    pool = app.bot_data.get("db_pool")
    if pool:
        await pool.close()
    ctx = app.bot_data.get("ctx")
    if ctx and ctx.redis:
        await ctx.redis.close()
    logger.info("🏁 Ресурсы освобождены")

# ============================================================
# АНТИСПАМ – совершенная версия (Redis Lua + in‑memory fallback)
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

# ============================================================
# ОБРАБОТЧИКИ
# ============================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    user_id = update.effective_user.id
    ctx = context.application.bot_data.get("ctx")
    if not ctx:
        return

    # Единый антиспам
    if not await check_rate_limit_redis(ctx, user_id, "text", 10, 10):
        await msg.reply_text("⚠️ Слишком часто. Подожди секунду.")
        return

    # Сначала состояния ввода
    if context.user_data.get('awaiting_pet_name'):
        return await handle_pet_name(update, context)
    if context.user_data.get('awaiting_named_blunt'):
        return await handle_named_name(update, context)
    if context.user_data.get('gifting_blunt_id'):
        return await handle_gift_username(update, context)

    raw_text = msg.text.strip()
    parts = raw_text.split()
    command = parts[0].lstrip("/").split("@")[0].lower()
    context.args = parts[1:]

    handler = TEXT_COMMAND_HANDLERS.get(command)
    if handler:
        try:
            await handler(update, context)
        except Exception:
            logger.exception(f"Ошибка команды '{command}' от {user_id}")
            await msg.reply_text("⚠️ Внутренняя ошибка. Попробуйте позже.")

# Примеры команд – добавьте все свои
async def start(update, context):
    await update.message.reply_text("Добро пожаловать в RPG!")

async def farm(update, context):
    args = context.args
    await update.message.reply_text(f"Вы фармите золото... {args}")

TEXT_COMMAND_HANDLERS = {
    "start": start,
    "farm": farm,
    # ...
}

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    # ваша логика

async def global_error_handler(update, context):
    logger.error("Ошибка при обработке обновления", exc_info=context.error)

# ============================================================
# ЗАПУСК (aiohttp + PTB + корректная обработка сигналов)
# ============================================================
async def main_async():
    # Инициализируем переменные вне try для безопасного finally
    tg_app = None
    runner = None

    try:
        try:
            import uvloop
            uvloop.install()
        except ImportError:
            pass

        if not settings.bot_token or not settings.database_url:
            raise RuntimeError("BOT_TOKEN and DATABASE_URL must be set")

        request = HTTPXRequest(
            connection_pool_size=50,
            read_timeout=10,
            write_timeout=10,
            connect_timeout=5
        )

        tg_app = (ApplicationBuilder()
                  .token(settings.bot_token)
                  .request(request)
                  .rate_limiter(AIORateLimiter())
                  .post_init(on_startup)
                  .post_shutdown(on_shutdown)
                  .build())

        tg_app.add_handler(MessageHandler(filters.TEXT, handle_text))
        tg_app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
        tg_app.add_handler(CallbackQueryHandler(button_handler))
        tg_app.add_error_handler(global_error_handler)

        await tg_app.initialize()
        logger.info("PTB инициализирован")

        # --- Кастомный вебхук-сервер ---
        update_semaphore = asyncio.Semaphore(100)  # плавная обработка пиков

        async def process_update(data: dict):
            ctx = tg_app.bot_data.get("ctx")
            if not ctx:
                logger.critical("Контекст отсутствует")
                return
            async with update_semaphore:
                try:
                    update = Update.de_json(data, tg_app.bot)
                    await tg_app.process_update(update)
                except Exception:
                    logger.exception("Ошибка обработки обновления")

        async def handle_webhook(request):
            # Проверка секретного токена
            if settings.webhook_secret:
                secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
                if secret != settings.webhook_secret:
                    logger.warning("Неверный секретный токен")
                    return web.Response(status=403)

            try:
                data = await request.json()
            except Exception:
                return web.Response(text="Bad Request", status=400)
            if not isinstance(data, dict) or "update_id" not in data:
                return web.Response(text="Bad Request", status=400)

            # Создаём задачу и следим за ней
            task = asyncio.create_task(process_update(data))
            active_updates.add(task)
            task.add_done_callback(active_updates.discard)
            return web.Response(text="OK")

        async def healthcheck(request):
            return web.Response(text="OK")

        app = web.Application()
        app.router.add_post(settings.webhook_path, handle_webhook)
        app.router.add_get("/healthz", healthcheck)

        # Установка вебхука
        if settings.webhook_url:
            await tg_app.bot.set_webhook(
                url=settings.webhook_url,
                secret_token=settings.webhook_secret,
                allowed_updates=["message", "callback_query"]
            )
        else:
            logger.warning("WEBHOOK_URL не задан – вебхук не установлен")

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", settings.port)
        await site.start()
        logger.info("Веб-сервер запущен")

        # Ожидание сигнала завершения
        stop_event = asyncio.Event()
        def shutdown_signal(signum):
            stop_event.set()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: shutdown_signal(signal.SIGTERM))
        loop.add_signal_handler(signal.SIGINT, lambda: shutdown_signal(signal.SIGINT))

        await stop_event.wait()

    except Exception:
        traceback.print_exc()
        sys.exit(1)
    finally:
        logger.info("Остановка сервера...")
        if tg_app:
            try:
                await tg_app.stop()
                await tg_app.shutdown()
            except Exception:
                logger.exception("Ошибка при остановке PTB")
        if runner:
            try:
                await runner.cleanup()
            except Exception:
                logger.exception("Ошибка при очистке aiohttp runner")
        logger.info("🏁 Приложение полностью остановлено")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
