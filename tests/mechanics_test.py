"""Тесты игровых механик: чистая логика + доменные сервисы на реальной БД.

Дополняет tests/smoke_test.py. Покрывает функции наград/прогрессии и сервисы
(питомец, война гильдий), чтобы будущая разбивка хендлеров была под защитой.

Запуск (нужны Postgres и Redis):
    export DATABASE_URL_AIVEN="postgresql://botuser:botpass@127.0.0.1:5432/botdb"
    export REDIS_URL="redis://127.0.0.1:6379/0"
    python tests/mechanics_test.py
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

from bot import (
    calculate_smoke_reward, _calculate_reward, daily_config,
    _calc_multiplier, _generate_mines_field, get_medal_target,
    get_rank_progress, _get_craft_stats, FARM_MEDALS,
    _build_next_day_preview, _build_daily_message, _reengagement_text, reengagement_push,
    create_tables, _run_migrations, PlayerRepository, Player,
    PetService, GuildWarService, WarConfig, WarSettings, PET_CONFIG,
)
from datetime import datetime, timedelta

TEST_UID = 999002


def test_pure(passed):
    # --- дунуть: значение всегда в допустимом множестве (2000 прогонов) ---
    class _P:  # p не используется функцией, но передаём объект
        smoke_count = 0
    for _ in range(2000):
        val = calculate_smoke_reward(_P(), happy_hour=False)
        assert val == 0 or val == -5 or 15 <= val <= 40, f"дунуть вернул {val}"
    # с happy hour положительный доход удваивается (30..80)
    for _ in range(2000):
        val = calculate_smoke_reward(_P(), happy_hour=True)
        assert val in (0, -5) or 30 <= val <= 80, f"дунуть HH вернул {val}"
    passed.append("calculate_smoke_reward: значения в допустимых диапазонах")

    # --- стрик-награда: титулы детерминированы, доход не ниже базы×hot ---
    r5 = _calculate_reward(5, daily_config)
    assert r5.title is None and r5.total_oac >= int(30 * 1.1)
    r7 = _calculate_reward(7, daily_config)
    assert r7.title == "🕊️" and r7.total_oac >= int(50 * 1.1)
    r14 = _calculate_reward(14, daily_config)
    assert r14.title == "🔮" and r14.total_oac >= int(100 * 1.1)
    passed.append("_calculate_reward: титулы 7/14 и hot-streak множитель")

    # --- множитель мин: 1.0 → 3.0, монотонно, границы точные ---
    assert _calc_multiplier(0) == 1.0
    assert _calc_multiplier(22) == 3.0
    prev = -1.0
    for step in range(0, 23):
        m = _calc_multiplier(step)
        assert 1.0 <= m <= 3.0 and m >= prev, f"множитель не монотонен на шаге {step}"
        prev = m
    passed.append("_calc_multiplier: 1.0→3.0, монотонно")

    # --- поле мин: 5x5, ровно 3 мины, координаты валидны ---
    for _ in range(500):
        field, mines = _generate_mines_field()
        assert len(field) == 5 and all(len(row) == 5 for row in field)
        assert len(mines) == 3
        assert all(0 <= r <= 4 and 0 <= c <= 4 for r, c in mines)
    passed.append("_generate_mines_field: 5x5, ровно 3 мины")

    # --- цель медали: следующий порог / максимум ---
    assert get_medal_target(0, FARM_MEDALS) == 1
    assert get_medal_target(5, FARM_MEDALS) == 10
    assert get_medal_target(50, FARM_MEDALS) == 250
    assert get_medal_target(10_000, FARM_MEDALS) == FARM_MEDALS[-1][0]
    passed.append("get_medal_target: пороги и максимум")

    # --- прогресс ранга: не падает и содержит метку на всех уровнях ---
    for bal in (0, 4999, 5000, 25000, 999999):
        s = get_rank_progress(bal)
        assert "Ранг" in s and "%" in s
    passed.append("get_rank_progress: рендер на всех рангах")

    # --- статистика крафта: возвращает имя медали и цель ---
    stats = _get_craft_stats(balance=100, blunts=3, craft_count=5)
    assert "medal_name" in stats and "target" in stats and stats["target"] >= 5
    passed.append("_get_craft_stats: структура ответа")

    # --- предпросмотр завтрашней награды (крючок предвкушения + titles) ---
    assert "День 2" in _build_next_day_preview(1, daily_config)
    p6 = _build_next_day_preview(6, daily_config)   # завтра день 7 → титул 🕊️
    assert "титул" in p6 and "🕊️" in p6
    assert "🔮" in _build_next_day_preview(13, daily_config)  # завтра день 14
    full = _build_daily_message(1, _calculate_reward(1, daily_config), daily_config)
    assert "OAC" in full and "Завтра" in full
    passed.append("_build_next_day_preview: предпросмотр + титулы 7/14")

    # --- текст пуш-возврата (приоритеты: серия вечером > фарм > ничего) ---
    cd = timedelta(minutes=30)
    evening = datetime(2026, 1, 1, 20, 0)
    morning = datetime(2026, 1, 1, 10, 0)
    old_farm = evening - timedelta(hours=1)      # фарм созрел
    fresh_farm = evening - timedelta(minutes=5)  # фарм не готов
    assert "серия" in _reengagement_text(old_farm, 3, None, evening, cd).lower()
    # зашёл сегодня → не про серию, но фарм готов → про грядку
    assert "Грядка" in _reengagement_text(old_farm, 3, evening.date().isoformat(), evening, cd)
    # утро (не вечер) → серия не триггерится, но фарм готов
    old_farm_morning = morning - timedelta(hours=1)
    assert "Грядка" in _reengagement_text(old_farm_morning, 3, None, morning, cd)
    # нет повода: фарм не готов, серии нет
    assert _reengagement_text(fresh_farm, 0, None, morning, cd) is None
    passed.append("_reengagement_text: серия/фарм/пусто по приоритету")


async def test_services(passed):
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL_AIVEN"], min_size=1, max_size=3)
    async with pool.acquire() as conn:
        await create_tables(conn)
        await _run_migrations(conn)
    redis_client = await aioredis.from_url(os.environ["REDIS_URL"])
    repo = PlayerRepository(pool, redis_client, TTLCache(maxsize=100, ttl=600))

    # чистим тестового игрока
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE user_id=$1", TEST_UID)

    # --- PetService: нет денег → no_money ---
    await repo.save(Player(user_id=TEST_UID, username="PetTester", balance=0, exists=True))
    pet_service = PetService(repo, PET_CONFIG)
    res = await pet_service.buy(TEST_UID, "dog")
    assert res and res["status"] == "no_money", f"ждали no_money, получили {res}"

    # --- достаточно денег → ok, баланс списан, питомец установлен ---
    async def _set_balance(p, conn):
        p.balance = 5000
    await repo.atomic_update(TEST_UID, _set_balance)
    res = await pet_service.buy(TEST_UID, "dog")
    assert res and res["status"] == "ok", f"ждали ok, получили {res}"
    after = await repo.get_by_id(TEST_UID)
    assert after.balance == 5000 - PET_CONFIG["dog"]["price"] and after.pet
    passed.append("PetService.buy: no_money / ok / списание баланса")

    # --- повторная покупка → already_have ---
    res = await pet_service.buy(TEST_UID, "dog")
    assert res and res["status"] == "already_have"
    # --- имя питомца обрезается до max_name_len ---
    long_name = "x" * 999
    await pet_service.set_name(TEST_UID, long_name)
    reloaded = await repo.get_by_id(TEST_UID)
    assert len(reloaded.pet_name) == PET_CONFIG["dog"]["max_name_len"]
    passed.append("PetService: already_have + обрезка имени")

    # --- PetService.feed: восстанавливает сытость и отмечает задание квеста pet ---
    async def _starve(p, conn):
        p.pet_hunger = 10
    await repo.atomic_update(TEST_UID, _starve)
    fed = await pet_service.feed(TEST_UID)
    assert fed and fed["status"] == "ok", f"ждали ok, получили {fed}"
    after_feed = await repo.get_by_id(TEST_UID)
    assert after_feed.pet_hunger == 100 and after_feed.daily_progress.get("pet") is True
    passed.append("PetService.feed: сытость=100 + задание pet отмечено")

    # --- GuildWarService: старт/стоп/статус ---
    war = GuildWarService(pool, redis_client, WarConfig(), WarSettings())
    await war.stop_war()
    assert await war.is_war_active() is False
    await war.start_war()
    assert await war.is_war_active() is True
    await war.stop_war()
    assert await war.is_war_active() is False
    passed.append("GuildWarService: start/stop/is_war_active")

    # --- reengagement_push: fail-closed без Redis (не шлёт, не падает) ---
    class _NoRedisCtx:
        db_pool = object()   # truthy
        redis = None
    await reengagement_push(_NoRedisCtx())   # должно тихо вернуться
    passed.append("reengagement_push: fail-closed без Redis")

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE user_id=$1", TEST_UID)
    await redis_client.aclose()
    await pool.close()


async def main() -> int:
    passed = []
    test_pure(passed)
    await test_services(passed)
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nТесты механик пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
