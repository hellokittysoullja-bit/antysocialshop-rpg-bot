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
    _farm_on_cooldown, _quest_progress_counts, _plural_steps, QUEST_TEMPLATES,
    _resolve_referrer, _reward_referrer,
    _plant_rate, _plant_upgrade_cost, _plant_pending,
    _days_left_in_week, _war_rally_line,
    create_tables, _run_migrations, PlayerRepository, Player,
    PetService, GuildWarService, WarConfig, WarSettings, PET_CONFIG,
)
from datetime import datetime, timedelta

TEST_UID = 999002


def test_pure(passed):
    # --- дунуть: значение всегда в допустимом множестве (2000 прогонов) ---
    class _P:  # p не используется функцией, но передаём объект
        smoke_count = 0
    from bot import build_smoke_effect, SMOKE_FLAVORS
    outcomes_seen = set()
    for _ in range(4000):
        val, outcome = calculate_smoke_reward(_P(), happy_hour=False)
        outcomes_seen.add(outcome)
        assert outcome in SMOKE_FLAVORS, f"неизвестный исход {outcome}"
        # число обязано соответствовать исходу — витрина честна
        if outcome == "jackpot":
            assert 80 <= val <= 160, f"джекпот вернул {val}"
        elif outcome == "win":
            assert 15 <= val <= 40, f"выигрыш вернул {val}"
        elif outcome == "loss":
            assert val == -5, f"проигрыш вернул {val}"
        else:
            assert val == 0, f"пусто вернул {val}"
        # флейвор не падает и рендерит знак корректно
        assert build_smoke_effect(outcome, val)
    assert {"jackpot", "win", "loss", "neutral"} <= outcomes_seen, f"не все исходы: {outcomes_seen}"
    # с happy hour положительный доход удваивается
    for _ in range(2000):
        val, outcome = calculate_smoke_reward(_P(), happy_hour=True)
        if outcome == "win":
            assert 30 <= val <= 80, f"дунуть HH win вернул {val}"
        elif outcome == "jackpot":
            assert 160 <= val <= 320, f"дунуть HH jackpot вернул {val}"
    passed.append("calculate_smoke_reward: число всегда соответствует исходу (флейвор честен)")

    # --- стрик-награда: титулы детерминированы, доход не ниже базы×hot ---
    r5 = _calculate_reward(5, daily_config)
    assert r5.title is None and r5.total_oac >= int(30 * 1.1)
    r7 = _calculate_reward(7, daily_config)
    assert r7.title == "🕊️" and r7.total_oac >= int(50 * 1.1)
    r14 = _calculate_reward(14, daily_config)
    assert r14.title == "🔮" and r14.total_oac >= int(100 * 1.1)
    passed.append("_calculate_reward: титулы 7/14 и hot-streak множитель")

    # --- анти-фантом: любой предметный бонус стрика ложится на реальное поле
    #     Player (иначе награда показывается, но не начисляется — как focus/lives) ---
    _pf = set(Player.model_fields.keys())
    for _bt in daily_config.random_bonus_weights:
        if _bt == "extra_oac":
            continue
        _fld = daily_config.item_to_field.get(_bt)
        assert _fld and _fld in _pf, f"стрик-бонус '{_bt}' → поле '{_fld}' не существует в Player (фантом-награда)"
    # прогон: всё, что реально выпадает, применимо к Player
    import random as _rnd
    for _ in range(3000):
        for _fld in _calculate_reward(_rnd.randint(1, 14), daily_config).inventory_items:
            assert _fld in _pf, f"стрик выдал несуществующее поле {_fld}"
    passed.append("Стрик: все предметные бонусы ложатся на реальные поля Player (не фантом)")

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
    # плантация на пределе — высший приоритет (лосс-авёрсия на конкретном OAC)
    capped = _reengagement_text(fresh_farm, 5, morning.date().isoformat(), morning, cd,
                                passive_level=3, passive_collected=morning - timedelta(hours=20))
    assert capped and "Плантация" in capped and "OAC" in capped
    # плантация ещё не на пределе → не триггерит (нет ложного повода)
    not_capped = _reengagement_text(fresh_farm, 0, morning.date().isoformat(), morning, cd,
                                    passive_level=3, passive_collected=morning - timedelta(hours=1))
    assert not_capped is None
    passed.append("_reengagement_text: плантация/серия/фарм/пусто по приоритету")

    # --- обгон в рейтинге: соц-статус-триггер, приоритет ниже плантации/серии,
    #     выше созревшего фарма ---
    rival = _reengagement_text(fresh_farm, 0, morning.date().isoformat(), morning, cd,
                               rival_drop=(5, 8))
    assert rival and "#5" in rival and "#8" in rival and "обошли" in rival.lower()
    # плантация приоритетнее обгона
    both = _reengagement_text(fresh_farm, 0, morning.date().isoformat(), morning, cd,
                              passive_level=3, passive_collected=morning - timedelta(hours=20),
                              rival_drop=(5, 8))
    assert "Плантация" in both
    # серия приоритетнее обгона
    both2 = _reengagement_text(old_farm, 3, None, evening, cd, rival_drop=(5, 8))
    assert "серия" in both2.lower()
    # без обгона (rival_drop=None) — фолбэк на фарм, как раньше
    assert _reengagement_text(old_farm_morning, 0, None, morning, cd, rival_drop=None) and \
           "Грядка" in _reengagement_text(old_farm_morning, 0, None, morning, cd, rival_drop=None)
    passed.append("_reengagement_text: обгон в рейтинге — приоритет и текст корректны")

    # --- грейс-фарм: первые фармы без кулдауна, потом кулдаун действует ---
    now2 = datetime(2026, 1, 1, 12, 0)
    recent = now2 - timedelta(minutes=5)    # недавно (в пределах 30 мин)
    long_ago = now2 - timedelta(minutes=40)  # давно (за пределами 30 мин)
    assert _farm_on_cooldown(0, recent, now2) is False   # грейс
    assert _farm_on_cooldown(4, recent, now2) is False   # грейс (последний бесплатный)
    assert _farm_on_cooldown(5, recent, now2) is True    # грейс кончился → кулдаун
    assert _farm_on_cooldown(10, long_ago, now2) is False  # кулдаун прошёл
    assert _farm_on_cooldown(10, None, now2) is False      # ни разу не фармил
    passed.append("_farm_on_cooldown: грейс + кулдаун")

    # --- подсчёт прогресса квеста (условия видимости заданий) ---
    ch1 = QUEST_TEMPLATES["chapter1"]
    prog = {"farm": True, "craft": True}
    # BLACK: farm,craft,smoke,ritual видимы (repent/pet отфильтрованы) → 2/4
    assert _quest_progress_counts(ch1, prog, "BLACK", False, False) == (2, 4)
    assert _quest_progress_counts(ch1, prog, "WHITE", False, False) == (2, 4)
    assert _quest_progress_counts(None, {}, "BLACK", False, False) == (0, 0)
    passed.append("_quest_progress_counts: фильтрация по условиям")

    # --- русское склонение «шаг» ---
    assert (_plural_steps(1), _plural_steps(2), _plural_steps(5),
            _plural_steps(11), _plural_steps(21)) == ("шаг", "шага", "шагов", "шагов", "шаг")
    passed.append("_plural_steps: склонение 1/2/5/11/21")

    # --- резолвер реферала: создатель зашит в blunt-ссылке (O(1), без БД) ---
    assert _resolve_referrer(["blunt_blunt_12345_1700000000_4321"], 999) == 12345
    assert _resolve_referrer(["blunt_blunt_999_1_2"], 999) is None   # сам себя
    assert _resolve_referrer(["b_abc"], 999) is None                 # чужой префикс
    assert _resolve_referrer(["blunt_garbage"], 999) is None         # мусор
    assert _resolve_referrer([], 999) is None
    passed.append("_resolve_referrer: парсинг создателя из ссылки")

    # --- Плантация: ставка / стоимость апгрейда / накопление с лимитом ---
    assert (_plant_rate(1), _plant_rate(5)) == (25, 125)
    assert (_plant_upgrade_cost(1), _plant_upgrade_cost(2)) == (600, 1350)
    pn = datetime(2026, 1, 1, 12, 0)
    assert _plant_pending(1, pn - timedelta(hours=4), pn) == (100, 4.0, False)
    e_cap, _h, capped = _plant_pending(1, pn - timedelta(hours=20), pn)
    assert e_cap == 200 and capped is True     # лимит 8ч × 25 OAC/ч
    assert _plant_pending(0, pn - timedelta(hours=4), pn) == (0, 0.0, False)  # не посажено
    passed.append("Плантация: rate/cost/pending + лимит накопления")

    # --- Прилавок: скидка ранга, цена, ротация витрины, таймер ---
    from bot import (_shop_discount_pct, _shop_price, _shop_today,
                     _shop_time_left, _build_shop_view, SHOP_ITEMS)
    assert (_shop_discount_pct(0), _shop_discount_pct(4999)) == (0, 0)
    assert (_shop_discount_pct(5000), _shop_discount_pct(20000),
            _shop_discount_pct(50000)) == (5, 10, 15)
    assert _shop_price(100, 0) == 100 and _shop_price(100, 10) == 90
    assert _shop_price(1, 15) >= 1                       # цена никогда не 0
    for field in {it["field"] for it in SHOP_ITEMS.values()}:
        assert field in Player.model_fields, f"товар пишет в несуществующее поле {field}"
    ordn = datetime(2026, 1, 1).toordinal()
    today = _shop_today(ordn)
    assert len(today) == 3 and len(set(today)) == 3      # 3 различных товара
    assert _shop_today(ordn) != _shop_today(ordn + 1)    # витрина сменилась назавтра
    h, m = _shop_time_left(datetime(2026, 1, 1, 23, 30))
    assert (h, m) == (0, 30)                             # до полуночи
    txt, kb = _build_shop_view(6000, datetime(2026, 1, 1, 12, 0))
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert "privilege" in cbs and "catalog" in cbs and "menu" in cbs  # мост в реальный магазин сохранён
    assert any(c and c.startswith("shop_buy_") for c in cbs)
    passed.append("Прилавок: скидка/цена/ротация/таймер + мост в магазин цел")

    # --- Возвышение: карточка ранг-апа честна и не дублирует имя ---
    from bot import _build_ascension_card
    from game_content import RANKS, RANK_LORE
    card = _build_ascension_card("🪦 Призрак", 20010)
    assert "ПРИЗРАК" in card and "В О З В Ы Ш Е Н И Е" in card
    assert RANK_LORE["🪦 Призрак"]["line"] in card
    assert "🪬 Некромант — ещё 29990 OAC" in card   # следующая ступень, без дубля имени
    assert "Призрак Призрак" not in card            # регрессия на дубль
    top = _build_ascension_card("🪬 Некромант", 50010)
    assert "вершине" in top                          # у топ-ранга нет «дальше»
    passed.append("Возвышение: карточка ранга честна, goal-gradient, без дублей")

    # --- Час Удачи: баннер с отсчётом только когда активен ---
    from bot import _happy_hour_banner
    class _Ctx:
        def __init__(self, cache): self.cache = cache
    n = datetime(2026, 1, 1, 12, 0)
    on = _happy_hour_banner(_Ctx({"happy_hour": True, "happy_hour_end": n + timedelta(minutes=18)}), n)
    assert "ЧАС УДАЧИ" in on and "18м" in on
    assert _happy_hour_banner(_Ctx({"happy_hour": False}), n) == ""
    assert _happy_hour_banner(None, n) == ""                    # fail-closed без ctx
    assert "ЧАС УДАЧИ" in _happy_hour_banner(_Ctx({"happy_hour": True}), n)  # без end не падает
    passed.append("Час Удачи: баннер FOMO с отсчётом, fail-closed")

    # --- Персистентность: каждое поле Player пишется в БД (ловит «поле живёт
    #     только в кэше» — так терялись onboarding_step/pet_hunger/repent_count) ---
    from repository import PLAYER_COLUMNS
    # Поля, которые намеренно НЕ хранятся отдельной колонкой (вычисляемые/служебные).
    NON_PERSISTED = set()
    unpersisted = set(Player.model_fields) - set(PLAYER_COLUMNS) - NON_PERSISTED
    assert not unpersisted, f"поля Player не сохраняются в БД (потеряются при сбросе кэша): {unpersisted}"
    passed.append(f"Персистентность: все {len(Player.model_fields)} полей Player пишутся в БД")

    # --- Кодекс блантов: визитка, метр редкостей, аспирация, титул ---
    from bot import _build_codex_header, _codex_prestige_title
    coll = [
        {"rarity": "epic", "name": "Пепел Короля", "rare_number": "E-1"},
        {"rarity": "rare", "name": "Шёпот", "rare_number": "R-1"},
        {"rarity": "common", "name": "Дым", "rare_number": "C-1"},
    ]
    ro = {"legendary": 0, "epic": 1, "rare": 2, "common": 3}
    coll.sort(key=lambda x: (ro[x["rarity"]], 999999))
    h = _build_codex_header(coll)
    assert "КОДЕКС" in h and "Твоя визитка" in h
    assert "Пепел Короля" in h                    # редчайший = визитка (epic > rare > common)
    assert "3/4" in h                             # 3 из 4 редкостей
    assert "🔒" in h and "Легендарного пока нет" in h   # незакрытый слот + аспирация
    # с легендаркой аспирация про легендарку исчезает
    coll2 = coll + [{"rarity": "legendary", "name": "Бездна", "rare_number": "L-9"}]
    coll2.sort(key=lambda x: (ro[x["rarity"]], 999999))
    h2 = _build_codex_header(coll2)
    assert "Бездна" in h2 and "Легендарного пока нет" not in h2 and "4/4" in h2
    assert _codex_prestige_title([]) == "🕳️ Пустая витрина"
    assert "Владыка" in _codex_prestige_title([{"rarity": "legendary"}] * 16)
    passed.append("Кодекс блантов: визитка/метр/аспирация/титул честны")

    # --- Виральность: блант из реф-ссылки для тёплого приветствия ---
    from bot import _shared_blunt_info
    ref = Player(user_id=100, username="korol", exists=True, inventory=[
        {"id": "blunt_100_1700_42", "type": "named", "name": "Пепел Короля", "rarity": "legendary"},
    ])
    info = _shared_blunt_info(ref, ["blunt_blunt_100_1700_42"])
    assert info == {"name": "Пепел Короля", "rarity": "legendary"}
    assert _shared_blunt_info(ref, ["blunt_blunt_100_9999_00"]) is None   # не тот блант
    assert _shared_blunt_info(ref, []) is None                            # нет аргумента
    assert _shared_blunt_info(None, ["blunt_x"]) is None                  # нет реферера
    passed.append("Виральность: блант из реф-ссылки для приветствия приглашённого")

    # --- Война гильдий: дней до итогов + мотивационная строка (долг/соревнование) ---
    assert _days_left_in_week(datetime(2024, 1, 1)) == 7   # понедельник
    assert _days_left_in_week(datetime(2024, 1, 7)) == 1   # воскресенье
    assert "ОТСТАЁТ" in _war_rally_line("BLACK", 100, 300)   # моя гильдия позади
    assert "ВЕДЁТ" in _war_rally_line("BLACK", 300, 100)     # моя гильдия впереди
    assert "Ноздря" in _war_rally_line("WHITE", 200, 200)    # поровну
    assert "Выбери гильдию" in _war_rally_line(None, 0, 0)   # без гильдии
    passed.append("Война гильдий: дни недели + рэлли-строка")


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

    # --- _reward_referrer: реферер получает +50 OAC, счётчик, метку 🩸 ---
    REF_UID = 999003
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE user_id=$1", REF_UID)
    await repo.save(Player(user_id=REF_UID, username="Referrer", balance=1000, exists=True))

    class _NoNetBot:
        async def send_message(self, *a, **k):
            raise RuntimeError("no network in test")
        async def send_photo(self, *a, **k):
            raise RuntimeError("no network in test")

    class _RefContext:
        def __init__(self):
            self.bot = _NoNetBot()
            self.bot_data = {}
            self.user_data = {}

    class _RefCtx:
        def __init__(self, repo):
            self.repo = repo

    before = await repo.get_by_id(REF_UID)
    await _reward_referrer(_RefCtx(repo), _RefContext(), REF_UID)
    after = await repo.get_by_id(REF_UID)
    assert after.balance == before.balance + 50, f"баланс {before.balance}->{after.balance}"
    assert after.referral_count == 1 and "🩸" in (after.titles or "")
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE user_id=$1", REF_UID)
    passed.append("_reward_referrer: +50 OAC + счётчик + метка 🩸")

    # --- Плантация: passive_level/passive_collected round-trip через БД + расчёт ---
    PLANT_UID = 999004
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE user_id=$1", PLANT_UID)
    plnt = Player(user_id=PLANT_UID, username="Planter", balance=1000, exists=True)
    plnt.passive_level = 2
    plnt.passive_collected = datetime.now() - timedelta(hours=4)
    await repo.save(plnt)
    reloaded_p = await repo.get_by_id(PLANT_UID)
    assert reloaded_p.passive_level == 2
    earned_p, _hh, _cc = _plant_pending(reloaded_p.passive_level, reloaded_p.passive_collected, datetime.now())
    assert 190 <= earned_p <= 210, f"урожай ур.2 за 4ч ≈ 200, получено {earned_p}"
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE user_id=$1", PLANT_UID)
    passed.append("Плантация: round-trip полей + расчёт урожая на БД")

    # --- Мины работают и без Redis (in-memory фолбэк) — «Рискнуть» не молчит ---
    from bot import _mines_state_get, _mines_state_set
    from types import SimpleNamespace
    st = {"field": [[0] * 5 for _ in range(5)], "mines": [[0, 1]], "bet": 50, "step": 0, "status": "playing"}
    ctx_nr = SimpleNamespace(redis=None, cache=TTLCache(maxsize=20, ttl=600))
    assert await _mines_state_get(ctx_nr, 777) is None            # пусто → None (а не крах)
    await _mines_state_set(ctx_nr, 777, st)
    assert await _mines_state_get(ctx_nr, 777) == st              # round-trip без Redis
    ctx_r = SimpleNamespace(redis=redis_client, cache=TTLCache(maxsize=20, ttl=600))
    await redis_client.delete("mines_game:778")
    await _mines_state_set(ctx_r, 778, st)
    assert await _mines_state_get(ctx_r, 778) == st              # round-trip с Redis
    await redis_client.delete("mines_game:778")
    passed.append("Мины: состояние round-trip с Redis и без (in-memory фолбэк)")


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
