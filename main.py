# ИМПОРТЫ
# ============================================================
import asyncio, logging, sys, signal, traceback
from datetime import datetime, timedelta, timezone
from typing import Set

import asyncpg
import redis.asyncio as aioredis
from aiohttp import web
from cachetools import TTLCache

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder,
    MessageHandler, CallbackQueryHandler,
    AIORateLimiter, filters
)
from telegram.request import HTTPXRequest

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
    PREFIX_HANDLERS
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
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

background_tasks: Set[asyncio.Task] = set()
active_updates: Set[asyncio.Task] = set()

# ============================================================
def resilient_task(func):
    """Перезапускает задачу при любом исключении."""
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
            except Exception:
                logger.warning(f"Ошибка загрузки изображения {rarity} из Redis")
    except Exception as e:
        logger.warning(f"Критическая ошибка прогрева блантов: {e}")

# ============================================================
async def background_jobs(ctx: AppContext):
    """Создаёт периодические задачи с авто-перезапуском."""

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

    for coro in (job_keep_db_alive, job_update_pulse, job_echo_of_distortion,
                 job_happy_hour, job_weekly_guild_rating):
        t = asyncio.create_task(coro())
        background_tasks.add(t)
        t.add_done_callback(background_tasks.discard)

    logger.info("✅ Фоновые джобы запущены (с авто-перезапуском)")

# ============================================================
async def on_startup(app: Application):
    """Инициализация ресурсов и контекста."""
    logger.info("=== ON_STARTUP CALLED ===")
    try:
        pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1, max_size=3, command_timeout=15,
            max_inactive_connection_lifetime=120.0,
            max_queries=50000,
        )
        app.bot_data["db_pool"] = pool

        async with pool.acquire() as conn:
            await create_tables(conn)
            await _run_migrations(conn)

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
                logger.warning(f"Redis недоступен: {e}")

        cache = TTLCache(maxsize=1000, ttl=600)
        repo = PlayerRepository(pool, redis_client, cache)
        war_service = GuildWarService(pool, redis_client, WarConfig(), WarSettings())
        pet_service = PetService(repo, {"dog": {"name": "🐕 Песик", "price": 3000, "max_name_len": 15}})
        achievement_service = AchievementService(pool, redis_client, repo)

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

        if redis_client:
            await load_blunt_images(ctx)

    except Exception:
        logger.exception("Критическая ошибка инициализации")
        raise

    await background_jobs(app.bot_data["ctx"])
    logger.info("🚀 Бот готов к работе")

# ============================================================
async def on_shutdown(app: Application):
    """Корректное завершение всех задач и освобождение ресурсов."""
    logger.info("🛑 Завершение работы...")

    # Отменяем фоновые задачи
    for t in background_tasks:
        t.cancel()
    # Отменяем активные обработки вебхуков
    for t in active_updates:
        t.cancel()

    # Ожидаем их завершения с таймаутом
    all_tasks = list(background_tasks) + list(active_updates)
    if all_tasks:
        done, pending = await asyncio.wait(all_tasks, timeout=5, return_when=asyncio.ALL_COMPLETED)
        for p in pending:
            p.cancel()
            logger.warning("Принудительно отменяем зависшую задачу")

    pool = app.bot_data.get("db_pool")
    if pool:
        await pool.close()
    ctx = app.bot_data.get("ctx")
    if ctx and ctx.redis:
        await ctx.redis.close()
    logger.info("🏁 Ресурсы освобождены")

# ============================================================
async def main_async():
    tg_app = None
    runner = None
    try:
        # optional uvloop
        try:
            import uvloop
            uvloop.install()
        except ImportError:
            pass

        if not settings.bot_token or not settings.database_url:
            raise RuntimeError("TOKEN and DATABASE_URL must be set")

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

        # Хендлеры добавляются ДО initialize()
        tg_app.add_handler(MessageHandler(filters.TEXT, handle_text))
        tg_app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
        tg_app.add_handler(CallbackQueryHandler(button_handler))
        tg_app.add_error_handler(global_error_handler)

        # ЯВНАЯ ИНИЦИАЛИЗАЦИЯ (вызывает on_startup)
        logger.info("🔄 Запуск tg_app.initialize()...")
        await tg_app.initialize()
        logger.info("✅ PTB инициализирован")

        ctx = tg_app.bot_data.get("ctx")
        if not ctx:
            raise RuntimeError("AppContext не был создан в on_startup. Аварийное завершение.")

        # Идемпотентный кэш для вебхуков
        idempotent_cache = TTLCache(maxsize=100_000, ttl=600)
        update_semaphore = asyncio.Semaphore(100)

        async def process_update(data: dict):
            update_id = data.get("update_id")
            if update_id is None:
                return

            # Idempotency check
            if update_id in idempotent_cache:
                return
            idempotent_cache[update_id] = True

            last_known = tg_app.bot_data.get("last_update_id", 0)
            if update_id <= last_known:
                return

            ctx_local = tg_app.bot_data.get("ctx")
            if not ctx_local:
                logger.critical("Контекст не найден! Сервис не работоспособен.")
                raise RuntimeError("AppContext missing")

            async with update_semaphore:
                try:
                    update = Update.de_json(data, tg_app.bot)
                    await tg_app.process_update(update)
                    # Сохраняем максимальный update_id
                    if update_id > tg_app.bot_data.get("last_update_id", 0):
                        tg_app.bot_data["last_update_id"] = update_id
                        if ctx_local.redis:
                            asyncio.create_task(ctx_local.redis.set("bot:last_update_id", update_id))
                except Exception:
                    logger.exception("Критическая ошибка обработки обновления")

        async def handle_webhook(request):
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

            task = asyncio.create_task(process_update(data))
            active_updates.add(task)
            task.add_done_callback(active_updates.discard)
            return web.Response(text="OK")

        async def healthcheck(request):
            try:
                async with ctx.db_pool.acquire(timeout=1) as conn:
                    await conn.execute("SELECT 1")
                if ctx.redis:
                    await ctx.redis.ping()
                return web.Response(text="OK")
            except Exception:
                return web.Response(text="FAIL", status=500)

        # aiohttp веб-сервер
        app = web.Application()
        app.router.add_post(settings.webhook_path, handle_webhook)
        app.router.add_get("/healthz", healthcheck)

        webhook_url = f"{settings.render_url}{settings.webhook_path}"
        logger.info(f"🌐 Устанавливаю вебхук: {webhook_url}")
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=settings.webhook_secret,
            allowed_updates=["message", "callback_query"]
        )

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", settings.port)
        await site.start()
        logger.info("🚀 Веб-сервер запущен и готов принимать запросы")

        # Ожидание сигнала завершения
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
        except NotImplementedError:
            # Windows fallback
            signal.signal(signal.SIGINT, lambda s, f: stop_event.set())
            signal.signal(signal.SIGTERM, lambda s, f: stop_event.set())

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