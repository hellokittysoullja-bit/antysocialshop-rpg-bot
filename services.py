"""Слой доменных сервисов.

Бизнес-логика поверх репозитория/моделей: война гильдий и питомцы.
Зависит только от нижних слоёв (repository, tenacity, pydantic) — без обратных
импортов из bot.py, поэтому циклов нет.
"""
import os
import time
import enum
import logging
from datetime import datetime, timedelta
from enum import Enum, auto

import asyncpg
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log,
)

from repository import PlayerRepository


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
