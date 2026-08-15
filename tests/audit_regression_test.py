"""Регресс-тесты по находкам аудита SLAYER.

Каждый кейс воспроизводил РЕАЛЬНЫЙ дефект прод-кода и теперь удерживает его
закрытым. Тесты гоняют настоящие функции bot.py/services.py — БД и Telegram
заменены минимальными двойниками, сами проверяемые функции не мокаются.

Запуск:  python tests/audit_regression_test.py
"""
import os
import sys
import types
import asyncio
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TOKEN", "123:DUMMY")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://u:p@127.0.0.1:5432/db")
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("RENDER_URL", "")
os.environ.setdefault("REDIS_URL", "")

logging.disable(logging.CRITICAL)

import bot
from bot import Player

PASSED = 0
FAILED = []


def check(cond, msg):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  OK  {msg}")
    else:
        FAILED.append(msg)
        print(f"  FAIL {msg}")


# ── Минимальные двойники Telegram / БД ───────────────────────────────
class FakeMsg:
    def __init__(self):
        self.text = "x"
        self.message_id = 1
        self.chat = types.SimpleNamespace(id=1)
        self.edits = []
        self.edit_calls = []   # (text, kwargs) — для тестов, которым нужен reply_markup

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        self.edit_calls.append((text, kw))
        return self

    async def reply_text(self, text, **kw):
        self.edits.append(text)
        self.edit_calls.append((text, kw))
        return self

    async def delete(self):
        pass


class FakeQuery:
    def __init__(self, data, uid=1):
        self.data = data
        self.message = FakeMsg()
        self.from_user = types.SimpleNamespace(id=uid, username="strannik", first_name="s")
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


class FakeUpdate:
    _next_id = 1000

    def __init__(self, data, uid=1):
        FakeUpdate._next_id += 1
        self.update_id = FakeUpdate._next_id
        self.callback_query = FakeQuery(data, uid)
        self.effective_user = self.callback_query.from_user
        self.effective_chat = types.SimpleNamespace(id=uid)
        self.effective_message = self.callback_query.message
        self.message = None


class FakeBot:
    def __init__(self):
        self.sent = []
        self.sent_calls = []   # (text, kwargs) — для тестов, которым нужен reply_markup
        self.photos_sent = []
        self.media_edits = []

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append(text)
        self.sent_calls.append((text, kw))
        return FakeMsg()

    async def edit_message_text(self, **kw):
        return FakeMsg()

    async def send_photo(self, chat_id=None, photo=None, **kw):
        self.photos_sent.append((chat_id, kw.get("caption")))
        m = FakeMsg()
        m.photo = [types.SimpleNamespace(file_id="new_photo_file_id")]
        return m

    async def edit_message_media(self, chat_id=None, message_id=None, media=None, **kw):
        self.media_edits.append((chat_id, message_id, getattr(media, "caption", None)))
        m = FakeMsg()
        m.photo = [types.SimpleNamespace(file_id="edited_photo_file_id")]
        return m

    async def get_me(self):
        return types.SimpleNamespace(username="testbot")

    async def get_user_profile_photos(self, uid, limit=1):
        return types.SimpleNamespace(photos=[])


class FakeContext:
    def __init__(self, ctx):
        self.bot_data = {"ctx": ctx}
        self.user_data = {}
        self.bot = FakeBot()
        self.application = types.SimpleNamespace(bot_data=self.bot_data)


class FakeConn:
    def __init__(self, rows=None, value=0):
        self._rows = rows or []
        self._value = value

    async def fetch(self, *a, **k):
        return self._rows

    async def fetchval(self, *a, **k):
        return self._value

    async def fetchrow(self, *a, **k):
        return self._rows[0] if self._rows else None

    async def execute(self, *a, **k):
        return None

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False
        return _Tx()


class FakePool:
    def __init__(self, rows=None, value=0):
        self._rows = rows
        self._value = value

    def acquire(self, *a, **k):
        rows, value = self._rows, self._value

        class _CM:
            async def __aenter__(self):
                return FakeConn(rows, value)

            async def __aexit__(self, *exc):
                return False

        return _CM()


class FakeRepo:
    """Повторяет ключевую семантику repository.atomic_update: дельта баланса
    капает в total_earned, обновление применяется к одному объекту игрока."""

    def __init__(self, player):
        self.p = player
        self.saves = 0

    async def get_by_id(self, uid, with_inventory=True):
        return self.p

    async def save(self, player, conn=None):
        self.saves += 1
        self.p = player

    async def atomic_update(self, uid, fn):
        before = self.p.balance or 0
        r = await fn(self.p, None)
        gain = (self.p.balance or 0) - before
        if gain > 0:
            self.p.total_earned = (self.p.total_earned or 0) + gain
        return r


class FakeMultiRepo:
    """Как FakeRepo, но держит НЕСКОЛЬКИХ игроков по uid — нужен для сценариев
    с двумя реальными сторонами (реферер + приглашённый), где однопользовательский
    FakeRepo не может различить, кого из двоих правят."""

    def __init__(self, players):
        self.players = {p.user_id: p for p in players}
        self.saves = 0

    async def get_by_id(self, uid, with_inventory=True):
        # Реальный repository.get_by_id ставит exists=True при найденной строке
        # (repository.py:106) — без этого повторный get_by_id внутри одного
        # запроса (напр. create_named_blunt перечитывает игрока) считает только
        # что созданного игрока несуществующим и подменяет его пустым Player().
        p = self.players.get(uid)
        if p is None:
            return Player(user_id=uid)
        p.exists = True
        return p

    async def save(self, player, conn=None):
        self.saves += 1
        self.players[player.user_id] = player

    async def atomic_update(self, uid, fn):
        p = self.players.get(uid)
        if p is None:
            return None
        before = p.balance or 0
        r = await fn(p, None)
        gain = (p.balance or 0) - before
        if gain > 0:
            p.total_earned = (p.total_earned or 0) + gain
        return r


class FakeMinesPool:
    """Мини-БД в памяти только под players.mines_state/mines_state_updated_at.

    _mines_state_get/_mines_state_set после переноса с Redis (Redis
    подтверждённо был недоступен в проде несколько дней подряд — «Мины»
    молчали ВСЕГДА, а не деградировали) делают настоящий SQL round-trip
    через ctx.db_pool. FakePool отдаёт фиксированные rows/value и не умеет
    «запомнить, что записали» — здесь маленький честный key-value поверх
    двух SQL-форм, которые реально шлёт код, без общего SQL-движка.
    """
    def __init__(self):
        self._store = {}   # uid -> (state_json, updated_at)

    def acquire(self, *a, **k):
        store = self._store

        class _Conn:
            async def execute(self, sql, *args):
                if "mines_state" in sql and "UPDATE" in sql:
                    state_json, uid = args
                    store[uid] = (state_json, datetime.now(timezone.utc))

            async def fetchrow(self, sql, *args):
                if "mines_state" not in sql:
                    return None
                uid = args[0]
                if uid not in store:
                    return None
                state_json, updated_at = store[uid]
                return {"mines_state": state_json, "mines_state_updated_at": updated_at}

        class _CM:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *exc):
                return False

        return _CM()


class FakeWar:
    def __init__(self):
        self.scores = []

    async def add_score(self, uid, action, conn=None):
        self.scores.append(action)

    async def add_score_raw(self, uid, points, conn=None):
        self.scores.append(points)


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def setex(self, k, ttl, v):
        self.store[k] = v

    async def set(self, k, v):
        self.store[k] = v

    async def get(self, k):
        return self.store.get(k)

    async def delete(self, k):
        self.store.pop(k, None)


def make_ctx(player, redis=None, pool=None, war=None, repo=None):
    return bot.AppContext(
        db_pool=pool or FakePool(), redis_client=redis, cache={},
        settings=bot.settings, repo=repo or FakeRepo(player),
        war_service=war or FakeWar(), pet_service=None, achievement_service=None)


# ── 1. Лабиринт: вход не падает при подключённом Redis ───────────────
async def test_lab_entry_survives_redis():
    p = Player(user_id=1, exists=True, balance=1000, total_earned=1000, lab_depth=1)
    ctx = make_ctx(p, redis=FakeRedis())
    u, c = FakeUpdate("lab_enter_confirm"), FakeContext(ctx)
    await bot.lab_enter_confirm(u, c)      # раньше: AttributeError на модульном redis=None
    check(c.user_data.get("lab_room") == 1,
          "вход в Лабиринт при подключённом Redis доходит до первой комнаты")
    # Снапшот забега в ctx.redis.set(f"lab_state:{uid}", ...) удалён целиком:
    # писался и НИКОГДА не читался обратно ни на восстановление после
    # рестарта, ни где-либо ещё — мёртвая страховка, которая ничего не
    # страховала. Проверяем, что она не вернулась случайно при правках рядом.
    check(not ctx.redis.store,
          "вход в Лабиринт больше не пишет мёртвый снапшот в Redis — "
          "состояние только в context.user_data")


# ── 2. Лабиринт: забег конечен (нет бесконечного фарма OAC) ──────────
async def test_lab_run_is_bounded():
    p = Player(user_id=1, exists=True, balance=0, total_earned=0, lab_depth=1)
    ctx = make_ctx(p)
    c = FakeContext(ctx)
    c.user_data.update({
        "lab_hp": 100, "lab_max_hp": 100, "lab_focus": 0, "lab_room": 1,
        "lab_total_rooms": 5, "lab_rewards": [],
        "lab_current_room": bot.LABYRINTH_ROOMS[3],
        "lab_msg_id": 1, "lab_chat_id": 1,
    })
    # «Бежать» лечит и раньше не тратил комнату → цикл был бесконечным.
    for _ in range(4):
        c.user_data["lab_current_room"] = bot.LABYRINTH_ROOMS[3]
        await bot.handle_lab_option(FakeUpdate("lab_escape"), c)
    check(c.user_data["lab_room"] == 5,
          "«Бежать» расходует комнату — счётчик дошёл 1→5 за 4 нажатия")

    c2 = FakeContext(ctx)
    c2.user_data.update({
        "lab_hp": 100, "lab_max_hp": 100, "lab_focus": 0, "lab_room": 1,
        "lab_total_rooms": 5, "lab_rewards": [],
        "lab_current_room": bot.LABYRINTH_ROOMS[3],
        "lab_msg_id": 1, "lab_chat_id": 1,
    })
    for _ in range(4):
        c2.user_data["lab_current_room"] = bot.LABYRINTH_ROOMS[3]
        await bot.handle_lab_option(FakeUpdate("lab_special"), c2)
    check(c2.user_data["lab_room"] == 5,
          "спец-действие расходует комнату — бесконечная добыча OAC закрыта")


# ── 2b. Лабиринт: комната не подменяется под игроком (M9) ────────────
async def test_lab_room_stable_across_non_advancing_actions():
    """show_lab_room раньше перевыбирала комнату (random.choice) на КАЖДЫЙ
    рендер, а не на каждый room_index. «Сконцентрироваться» тратит Фокус на
    бонус к следующей атаке и НЕ продвигает room_index (комната не покинута)
    — но экран после неё показывал уже другую комнату с другими шансами и
    наградой, чем ту, для которой игрок только что принял тактическое
    решение. Единственная механика в игре с настоящим риск-менеджментом
    (выбор тира атаки, трата HP/Фокуса) на ощущение играла как лотерея."""
    p = Player(user_id=1, exists=True, balance=0, total_earned=0, lab_depth=1)
    ctx = make_ctx(p)
    c = FakeContext(ctx)
    c.user_data.update({
        "lab_hp": 100, "lab_max_hp": 100, "lab_focus": 3, "lab_room": 1,
        "lab_total_rooms": 5, "lab_rewards": [], "lab_depth": 1,
        "lab_msg_id": 1, "lab_chat_id": 1,
    })
    await bot.show_lab_room(FakeUpdate("lab_noop", uid=1), c)
    room_before = c.user_data["lab_current_room"]["name"]

    await bot.handle_lab_option(FakeUpdate("lab_focus_use", uid=1), c)
    check(c.user_data["lab_room"] == 1,
          "«Сконцентрироваться» не продвигает счётчик комнат")
    check(c.user_data["lab_current_room"]["name"] == room_before,
          "и не подменяет саму комнату — тактическое решение остаётся в силе")

    # Повторный рендер БЕЗ продвижения (тот же room_index) — тоже должен
    # держать ту же комнату, не только сразу после конкретного действия.
    await bot.show_lab_room(FakeUpdate("lab_noop", uid=1), c)
    check(c.user_data["lab_current_room"]["name"] == room_before,
          "и повторный рендер той же комнаты её не перевыбирает")

    # Реальное продвижение (атака) — комната ДОЛЖНА смениться на новую.
    await bot.handle_lab_option(FakeUpdate("lab_attack_0", uid=1), c)
    check(c.user_data["lab_room"] == 2,
          "атака низшего тира продвигает забег на следующую комнату")


