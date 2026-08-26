"""Инварианты Жара и Забоя — накопителя механики «Дунуть».

Корневой дефект, который чинит Жар: 73% тяг не давали РОВНО НИЧЕГО. Игрок
делал главный жест игры и в трёх случаях из четырёх не получал никакого
сигнала. Жар двигается на КАЖДОЙ тяге, поэтому нулевая по OAC тяга остаётся
шагом к гарантированному призу.

Самая опасная сторона правки — экономика: крафт ничем не ограничен, поэтому
если цикл «скрутить блант → дунуть» станет прибыльным, игра получит
бесконечный принтер OAC. Главная проверка здесь — полная симуляция цикла
ВМЕСТЕ с Забоем и с блантами, которые Забой возвращает (они рекурсивно
удешевляют цикл, и алгеброй это легко посчитать неверно).

    python tests/smoke_heat_test.py
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TOKEN", "1")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://x:y@127.0.0.1:5432/z")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("RENDER_URL", "")
sys.path.insert(0, ROOT)

import bot  # noqa: E402
from config import GAME_CONFIG  # noqa: E402


def _simulate_full_cycle(n=400_000):
    """Полный цикл игрока, который только крафтит и курит.

    Возвращает (net_per_puff, hit_rate, cards, bursts, kinds). Бланты, выпавшие
    из Забоя, кладутся в свёрток и тратятся вместо крафта — иначе стоимость
    цикла была бы завышена, а вывод «сток» — ложно спокойным.
    """
    eff_blunt = GAME_CONFIG["craft_cost"] / 1.05  # 5% шанс второго при крафте
    dry = heat = stock = 0
    oac = 0.0
    bought = hits = cards = bursts = 0
    kinds = {}
    for _ in range(n):
        if stock > 0:
            stock -= 1
        else:
            bought += 1
        earned, outcome = bot.calculate_smoke_reward(None, False, dry_count=dry)
        oac += earned
        if earned > 0:
            hits += 1
        dry = dry + 1 if outcome == "neutral" else 0
        heat += 3 if outcome == "jackpot" else 1
        if heat >= bot.SMOKE_HEAT_MAX:
            heat = 0
            bursts += 1
            kind, amount = bot._roll_smoke_burst()
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == "oac":
                oac += amount
            elif kind == "blunts":
                stock += amount
            else:
                cards += 1
    return (oac - bought * eff_blunt) / n, hits / n, cards, bursts, kinds


def check_cycle_is_still_a_sink():
    """Цикл крафт→дунуть обязан остаться УБЫТОЧНЫМ вместе с Забоем.

    Крафт не ограничен ничем, кроме OAC. Если полный цикл выйдет в плюс,
    игрок сможет крутить его бесконечно и печатать валюту — обесценятся
    ранги, топ и все цели разом.
    """
    net, _hit, _c, _b, _k = _simulate_full_cycle()
    assert net < 0, (f"ПРИНТЕР OAC: полный цикл с Забоем даёт {net:+.2f} за тягу — "
                     f"крафт ничем не ограничен, это бесконечная эмиссия")
    # И при этом не должен быть настолько злым, как до правки (−5.6):
    # наказание за главный глагол игры — то, что мы и чинили.
    assert net > -4.5, f"цикл снова стал наказанием: {net:+.2f} за тягу"


def check_every_puff_advances_heat():
    """КАЖДАЯ тяга двигает Жар — в том числе пустая. Это и есть весь смысл:
    нулевая по OAC тяга перестаёт быть «ничем»."""
    heat = 0
    for _ in range(5000):
        before = heat
        _e, outcome = bot.calculate_smoke_reward(None, False, dry_count=0)
        heat += 3 if outcome == "jackpot" else 1
        assert heat > before, "тяга не сдвинула Жар — пустой исход снова стал пустым"
        if heat >= bot.SMOKE_HEAT_MAX:
            heat = 0


def check_burst_pool_is_well_formed():
    """Пул Забоя: веса дают ровно 1.0, все типы известны коду, ни один
    не потерян (дубль ключа/опечатка молча съели бы целый тип приза —
    ровно так когда-то умер весь хаб «Удача», см. crash_guard_test)."""
    total = sum(w for w, _k, _r in bot.SMOKE_BURST_POOL)
    assert abs(total - 1.0) < 1e-9, f"веса пула Забоя дают {total}, а не 1.0"
    known = {"oac", "blunts", "card"}
    for _w, kind, _r in bot.SMOKE_BURST_POOL:
        assert kind in known, f"неизвестный тип приза Забоя: {kind!r}"
    seen = set()
    for _ in range(20000):
        kind, amount = bot._roll_smoke_burst()
        seen.add(kind)
        assert kind in known
        if kind == "card":
            assert amount is None
        else:
            assert isinstance(amount, int) and amount > 0, f"{kind} вернул {amount!r}"
    assert seen == known, f"не все типы приза выпадают: {seen}"


def check_heat_bar_renders_honestly():
    """Шкала Жара всегда ровно SMOKE_HEAT_MAX символов и отражает реальное
    значение — витрина не должна врать о прогрессе (то же правило честности
    чисел, что и у шансов бланта)."""
    for heat in range(0, bot.SMOKE_HEAT_MAX + 1):
        bar = bot._smoke_heat_bar(heat)
        assert len(bar) == bot.SMOKE_HEAT_MAX * 2 or bar.count("🔥") == heat, bar
        assert bar.count("🔥") == heat, f"шкала показала {bar.count('🔥')} вместо {heat}"
        assert bar.count("▫️") == bot.SMOKE_HEAT_MAX - heat
    # Значения вне диапазона не должны ломать рендер
    assert bot._smoke_heat_bar(-5).count("🔥") == 0
    assert bot._smoke_heat_bar(999).count("🔥") == bot.SMOKE_HEAT_MAX


def check_collection_not_flooded():
    """Карты из Забоя — долгосрочный крючок, но легендарки обязаны остаться
    редкими: если дым начнёт печатать легендарки, обесценится вся коллекция
    (главный визуальный актив игры)."""
    _net, _hit, cards, _b, _k = _simulate_full_cycle()
    n = 400_000
    cards_per_puff = cards / n
    # при 30 тягах в день — сколько легендарок в месяц (2% редкости у карты)
    legend_per_month = cards_per_puff * 0.02 * 30 * 30
    assert legend_per_month < 3, (
        f"дым печатает {legend_per_month:.1f} легендарок/мес — коллекция обесценится")
    assert cards_per_puff > 0.01, "карты выпадают слишком редко, чтобы быть крючком"


def check_pick_no_repeat_edge_cases():
    """_pick_no_repeat не падает на вырожденных пулах (0/1 элемент)."""
    assert bot._pick_no_repeat([], "k", {}) is None
    assert bot._pick_no_repeat(["only"], "k", {}) == "only"
    progress = {"k": "only"}
    assert bot._pick_no_repeat(["only"], "k", progress) == "only"


def check_flavor_and_names_never_repeat_immediately():
    """Ни флейвор тяги, ни имя карты из Забоя не повторяются два раза подряд
    у одного игрока — точно тот же стимул подряд заметно слабее активирует
    чувствительные к новизне зоны, чем новый (repetition suppression:
    Grill-Spector, Henson & Martin, 2006), а на фиксированном пуле это
    ускоряет привыкание (Brickman & Campbell, 1971). Слой чисто
    декоративный — вероятность самого исхода не трогает (см.
    calculate_smoke_reward, не затронут этой правкой)."""
    for outcome, pool in bot.SMOKE_FLAVORS.items():
        assert len(pool) >= 2, f"корзина {outcome} слишком мала для анти-повтора"
        progress = {}
        prev_name = None
        seen = set()
        for _ in range(500):
            name, _text = bot._pick_smoke_flavor(outcome, progress)
            assert name != prev_name, f"флейвор {outcome} повторился подряд: {name}"
            seen.add(name)
            prev_name = name
        assert seen == {n for n, _t in pool}, f"{outcome}: не все флейворы достижимы, выпало {seen}"

    progress = {}
    prev = None
    seen = set()
    for _ in range(3000):
        name = bot._pick_no_repeat(bot.SMOKE_BURST_BLUNT_NAMES, "last_burst_name", progress)
        assert name != prev, f"имя карты Забоя повторилось подряд: {name}"
        seen.add(name)
        prev = name
    assert seen == set(bot.SMOKE_BURST_BLUNT_NAMES), f"не все имена карт Забоя достижимы: {seen}"


def main():
    random.seed(20260826)
    passed = []
    check_cycle_is_still_a_sink()
    passed.append("цикл крафт→дунуть остаётся стоком даже вместе с Забоем (нет принтера OAC)")
    check_every_puff_advances_heat()
    passed.append("каждая тяга двигает Жар — пустой исход перестал быть «ничем»")
    check_burst_pool_is_well_formed()
    passed.append("пул Забоя корректен: веса=1.0, все три типа приза реально выпадают")
    check_heat_bar_renders_honestly()
    passed.append("шкала Жара честна и не ломается на границах")
    check_collection_not_flooded()
    passed.append("карты из Забоя — крючок, но легендарки остаются редкими")
    check_pick_no_repeat_edge_cases()
    passed.append("_pick_no_repeat не падает на вырожденных пулах")
    check_flavor_and_names_never_repeat_immediately()
    passed.append("флейворы тяги и имена карт Забоя не повторяются подряд, все достижимы")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты Жара и Забоя пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
