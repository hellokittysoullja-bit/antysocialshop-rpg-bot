"""Тесты на реальных Postgres + Redis для трёх находок:

  1) nft_registry активирован — /check и достижение check_10 работают.
  2) total_earned на Минах учитывает НЕТТО-прибыль, а не всю выплату.
  3) Дарение бланта: атомарный перевод, числовой ID, очередь для тех, кого
     нет в игре, автопередача при их /start, устойчивость к смене @username.

Запуск (нужны запущенные Postgres и Redis):

    export DATABASE_URL_AIVEN="postgresql://botuser@127.0.0.1:5432/botdb"
    export REDIS_URL="redis://127.0.0.1:6379/0"
    python tests/gift_nft_earned_test.py
"""
import os
import sys
import types
import asyncio
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TOKEN", "123:DUMMY")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://botuser@127.0.0.1:5432/botdb")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("RENDER_URL", "")

logging.disable(logging.CRITICAL)

import asyncpg
import redis.asyncio as aioredis
from cachetools import TTLCache

import bot
from bot import (
    create_tables, _run_migrations, PlayerRepository, Player, AppContext,
    create_named_blunt, check_blunt, handle_gift_username, gift_blunt_start,
    _refresh_username, _claim_pending_gifts, _transfer_named_item,
)
from services import GuildWarService, WarConfig, WarSettings, PetService

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


# ── Минимальные двойники Telegram, привязанные к реальной БД/Redis ──────
class FakeMsg:
    def __init__(self, chat_id=1):
        self.chat = types.SimpleNamespace(id=chat_id)
        self.message_id = 1
        self.texts = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.texts.append(text)
        return self

    async def delete(self):
        pass


class FakeUser:
    def __init__(self, uid, username=None, first_name="s"):
        self.id = uid
        self.username = username
        self.first_name = first_name


class FakeMessage(FakeMsg):
    def __init__(self, uid, text, username=None):
        super().__init__(chat_id=uid)
        self.text = text
        self.from_user = FakeUser(uid, username)


class FakeUpdate:
    _n = 5000

    def __init__(self, uid, text=None, username=None):
        FakeUpdate._n += 1
        self.update_id = FakeUpdate._n
        self.effective_user = FakeUser(uid, username)
        self.effective_chat = types.SimpleNamespace(id=uid)
        # effective_message нужен ВСЕГДА (даже для /start без явного текста —
        # start() шлёт ответ именно через него), только .message (кнопка/текст
        # конкретной команды дарения) — опционален.
        self.effective_message = FakeMessage(uid, text or "/start", username)
        self.message = self.effective_message if text is not None else None
        self.callback_query = None


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append((chat_id, text))
        return FakeMsg()

    async def get_me(self):
        return types.SimpleNamespace(username="testbot")


class FakeContext:
    def __init__(self, ctx, args=None):
        self.bot_data = {"ctx": ctx}
        self.user_data = {}
        self.bot = FakeBot()
        self.args = args or []
        self.application = types.SimpleNamespace(bot_data=self.bot_data)


async def make_real_ctx(pool, redis_client):
    cache = TTLCache(maxsize=2000, ttl=600)
    repo = PlayerRepository(pool, redis_client, cache)
    war = GuildWarService(pool, redis_client, WarConfig(), WarSettings())
    pet = PetService(repo, {"dog": {"name": "🐕 Песик", "price": 3000, "max_name_len": 15}})
    return AppContext(db_pool=pool, redis_client=redis_client, cache=cache,
                      settings=bot.settings, repo=repo, war_service=war,
                      pet_service=pet, achievement_service=None)


async def make_player(ctx, uid, username="", balance=1000, guild=None):
    p = Player(user_id=uid, username=username, balance=balance,
              total_earned=balance, guild=guild, exists=True)
    await ctx.repo.save(p)
    return p


