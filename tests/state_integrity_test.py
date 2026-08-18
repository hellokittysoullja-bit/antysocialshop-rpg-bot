"""Интеграционные регрессии Quality Gate 01.

Запуск: DATABASE_URL_AIVEN=postgresql:///antysocialshop_state_test python3 tests/state_integrity_test.py
Тест намеренно использует локальный Postgres: проверяются именно транзакции,
а не только форма SQL в исходном файле.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("TOKEN", "123456:state-integrity-test-token")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql:///antysocialshop_state_test")
os.environ.setdefault("RENDER_URL", "http://localhost")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncpg
from cachetools import TTLCache

import bot
from game_models import Player
from repository import PlayerRepository


async def make_repo():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL_AIVEN"], min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await bot.create_tables(conn)
        await bot._run_migrations(conn)
        await conn.execute("TRUNCATE TABLE lab_runs, pending_gifts, achievements_awarded, players CASCADE")
    return pool, PlayerRepository(pool, None, TTLCache(maxsize=100, ttl=60))


async def seed_player(repo, user_id, username, inventory=None, balance=0):
    player = Player(
        user_id=user_id,
        username=username,
        balance=balance,
        total_earned=balance,
        inventory=inventory or [],
        exists=True,
    )
    await repo.save(player)


async def check_pending_gift_is_single_transaction(repo, pool):
    item = {"id": "named_gift_1", "type": "named", "name": "Сохранённый дар"}
    await seed_player(repo, 1001, "giver", [item])
    await seed_player(repo, 1002, "receiver")

    status, queued = await repo.atomic_enqueue_named_gift(
        1001, "named_gift_1", username_lower="receiver"
    )
    assert status == "ok" and queued["id"] == "named_gift_1"
    giver = await repo.get_by_id(1001)
    assert not giver.inventory, "предмет должен покинуть дарителя только вместе с постановкой в очередь"
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM pending_gifts WHERE username_lower='receiver'")
    assert count == 1, "предмет обязан существовать в очереди после изъятия"

    received = await repo.atomic_claim_pending_gifts(1002, "receiver")
    assert [gift["id"] for gift in received] == ["named_gift_1"]
    receiver = await repo.get_by_id(1002)
    assert [gift["id"] for gift in receiver.inventory] == ["named_gift_1"]
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM pending_gifts WHERE username_lower='receiver'")
    assert count == 0, "очередь удаляется только после зачисления в инвентарь"
    assert await repo.atomic_claim_pending_gifts(1002, "receiver") == [], "повторная выдача не создаёт дубликат"


async def check_lab_run_survives_reload_and_pays_once(repo, pool):
    await seed_player(repo, 2001, "runner", balance=100)
    room = bot._lab_room_for_depth(1)
    state = {
        "lab_room": 1,
        "lab_hp": 83,
        "lab_max_hp": 100,
        "lab_focus": 2,
        "lab_rewards": [25, 40],
        "lab_depth": 1,
        "lab_total_rooms": 5,
        "lab_attack_bonus": 0.5,
        "lab_focused_attack": True,
        "lab_curse_rooms": 1,
        "lab_amulet": True,
        "lab_current_room": room,
        "lab_turn": 7,
        "lab_phase": "active",
    }

    async def mark_attempt(player, conn):
        player.daily_progress = {"lab": True}
        return "marked"

    status, payload = await repo.atomic_start_lab_run(2001, state, mark_attempt)
    assert status == "ok" and payload == "marked"
    restored = await repo.get_lab_run(2001)
    expected_json_state = json.loads(json.dumps(state))
    assert restored == expected_json_state, "рестарт обязан восстановить полный снимок без подмены комнаты"

    async def mark_final(run, conn):
        run["lab_phase"] = "final"
        return "ready"

    mutation = await repo.atomic_mutate_lab_run(2001, mark_final)
    assert mutation[0] == "ready" and mutation[1]["lab_phase"] == "final"

    async def settle(player, run, conn):
        if run.get("lab_phase") != "final":
            return None
        player.balance += sum(run["lab_rewards"]) + 50
        return player.balance

    result = await repo.atomic_finish_lab_run(2001, settle)
    assert result == 215, "финал должен выплатить базовый сундук и сохранённую добычу ровно один раз"
    assert await repo.get_lab_run(2001) is None, "завершённый забег удаляется вместе с выплатой"
    assert await repo.atomic_finish_lab_run(2001, settle) is None, "повторный финал не платит второй раз"


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()
        self.bot_data = {}


class FakeAppContext:
    def __init__(self, repo):
        self.repo = repo
        self.redis = None


async def check_achievement_reward_does_not_double_count():
    player = Player(user_id=3001, balance=10, total_earned=10)
    bot._apply_achievement_reward(player, "+25 OAC, Титул 🕯️, Фон 👁️, Рамка 🩸")
    assert player.balance == 35
    assert player.total_earned == 10, "atomic_update добавит дельту один раз при сохранении"
    assert "🕯️" in player.titles
    assert "👁️" in player.profile_skins["unlocked_backgrounds"]
    assert "🩸" in player.profile_skins["unlocked_frames"]
    source = Path(bot.__file__).read_text(encoding="utf-8")
    assert "LOCK TABLE achievements_awarded" not in source, "глобальная блокировка достижений не должна вернуться"


async def check_achievement_concurrency(repo, pool):
    await seed_player(repo, 4001, "achiever")

    async def prepare(player, conn):
        player.craft_count = 15
        player.onboarding_step = -1

    await repo.atomic_update(4001, prepare)
    context = FakeContext()
    app_context = FakeAppContext(repo)
    await asyncio.gather(
        bot.check_achievements(4001, context, ctx=app_context),
        bot.check_achievements(4001, context, ctx=app_context),
    )
    player = await repo.get_by_id(4001)
    assert player.balance == 100 and player.total_earned == 100, "параллельная проверка выдаёт +100 OAC ровно один раз"
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT ach_id FROM achievements_awarded WHERE user_id=4001 ORDER BY ach_id")
    assert [row["ach_id"] for row in rows] == ["craft_1", "craft_15"], "каждое достижение записано единожды"
    assert len(context.bot.messages) == 2, "уведомления соответствуют двум уникальным достижениям"


async def main():
    pool, repo = await make_repo()
    try:
        await check_pending_gift_is_single_transaction(repo, pool)
        print("OK pending gifts: атомарное изъятие, выдача и защита от дубля")
        await check_lab_run_survives_reload_and_pays_once(repo, pool)
        print("OK labyrinth: персистентный снимок и единственная выплата")
        await check_achievement_reward_does_not_double_count()
        await check_achievement_concurrency(repo, pool)
        print("OK achievements: награда без двойного учёта, без global lock и без дубля при конкуренции")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
