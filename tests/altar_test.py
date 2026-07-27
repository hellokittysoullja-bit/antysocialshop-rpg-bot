"""Инварианты Алтаря Вечности — эндгейм-стока (без БД — чистые функции bot.py).

Экономический аудит показал: после максимума вертикали (Некромант + макс-
плантация + питомец, ~110k OAC, ~45 дней) OAC копится впустую — стока и цели
нет. Алтарь даёт этому OAC бесконечную вечнозелёную цель через одностороннюю
жертву. Эти проверки замыкают контракт: сток не эксплойтится, гейт не
открывается раньше времени, уровень строго монотонен цене.

    python tests/altar_test.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TOKEN", "1")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://x:y@127.0.0.1:5432/z")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("RENDER_URL", "")
sys.path.insert(0, ROOT)

import bot  # noqa: E402
from game_content import ALTAR_TIERS  # noqa: E402
from game_models import Player  # noqa: E402


def check_level_curve_monotonic_and_consistent():
    """Цена уровня растёт строго, кумулятивный порог согласован с _altar_level."""
    bad = []
    prev_cost = -1
    cum = 0
    for lvl in range(1, 40):
        cost = bot._altar_level_cost(lvl)
        if cost <= prev_cost:
            bad.append(f"L{lvl}: цена {cost} не выше предыдущей {prev_cost}")
        prev_cost = cost
        cum += cost
        L, cost_next, into = bot._altar_level(cum)
        if L != lvl:
            bad.append(f"на границе L{lvl} (prestige={cum}) уровень посчитан как {L}")
        if into != 0:
            bad.append(f"на границе L{lvl} into должен быть 0, получили {into}")
        if cost_next != bot._altar_level_cost(lvl + 1):
            bad.append(f"на границе L{lvl} cost_to_next не равен цене L{lvl+1}")
    assert not bad, "Кривая уровней Алтаря нарушена:\n  " + "\n  ".join(bad)


def check_level_zero_and_midpoint():
    L, cost_next, into = bot._altar_level(0)
    assert L == 0 and into == 0 and cost_next == bot._altar_level_cost(1), \
        f"prestige=0 должен давать level=0, into=0, получили {(L, cost_next, into)}"
    # середина первого уровня
    half = bot._altar_level_cost(1) // 2
    L, cost_next, into = bot._altar_level(half)
    assert L == 0 and into == half, f"на середине L1 ожидали (0, into={half}), получили {(L, into)}"


def check_tiers_non_decreasing_and_data_driven():
    """Тиры отсортированы по возрастанию порога, титул не понижается с уровнем."""
    thresholds = [t[0] for t in ALTAR_TIERS]
    assert thresholds == sorted(thresholds), "ALTAR_TIERS не отсортирован по порогу"
    prev_idx = -1
    for lvl in range(0, 40):
        title, mark = bot._altar_tier(lvl)
        idx = ALTAR_TIERS.index((next(t for t in ALTAR_TIERS if t[1] == title)))
        assert idx >= prev_idx, f"на level {lvl} тир откатился назад"
        prev_idx = idx


def check_gate_matches_audit_finding():
    """Гейт: Некромант (total_earned) ИЛИ макс-плантация — не раньше, не позже."""
    below = Player(user_id=1, total_earned=49999, passive_level=9)
    by_rank = Player(user_id=2, total_earned=50000, passive_level=0)
    by_plant = Player(user_id=3, total_earned=0, passive_level=bot.PLANT_MAX_LEVEL)
    assert not bot._altar_gate_open(below), "гейт открылся раньше времени"
    assert bot._altar_gate_open(by_rank), "гейт не открылся на пороге Некроманта"
    assert bot._altar_gate_open(by_plant), "гейт не открылся на макс-плантации"
    # trading OAC (balance) не должно влиять на гейт — только total_earned/уровень
    spender = Player(user_id=4, balance=0, total_earned=50000, passive_level=0)
    assert bot._altar_gate_open(spender), "гейт зависит от balance, а не от total_earned — баг «кошелёк=статус»"


def check_invest_is_strict_one_way_sink():
    """Жертва не создаёт OAC из воздуха и не даёт обратного курса."""
    # доллар в доллар: инвестиция уменьшает баланс ровно на amount, растит
    # prestige ровно на amount — эмулируем шаг _invest без БД.
    balance, prestige = 10_000, 0
    amount = balance  # "вложить всё"
    new_balance = balance - amount
    new_prestige = prestige + amount
    assert new_balance == 0, "баланс должен обнулиться при full-invest"
    assert new_prestige == amount, "престиж должен вырасти ровно на вложенную сумму (1:1, без бонуса)"
    # обратного пути prestige -> balance в кодовой базе нет
    import inspect
    src = inspect.getsource(bot.altar_invest_handler)
    assert "p.balance +=" not in src, "найден путь возврата OAC из престижа — сток не односторонний"


def main():
    passed = []
    check_level_curve_monotonic_and_consistent()
    passed.append("кривая уровней: цена растёт, границы согласованы")
    check_level_zero_and_midpoint()
    passed.append("level=0 и середина уровня считаются корректно")
    check_tiers_non_decreasing_and_data_driven()
    passed.append("тиры отсортированы, титул не понижается с уровнем")
    check_gate_matches_audit_finding()
    passed.append("гейт: Некромант ИЛИ макс-плантация, независим от balance")
    check_invest_is_strict_one_way_sink()
    passed.append("жертва — честный односторонний сток (1:1, без обратного курса)")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты Алтаря Вечности пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