# ═══════════════════ 1. nft_registry активирован ═══════════════════════
async def test_nft_registry(pool, redis_client):
    ctx = await make_real_ctx(pool, redis_client)
    await make_player(ctx, 101, "creator", balance=1000)

    item = await create_named_blunt(101, "Тестовый Блант", rarity="rare", ctx=ctx)
    check(item.get("serial") is not None, "create_named_blunt проставил serial из реестра")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT created_by, rarity FROM nft_registry WHERE blunt_id=$1", item["id"])
    check(row is not None and row["created_by"] == 101,
          "блант реально попал в nft_registry с верным владельцем")

    # /check теперь должен находить его, а не падать на JSONB LIKE
    u = FakeUpdate(101, text=f"/check {item['rare_number']}")
    c = FakeContext(ctx, args=[item["rare_number"]])
    await check_blunt(u, c)
    reply = u.message.texts[-1] if u.message.texts else ""
    check("ДЕТАЛИ NFT БЛАНТА" in reply, "/check находит зарегистрированный блант (JSONB-запрос не падает)")

    player_after = await ctx.repo.get_by_id(101)
    check(player_after.check_count == 1, "check_count инкрементируется атомарно")

    # Коллизия rare_number должна разрешаться перегенерацией, а не падением
    item2 = await create_named_blunt(101, "Второй", rarity="rare", ctx=ctx)
    check(item2["rare_number"] != item["rare_number"] or item2["serial"] != item["serial"],
          "второй блант получает уникальную запись даже при потенциальной коллизии номера")


# ═══════════════ 2. total_earned на Минах = нетто-прибыль ═══════════════
async def test_mines_net_profit(pool, redis_client):
    ctx = await make_real_ctx(pool, redis_client)
    p = await make_player(ctx, 202, "miner", balance=10000)

    bet = 1000
    async def _deduct(pl, conn):
        pl.balance -= bet
    await ctx.repo.atomic_update(202, _deduct)
    after_bet = await ctx.repo.get_by_id(202)
    check(after_bet.total_earned == 10000,
          "ставка (убыток) НЕ прибавляет total_earned (уже верно и до правки)")

    multiplier = 1.09  # шаг 1 из 22
    win = int(bet * multiplier)

    async def _cash(pl, conn):
        pl.balance += win
        pl.total_earned = max(0, (pl.total_earned or 0) - bet)
        return pl.balance
    await ctx.repo.atomic_update(202, _cash)

    final = await ctx.repo.get_by_id(202)
    expected_earned = 10000 + (win - bet)
    check(final.total_earned == expected_earned,
          f"total_earned вырос НА (win-bet)={win-bet}, а не на весь win={win} "
          f"(итог {final.total_earned}, ожидалось {expected_earned})")
    check(final.total_earned >= final.balance,
          "инвариант total_earned ≥ balance не нарушен полом в repository.save()")
    check(final.balance == 10000 - bet + win, "баланс изменился ровно на (win-bet) за раунд")


async def test_mines_full_clear_net_profit(pool, redis_client):
    """Тот же фикс, но по ветке 'won' (все 22 клетки) в _mines_open_cell."""
    ctx = await make_real_ctx(pool, redis_client)
    await make_player(ctx, 203, "clearer", balance=5000)

    bet = 500
    async def _deduct(pl, conn):
        pl.balance -= bet
    await ctx.repo.atomic_update(203, _deduct)

    multiplier = 3.0
    win = int(bet * multiplier)

    async def _win(pl, conn):
        pl.balance += win
        pl.total_earned = max(0, (pl.total_earned or 0) - bet)
        return pl.balance
    await ctx.repo.atomic_update(203, _win)

    final = await ctx.repo.get_by_id(203)
    check(final.total_earned == 5000 + (win - bet),
          "полная очистка поля тоже засчитывает только net-прибыль")