# ── 3. Мины: кэшаут платит один раз и не сжигает партию впустую ──────
async def test_mines_cashout():
    p = Player(user_id=1, exists=True, balance=0, total_earned=0)
    ctx = make_ctx(p, redis=FakeRedis(), pool=FakeMinesPool())
    state = {"field": [[0] * 5 for _ in range(5)], "mines": [[0, 0]], "bet": 100,
             "step": 3, "multiplier": 1.27, "status": "playing", "created_at": 0}
    await bot._mines_state_set(ctx, 1, state)

    await bot._mines_cashout_wrapper(FakeUpdate("mines_cashout"), FakeContext(ctx))
    first = p.balance
    check(first == 127, f"кэшаут начислил ставку×множитель ({first} OAC)")

    await bot._mines_cashout_wrapper(FakeUpdate("mines_cashout"), FakeContext(ctx))
    check(p.balance == first,
          "повторный тап «Забрать» НЕ платит второй раз за ту же ставку")

    # Кэшаут без единой открытой клетки не должен убивать партию.
    p2 = Player(user_id=2, exists=True, balance=0, total_earned=0)
    ctx2 = make_ctx(p2, redis=FakeRedis(), pool=FakeMinesPool())
    st = {"field": [[0] * 5 for _ in range(5)], "mines": [[0, 0]], "bet": 100,
          "step": 0, "multiplier": 1.0, "status": "playing", "created_at": 0}
    await bot._mines_state_set(ctx2, 2, st)
    await bot._mines_cashout_wrapper(FakeUpdate("mines_cashout", uid=2), FakeContext(ctx2))
    after = await bot._mines_state_get(ctx2, 2)
    check(after["status"] == "playing" and p2.balance == 0,
          "«Забрать» с нулём открытых клеток не сжигает партию — она продолжается")


# ── 3b. Мины переживают сериализацию состояния (JSONB в Postgres) ────
# Redis подтверждённо был недоступен в проде несколько дней подряд — «Мины»
# при старом «Redis, если есть, иначе in-memory кэш» без Redis теряли партию
# на любом рестарте процесса точно так же, как без всякого фолбэка. Состояние
# перенесено на players.mines_state (Postgres) — тест теперь бьёт по этому
# пути напрямую, не по Redis, который код с сегодняшнего дня не трогает.
async def test_mines_survive_redis_roundtrip():
    p = Player(user_id=3, exists=True, balance=1000, total_earned=1000)
    ctx = make_ctx(p, redis=FakeRedis(), pool=FakeMinesPool())
    c = FakeContext(ctx)

    await bot._mines_start_game(FakeUpdate("mines_bet_100", uid=3), c, 3, 100, ctx)
    state = await bot._mines_state_get(ctx, 3)
    check(state is not None and isinstance(state["mines"][0], list),
          "состояние партии прошло через JSON (координаты стали списками)")
    check(bot._mines_positions(state) == {tuple(m) for m in state["mines"]},
          "_mines_positions нормализует координаты обратно в кортежи")

    # Ищем заведомо безопасную клетку и открываем её — раньше здесь падал
    # TypeError: unhashable type 'list', и «Мины» были неиграбельны с Redis.
    # Тот же контракт JSON→кортежи обязан держаться и на Postgres-хранилище.
    mines = bot._mines_positions(state)
    safe = next((r, col) for r in range(5) for col in range(5) if (r, col) not in mines)
    u = FakeUpdate(f"mines_open_{safe[0]}_{safe[1]}", uid=3)
    await bot._mines_open_cell_wrapper(u, c)
    after = await bot._mines_state_get(ctx, 3)
    check(after["step"] == 1 and after["status"] == "playing",
          "клетка открывается корректно (шаг засчитан)")


# ── 4. Гильдия: кнопка не создаёт профиль в обход /start ─────────────
async def test_guild_join_requires_start():
    ghost = Player(user_id=9)              # exists=False — человек без /start
    ctx = make_ctx(ghost)
    u, c = FakeUpdate("guild_join_BLACK", uid=9), FakeContext(ctx)
    await bot.guild_join_handler(u, c)
    check(ctx.repo.saves == 0 and ghost.guild is None,
          "вступление без /start отклонено — «призрачный» профиль не создаётся")
    check(any(a and "/start" in a for a in u.callback_query.answers),
          "игроку сказано начать со /start, а не молчание")


# ── 5. Алтарь: экран перерисовывается после жертвы ───────────────────
async def test_altar_rerenders():
    p = Player(user_id=1, exists=True, balance=10000, total_earned=60000, prestige=0)
    ctx = make_ctx(p, pool=FakePool(rows=[], value=0))
    u, c = FakeUpdate("altar_invest_all"), FakeContext(ctx)
    await bot.altar_invest_handler(u, c)
    check(p.prestige == 10000 and p.balance == 0, "жертва списана и обращена в престиж")
    check(any("АЛТАРЬ" in t for t in u.callback_query.message.edits),
          "после жертвы экран Алтаря перерисован (не залипает на старых цифрах)")
    last = [t for t in u.callback_query.message.edits if "АЛТАРЬ" in t][-1]
    # "10 000", не "10000" — крупные числа теперь идут через _fmt_oac
    # (разделитель разрядов, см. полировку читаемости Алтаря).
    check("10 000 OAC" in last, "перерисовка показывает СВЕЖИЙ престиж, а не прежний")


# ── 6. Отмена именного бланта не падает ──────────────────────────────
async def test_cancel_named():
    p = Player(user_id=1, exists=True, balance=100, total_earned=100)
    ctx = make_ctx(p)
    u, c = FakeUpdate("cancel_named"), FakeContext(ctx)
    await bot.cancel_named(u, c)           # раньше: NameError: craft_callback
    check(any("КРАФТ" in t.upper() for t in u.callback_query.message.edits),
          "«❌ Отмена» возвращает в меню крафта, а не в NameError")


# ── 7. Война гильдий: очки капают за КАЖДОЕ действие ─────────────────
async def test_war_score_per_action():
    from services import GuildWarService, WarConfig, WarSettings, WarAction

    class R:
        async def setnx(self, k, v):
            return True

        async def expire(self, *a):
            pass

    svc = GuildWarService(None, R(), WarConfig(), WarSettings())
    got = []

    async def fake(uid, pts, action, conn=None):
        got.append(pts)

    svc._add_score_retry = fake
    for _ in range(25):
        await svc.add_score(1, WarAction.CRAFT)
    check(len(got) == 25 and sum(got) == 250,
          f"25 крафтов → 25 начислений на {sum(got)} очков (было: 1 за всю неделю)")


# ── 8. Храм: обещанный бонус реально применяется к фарму ─────────────
async def test_temple_bonus_applies():
    check(bot._temple_tier(0)["bonus"] == 0, "храм ур.1 — бонуса нет")
    check(bot._temple_tier(45000)["bonus"] == 10, "храм ур.3 (45k) — +10%")
    check(bot._temple_tier(999999)["bonus"] == 25, "храм ур.5 — потолок +25%")

    p = Player(user_id=1, exists=True, smoke_count=0, guild="BLACK")
    c = FakeContext(make_ctx(p))
    base = [bot._calculate_farm_reward(p, c, 0)[0] for _ in range(4000)]
    with_temple = [bot._calculate_farm_reward(p, c, 25)[0] for _ in range(4000)]
    ratio = (sum(with_temple) / len(with_temple)) / (sum(base) / len(base))
    check(1.18 < ratio < 1.32,
          f"+25% Храма реально повышают добычу (наблюдаемое ×{ratio:.2f})")

    shown = bot._format_farm_message(100, False, False, "", 1, 10, 500, 500, 25)
    check("+25%" in shown, "карточка фарма показывает вклад Храма (сток стал видимым)")


# ── 8b. Донат квеста: цель накопительная (M15) ────────────────────────
async def test_donate_quest_progress_is_cumulative():
    """Задание «Пожертвовать» Главы 2 объявляет target=200 OAC. Раньше ЛЮБОЙ
    донат (даже минимальные 100) закрывал шаг сразу — заявленная цель была
    декоративной. Теперь прогресс копится за цикл и сверяется с target."""
    today = datetime.now().date().isoformat()
    p = Player(user_id=1, exists=True, balance=1000, total_earned=1000, guild="BLACK",
               daily_progress={"reset_date": today, "quest_id": "chapter2"})
    ctx = make_ctx(p)

    await bot.shrine_donate_handler(FakeUpdate("shrine_donate_100", uid=1), FakeContext(ctx))
    check(p.daily_progress.get("donate_amount") == 100, "первый донат накапливается (100/200)")
    check(p.daily_progress.get("donate") is False,
          "донат 100 из 200 НЕ закрывает задание (было: закрывал любой донат)")

    await bot.shrine_donate_handler(FakeUpdate("shrine_donate_100", uid=1), FakeContext(ctx))
    check(p.daily_progress.get("donate_amount") == 200, "второй донат доводит сумму до цели")
    check(p.daily_progress.get("donate") is True, "достижение target=200 закрывает задание")

    # Ветка отказа возвращает кортеж короче ("no_money",) — ловим регрессию
    # на unpacking (status, cycle_donated, target = result) без проверки
    # длины, которая упала бы ValueError на бедном игроке вместо алерта.
    poor = Player(user_id=2, exists=True, balance=0, total_earned=0, guild="BLACK",
                  daily_progress={"reset_date": today, "quest_id": "chapter2"})
    ctx2 = make_ctx(poor)
    u2 = FakeUpdate("shrine_donate_100", uid=2)
    await bot.shrine_donate_handler(u2, FakeContext(ctx2))
    check(u2.callback_query.answers and "Недостаточно" in u2.callback_query.answers[0],
          "донат без денег даёт алерт, а не падает на unpacking результата")


# ── 9. Именной блант: имя в БД совпадает с показанным ────────────────
async def test_named_blunt_name_persisted():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bot.py"), encoding="utf-8").read()
    body = src[src.index("async def handle_named_name"):src.index("async def handle_use_dust")]
    mutate_at = body.index("meme_name = mutate_name(")
    create_at = body.index("item = await create_named_blunt(")
    check(mutate_at < create_at,
          "имя мутируется ДО создания предмета — в БД ложится ровно то, что видит игрок")
    check("create_named_blunt(uid, meme_name" in body,
          "в инвентарь уходит показанное имя, а не исходный ввод")
    check('item["original_name"] = original_name' in body,
          "исходный ввод игрока сохраняется рядом (его показывают в деталях)")

    # Автограф-фрейминг должен стоять в обоих местах (бесплатный ре-нейминг
    # и платный крафт) — извлекается ДО того, как index() ниже сдвинет body.
    free_branch = body[:body.index("=== ГЕНЕРАЦИЯ ИМЕНИ")]
    check("автограф" in free_branch and "автограф" in body[body.index("=== ГЕНЕРАЦИЯ ИМЕНИ"):],
          "оба момента (бесплатный и платный именной блант) используют autograph-фрейминг (extended self)")

    # SLAYER Red Team (A₈) поймал реальный баг: гарантированный CTA «Первый
    # друг» раньше стоял в ветке переименования БЕЗЫМЯННОГО named-item —
    # но create_named_blunt нигде в файле не вызывается с пустым именем
    # (стартовый блант получает имя сразу при регистрации), так что та
    # ветка мертва для любого реального игрока, и CTA никогда никому не
    # показывался. Теперь CTA — в onboarding_reward, единственном по-настоящему
    # гарантированном (onboarding_step необратимо 2→-1) экране.
    check('callback_data="invite_friend"' not in free_branch,
          "мёртвая ветка переименования больше не несёт CTA, который никогда не показывался")


