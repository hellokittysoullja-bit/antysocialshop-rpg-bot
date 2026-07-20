"""Инварианты пити-таймера гачи «Дунуть» (без БД — чистая функция bot.py).

Гача «Дунуть»: 80% тяг пусты (55% ноль + 25% минус) → серия сухих тяг убивает
дофамин («10 тяг — ничего»). Пити-гарант: после SMOKE_PITY_THRESHOLD сухих
подряд следующая — гарантированно выигрыш. Эти проверки замыкают контракт:
гарант реально срабатывает, не ломает распределение до порога, и гача остаётся
нетто-стоком (никакой инфляции).

    python tests/smoke_pity_test.py
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


def check_pity_guarantees_win_at_threshold():
    """На пороге сухих тяг результат ВСЕГДА выигрыш (никогда не пусто)."""
    bad = []
    thr = bot.SMOKE_PITY_THRESHOLD
    for _ in range(20000):
        earned, outcome = bot.calculate_smoke_reward(None, False, dry_count=thr - 1)
        if outcome in ("loss", "neutral") or earned <= 0:
            bad.append(f"на пороге dry={thr-1} выпало {outcome!r}/{earned} — гарант не сработал")
            break
    assert not bad, "Пити-гарант не срабатывает:\n  " + "\n  ".join(bad)


def check_below_threshold_untouched():
    """Ниже порога гарант НЕ форсится — сухие исходы всё ещё возможны."""
    saw_dry = False
    for _ in range(20000):
        _e, outcome = bot.calculate_smoke_reward(None, False, dry_count=0)
        if outcome in ("loss", "neutral"):
            saw_dry = True
            break
    assert saw_dry, "ниже порога сухие тяги исчезли — гарант форсится слишком рано"


def check_still_net_sink():
    """Стационарный EV/тяга (со сбросом счётчика) остаётся < цены крафта 15.

    Считаем как в проде: счётчик растёт на сухих и обнуляется на выигрыше, поэтому
    подряд не может быть больше SMOKE_PITY_THRESHOLD сухих. Это честный
    стационарный доход — он и должен быть ниже стоимости бланта (гача = сток).
    """
    from config import GAME_CONFIG
    craft_cost = GAME_CONFIG["craft_cost"]
    N = 500000
    dry = 0
    tot = 0
    for _ in range(N):
        earned, outcome = bot.calculate_smoke_reward(None, False, dry_count=dry)
        tot += earned
        dry = dry + 1 if outcome in ("loss", "neutral") else 0
    ev = tot / N
    assert ev < craft_cost, (f"гача перестала быть стоком: стационарный EV {ev:.2f} "
                             f"≥ craft {craft_cost} — инфляция")
    # И всё же ощутимо выше прежних 6.6 (пити реально смягчает засухи):
    assert ev > 6.6, f"пити не поднял пол: EV {ev:.2f} ≤ базовых 6.6"


def main():
    passed = []
    check_pity_guarantees_win_at_threshold()
    passed.append("пити-гарант: на пороге всегда выигрыш")
    check_below_threshold_untouched()
    passed.append("ниже порога распределение не тронуто")
    check_still_net_sink()
    passed.append("гача остаётся нетто-стоком (нет инфляции)")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты пити-гачи пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