# ═══════════════════════ 3. Дарение блантов ═════════════════════════════
async def test_gift_between_existing_players(pool, redis_client):
    ctx = await make_real_ctx(pool, redis_client)
    giver = await make_player(ctx, 301, "giver301", balance=1000)
    await make_player(ctx, 302, "receiver302", balance=1000)

    item = await create_named_blunt(301, "Дарёный", rarity="common", ctx=ctx, player=giver)
    await ctx.repo.save(giver)

    status, moved = await _transfer_named_item(ctx, 301, 302, item["id"])
    check(status == "ok", "прямой перевод между двумя существующими игроками проходит")

    g = await ctx.repo.get_by_id(301)
    r = await ctx.repo.get_by_id(302)
    check(not any(it["id"] == item["id"] for it in g.inventory),
          "предмет исчез у дарителя")
    check(any(it["id"] == item["id"] for it in r.inventory),
          "предмет появился у получателя — и ровно один раз")
    check(sum(1 for it in r.inventory if it["id"] == item["id"]) == 1,
          "предмет не задублировался")


async def test_gift_by_numeric_id(pool, redis_client):
    """Раньше промпт обещал числовой ID, а обработчик его не поддерживал —
    ввод чистой цифры ВСЕГДА получал «игрок не найден»."""
    ctx = await make_real_ctx(pool, redis_client)
    giver = await make_player(ctx, 401, "giver401", balance=1000)
    await make_player(ctx, 402, "somebody", balance=1000)  # без username-совпадения

    item = await create_named_blunt(401, "ПоID", rarity="common", ctx=ctx, player=giver)
    await ctx.repo.save(giver)

    u = FakeUpdate(401, text="402")
    c = FakeContext(ctx)
    c.user_data["gifting_blunt_id"] = item["id"]
    await handle_gift_username(u, c)
    reply = u.message.texts[-1]
    check("подарен" in reply.lower(), f"числовой ID теперь реально передаёт предмет (ответ: {reply!r})")

    r = await ctx.repo.get_by_id(402)
    check(any(it["id"] == item["id"] for it in r.inventory), "предмет дошёл до игрока по числовому ID")


async def test_gift_queues_for_unknown_username(pool, redis_client):
    """Получателя нет в БД → не отказ, а очередь (см. pending_gifts)."""
    ctx = await make_real_ctx(pool, redis_client)
    giver = await make_player(ctx, 501, "giver501", balance=1000)
    item = await create_named_blunt(501, "ВОчереди", rarity="common", ctx=ctx, player=giver)
    await ctx.repo.save(giver)

    u = FakeUpdate(501, text="brandnew_stranger")
    c = FakeContext(ctx)
    c.user_data["gifting_blunt_id"] = item["id"]
    await handle_gift_username(u, c)
    reply = u.message.texts[-1]
    check("будет ждать" in reply, f"незнакомый username не отклоняется — ставится в очередь (ответ: {reply!r})")

    g = await ctx.repo.get_by_id(501)
    check(not any(it["id"] == item["id"] for it in g.inventory),
          "предмет сразу изъят у дарителя (не может подарить его снова, пока ждёт очередь)")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username_lower, from_user_id FROM pending_gifts WHERE username_lower=$1",
            "brandnew_stranger")
    check(row is not None and row["from_user_id"] == 501, "запись легла в pending_gifts")


async def test_gift_delivered_on_new_player_start(pool, redis_client):
    """Новый игрок регистрируется под тем самым username → подарок находит
    его АВТОМАТИЧЕСКИ на первом /start, без участия дарителя."""
    ctx = await make_real_ctx(pool, redis_client)
    giver = await make_player(ctx, 601, "giver601", balance=1000)
    item = await create_named_blunt(601, "ЖдущийНового", rarity="rare", ctx=ctx, player=giver)
    await ctx.repo.save(giver)

    u = FakeUpdate(601, text="future_newbie")
    c = FakeContext(ctx)
    c.user_data["gifting_blunt_id"] = item["id"]
    await handle_gift_username(u, c)

    # Новый человек с ЭТИМ username пишет /start
    u2 = FakeUpdate(602, username="future_newbie")
    c2 = FakeContext(ctx)
    await bot.start(u2, c2)

    newbie = await ctx.repo.get_by_id(602)
    check(any(it["id"] == item["id"] for it in newbie.inventory),
          "подарок автоматически оказался в инвентаре новичка при первом /start")

    async with pool.acquire() as conn:
        cnt = await conn.fetchval("SELECT COUNT(*) FROM pending_gifts WHERE username_lower='future_newbie'")
    check(cnt == 0, "запись из очереди удалена после выдачи (не выдастся повторно)")