# ── 9b. Легендарка за Пыль: тоже получает кнопку «Поделиться» ────────
async def test_dust_legendary_has_share_button():
    """_win_share_button уже стоит на ранг-апе, джекпоте «Дунуть» и рекорде
    Лабиринта — три личных триумфа, транслируемых в гильд-чат АНОНИМНО для
    чужих, но без единой кнопки поделиться СВОЕЙ победой у самого игрока за
    пределами официального чата (см. докстринг _win_share_button). Легендарка
    за Кристальную Пыль — тот же класс победы (гарантированная легендарка),
    но кнопки у неё не было вообще — только «🔙 В меню». Статическая проверка
    (не поведенческая): create_named_blunt внутри требует полноценной БД-
    транзакции, которую FakeRepo/FakePool не воспроизводят бесплатно."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bot.py"), encoding="utf-8").read()
    start = src.index("async def handle_use_dust")
    end = src.index("async def _clear_named_blunt_state_after")
    body = src[start:end]
    check("_win_share_button(" in body,
          "handle_use_dust предлагает поделиться победой, как и остальные личные триумфы")


# ── 30. Гарантированный CTA «Первый друг» теперь на РЕАЛЬНО гарантированном
#        экране (onboarding_reward), не на мёртвой ветке ────────────────────
async def test_onboarding_reward_has_reachable_referral_cta():
    """onboarding_step необратимо переходит 2→-1 непосредственно перед этим
    экраном (craft_normal_v2), и путь farm→craft_normal→reward — обязательный
    маршрут онбординга, не опциональный. В отличие от прежнего места (ветка
    переименования безымянного бланта в handle_named_name, которая никогда
    не выполнялась ни для одного реального игрока), этот экран действительно
    гарантирован."""
    def _last_markup(upd):
        calls = upd.callback_query.message.edit_calls
        return calls[-1][1].get("reply_markup") if calls else None

    def _has_invite_button(markup):
        if not markup:
            return False
        return any(getattr(btn, "callback_data", None) == "invite_friend"
                   for row in markup.inline_keyboard for btn in row)

    p1 = Player(user_id=1, exists=True, balance=100, total_earned=100,
                referral_count=0, daily_progress={})
    ctx1 = make_ctx(p1)
    upd1 = FakeUpdate("onboarding_reward", uid=1)
    await bot.onboarding_reward(upd1, FakeContext(ctx1))
    check(_has_invite_button(_last_markup(upd1)),
          "игрок БЕЗ рефералов видит гарантированный CTA на реально гарантированном экране")

    p2 = Player(user_id=2, exists=True, balance=100, total_earned=100,
                referral_count=3, daily_progress={})
    ctx2 = make_ctx(p2)
    upd2 = FakeUpdate("onboarding_reward", uid=2)
    await bot.onboarding_reward(upd2, FakeContext(ctx2))
    check(not _has_invite_button(_last_markup(upd2)),
          "игрок, который УЖЕ приглашал, не видит CTA повторно (не нагрузка, а разовый крючок)")


# ── 31. Реферальная награда: закрыт эксплойт мгновенного фарма ────────
async def test_referral_registration_defers_full_reward():
    """SLAYER Red Team (A₂ Behavioral Econ/Ethics, E3/E5): раньше реферер получал
    +50 OAC, легендарный блант и метку МГНОВЕННО на голый /start приглашённого
    по ссылке — фармабельно пустыми аккаунтами, ни разу не открывшими игру:
    создал N аккаунтов, тапнул ссылку N раз, собрал N легендарок без единого
    реального игрока на том конце. Регистрация теперь только линкует
    invited_by и шлёт лёгкое «ожидай» уведомление; полная награда проверяется
    отдельными тестами ниже, на реальном завершении обучения приглашённым."""
    referrer = Player(user_id=555, exists=True, username="referrer",
                       balance=500, total_earned=500, referral_count=0)
    repo = FakeMultiRepo([referrer])
    ctx = make_ctx(referrer, repo=repo)
    upd = FakeUpdate("start", uid=777)
    fctx = FakeContext(ctx)
    fctx.args = ["ref_555"]
    await bot.start(upd, fctx)

    new_player = repo.players.get(777)
    check(new_player is not None and new_player.invited_by == 555,
          "приглашённый корректно связан с реферером (invited_by)")
    check(referrer.balance == 500 and referrer.referral_count == 0,
          "реферер НЕ получает награду мгновенно на голый /start приглашённого")
    check(any(t and "зашёл" in t and "Награда откроется" in t for t in fctx.bot.sent),
          "реферер получает лёгкое уведомление-ожидание вместо мгновенной награды")


async def test_referral_reward_fires_on_onboarding_completion():
    """Реальная награда (_reward_referrer) срабатывает, когда приглашённый
    ЗАВЕРШИЛ обучение (последний крафт, onboarding_step 2→-1) — реальное
    игровое действие, не факт регистрации. Органический игрок (без invited_by)
    не должен вызывать _reward_referrer вовсе."""
    calls = []
    async def _fake_reward(ctx, context, creator_id, new_username=None):
        calls.append(creator_id)
    orig = bot._reward_referrer
    bot._reward_referrer = _fake_reward
    try:
        p = Player(user_id=10, exists=True, balance=100, total_earned=100,
                   blunts=0, craft_count=0, onboarding_step=2, invited_by=555,
                   daily_progress={}, username="friend")
        repo = FakeMultiRepo([p])
        ctx = make_ctx(p, repo=repo)
        upd = FakeUpdate("craft_normal", uid=10)
        await bot.handle_craft_normal_v2(upd, FakeContext(ctx))
        check(calls == [555],
              "завершение обучения приглашённым (последний крафт) запускает награду рефереру")

        calls.clear()
        p2 = Player(user_id=11, exists=True, balance=100, total_earned=100,
                    blunts=0, craft_count=0, onboarding_step=2, invited_by=None,
                    daily_progress={}, username="organic")
        repo2 = FakeMultiRepo([p2])
        ctx2 = make_ctx(p2, repo=repo2)
        upd2 = FakeUpdate("craft_normal", uid=11)
        await bot.handle_craft_normal_v2(upd2, FakeContext(ctx2))
        check(calls == [],
              "органический игрок (без invited_by) не запускает несуществующую награду")
    finally:
        bot._reward_referrer = orig


async def test_skip_onboarding_referral_reward_guarded_against_double_fire():
    """«Пропустить обучение» — тоже реальное действие внутри игры (не голый
    /start), так что тоже открывает награду рефереру. _was_active защищает от
    повторного начисления, если кнопка нажата ещё раз после того, как
    обучение уже завершено (в частности — уже награждённый прошлым нажатием)."""
    calls = []
    async def _fake_reward(ctx, context, creator_id, new_username=None):
        calls.append(creator_id)
    async def _fake_menu(player, ctx, context, full_mode=False):
        return "stub", None
    orig_reward = bot._reward_referrer
    orig_menu = bot.build_main_menu
    bot._reward_referrer = _fake_reward
    bot.build_main_menu = _fake_menu
    try:
        p = Player(user_id=20, exists=True, balance=100, total_earned=100,
                   onboarding_step=1, invited_by=555, daily_progress={},
                   username="skipper")
        repo = FakeMultiRepo([p])
        ctx = make_ctx(p, repo=repo)

        await bot.skip_onboarding_handler(FakeUpdate("skip_onboarding", uid=20), FakeContext(ctx))
        check(calls == [555],
              "«Пропустить обучение» тоже засчитывается как реальное завершение — награда рефереру приходит")

        await bot.skip_onboarding_handler(FakeUpdate("skip_onboarding", uid=20), FakeContext(ctx))
        check(calls == [555],
              "повторное нажатие «Пропустить» после уже завершённого онбординга не начисляет награду повторно")
    finally:
        bot._reward_referrer = orig_reward
        bot.build_main_menu = orig_menu


# ── 11. Добавление бота в чат — больше не немой момент ───────────────
async def test_bot_added_to_chat_announces():
    """welcome_new_member раньше пропускал ЛЮБОГО бота в new_chat_members
    (`if member.is_bot: continue`), включая случай, когда новый участник —
    это МЫ САМИ. Итог: человек добавляет бота в чат, полный живых людей
    (ровно то, что предлагает кнопка «Добавить бота в свой чат»
    в invite_friend_handler) — и ровно ничего не происходит, ни одного
    сообщения. Теперь: добавление СЕБЯ — анонс с CTA-ссылкой на ЛС;
    добавление ЧУЖОГО бота — по-прежнему тишина (не наше дело болтать
    про чужих ботов)."""
    class FakeMember:
        def __init__(self, uid, is_bot, username="human"):
            self.id = uid
            self.is_bot = is_bot
            self.username = username
            self.first_name = "Human"

    class FakeGroupMsg:
        def __init__(self, members):
            self.new_chat_members = members
            self.chat = types.SimpleNamespace(id=-100777)
            self.edits = []

        async def reply_text(self, text, **kw):
            self.edits.append((text, kw))
            return self

    class FakeGroupUpdate:
        def __init__(self, members):
            self.message = FakeGroupMsg(members)
            self.effective_message = self.message

    OUR_BOT_ID = 999999

    async def fake_get_me():
        return types.SimpleNamespace(username="testbot", id=OUR_BOT_ID)

    # Случай 1: нас самих добавили в чат, полный живых людей.
    fctx = FakeContext(None)
    fctx.bot.get_me = fake_get_me
    upd = FakeGroupUpdate([FakeMember(OUR_BOT_ID, True, "testbot")])
    await bot.welcome_new_member(upd, fctx)
    check(len(upd.message.edits) == 1,
          "добавление бота в чат больше не немое — приходит ровно одно сообщение")
    if upd.message.edits:
        text, kw = upd.message.edits[0]
        kb = kw.get("reply_markup")
        url = kb.inline_keyboard[0][0].url if kb else None
        check(bool(url) and "?start=" in url,
              "анонс несёт url-кнопку с диплинком в ЛС (не callback — игрока в БД ещё нет)")

    # Случай 2: в чат добавили ЧУЖОГО бота — не наше дело, молчим как раньше.
    fctx2 = FakeContext(None)
    fctx2.bot.get_me = fake_get_me
    upd2 = FakeGroupUpdate([FakeMember(555555, True, "someotherbot")])
    await bot.welcome_new_member(upd2, fctx2)
    check(len(upd2.message.edits) == 0,
          "добавление ЧУЖОГО бота по-прежнему не порождает анонса от нас")

    # Случай 3: в чат зашёл ЖИВОЙ человек, который НИ РАЗУ не открывал ЛС с
    # ботом (самый частый случай group-discovery — не личная реф-ссылка).
    # get_by_id для такого возвращает не None, а пустой Player(exists=False)
    # — `if player:` раньше был True в любом случае, поэтому этот человек
    # получал те же callback-кнопки «Вступить в гильдию», что и уже
    # зарегистрированный игрок без гильдии. Тап по ним бил в
    # guild_join_handler → "Сначала активируйся: нажми /start" и на этом всё:
    # эфемерный alert без единой кнопки, а /start нужно было печатать руками.
    never_registered = Player(user_id=777, exists=False)
    ctx3 = make_ctx(never_registered)
    fctx3 = FakeContext(ctx3)
    fctx3.bot.get_me = fake_get_me
    upd3 = FakeGroupUpdate([FakeMember(777, False, "newcomer")])
    await bot.welcome_new_member(upd3, fctx3)
    check(bool(fctx3.bot.sent_calls), "незарегистрированному новичку из группы всё ещё приходит приветствие")
    if fctx3.bot.sent_calls:
        _, kw = fctx3.bot.sent_calls[-1]
        kb = kw.get("reply_markup")
        btn = kb.inline_keyboard[0][0] if kb else None
        url = getattr(btn, "url", None) if btn else None
        check(bool(url) and "?start=" in url,
              "незарегистрированный получает url-диплинк в ЛС, а не мёртвую callback-кнопку гильдии")


# ── 12. RetryAfter/Forbidden — ожидаемый шум, не «🚨 Ошибка» админу ──
async def test_flood_control_is_not_admin_noise():
    """С тех пор как групповой канал роста заработал, много игроков жмут
    кнопки в ОДНОМ групповом чате — совокупная частота ответов бота легко
    превышает лимит Telegram на чат (RetryAfter/flood control), а часть
    адресатов недостижима (Forbidden). Раньше ОБА класса шли админу как
    «🚨 Ошибка в X», неотличимо от настоящего бага — на активном групповом
    чате это залп из десятков одинаковых алертов ни о чём. Проверяем:
    RetryAfter/Forbidden НЕ алармят админа и дают пользователю дружелюбный
    текст, а обычное исключение по-прежнему алармит (шум отфильтрован
    точечно, не весь error-reporting отключён)."""
    from telegram.error import RetryAfter

    @bot.game_handler
    async def _boom_flood(update, context):
        raise RetryAfter(27)

    @bot.game_handler
    async def _boom_real(update, context):
        raise ValueError("настоящий баг")

    old_admin_id = bot.settings.admin_id
    bot.settings.admin_id = 999999999
    try:
        fctx1 = FakeContext(None)
        upd1 = FakeUpdate("x", uid=42)
        await _boom_flood(upd1, fctx1)
        check(not fctx1.bot.sent, f"RetryAfter не должен алармить админа: {fctx1.bot.sent}")
        check(upd1.callback_query.answers and "секунд" in (upd1.callback_query.answers[-1] or ""),
              "пользователь при RetryAfter видит дружелюбный текст, не «внутренняя ошибка»")

        fctx2 = FakeContext(None)
        upd2 = FakeUpdate("x", uid=43)
        await _boom_real(upd2, fctx2)
        check(bool(fctx2.bot.sent), "обычное исключение по-прежнему алармит админа (шум отфильтрован точечно)")
        # CallbackQuery в PTB имеет __slots__ — нельзя пометить объект как «уже
        # отвечен». Если хендлер сам успел ответить на callback_query ДО
        # падения (обычная практика), повторный answer() из этой ветки может
        # тихо потеряться — игрок не увидит вообще никакого сигнала о сбое.
        # Дублируем предупреждение сообщением в чат — канал, не зависящий от
        # состояния уже отвеченного callback_query.
        check(len(fctx2.bot.sent) >= 2,
              f"на настоящую ошибку игрок получает и алерт, и сообщение в чат "
              f"(не только опору на ephemeral alert): {fctx2.bot.sent}")
    finally:
        bot.settings.admin_id = old_admin_id


# ── 13. Навигация из профиля: «В меню» отвечает даже из фото-сообщения ──
async def test_menu_handler_recovers_from_non_text_message():
    """Раньше профиль с аватаркой Telegram уходил ФОТО-сообщением, и
    menu_handler звал query.message.edit_text(...) напрямую, без фолбэка —
    Telegram не даёт отредактировать фото-сообщение в текстовое (нет
    text на входе), edit_text падал BadRequest, и кнопка «🏰 В меню»
    просто ничего не делала: ни ответа, ни ошибки, полная тишина для
    игрока. Профиль теперь всегда текстовый (см. profile_callback), но
    menu_handler остаётся хрупким для ЛЮБОГО другого фото-экрана с такой
    кнопкой — проверяем сам menu_handler: он обязан ответить (отредактировать
    или прислать новое), даже если нажатие пришло из сообщения без текста."""
    from telegram.error import BadRequest as _BadRequest

    class _PhotoMsg(FakeMsg):
        def __init__(self):
            super().__init__()
            self.text = None   # фото-сообщение: нет текста для edit_text

        async def edit_text(self, text, **kw):
            raise _BadRequest("There is no text in the message to edit")

    MENU_UID = 999008
    today = datetime.now().date().isoformat()
    p = Player(user_id=MENU_UID, username="menutest", balance=100, exists=True,
              daily_progress={"reset_date": today, "quest_id": "chapter1", "reward_claimed": True})
    ctx = make_ctx(p)

    upd = FakeUpdate("menu", uid=MENU_UID)
    upd.callback_query.message = _PhotoMsg()
    upd.effective_message = upd.callback_query.message
    fctx = FakeContext(ctx)

    await bot.menu_handler(upd, fctx)
    check(bool(fctx.bot.sent), "«🏰 В меню» из фото-сообщения отвечает (новым сообщением), а не молчит")


# ── 13b. share_blunt_handler/blunt_details_handler: та же болезнь витрины ──
async def test_share_blunt_handler_recovers_from_photo_message():
    """Пользователь: «🚨 Ошибка в share_blunt_handler — There is no text in
    the message to edit». Кнопка «🔗 Поделиться» реально нажимается из
    витрины «Мои бланты» — та теперь фото-сообщение (см. edit_or_send_photo),
    у него caption, а не text. share_blunt_handler звал
    query.message.edit_text(...) НАПРЯМУЮ, без фолбэка — падал BadRequest,
    необработанным долетая до админа на каждое такое нажатие."""
    from telegram.error import BadRequest as _BadRequest

    class _PhotoMsg(FakeMsg):
        def __init__(self):
            super().__init__()
            self.text = None
            self.photo = [types.SimpleNamespace(file_id="witrina")]

        async def edit_text(self, text, **kw):
            raise _BadRequest("There is no text in the message to edit")

    inv = [{"id": "blunt_1", "type": "named", "name": "Тест", "rarity": "rare",
           "rare_number": "R-0001", "hash": "0xdead", "reaction": "..."}]
    p = Player(user_id=1, exists=True, balance=100, total_earned=100, inventory=inv)
    ctx = make_ctx(p)

    upd = FakeUpdate("share_blunt_blunt_1", uid=1)
    upd.callback_query.message = _PhotoMsg()
    upd.effective_message = upd.callback_query.message
    fctx = FakeContext(ctx)

    await bot.share_blunt_handler(upd, fctx)
    # Проверяем содержимое, не просто "что-то отправлено": game_handler
    # теперь и сам умеет слать дублирующее сообщение при НЕОЖИДАННОЙ ошибке
    # (см. фикс #8) — простое "было хоть одно сообщение" совпало бы и со
    # старым сломанным кодом (краш → общий "⚠️ Что-то пошло не так" от
    # обёртки), не различая «хендлер реально показал экран шеринга» от
    # «упал, обёртка спасла общим текстом». Ищем настоящий текст экрана.
    check(any("Отправь другу" in (t or "") for t in fctx.bot.sent),
          f"share_blunt_handler реально показывает экран шеринга из фото-витрины, "
          f"а не проваливается в общий crash-фолбэк: {fctx.bot.sent}")


async def test_blunt_details_fallback_uses_edit_or_reply():
    """blunt_details_handler: если оба пути отправки фото-карточки отказали
    (редкий двойной сбой), query.message — всё ещё витрина (фото). Раньше
    фолбэк звал голый query.message.edit_text(...) — та же болезнь, что и в
    share_blunt_handler, просто в более редкой ветке (не проверено
    поведенчески — требует форсировать двойной сбой рендера; проверяем
    статически, что фолбэк идёт через edit_or_reply)."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bot.py"), encoding="utf-8").read()
    start = src.index("async def blunt_details_handler(")
    end = src.index("\n@rate_limit(1)\n@game_handler\nasync def share_blunt_handler(", start)
    body = src[start:end]
    check("edit_or_reply" in body,
          "фолбэк blunt_details_handler (оба пути фото отказали) обязан идти через edit_or_reply")
    check("await query.message.edit_text(text=text" not in body,
          "blunt_details_handler больше не зовёт голый query.message.edit_text напрямую")


