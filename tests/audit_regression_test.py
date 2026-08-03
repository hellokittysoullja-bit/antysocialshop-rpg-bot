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

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        return self

    async def reply_text(self, text, **kw):
        self.edits.append(text)
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
        self.photos_sent = []
        self.media_edits = []

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append(text)
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


def make_ctx(player, redis=None, pool=None, war=None):
    return bot.AppContext(
        db_pool=pool or FakePool(), redis_client=redis, cache={},
        settings=bot.settings, repo=FakeRepo(player),
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
               test_mines_cashout, test_mines_survive_redis_roundtrip,
               test_guild_join_requires_start,
               test_altar_rerenders, test_cancel_named,
               test_war_score_per_action, test_temple_bonus_applies,
               test_named_blunt_name_persisted, test_bot_added_to_chat_announces,
               test_flood_control_is_not_admin_noise,
               test_menu_handler_recovers_from_non_text_message,
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
