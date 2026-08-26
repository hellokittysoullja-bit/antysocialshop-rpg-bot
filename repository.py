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
    "user_id", "username", "balance", "total_earned", "blunts", "guild", "last_farm",
    "last_ritual", "last_repent", "last_daily", "titles", "last_farm_date", "passive_level",
    "passive_collected", "karma", "inhaled", "smoke_count", "farm_count",
    "craft_count", "ritual_count", "referral_count", "last_mines",
    "inventory", "invited_by", "profile_skins", "login_streak",
    "last_login_date", "streak_freezes", "oath", "keys", "check_count", "m_essence",
    "lab_chests", "lab_deaths", "alchemy_count", "last_lab_attempt",
    "donated", "daily_progress", "pending_transfer", "lab_depth", "pet", "pet_name",
    "repent_count", "onboarding_step", "pet_hunger", "exists", "prestige",
    "lab_best_oac", "mines_best_step", "smoke_heat",
)
# Не в PLAYER_COLUMNS намеренно: last_reengagement_sent, last_winback_sent,
# last_known_rank, mines_state, mines_state_updated_at. Все пять пишутся
# точечным UPDATE одной колонки в горячих путях (фоновая джоба, перебирающая
# много игроков за раз; клик по клетке в «Минах» на каждый тап) — грузить
# через них весь объект Player и гонять save() по ~50 колонкам ради одного
# поля было бы накладно и не даёт ничего, поскольку раздельная запись не
# требует согласованности с остальными полями игрока в той же транзакции.
# save() эти колонки просто не видит и не трогает — конфликта нет.


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
            # Помечаем частичную загрузку, чтобы save() не записал пустой
            # инвентарь поверх реальной коллекции игрока.
            player._inventory_loaded = bool(with_inventory)
            # Частично загруженного игрока в кэш НЕ кладём: приватный флаг не
            # переживает сериализацию, и следующий читатель принял бы пустой
            # инвентарь за настоящий — баг вернулся бы через кэш.
            if player._inventory_loaded:
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
        # Страховочный пол статуса: заработано не может быть меньше того, что
        # игрок держит в руках. Нужен для (а) бэкфилла старых игроков, (б) путей,
        # которые сохраняются напрямую через save() минуя atomic_update.
        if (player.total_earned or 0) < player.balance:
            player.total_earned = player.balance
        player.exists = True
        if conn and conn.is_closed():
            conn = None

        columns = PLAYER_COLUMNS
        json_cols = {"inventory", "profile_skins", "pending_transfer", "daily_progress"}
        # Колонки, которые этот объект не вправе перезаписывать: инвентарь, если
        # он не загружался (иначе пустой список затрёт коллекцию). Для новой
        # строки INSERT всё равно проставит дефолт — терять нечего.
        skip_update = set() if player._inventory_loaded else {"inventory"}
        cols_sql = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
        update_set = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in columns
                               if c != "user_id" and c not in skip_update)
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

                # Единая точка учёта заработка. Все 40+ начислений (фарм, тяга,
                # медали, ритуал, исповедь, плантация, квесты, колесо, лабиринт…)
                # идут через atomic_update, поэтому дельту достаточно поймать
                # здесь — не размазывая += по всему bot.py и не рискуя забыть
                # источник. Растёт только вверх: траты статус не отбирают.
                _bal_before = player.balance or 0
                result = await update_func(player, conn)
                _gain = (player.balance or 0) - _bal_before
                if _gain > 0:
                    player.total_earned = (player.total_earned or 0) + _gain
                await self.save(player, conn=conn)
                logger.info("Атомарное обновление для игрока %d успешно завершено", user_id)
                return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def atomic_pair_update(self, user_id_a: int, user_id_b: int, update_func):
        """Атомарно блокирует ДВУХ игроков в ОДНОЙ транзакции и передаёт их
        update_func(player_a, player_b, conn) для правки обоих сразу.

        Единственный способ безопасно передать предмет от одного игрока
        другому: раньше дарение читало обоих игроков через get_by_id (может
        отдать снимок из Redis-кэша с TTL 10с) и делало ДВА независимых save()
        — ни блокировки, ни общей транзакции. Крах между двумя save()
        удваивал или терял предмет; конкурентное действие любого из двоих
        (фарм, крафт — что угодно) в эти секунды откатывалось перезаписью
        устаревшего снимка. Здесь оба игрока блокируются SELECT…FOR UPDATE в
        ОДНОЙ транзакции, правки и оба save() — тоже в ней; крах откатывает
        всё, конкурентное чтение других запросов просто ждёт лока.

        Блокировка берётся в порядке возрастания user_id — детерминированно
        для ЛЮБОЙ пары, что исключает deadlock при встречных подарках A→B и
        B→A, идущих одновременно.
        """
        if not user_id_a or user_id_a <= 0 or not user_id_b or user_id_b <= 0:
            raise ValueError("Некорректный user_id при парном атомарном обновлении")

        lo, hi = sorted((user_id_a, user_id_b))
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                columns = PLAYER_COLUMNS
                cols_sql = ", ".join(f'"{c}"' for c in columns)
                loaded = {}
                for uid in (lo, hi):
                    row = await conn.fetchrow(
                        f"SELECT {cols_sql} FROM players WHERE user_id = $1 FOR UPDATE", uid)
                    if not row:
                        loaded[uid] = None
                        continue
                    p = dict(row)
                    p["inventory"] = _json_safe_load(p.get("inventory"), [])
                    p["profile_skins"] = _json_safe_load(p.get("profile_skins"), {})
                    p["pending_transfer"] = _json_safe_load(p.get("pending_transfer"), None)
                    p["daily_progress"] = _json_safe_load(p.get("daily_progress"), {})
                    loaded[uid] = Player(**p)

                player_a, player_b = loaded[user_id_a], loaded[user_id_b]
                result = await update_func(player_a, player_b, conn)
                for p in (player_a, player_b):
                    if p is not None:
                        await self.save(p, conn=conn)
                return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def atomic_enqueue_named_gift(self, from_uid: int, blunt_id: str,
                                        username_lower: str = None,
                                        target_user_id: int = None):
        """Атомарно переносит именной предмет из инвентаря в очередь подарков.

        Получатель может ещё не существовать в базе, поэтому обычный
        ``atomic_pair_update`` здесь неприменим. Критично, чтобы изъятие
        предмета у дарителя и INSERT в pending_gifts жили в ОДНОЙ транзакции:
        иначе ошибка сети/БД между двумя независимыми шагами навсегда теряла
        предмет игрока.
        """
        if not from_uid or from_uid <= 0:
            raise ValueError("Некорректный from_uid для отложенного подарка")
        if not blunt_id:
            raise ValueError("Пустой blunt_id для отложенного подарка")
        if not username_lower and not target_user_id:
            raise ValueError("Для отложенного подарка нужен username или user_id")

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                columns = PLAYER_COLUMNS
                cols_sql = ", ".join(f'"{c}"' for c in columns)
                row = await conn.fetchrow(
                    f"SELECT {cols_sql} FROM players WHERE user_id = $1 FOR UPDATE",
                    from_uid,
                )
                if not row:
                    return ("giver_missing", None)

                raw_player = dict(row)
                raw_player["inventory"] = _json_safe_load(raw_player.get("inventory"), [])
                raw_player["profile_skins"] = _json_safe_load(raw_player.get("profile_skins"), {})
                raw_player["pending_transfer"] = _json_safe_load(raw_player.get("pending_transfer"), None)
                raw_player["daily_progress"] = _json_safe_load(raw_player.get("daily_progress"), {})
                giver = Player(**raw_player)

                item = next(
                    (it for it in (giver.inventory or [])
                     if it.get("id") == blunt_id and it.get("type") == "named"),
                    None,
                )
                if item is None:
                    return ("not_owned", None)

                item = dict(item)
                giver.inventory = [it for it in giver.inventory if it.get("id") != blunt_id]
                await conn.execute(
                    "INSERT INTO pending_gifts (username_lower, target_user_id, item, from_user_id) "
                    "VALUES ($1, $2, $3, $4)",
                    username_lower, target_user_id,
                    json.dumps(item, separators=(",", ":"), default=str), from_uid,
                )
                await self.save(giver, conn=conn)
                return ("ok", item)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def atomic_claim_pending_gifts(self, user_id: int, username: str = "") -> list[dict]:
        """Атомарно зачисляет все ожидающие предметы и удаляет записи очереди.

        Строка игрока и найденные подарки блокируются в одной транзакции. Если
        сериализация инвентаря, его сохранение или DELETE очереди не проходят,
        транзакция откатывается целиком: подарок остаётся в pending_gifts, а не
        исчезает между ``DELETE`` и последующим ``save()``.
        """
        if not user_id or user_id <= 0:
            raise ValueError("Некорректный user_id при выдаче отложенных подарков")

        username_lower = (username or "").lower() or None
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                columns = PLAYER_COLUMNS
                cols_sql = ", ".join(f'"{c}"' for c in columns)
                row = await conn.fetchrow(
                    f"SELECT {cols_sql} FROM players WHERE user_id = $1 FOR UPDATE",
                    user_id,
                )
                if not row:
                    return []

                raw_player = dict(row)
                raw_player["inventory"] = _json_safe_load(raw_player.get("inventory"), [])
                raw_player["profile_skins"] = _json_safe_load(raw_player.get("profile_skins"), {})
                raw_player["pending_transfer"] = _json_safe_load(raw_player.get("pending_transfer"), None)
                raw_player["daily_progress"] = _json_safe_load(raw_player.get("daily_progress"), {})
                receiver = Player(**raw_player)

                rows = await conn.fetch(
                    "SELECT id, item FROM pending_gifts "
                    "WHERE target_user_id = $1 "
                    "OR (username_lower = $2 AND username_lower IS NOT NULL) "
                    "FOR UPDATE",
                    user_id,
                    username_lower,
                )
                if not rows:
                    return []

                items: list[dict] = []
                ids: list[int] = []
                for gift_row in rows:
                    item = _json_safe_load(gift_row["item"], {})
                    if isinstance(item, dict) and item:
                        items.append(item)
                        ids.append(gift_row["id"])
                    else:
                        logger.error("Повреждённый подарок id=%s оставлен в очереди для разбора", gift_row["id"])

                if not items:
                    return []

                receiver.inventory = list(receiver.inventory or []) + items
                await self.save(receiver, conn=conn)
                await conn.execute("DELETE FROM pending_gifts WHERE id = ANY($1::int[])", ids)
                return items

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def get_lab_run(self, user_id: int) -> dict | None:
        """Возвращает персистентный снимок активного забега Лабиринта."""
        if not user_id or user_id <= 0:
            raise ValueError("Некорректный user_id при чтении забега Лабиринта")
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT state FROM lab_runs WHERE user_id=$1", user_id)
        if not row:
            return None
        state = _json_safe_load(row["state"], None)
        return state if isinstance(state, dict) else None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def atomic_start_lab_run(self, user_id: int, state: dict, update_player):
        """Атомарно фиксирует списание попытки и создание нового забега.

        Если процесс упадёт между кулдауном и записью состояния, транзакция
        откатит оба шага. Повторное нажатие вместо второго забега возвращает
        существующий снимок.
        """
        if not user_id or user_id <= 0:
            raise ValueError("Некорректный user_id при старте Лабиринта")
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                columns = PLAYER_COLUMNS
                cols_sql = ", ".join(f'"{c}"' for c in columns)
                player_row = await conn.fetchrow(
                    f"SELECT {cols_sql} FROM players WHERE user_id=$1 FOR UPDATE", user_id)
                if not player_row:
                    return ("player_missing", None)

                existing = await conn.fetchrow(
                    "SELECT state FROM lab_runs WHERE user_id=$1 FOR UPDATE", user_id)
                if existing:
                    current = _json_safe_load(existing["state"], {})
                    return ("already_active", current if isinstance(current, dict) else {})

                raw_player = dict(player_row)
                raw_player["inventory"] = _json_safe_load(raw_player.get("inventory"), [])
                raw_player["profile_skins"] = _json_safe_load(raw_player.get("profile_skins"), {})
                raw_player["pending_transfer"] = _json_safe_load(raw_player.get("pending_transfer"), None)
                raw_player["daily_progress"] = _json_safe_load(raw_player.get("daily_progress"), {})
                player = Player(**raw_player)
                result = await update_player(player, conn)
                await self.save(player, conn=conn)
                await conn.execute(
                    "INSERT INTO lab_runs (user_id, state, started_at, updated_at) "
                    "VALUES ($1, $2::jsonb, NOW(), NOW())",
                    user_id, json.dumps(state, separators=(",", ":"), default=str),
                )
                return ("ok", result)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def atomic_mutate_lab_run(self, user_id: int, mutate):
        """Сериализует изменение активного забега и сохраняет новый снимок.

        Вызвавшая функция получает состояние только под блокировкой строки
        ``lab_runs``. Два параллельных тапа не могут применить одну комнату или
        один бонус дважды: второй увидит состояние после первого коммита.
        """
        if not user_id or user_id <= 0:
            raise ValueError("Некорректный user_id при изменении Лабиринта")
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT state FROM lab_runs WHERE user_id=$1 FOR UPDATE", user_id)
                if not row:
                    return None
                state = _json_safe_load(row["state"], {})
                if not isinstance(state, dict):
                    raise ValueError("Повреждённое состояние Лабиринта")
                result = await mutate(state, conn)
                await conn.execute(
                    "UPDATE lab_runs SET state=$2::jsonb, updated_at=NOW() WHERE user_id=$1",
                    user_id, json.dumps(state, separators=(",", ":"), default=str),
                )
                return result, state

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def atomic_finish_lab_run(self, user_id: int, finalize):
        """Атомарно применяет итог забега и удаляет его снимок.

        Выплата, достижения и удаление забега являются одним коммитом. Это
        исключает и потерю награды при рестарте, и повторную выплату от старой
        кнопки после уже завершённой попытки.
        """
        if not user_id or user_id <= 0:
            raise ValueError("Некорректный user_id при завершении Лабиринта")
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                columns = PLAYER_COLUMNS
                cols_sql = ", ".join(f'"{c}"' for c in columns)
                player_row = await conn.fetchrow(
                    f"SELECT {cols_sql} FROM players WHERE user_id=$1 FOR UPDATE", user_id)
                if not player_row:
                    return None
                run_row = await conn.fetchrow(
                    "SELECT state FROM lab_runs WHERE user_id=$1 FOR UPDATE", user_id)
                if not run_row:
                    return None

                raw_player = dict(player_row)
                raw_player["inventory"] = _json_safe_load(raw_player.get("inventory"), [])
                raw_player["profile_skins"] = _json_safe_load(raw_player.get("profile_skins"), {})
                raw_player["pending_transfer"] = _json_safe_load(raw_player.get("pending_transfer"), None)
                raw_player["daily_progress"] = _json_safe_load(raw_player.get("daily_progress"), {})
                player = Player(**raw_player)
                state = _json_safe_load(run_row["state"], {})
                if not isinstance(state, dict):
                    raise ValueError("Повреждённое состояние Лабиринта")

                # Та же дельта-логика, что в atomic_update (единая точка учёта
                # заработка) — здесь её не было: _lab_win/_lab_die начисляют
                # p.balance напрямую, а save() ниже не пересчитывает
                # total_earned сам. Без этой строки сундук/утешительный приз
                # Лабиринта пополнял бы кошелёк, но не двигал ранг/статус —
                # ровно то расхождение «кошелёк ≠ статус», которое уже чинили
                # в Минах и Храме (см. atomic_update).
                _bal_before = player.balance or 0
                result = await finalize(player, state, conn)
                # None означает, что состояние ещё не готово к завершению.
                # В этом случае не меняем игрока и не удаляем снимок забега.
                if result is None:
                    return None
                _gain = (player.balance or 0) - _bal_before
                if _gain > 0:
                    player.total_earned = (player.total_earned or 0) + _gain
                await self.save(player, conn=conn)
                await conn.execute("DELETE FROM lab_runs WHERE user_id=$1", user_id)
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