# ── 14. profile_callback: редактирует из текста, чистит за собой из фото ──
async def test_profile_edits_from_text_deletes_from_photo():
    """Пользователь: заход в «Мои бланты» из профиля работает, но «назад» —
    снова НОВОЕ сообщение профиля вместо редактирования того же самого,
    хотя у полированных ботов экран меняется на месте («изменено» внизу).

    Полная симметрия невозможна физически: витрина «Мои бланты» — реальное
    фото, Telegram не даёт превратить фото-сообщение в текстовое через edit.
    Но заход в профиль из ДРУГОГО ТЕКСТОВОГО экрана (меню, достижения…)
    обязан редактироваться на месте — это и есть «единый живой экран»,
    ничем не отличающийся от того, что уже работает в остальной игре. А
    для единственного настоящего исключения (фото) — старое сообщение
    обязано убираться, а не висеть мусором рядом с новым."""
    p = Player(user_id=1, exists=True, balance=100, total_earned=100)
    ctx = make_ctx(p)

    class _TrackedMsg(FakeMsg):
        def __init__(self, text_value):
            super().__init__()
            self.text = text_value
            self.deleted = False

        async def delete(self):
            self.deleted = True

    # Случай 1: заход из ТЕКСТОВОГО экрана (меню) — редактирует то же сообщение.
    upd1 = FakeUpdate("profile", uid=1)
    upd1.callback_query.message = _TrackedMsg("предыдущий текстовый экран")
    upd1.effective_message = upd1.callback_query.message
    fctx1 = FakeContext(ctx)
    await bot.profile_callback(upd1, fctx1)
    check(len(upd1.callback_query.message.edits) == 1,
          "заход в профиль из текста редактирует то же сообщение (не плодит новое)")
    check(not upd1.callback_query.message.deleted,
          "текстовое сообщение не удаляется — оно было отредактировано на месте")

    # Случай 2: заход из ФОТО-сообщения (витрина «Мои бланты») — новое +
    # уборка старого, чтобы не копился мусор из мёртвых экранов.
    upd2 = FakeUpdate("profile", uid=1)
    upd2.callback_query.message = _TrackedMsg(None)   # фото: text=None
    upd2.effective_message = upd2.callback_query.message
    fctx2 = FakeContext(ctx)
    await bot.profile_callback(upd2, fctx2)
    check(len(upd2.callback_query.message.edits) == 1,
          "заход в профиль из фото шлёт новое сообщение (отредактировать фото в текст нельзя)")
    check(upd2.callback_query.message.deleted,
          "старое фото-сообщение убирается — не остаётся мусором рядом с новым профилем")


