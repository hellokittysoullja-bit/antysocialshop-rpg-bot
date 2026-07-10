"""Смоук-тест бота на реальных Postgres + Redis.

Проверяет, что модуль поднимается, схема/миграции применяются и ключевые
чистые функции ведут себя корректно. Не мокает БД — гоняет по-настоящему.

Запуск (нужны запущенные Postgres и Redis):

    export DATABASE_URL_AIVEN="postgresql://botuser:botpass@127.0.0.1:5432/botdb"
    export REDIS_URL="redis://127.0.0.1:6379/0"
    python tests/smoke_test.py

TOKEN/ADMIN_ID подставляются фиктивные — сеть в Telegram не идёт.
Скрипт создаёт и удаляет тестового игрока с user_id=999001.
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TOKEN", "123:DUMMY")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://botuser:botpass@127.0.0.1:5432/botdb")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("RENDER_URL", "")

import asyncpg
import redis.asyncio as aioredis
from cachetools import TTLCache

import bot
from bot import (
    create_tables, _run_migrations, PlayerRepository, Player,
    compute_rank_info, _calculate_farm_reward, _format_farm_message,
    build_main_menu, FARM_MEDALS, get_medal_target,
)

TEST_UID = 999001


class _DummyTgContext:
    """Минимальная замена telegram context для чистых функций."""
    def __init__(self):
        self.user_data = {}
        self.bot_data = {}


class _Ctx:
    """Лёгкий AppContext-подобный объект (нужны только repo/cache)."""
    def __init__(self, repo, cache):
        self.repo = repo
        self.cache = cache
        self.war_service = None


async def main() -> int:
    passed = []

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL_AIVEN"], min_size=1, max_size=3)
    async with pool.acquire() as conn:
        await create_tables(conn)
        await _run_migrations(conn)
    passed.append("create_tables + _run_migrations")

    redis_client = await aioredis.from_url(os.environ["REDIS_URL"])
    cache = TTLCache(maxsize=100, ttl=600)
    repo = PlayerRepository(pool, redis_client, cache)
    ctx = _Ctx(repo, cache)

    # 1. round-trip игрока (БД + Redis)
    player = Player(user_id=TEST_UID, username="SmokeTester", balance=0, exists=True)
    await repo.save(player)
    loaded = await repo.get_by_id(TEST_UID)
    assert loaded.exists and loaded.username == "SmokeTester"
    passed.append("Player save -> get_by_id round-trip")

    # 2. ранги на всех порогах + prev/next
    for bal, name in {0: "Рекрут", 5000: "Ветеран", 20000: "Призрак",
                      50000: "Некромант", 25000: "Призрак"}.items():
        assert compute_rank_info(bal)[1] == name, f"ранг {bal}"
    _, _, _, _, nt, pt = compute_rank_info(25000)
    assert nt == 50000 and pt == 20000
    passed.append("compute_rank_info + пороги")

    # 3. Happy Hour читается из ctx.cache (регресс на баг farm/bot_data)
    tg = _DummyTgContext()
    tg.bot_data["ctx"] = ctx
    cache["happy_hour"] = True
    _, _, happy = _calculate_farm_reward(Player(user_id=TEST_UID), tg)
    assert happy is True, "Happy Hour не подхватился из ctx.cache"
    cache["happy_hour"] = False
    _, _, no_happy = _calculate_farm_reward(Player(user_id=TEST_UID), tg)
    assert no_happy is False
    passed.append("Happy Hour из ctx.cache")

    # 4. фарм-сообщение (баннеры крит/x10/HH)
    for crit, hh, earned in [(False, False, 60), (True, False, 200),
                             (True, False, 2000), (False, True, 120)]:
        assert "OAC" in _format_farm_message(
            earned, crit, hh, "", 1, get_medal_target(1, FARM_MEDALS), 500)
    passed.append("_format_farm_message баннеры")

    # 5. build_main_menu на живом игроке (дедуп рангов + daily_progress)
    text, kb = await build_main_menu(await repo.get_by_id(TEST_UID), ctx,
                                     _DummyTgContext(), full_mode=True)
    assert "ГЛАВНОЕ МЕНЮ" in text and kb.inline_keyboard
    passed.append("build_main_menu (full)")

    # уборка
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE user_id=$1", TEST_UID)
    await redis_client.aclose()
    await pool.close()

    for name in passed:
        print(f"  OK  {name}")
    print(f"\nСмоук-тест пройден: {len(passed)}/6")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
