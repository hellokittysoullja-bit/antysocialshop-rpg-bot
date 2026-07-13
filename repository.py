"""Слой доступа к данным: репозиторий игроков.

Circuit breakers и низкоуровневые утилиты берутся из infra, модель — из
game_models. Не зависит от bot.py (нет обратных импортов → нет циклов).
"""
import json
import logging

from tenacity import retry, stop_after_attempt, wait_exponential
from cachetools import TTLCache

from infra import redis_breaker, db_breaker, _json_safe_load
from game_models import Player

try:
    import pybreaker
except Exception:  # pragma: no cover
    pybreaker = None

logger = logging.getLogger(__name__)

# Единый источник правды: полный порядок колонок таблицы players.
# Используется во всех операциях чтения/записи (get_by_id, save, atomic_update),
# чтобы схема была описана ровно в одном месте.
PLAYER_COLUMNS = (
    "user_id", "username", "balance", "blunts", "guild", "last_farm",
    "last_ritual", "last_repent", "last_daily", "titles", "last_farm_date", "passive_level",
    "passive_collected", "karma", "inhaled", "smoke_count", "farm_count",
    "craft_count", "ritual_count", "referral_count", "last_mines",
    "inventory", "invited_by", "profile_skins", "login_streak",
    "last_login_date", "oath", "keys", "check_count", "m_essence",
    "lab_chests", "lab_deaths", "alchemy_count", "last_lab_attempt",
    "donated", "daily_progress", "pending_transfer", "lab_depth", "pet", "pet_name",
    "repent_count", "onboarding_step", "pet_hunger", "exists",
)


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

            columns = PLAYER_COLUMNS
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

        columns = PLAYER_COLUMNS
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
                columns = PLAYER_COLUMNS
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