# ── 15. Профиль с аватаркой ↔ витрина: настоящий edit одного сообщения ──
async def test_profile_avatar_edits_media_in_place():
    """Пользователь настаивал: у других ботов заход в профиль (фото) →
    витрина (фото) → назад в профиль (фото) — одно и то же сообщение,
    Telegram показывает «изменено», ни одного нового. Это физически
    возможно ИМЕННО для пары экранов, у которых ОБА — реальное фото
    (аватарка ↔ витрина коллекции): editMessageMedia меняет и картинку, и
    подпись одним вызовом. Проверяем: если у игрока есть аватарка И текущее
    сообщение уже фото (например, витрина) — profile_callback редактирует
    его через edit_message_media, а НЕ шлёт новое + не удаляет старое."""
    p = Player(user_id=1, exists=True, balance=100, total_earned=100)
    ctx = make_ctx(p)

    class _PhotoScreenMsg(FakeMsg):
        """Имитирует УЖЕ фото-сообщение (например, витрину «Мои бланты»)."""
        def __init__(self):
            super().__init__()
            self.text = None
            self.photo = [types.SimpleNamespace(file_id="witrina_file_id")]
            self.message_id = 777
            self.deleted = False

        async def delete(self):
            self.deleted = True

    async def _has_avatar(uid, limit=1):
        return types.SimpleNamespace(
            photos=[[types.SimpleNamespace(file_id="avatar_file_id")]])

    upd = FakeUpdate("profile", uid=1)
    upd.callback_query.message = _PhotoScreenMsg()
    upd.effective_message = upd.callback_query.message
    fctx = FakeContext(ctx)
    fctx.bot.get_user_profile_photos = _has_avatar

    await bot.profile_callback(upd, fctx)

    check(len(fctx.bot.media_edits) == 1,
          "профиль с аватаркой, пришли из фото — редактирует то же сообщение через editMessageMedia")
    check(not fctx.bot.photos_sent,
          "не шлёт новое фото-сообщение, когда editMessageMedia применим")
    check(not upd.callback_query.message.deleted,
          "старое фото-сообщение НЕ удаляется — оно отредактировано на месте, не заменено")


# ── 16. edit_or_reply чистит за собой — общий хелпер, 33 вызова по игре ──
async def test_edit_or_reply_cleans_up_own_message_not_user_command():
    """edit_or_reply — универсальный примитив «покажи этот экран», 33 вызова
    по всей игре. Раньше при неудачном редактировании (например, пришли из
    фото-сообщения — у него caption, а не text, editMessageText не
    применим) слал новое сообщение, но НИКОГДА не убирал старое — единый
    живой экран копил мусор из мёртвых предыдущих экранов на КАЖДОМ таком
    переходе по всей игре, не только в профиле (см. skins_menu_handler:
    «🎨 Кастомизация» прямо из фото-профиля била в ровно этот же баг).
    Исправлено один раз в общем хелпере — чинит сразу все вызовы.

    Разница по источнику: своё сообщение (пришли из нажатия кнопки) можно
    чистить за собой; сообщение ИГРОКА (пришли из команды вроде /rules) —
    нельзя, удалять чужой ввод без спроса неожиданно для игрока и не
    всегда разрешено правами бота в группе."""
    from telegram.error import BadRequest as _BadRequest

    class _PhotoMsg(FakeMsg):
        def __init__(self):
            super().__init__()
            self.text = None
            self.deleted = False

        async def edit_text(self, text, **kw):
            raise _BadRequest("There is no text in the message to edit")

        async def delete(self):
            self.deleted = True

    # Случай 1: пришли из CALLBACK (своё сообщение) — фолбэк убирает старое.
    upd1 = FakeUpdate("x", uid=1)
    upd1.callback_query.message = _PhotoMsg()
    upd1.effective_message = upd1.callback_query.message
    fctx1 = FakeContext(None)
    await bot.edit_or_reply(upd1, fctx1, "новый текст")
    check(bool(fctx1.bot.sent), "фолбэк отправляет новое сообщение")
    check(upd1.callback_query.message.deleted,
          "своё сообщение (из callback) убирается при фолбэке — не копится мусор")

    # Случай 2: пришли из КОМАНДЫ игрока (update.message) — фолбэк НЕ трогает
    # чужой ввод, даже если формально не мог его отредактировать.
    upd2 = FakeUpdate("x", uid=1)
    upd2.callback_query = None
    upd2.message = _PhotoMsg()
    upd2.effective_message = upd2.message
    fctx2 = FakeContext(None)
    await bot.edit_or_reply(upd2, fctx2, "новый текст")
    check(bool(fctx2.bot.sent), "фолбэк отправляет новое сообщение и из команды тоже")
    check(not upd2.message.deleted,
          "сообщение ИГРОКА (команда, не callback) не удаляется — трогать чужой ввод нельзя")


# ── 17. edit_or_send_photo не гоняет два editMessageMedia на одно сообщение ──
async def test_edit_or_send_photo_debounces_concurrent_edits():
    """Рендер+отправка фото ощутимо дольше правки текста — быстрый двойной
    тап (листание витрины «Далее»/«Далее») успевает запустить ВТОРОЙ
    editMessageMedia на то же message_id, пока первый ещё не долетел:
    гонка двух правок одного сообщения, непредсказуемый порядок применения.
    Проверяем: пока первый вызов «в полёте» (искусственная задержка имитирует
    медленный рендер), второй по ТОМУ ЖЕ сообщению не порождает второй
    реальный вызов Telegram — тихо игнорируется вместо гонки."""
    class _SlowBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.media_edit_calls = 0

        async def edit_message_media(self, chat_id=None, message_id=None, media=None, **kw):
            self.media_edit_calls += 1
            await asyncio.sleep(0.05)   # имитация медленного рендера/отправки
            return await super().edit_message_media(chat_id=chat_id, message_id=message_id,
                                                     media=media, **kw)

    class _PhotoMsg(FakeMsg):
        def __init__(self):
            super().__init__()
            self.photo = [types.SimpleNamespace(file_id="orig")]
            self.message_id = 555

    upd = FakeUpdate("x", uid=1)
    upd.callback_query.message = _PhotoMsg()
    upd.effective_message = upd.callback_query.message
    fctx = FakeContext(None)
    fctx.bot = _SlowBot()

    t1 = asyncio.create_task(bot.edit_or_send_photo(upd, fctx, "photo1", "caption1"))
    await asyncio.sleep(0.01)   # даём первому дойти до edit_message_media и взять лок
    t2 = asyncio.create_task(bot.edit_or_send_photo(upd, fctx, "photo2", "caption2"))
    await asyncio.gather(t1, t2)

    check(fctx.bot.media_edit_calls == 1,
          "второй тап по тому же сообщению, пока первый ещё в полёте, не запускает второй editMessageMedia")

    # Лок обязан снова открыться после завершения — следующий (не гоночный)
    # вызов должен пройти нормально.
    await bot.edit_or_send_photo(upd, fctx, "photo3", "caption3")
    check(fctx.bot.media_edit_calls == 2,
          "лок отпускается после завершения — следующий, не гоночный вызов проходит")


# ── 18. _fmt_oac: разделитель разрядов на крупных числах ─────────────
async def test_fmt_oac_thousands_separator():
    """Пороги рангов и Алтаря доходят до десятков тысяч; на экране, где
    решение принимается по цифре (Алтарь — необратимый донат всего
    баланса), читаемость числа не мелочь. Проверяем чистую функцию
    форматирования: пробел как разделитель разрядов, малые числа и
    отрицательные/нечисловые значения не ломаются."""
    check(bot._fmt_oac(50000) == "50 000", f"{bot._fmt_oac(50000)!r}")
    check(bot._fmt_oac(1234567) == "1 234 567", f"{bot._fmt_oac(1234567)!r}")
    check(bot._fmt_oac(85) == "85", "малые числа — без разделителя")
    check(bot._fmt_oac(0) == "0", "ноль — без разделителя")
    check(bot._fmt_oac("не число") == "не число", "нечисловой ввод не роняет форматирование")


# ── 19. /help — справка по механикам, которых нет в «Правилах» ───────
async def test_help_covers_mechanics_missing_from_rules():
    """«Правила мира» не упоминали Лабиринт, Мины, Алтарь, Плантацию,
    Питомца, Достижения, Витрину и рефералов ни словом — у половины
    механик игры не было НИ ОДНОГО места, где новичок мог бы прочитать,
    что это такое. Проверяем: /help существует, реально описывает эти
    механики, и на него есть кросс-ссылка с экрана «Правила»."""
    p = Player(user_id=1, exists=True, balance=100, total_earned=100)
    ctx = make_ctx(p)
    upd = FakeUpdate("help", uid=1)
    fctx = FakeContext(ctx)
    await bot.help_callback(upd, fctx)
    check(len(upd.callback_query.message.edits) == 1, "«/help» отвечает ровно одним сообщением")
    text = upd.callback_query.message.edits[0]
    for mechanic in ("Лабиринт", "Мины", "Алтарь", "Плантация", "Питомец",
                     "Витрина", "Достижения", "Пригласить друга"):
        check(mechanic in text, f"справка описывает «{mechanic}» — раньше об этом негде было прочитать")

    check("help" in bot.TEXT_COMMAND_HANDLERS and bot.TEXT_COMMAND_HANDLERS["help"] is bot.help_callback,
          "команда /help зарегистрирована")
    check("help" in bot.CALLBACKS and bot.CALLBACKS["help"] is bot.help_callback,
          "кнопка callback_data=\"help\" зарегистрирована")

    # Кросс-ссылка: с «Правил» на полную справку — ловим reply_markup, а не
    # только текст (FakeMsg.edit_text по умолчанию не сохраняет kwargs).
    class _KbCapturingMsg(FakeMsg):
        def __init__(self):
            super().__init__()
            self.last_kb = None

        async def edit_text(self, text, reply_markup=None, **kw):
            self.last_kb = reply_markup
            return await super().edit_text(text, reply_markup=reply_markup, **kw)

    upd2 = FakeUpdate("rules", uid=1)
    upd2.callback_query.message = _KbCapturingMsg()
    upd2.effective_message = upd2.callback_query.message
    fctx2 = FakeContext(ctx)
    await bot.rules_callback(upd2, fctx2)
    kb = upd2.callback_query.message.last_kb
    cb_data = [btn.callback_data for row in kb.inline_keyboard for btn in row] if kb else []
    check("help" in cb_data, "с экрана «Правила» есть кнопка на полную справку («help» среди callback_data)")


# ── 20. build_share_url ОБЯЗАН нести url= — иначе шер ведёт в никуда ──
async def test_build_share_url_requires_url_param():
    """Пользователь: любая кнопка «Поделиться»/«Отправить другу» открывала
    просто браузер и всё — ни с одним другом поделиться было нельзя.
    Причина: t.me/share/url без параметра url= не открывает нативный пикер
    выбора получателя вообще, резолвится в мёртвую страницу. Ссылка была
    зашита ТОЛЬКО внутри text= — выглядело как рабочий шеринг, по факту ни
    одна кнопка «Поделиться» в игре не работала с самого своего появления:
    единственный настоящий вирусный канал был сломан насквозь."""
    url = bot.build_share_url("https://t.me/testbot?start=ref_1", "текст сообщения")
    check(url.startswith("https://t.me/share/url?url="),
          f"url= обязателен и идёт первым параметром: {url!r}")
    check("start%3Dref_1" in url, f"реферальная ссылка реально попадает в url=: {url!r}")
    check("text=" in url, f"текст сообщения тоже передаётся отдельным параметром: {url!r}")

    # Без текста — url= всё равно должен присутствовать (не выродиться в
    # пустой t.me/share/url без единого параметра).
    url2 = bot.build_share_url("https://t.me/testbot?start=ref_2")
    check(url2.startswith("https://t.me/share/url?url="), f"url= обязателен даже без текста: {url2!r}")
    check("text=" not in url2, "пустой text не добавляет мусорный параметр")


async def test_share_buttons_pass_url_param():
    """Статически проверяет все реальные места, где строится кнопка
    «Поделиться»/«Отправить другу»: build_share_url обязан получать ДВА
    аргумента (первый — реальная ссылка, ref_link), не один голый текст.
    Ровно один аргумент — это тот самый регресс, который сломал шеринг
    везде в игре разом."""
    import re as _re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bot.py"), encoding="utf-8").read()
    calls = _re.findall(r"build_share_url\(([^)]*)\)", src)
    check(len(calls) >= 4, f"ожидали минимум 4 места, вызывающих build_share_url: нашли {len(calls)}")
    bad = [c for c in calls if "," not in c]
    check(not bad, f"build_share_url вызван БЕЗ ref_link отдельным аргументом (сломанный шеринг): {bad}")


