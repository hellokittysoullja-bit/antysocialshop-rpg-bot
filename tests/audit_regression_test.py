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

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append(text)
        return FakeMsg()

    async def edit_message_text(self, **kw):
        return FakeMsg()

    async def get_me(self):
        return types.SimpleNamespace(username="testbot")


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
    check("10000 OAC" in last, "перерисовка показывает СВЕЖИЙ престиж, а не прежний")


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