async def test_username_change_resolved_via_refresh(pool, redis_client):
    """Игрок меняет @username → старое дарение по НОВОМУ нику должно
    находить его при следующем /start (а не только при регистрации)."""
    ctx = await make_real_ctx(pool, redis_client)
    await make_player(ctx, 701, "old_nick", balance=1000)

    # Игрок сменил ник в Telegram и просто снова написал /start.
    u = FakeUpdate(701, username="new_nick")
    c = FakeContext(ctx)
    await bot.start(u, c)

    async with pool.acquire() as conn:
        uname = await conn.fetchval("SELECT username FROM players WHERE user_id=701")
    check(uname == "new_nick", "players.username обновился на текущий Telegram-ник при обычном /start")

    giver = await make_player(ctx, 702, "giver702", balance=1000)
    item = await create_named_blunt(702, "ПоНовомуНику", rarity="common", ctx=ctx, player=giver)
    await ctx.repo.save(giver)

    u3 = FakeUpdate(702, text="new_nick")
    c3 = FakeContext(ctx)
    c3.user_data["gifting_blunt_id"] = item["id"]
    await handle_gift_username(u3, c3)
    reply = u3.message.texts[-1]
    check("подарен" in reply.lower(),
          f"дарение по АКТУАЛЬНОМУ username находит игрока сразу (ответ: {reply!r})")


async def test_gift_rejects_garbage_username(pool, redis_client):
    """Опечатка/мусор не должна тихо создавать мёртвую запись в очереди."""
    ctx = await make_real_ctx(pool, redis_client)
    giver = await make_player(ctx, 801, "giver801", balance=1000)
    item = await create_named_blunt(801, "Тест", rarity="common", ctx=ctx, player=giver)
    await ctx.repo.save(giver)

    u = FakeUpdate(801, text="а")  # 1 символ, не валидный username
    c = FakeContext(ctx)
    c.user_data["gifting_blunt_id"] = item["id"]
    await handle_gift_username(u, c)
    reply = u.message.texts[-1]
    check("не похоже" in reply, f"явный мусор отклоняется с понятным сообщением (ответ: {reply!r})")

    async with pool.acquire() as conn:
        cnt = await conn.fetchval("SELECT COUNT(*) FROM pending_gifts WHERE username_lower='а'")
    check(cnt == 0, "мусорный ввод не создал запись в очереди")
    g = await ctx.repo.get_by_id(801)
    check(any(it["id"] == item["id"] for it in g.inventory),
          "предмет остался у дарителя после отклонённой попытки")


async def main():
    dsn = os.environ["DATABASE_URL_AIVEN"].replace("+asyncpg", "")
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    redis_client = await aioredis.from_url(os.environ["REDIS_URL"])
    await redis_client.flushdb()

    async with pool.acquire() as conn:
        await create_tables(conn)
        await _run_migrations(conn)

    print("\nnft_registry / /check / достижение:\n" + "─" * 46)
    await test_nft_registry(pool, redis_client)

    print("\ntotal_earned на Минах — net-прибыль:\n" + "─" * 46)
    await test_mines_net_profit(pool, redis_client)
    await test_mines_full_clear_net_profit(pool, redis_client)

    print("\nДарение блантов:\n" + "─" * 46)
    await test_gift_between_existing_players(pool, redis_client)
    await test_gift_by_numeric_id(pool, redis_client)
    await test_gift_queues_for_unknown_username(pool, redis_client)
    await test_gift_delivered_on_new_player_start(pool, redis_client)
    await test_username_change_resolved_via_refresh(pool, redis_client)
    await test_gift_rejects_garbage_username(pool, redis_client)

    await pool.close()
    await redis_client.aclose()

    total = PASSED + len(FAILED)
    print("\n" + "─" * 46)
    if FAILED:
        print(f"ПРОВАЛЕНО {len(FAILED)}/{total}:")
        for f in FAILED:
            print(f"  • {f}")
        return 1
    print(f"Тесты пройдены: {PASSED}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