async def test_win_share_button_does_not_duplicate_link_in_text():
    """После фикса ссылка шла ОДНОВРЕМЕННО в url= (Telegram сам показывает
    её другу с превью) И была по-прежнему зашита в text= — друг получал
    одну и ту же голую ссылку дважды подряд на экране (баг, найденный
    пользователем на реальной отправке). Ссылка обязана попадать в
    итоговый диплинк ровно один раз (через url=), не дважды."""
    bot_username = "testbot"

    class _FakeBotGetMe:
        async def get_me(self):
            return types.SimpleNamespace(username=bot_username)

    fctx = types.SimpleNamespace(bot=_FakeBotGetMe())
    btn = await bot._win_share_button(fctx, 42, "⚡ Я достиг ранга Ветеран!")
    ref_link = f"https://t.me/{bot_username}?start=ref_42"
    from urllib.parse import quote as _quote
    encoded = _quote(ref_link, safe='')
    check(btn.url.count(encoded) == 1,
          f"ссылка должна встречаться в диплинке ровно один раз (через url=), "
          f"а не дублироваться внутри text=: {btn.url!r}")


# ── 21. query.answer() гасит спиннер загрузки — 3 подтверждённых пробела ──
async def test_query_answer_called_in_profile_craft_smoke():
    """profile_callback/craft_callback_v2/smoke_callback раньше НИ РАЗУ не
    звали query.answer() — кнопка визуально «висит» с крутящимся
    индикатором загрузки Telegram, хотя нажатие уже обработано."""
    p = Player(user_id=1, exists=True, balance=100, total_earned=100)
    ctx = make_ctx(p)

    upd = FakeUpdate("profile", uid=1)
    fctx = FakeContext(ctx)
    await bot.profile_callback(upd, fctx)
    check(len(upd.callback_query.answers) >= 1, "profile_callback гасит спиннер (query.answer вызван)")

    upd2 = FakeUpdate("craft", uid=1)
    fctx2 = FakeContext(ctx)
    await bot.craft_callback_v2(upd2, fctx2)
    check(len(upd2.callback_query.answers) >= 1, "craft_callback_v2 гасит спиннер (query.answer вызван)")

    p2 = Player(user_id=2, exists=True, balance=100, total_earned=100, blunts=0)
    ctx2 = make_ctx(p2)
    upd3 = FakeUpdate("smoke", uid=2)
    fctx3 = FakeContext(ctx2)
    await bot.smoke_callback(upd3, fctx3)
    check(len(upd3.callback_query.answers) >= 1,
          "smoke_callback (пустой свёрток) гасит спиннер (query.answer вызван)")


# ── 22. send_chat_action перед некэшированным рендером фото ──────────
async def test_slow_renders_send_chat_action():
    """Некэшированный рендер витрины/карточки бланта — реальная задержка
    (executor + сборка изображения), экран всё это время выглядит
    замершим на прежнем кадре. send_chat_action — нативный, бесплатный
    индикатор Telegram («отправляет фото…») — статически проверяем, что
    обе функции, которые реально рендерят, его зовут."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bot.py"), encoding="utf-8").read()
    wall_start = src.index("async def _send_collection_wall(")
    wall_end = src.index("\nasync def achievements_callback(", wall_start)
    check("send_chat_action" in src[wall_start:wall_end],
          "_send_collection_wall должна слать send_chat_action перед некэшированным рендером")

    card_start = src.index("async def _send_blunt_card(")
    card_end = src.index("\nasync def send_whisper_dm(", card_start)
    check("send_chat_action" in src[card_start:card_end],
          "_send_blunt_card должна слать send_chat_action перед некэшированным рендером")


# ── 24. query.answer() гасит спиннер — вторая волна пробелов (Мины/Ритуал/
#        Привилегия), плюс edit_or_reply/send_whisper_dm отвечают централизованно ──
async def test_query_answer_called_second_wave():
    """Продолжение находки #21: тот же дефект (ни разу не звали
    query.answer() на успешном пути) обнаружился в _mines_open_cell_wrapper
    (САМЫЙ частый тап во всей игре — каждая открытая клетка Мин) и
    _mines_cashout_wrapper, в ritual_callback (рендерит через
    animate_progress_bar, минуя edit_or_reply) и в privilege_callback
    (раньше — msg.reply_text напрямую, вообще без edit_or_reply). Плюс
    структурная проверка: edit_or_reply и send_whisper_dm теперь отвечают
    сами — этим чинится каждый экран, который через них рендерится."""
    p = Player(user_id=1, exists=True, balance=1000, total_earned=1000, guild="BLACK")
    mines_ctx = make_ctx(p, redis=FakeRedis(), pool=FakeMinesPool())
    state = {"field": [[0] * 5 for _ in range(5)], "mines": [[4, 4]], "bet": 100,
             "step": 0, "multiplier": 1.0, "status": "playing", "created_at": 0}
    await bot._mines_state_set(mines_ctx, 1, state)
    upd = FakeUpdate("mines_open_0_0", uid=1)
    await bot._mines_open_cell_wrapper(upd, FakeContext(mines_ctx))
    check(len(upd.callback_query.answers) >= 1,
          "_mines_open_cell_wrapper гасит спиннер на КАЖДОЙ открытой клетке")

    state2 = {"field": [[1] * 5 for _ in range(4)] + [[0] * 5], "mines": [[4, 4]], "bet": 100,
              "step": 5, "multiplier": 1.5, "status": "playing", "created_at": 0}
    await bot._mines_state_set(mines_ctx, 1, state2)
    upd2 = FakeUpdate("mines_cashout", uid=1)
    await bot._mines_cashout_wrapper(upd2, FakeContext(mines_ctx))
    check(len(upd2.callback_query.answers) >= 1,
          "_mines_cashout_wrapper гасит спиннер на успешном кэшауте")

    # ritual/privilege/catalog зовут check_achievements/обычные SELECT'ы —
    # им нужен обычный FakePool (FakeMinesPool отвечает только на SQL Мин).
    ctx = make_ctx(p)
    upd3 = FakeUpdate("ritual", uid=1)
    await bot.ritual_callback(upd3, FakeContext(ctx))
    check(len(upd3.callback_query.answers) >= 1,
          "ritual_callback гасит спиннер (рендер идёт мимо edit_or_reply)")

    upd4 = FakeUpdate("privilege", uid=1)
    await bot.privilege_callback(upd4, FakeContext(ctx))
    check(len(upd4.callback_query.answers) >= 1, "privilege_callback гасит спиннер")
    check(upd4.callback_query.message.edits, "privilege_callback правит единый экран на месте, не шлёт новое сообщение")

    upd5 = FakeUpdate("catalog", uid=1)
    await bot.catalog_callback(upd5, FakeContext(ctx))
    check(len(upd5.callback_query.answers) >= 1,
          "catalog_callback гасит спиннер (через централизованный фикс в edit_or_reply)")


# ── 25. send_whisper_dm больше не оставляет игрока без единой кнопки ─────
async def test_send_whisper_dm_has_navigation():
    """Донат в Храм списывает реальный баланс и раньше подтверждал это
    сообщением БЕЗ единой кнопки — игроку приходилось печатать /menu
    руками, чтобы продолжить играть."""
    p = Player(user_id=1, exists=True, balance=100, total_earned=100)
    ctx = make_ctx(p)
    upd = FakeUpdate("x", uid=1)
    await bot.send_whisper_dm(upd, FakeContext(ctx), "💎 Ты внёс 50 OAC в Храм. Спасибо, Странник!")
    check(len(upd.callback_query.answers) >= 1, "send_whisper_dm гасит спиннер")


# ── 26. Незарегистрированный чужак не видит игровые экраны в обход /start ──
async def test_unregistered_stranger_gets_start_prompt():
    """get_by_id для никогда не регистрировавшегося возвращает не None, а
    пустой Player(exists=False) (repository.py: return Player(user_id=user_id))
    — тот же баг, что уже чинили в welcome_new_member/guild_join_handler.
    guild_info_callback/privilege_callback/luck_callback/lab_enter не идут
    через @game_handler (нет гейта выше) и проверяли `if not player`/
    `not player.user_id` — оба всегда False для такого Player, так что
    незнакомец, набравший /guild, /privilege, /luck или /lab БЕЗ /start,
    видел полноценный игровой экран с нулевыми полями вместо приглашения
    активироваться."""
    stranger = Player(user_id=999, exists=False)
    ctx = make_ctx(stranger)

    def _last_edit(upd):
        edits = upd.callback_query.message.edits
        return (edits[-1] or "") if edits else ""

    upd1 = FakeUpdate("guild_info", uid=999)
    await bot.guild_info_callback(upd1, FakeContext(ctx))
    check("start" in _last_edit(upd1).lower(),
          f"guild_info_callback отправляет незнакомца к /start, а не в экран Гильдий: {_last_edit(upd1)!r}")

    upd2 = FakeUpdate("privilege", uid=999)
    await bot.privilege_callback(upd2, FakeContext(ctx))
    check("start" in _last_edit(upd2).lower(),
          f"privilege_callback отправляет незнакомца к /start: {_last_edit(upd2)!r}")

    upd3 = FakeUpdate("luck", uid=999)
    await bot.luck_callback(upd3, FakeContext(ctx))
    check("start" in _last_edit(upd3).lower(),
          f"luck_callback отправляет незнакомца к /start вместо хаба Удачи: {_last_edit(upd3)!r}")

    upd4 = FakeUpdate("lab_start", uid=999)
    await bot.lab_enter(upd4, FakeContext(ctx))
    check("start" in _last_edit(upd4).lower(),
          f"lab_enter отвечает незнакомцу приглашением /start, а не молчит: {_last_edit(upd4)!r}")


# ── 27. Карта Фабрики: фог-заметка честно считает закрытые локации ───────
async def test_world_hub_fog_note_matches_locked_locations():
    """world_hub (теперь «🗺️ Карта Фабрики») помечает закрытые локации
    «в тумане» вместо 🔒 и считает их число в заметке под картой — эта
    заметка не должна расходиться с тем, сколько кнопок реально в тумане."""
    # Низкий ранг: Питомец и Алтарь оба закрыты → 2 локации в тумане.
    low = Player(user_id=1, exists=True, balance=100, total_earned=100, passive_level=1)
    ctx = make_ctx(low)
    upd = FakeUpdate("world_hub", uid=1)
    await bot.world_hub(upd, FakeContext(ctx))
    text = upd.callback_query.message.edits[-1] if upd.callback_query.message.edits else ""
    check("2 локации" in text, f"низкий ранг: заметка честно считает 2 локации в тумане: {text!r}")

    # Максимальный игрок: Ветеран с питомцем + Алтарь открыт → тумана нет.
    maxed = Player(user_id=2, exists=True, balance=100000, total_earned=100000,
                   pet="🐕 Песик", passive_level=10, prestige=0)
    ctx2 = make_ctx(maxed)
    upd2 = FakeUpdate("world_hub", uid=2)
    await bot.world_hub(upd2, FakeContext(ctx2))
    text2 = upd2.callback_query.message.edits[-1] if upd2.callback_query.message.edits else ""
    check("Вся карта открыта" in text2, f"максимальный игрок видит честное «вся карта открыта»: {text2!r}")
    check("туман" not in text2.split("</b>")[-1] or "🌫️" not in text2,
          "у максимального игрока нет тумана в заметке")


# ── 28. Алтарь-в-тумане — витрина эндгейма, не просто «закрыто» ──────────
async def test_altar_locked_shows_endgame_showcase():
    """Раньше тап по закрытому Алтарю показывал только условие гейта —
    отвечает не пиону «а смысл?», это сухая механика. Теперь виден весь
    ряд титулов (curiosity gap: конкретная цель ближе тянет, чем
    абстрактная), и, если престиж уже у кого-то есть, честный топ-3."""
    p = Player(user_id=1, exists=True, balance=100, total_earned=100, passive_level=1)
    rows = [{"username": "Странник1", "prestige": 500}]
    ctx = make_ctx(p, pool=FakePool(rows=rows))
    upd = FakeUpdate("altar_hub", uid=1)
    await bot.altar_hub(upd, FakeContext(ctx))
    text = upd.callback_query.message.edits[-1] if upd.callback_query.message.edits else ""
    check("Послушник Алтаря" in text and "Вечный" in text,
          f"закрытый Алтарь показывает весь ряд титулов, а не только факт блокировки: {text!r}")
    check("Странник1" in text, f"закрытый Алтарь показывает честный топ уже вложивших: {text!r}")


# ── 29. Первое сообщение: атмосфера ДО награды, но БЕЗ второго тапа ──────
async def test_welcome_text_has_framing_without_extra_tap():
    """Комментарий в _create_new_player сам документирует прошлый провал:
    старое первое сообщение было «стеной лора + выбором фракции ДО первого
    действия» — 2 тапа и 2 экрана до первой награды, там терялось больше
    половины холодного трафика. Новая атмосферная строка обязана жить
    ВНУТРИ того же сообщения (Narrative Transportation: фрейминг до опыта
    меняет сам опыт) и не добавлять ни одного лишнего тапа/декоративного
    экрана — единственная кнопка по-прежнему должна вести прямиком в farm."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bot.py"), encoding="utf-8").read()
    body = src[src.index("async def _create_new_player"):src.index("async def defer_faction_handler")]
    check("Фабрика №9 давно ждала" in body,
          "атмосферная строка добавлена в welcome_text")
    check('InlineKeyboardButton("🍬 Собрать первый урожай", callback_data="farm")' in body,
          "кнопка по-прежнему одна и ведёт прямиком в фарм — ни одного лишнего тапа/экрана")
    check(body.count("InlineKeyboardMarkup(") == 1,
          "ровно одна клавиатура в welcome-сообщении — атмосфера не породила отдельный экран-прокладку")


# ── 23. «🏰 В меню» на экранах удачи вёл НЕ в меню — мисклейбл пофикшен ──
async def test_luck_result_buttons_dont_claim_menu():
    """Результаты Колеса/Алхимии показывали кнопку «🏰 В меню», но
    callback_data вёл в хаб «Удача» (Колесо/Мины/Алхимия), а не в реальное
    главное меню игры — игрок жал «в меню», ожидая выйти из азартной
    секции, а вместо этого оставался внутри неё."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bot.py"), encoding="utf-8").read()
    check('InlineKeyboardButton("🏰 В меню", callback_data="luck")' not in src,
          "кнопка результата Колеса/Алхимии больше не выдаёт себя за «В меню»")
    check(src.count('InlineKeyboardButton("🍀 К удаче", callback_data="luck")') == 2,
          "оба экрана (Колесо, Алхимия) честно ведут «К удаче», не «В меню»")


# ── 33. Колесо/Алхимия: подвеска ожидания перед раскрытием результата ──
async def test_wheel_and_alchemy_have_suspense_before_reveal():
    """SLAYER Red Team (Cluster Б, A1 Dopamine/Neuroscience): дофаминовые
    нейроны отвечают сильнее на предсказывающий сигнал, чем на сам приз
    (Schultz 1997, reward prediction error) — раньше обе азартные механики
    раскрывали результат МГНОВЕННО на тап, ноль кадров ожидания. Проверяем,
    что подвеска реально рендерится (несколько edit_text ДО финального
    результата), а сам финальный результат/кнопка при этом не меняются."""
    p = Player(user_id=1, exists=True, balance=1000, total_earned=1000,
               blunts=20, last_daily=None, daily_progress={})
    ctx = make_ctx(p)
    upd = FakeUpdate("luck_wheel", uid=1)
    await bot._process_wheel(upd, FakeContext(ctx), 1, p, bot.LUCK_CONFIG, ctx)
    calls = upd.callback_query.message.edit_calls
    check(len(calls) == 5,
          f"Колесо: 4 кадра подвески + 1 финальный результат (было 0+1): получили {len(calls)}")
    check("крутится" in calls[0][0],
          "первый кадр Колеса — визуальная подвеска, не готовый результат")
    final_kb = calls[-1][1].get("reply_markup")
    check(final_kb and final_kb.inline_keyboard[0][0].callback_data == "luck"
          and final_kb.inline_keyboard[0][0].text == "🍀 К удаче",
          "финальный кадр Колеса несёт настоящую кнопку результата, не подвеску")

    p2 = Player(user_id=2, exists=True, balance=1000, total_earned=1000,
                blunts=20, daily_progress={})
    ctx2 = make_ctx(p2)
    upd2 = FakeUpdate("alchemy_confirm", uid=2)
    await bot._process_alchemy_confirm(upd2, FakeContext(ctx2), 2, p2, bot.LUCK_CONFIG, ctx2)
    calls2 = upd2.callback_query.message.edit_calls
    check(len(calls2) == 5,
          f"Алхимия: 4 кадра подвески + 1 финальный результат (было 0+1): получили {len(calls2)}")
    check("кипит" in calls2[0][0],
          "первый кадр Алхимии — визуальная подвеска (кипящая реакция), не готовый результат")
    final_kb2 = calls2[-1][1].get("reply_markup")
    check(final_kb2 and final_kb2.inline_keyboard[0][0].callback_data == "luck",
          "финальный кадр Алхимии несёт настоящую кнопку результата, не подвеску")


# ── 34. Названия медалей: свой голос на каждой дорожке, без слома падежа ─
async def test_medal_names_are_thematic_and_grammar_safe():
    """SLAYER Red Team (Cluster «начало игры», A5 Voice/Copy): Бронза/
    Серебро/Золото/Платина были ОДНИМ шаблоном на всех пяти дорожках сразу —
    голос, который ничего не говорит рядом с «Фабрика №9»/«Искажение». Заодно
    ловим грамматику: "повышен до {name}"/"{N} до {name}" требуют
    родительного падежа — с новыми многословными титулами это стало бы
    заметной ломаной фразой, если бы вставки остались падежными."""
    tracks = (bot.FARM_MEDALS, bot.CRAFT_MEDALS, bot.SMOKE_MEDALS,
              bot.RITUAL_MEDALS, bot.REPENT_MEDALS)
    all_names = [name for track in tracks for _, name, _ in track]
    check(not any(generic in name for name in all_names
                  for generic in ("Бронза", "Серебро", "Золото", "Платина")),
          "ни одна дорожка не использует старый олимпийский шаблон")
    check(len(set(all_names)) == len(all_names),
          "все 20 титулов (5 дорожек × 4 тира) различны — ни одного дубля между дорожками")

    text, bonus = bot.get_medal_text_and_reward(0, 1, bot.FARM_MEDALS)
    check("Новый уровень:" in text and bot.FARM_MEDALS[0][1] in text,
          "рангап медали — label-конструкция, не требует падежа от названия")
    check("повышен до" not in text,
          "старая падежно-ломкая формулировка не вернулась")
    check(bonus == bot.FARM_MEDALS[0][2], "награда за медаль не тронута правкой текста")

    progress = bot.get_medal_progress(bot.FARM_MEDALS[0][0] - 1, bot.FARM_MEDALS, just_leveled=False)
    check("до цели:" in progress,
          "goal-gradient строка тоже label-конструкция, не падежная вставка")


# ── 35. Профиль новичка: серия видна, пустые поля — не тупик ──────────
async def test_profile_shows_streak_and_empty_state_hooks():
    """Early-game / D1-D2 retention: «Титул: —» и «Заслуги: —» были тупиковыми
    прочерками для новичка — теперь оба несут конкретный, честный следующий
    шаг (Zeigarnik: незакрытое тянет внимание сильнее закрытого). Серия
    входов теперь видна на самом экране идентичности (профиль), не только
    в главном меню, — постоянное, не разовое напоминание «не рви цепочку»."""
    p = Player(user_id=1, exists=True, balance=100, total_earned=100,
               login_streak=0, titles="", profile_skins={})
    ctx = make_ctx(p)
    upd = FakeUpdate("profile", uid=1)
    await bot.profile_callback(upd, FakeContext(ctx))
    text = upd.callback_query.message.edit_calls[-1][0]
    check("Серия входов" not in text,
          "серия НЕ показывается при streak=0 (нечего защищать loss-aversion'ом)")
    check("ещё нет — первый за 7-дневную серию" in text,
          "пустой Титул — конкретная честная цель, не тупиковый прочерк")
    check("0/4 — см. 🏆 Достижения ниже" in text,
          "пустые Заслуги — счётчик-цель со ссылкой на реальный экран, не тупиковый прочерк")

    p2 = Player(user_id=2, exists=True, balance=100, total_earned=100,
                login_streak=5, streak_freezes=1, titles="🩸", profile_skins={})
    ctx2 = make_ctx(p2)
    upd2 = FakeUpdate("profile", uid=2)
    await bot.profile_callback(upd2, FakeContext(ctx2))
    text2 = upd2.callback_query.message.edit_calls[-1][0]
    check("🔥 <b>Серия входов:</b> 5 дн. · ❄️1" in text2,
          "серия входов видна на профиле с заморозками, если streak>=1")
    check("есть, но не выбран — 🎨 Кастомизация" in text2,
          "заработанный, но не выбранный титул — конкретная подсказка, не тот же прочерк")

    kb = upd2.callback_query.message.edit_calls[-1][1].get("reply_markup")
    has_ach_btn = kb and any(btn.callback_data == "achievements_profile"
                              for row in kb.inline_keyboard for btn in row)
    check(has_ach_btn,
          "профиль несёт реальную кнопку в Достижения (achievements_profile — "
          "раньше эта ветка существовала в achievements_callback, но ни одна "
          "кнопка в игре её не вызывала)")


# ── 37. Мины: HTML вместо сломанного легаси-Markdown ──────────────────
async def test_mines_screens_use_html_not_broken_markdown():
    """Мины были единственным очагом во всей игре с parse_mode='Markdown' —
    легаси-режим Telegram понимает только ОДИНАРНЫЕ звёздочки для жирного;
    "**двойные**", которыми был размечен весь экран, вообще не его синтаксис
    (это MarkdownV2/CommonMark) — либо съедались без жирности, либо ломали
    рендер. Код-блоки на тройных бэктиках заменены на HTML <pre>."""
    p = Player(user_id=1, exists=True, balance=1000, total_earned=1000)
    ctx = make_ctx(p, redis=FakeRedis(), pool=FakeMinesPool())

    upd = FakeUpdate("luck_mines", uid=1)
    await bot.luck_mines_handler(upd, FakeContext(ctx))
    text, kw = upd.callback_query.message.edit_calls[-1]
    check(kw.get("parse_mode") == "HTML", "меню ставки Мин размечено HTML, не Markdown")
    check("<b>МИНЫ</b>" in text and "**" not in text,
          "заголовок — настоящий HTML-жирный, не сломанные двойные звёздочки")

    state = {"field": [[0] * 5 for _ in range(5)], "mines": [[0, 0]], "bet": 100,
             "step": 3, "multiplier": 1.27, "status": "playing", "created_at": 0}
    await bot._mines_state_set(ctx, 1, state)
    upd2 = FakeUpdate("mines_cashout", uid=1)
    await bot._mines_cashout_wrapper(upd2, FakeContext(ctx))
    text2, kw2 = upd2.callback_query.message.edit_calls[-1]
    check(kw2.get("parse_mode") == "HTML", "экран результата Мин размечен HTML, не Markdown")
    check("<pre>" in text2 and "```" not in text2,
          "игровое поле — HTML <pre>, не markdown-код-блок")
    check("**" not in text2, "на экране результата не осталось сломанных двойных звёздочек")


# ── 35b. Мины — часть направляемого онбординга Главы 1 ────────────────
async def test_mines_is_reachable_from_quest_and_marks_progress():
    """Фарм/крафт/дунуть (Глава 1) — три экрана без единого реального
    решения игрока (тап → случайное число, статистику которого мозг
    полностью моделирует за первые 10-20 повторов — reward prediction error
    Schultz 1997 гаснет, действие ощущается рутиной). Мины — единственная
    механика в игре, где выбор (какую клетку открыть, когда забрать
    выигрыш) реально делает игрок; уже доступна с первой минуты (0 OAC
    кулдаун, ставка от 50 при старте в 800 OAC) — но не была частью
    направляемого онбординга. Новичок мог пройти всю Главу 1 и уйти, ни
    разу не увидев единственную механику с настоящей агентностью."""
    chapter1_keys = {t["key"] for t in bot.QUEST_TEMPLATES["chapter1"]["tasks"]}
    check("mines" in chapter1_keys, "Мины — реальный шаг Главы 1, не спрятаны за её пределами")

    p = Player(user_id=1, exists=True, balance=800, total_earned=800,
               onboarding_step=1, daily_progress={"quest_id": "chapter1"})
    ctx = make_ctx(p)

    # Кнопка задания «Мины» доводит до экрана выбора ставки, а не «Неизвестное задание».
    upd = FakeUpdate("quest_mines", uid=1)
    await bot.handle_quest_action(upd, FakeContext(ctx))
    check(not any("Неизвестное" in (a or "") for a in upd.callback_query.answers),
          "кнопка «Мины» в чек-листе Главы 1 доходит до реального экрана")

    # Сам запуск партии (реальный выбор ставки) отмечает шаг квеста — тот же
    # принцип, что у входа в Лабиринт (_mark_lab): считается знакомство с
    # механикой, а не конкретный исход партии.
    upd2 = FakeUpdate("mines_bet_50", uid=1)
    await bot._mines_start_game(upd2, FakeContext(ctx), 1, 50, ctx)
    check(p.daily_progress.get("mines") is True,
          "старт партии Мин отмечает задание квеста")


# ── 36. Крафт/дунуть: та же цепочка «что дальше», что уже на фарме ────
async def test_craft_and_smoke_chain_to_next_quest_step():
    """next_quest_step раньше подключён только к экрану фарма — крафт и
    дуновение раскрывали результат и ничего не предлагали дальше, разрывая
    цепочку фарм→крафт→дунуть на каждом следующем звене."""
    p = Player(user_id=1, exists=True, balance=1000, total_earned=1000,
               blunts=0, craft_count=0, onboarding_step=1, daily_progress={})
    ctx = make_ctx(p)
    upd = FakeUpdate("craft_normal", uid=1)
    await bot.handle_craft_normal_v2(upd, FakeContext(ctx))
    text = upd.callback_query.message.edit_calls[-1][0]
    check("💡" in text, "экран крафта несёт подсказку следующего шага квеста")

    p2 = Player(user_id=2, exists=True, balance=1000, total_earned=1000,
                blunts=5, smoke_count=0, onboarding_step=1, daily_progress={})
    ctx2 = make_ctx(p2)
    upd2 = FakeUpdate("smoke", uid=2)
    await bot.do_smoke(upd2, FakeContext(ctx2))
    text2 = upd2.callback_query.message.edit_calls[-1][0]
    check("💡" in text2, "экран дуновения несёт подсказку следующего шага квеста")


# ── 32. Час Удачи: личное DM-уведомление, не только чат гильдии ──────
async def test_happy_hour_dm_broadcast_reaches_candidates():
    """SLAYER Red Team (Cluster Б, A1 Dopamine/Neuroscience + A3 Systems):
    раньше пик Часа Удачи (x2 OAC, 30 минут) был виден только в публичном
    чате гильдии и пассивным баннером внутри самого бота — тому, кто не
    открыл бот именно в эти 30 минут, событие было немым. Личное DM тянет
    игрока обратно ровно в момент реальной ценности (раз в сутки — не спам)."""
    class _FakeResp:
        status_code = 200
        text = "{}"

    class _FakeAsyncClient:
        posts = []
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **k):
            _FakeAsyncClient.posts.append(json)
            return _FakeResp()

    pool = FakePool(rows=[{"user_id": 111}, {"user_id": 222}])
    ctx = make_ctx(Player(user_id=1), pool=pool)
    orig_client = bot.httpx.AsyncClient
    bot.httpx.AsyncClient = _FakeAsyncClient
    _FakeAsyncClient.posts = []
    try:
        await bot._happy_hour_dm_broadcast(ctx)
        check(len(_FakeAsyncClient.posts) == 2,
              "DM уходит каждому кандидату из выборки (blocked_at IS NULL)")
        check(all(p["chat_id"] in (111, 222) for p in _FakeAsyncClient.posts),
              "получатели — реальные uid из выборки")
        check(all("x2 OAC" in p["text"] and "ЧАС УДАЧИ" in p["text"] for p in _FakeAsyncClient.posts),
              "текст сообщает и про x2, и про сам Час Удачи")
        check(all(p.get("reply_markup", {}).get("inline_keyboard") for p in _FakeAsyncClient.posts),
              "сообщение несёт кнопку прямого входа в фарм, не тупиковый текст")
    finally:
        bot.httpx.AsyncClient = orig_client


# ── 32b. Час Удачи DM: не бьёт по всей базе, только по «тёплым» ──────
async def test_happy_hour_dm_excludes_dormant_players():
    """Раньше запрос был `WHERE blocked_at IS NULL` без окна активности —
    ежедневная рассылка достигала и тех, кого winback_push сознательно не
    трогает после 30 дней простоя (сам winback объясняет почему: «шанс
    раздражить/схватить блок растёт быстрее, чем шанс вернуть»). На проде
    это била рассылка ~176 адресатам с 50% блоком за один прогон, и именно
    поэтому winback находил уже заблокировавшими 96% своих целей — та же
    когорта успевала выгореть от ежедневного DM раньше, чем до неё доходил
    редкий еженедельный winback."""
    class _SqlCapturingPool:
        def __init__(self, rows=None):
            self._rows = rows or []
            self.last_sql = None
            self.last_args = None

        def acquire(self, *a, **k):
            pool = self

            class _Conn:
                async def fetch(self, sql, *args):
                    pool.last_sql = sql
                    pool.last_args = args
                    return pool._rows

            class _CM:
                async def __aenter__(self):
                    return _Conn()
                async def __aexit__(self, *exc):
                    return False
            return _CM()

    pool = _SqlCapturingPool(rows=[])
    ctx = make_ctx(Player(user_id=1), pool=pool)
    await bot._happy_hour_dm_broadcast(ctx)

    check(pool.last_sql is not None and "last_farm" in pool.last_sql,
          "запрос фильтрует по last_farm — не бьёт по всей базе разом")
    check(bool(pool.last_args) and isinstance(pool.last_args[0], datetime),
          "передан cutoff-параметр окна активности")
    if pool.last_args:
        now = datetime.now(timezone.utc)
        gap = now - pool.last_args[0]
        check(timedelta(days=2, hours=20) < gap < timedelta(days=3, hours=4),
              f"окно ~3 дня — та же граница, что reengagement_push (получено: {gap})")


async def test_happy_hour_trigger_schedules_dm_broadcast():
    """happy_hour_trigger должен ЗАПУСТИТЬ рассылку (не забыть про неё при
    рефакторинге) — проверяем реальным monkeypatch на модуле, не догадкой по
    исходнику."""
    class _FakeResp:
        status_code = 200
        text = "{}"

    class _FakeAsyncClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _FakeResp()

    calls = []
    async def _fake_broadcast(ctx):
        calls.append(ctx)
    async def _fake_reset(ctx, delay):
        pass
    orig_broadcast = bot._happy_hour_dm_broadcast
    orig_reset = bot._reset_happy_hour_after
    orig_client = bot.httpx.AsyncClient
    bot._happy_hour_dm_broadcast = _fake_broadcast
    bot._reset_happy_hour_after = _fake_reset
    bot.httpx.AsyncClient = _FakeAsyncClient
    try:
        ctx = make_ctx(Player(user_id=1))
        await bot.happy_hour_trigger(ctx)
        await asyncio.sleep(0)   # даём créate_task прокрутиться
        check(len(calls) == 1, "happy_hour_trigger реально планирует DM-рассылку")
    finally:
        bot._happy_hour_dm_broadcast = orig_broadcast
        bot._reset_happy_hour_after = orig_reset
        bot.httpx.AsyncClient = orig_client


# ── 10. Внутренние вызовы @cb-хендлеров без лишнего ctx ──────────────
async def test_no_double_ctx_callsites():
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bot.py"), encoding="utf-8").read()
    # @cb сам подставляет ctx третьим аргументом; передача его руками = TypeError,
    # который декоратор молча гасит в лог (экран просто не обновляется).
    cb_handlers = ("menu_handler", "daily_quest_hub", "skins_menu_handler",
                   "shop_callback", "pet_preview", "world_hub", "destiny_hub",
                   "progress_hub_handler", "claim_reward_handler", "guild_shrine_callback")
    bad = [h for h in cb_handlers
           if re.search(rf"await {h}\(update, context, ctx\)", src)]
    check(not bad, f"ни один @cb-хендлер не зовётся с лишним ctx (нарушители: {bad or '—'})")


async def main():
    print("\nРегресс по находкам аудита SLAYER\n" + "─" * 46)
    for fn in (test_lab_entry_survives_redis, test_lab_run_is_bounded,
               test_lab_room_stable_across_non_advancing_actions,
               test_mines_cashout, test_mines_survive_redis_roundtrip,
               test_guild_join_requires_start,
               test_altar_rerenders, test_cancel_named,
               test_war_score_per_action, test_temple_bonus_applies,
               test_donate_quest_progress_is_cumulative,
               test_named_blunt_name_persisted, test_dust_legendary_has_share_button,
               test_bot_added_to_chat_announces,
               test_flood_control_is_not_admin_noise,
               test_menu_handler_recovers_from_non_text_message,
               test_share_blunt_handler_recovers_from_photo_message,
               test_blunt_details_fallback_uses_edit_or_reply,
               test_profile_edits_from_text_deletes_from_photo,
               test_profile_avatar_edits_media_in_place,
               test_edit_or_reply_cleans_up_own_message_not_user_command,
               test_edit_or_send_photo_debounces_concurrent_edits,
               test_fmt_oac_thousands_separator,
               test_help_covers_mechanics_missing_from_rules,
               test_build_share_url_requires_url_param,
               test_share_buttons_pass_url_param,
               test_win_share_button_does_not_duplicate_link_in_text,
               test_query_answer_called_in_profile_craft_smoke,
               test_slow_renders_send_chat_action,
               test_luck_result_buttons_dont_claim_menu,
               test_query_answer_called_second_wave,
               test_send_whisper_dm_has_navigation,
               test_unregistered_stranger_gets_start_prompt,
               test_world_hub_fog_note_matches_locked_locations,
               test_altar_locked_shows_endgame_showcase,
               test_welcome_text_has_framing_without_extra_tap,
               test_onboarding_reward_has_reachable_referral_cta,
               test_referral_registration_defers_full_reward,
               test_referral_reward_fires_on_onboarding_completion,
               test_skip_onboarding_referral_reward_guarded_against_double_fire,
               test_happy_hour_dm_broadcast_reaches_candidates,
               test_happy_hour_dm_excludes_dormant_players,
               test_happy_hour_trigger_schedules_dm_broadcast,
               test_wheel_and_alchemy_have_suspense_before_reveal,
               test_medal_names_are_thematic_and_grammar_safe,
               test_profile_shows_streak_and_empty_state_hooks,
               test_craft_and_smoke_chain_to_next_quest_step,
               test_mines_screens_use_html_not_broken_markdown,
               test_mines_is_reachable_from_quest_and_marks_progress,
               test_no_double_ctx_callsites):
        print(f"\n{fn.__name__}:")
        await fn()
    total = PASSED + len(FAILED)
    print("\n" + "─" * 46)
    if FAILED:
        print(f"ПРОВАЛЕНО {len(FAILED)}/{total}:")
        for f in FAILED:
            print(f"  • {f}")
        return 1
    print(f"Регресс пройден: {PASSED}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
