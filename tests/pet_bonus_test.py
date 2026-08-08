"""Инварианты data-driven бонуса питомца (без БД — чистые функции bot.py).

Питомец-компаньон спроектирован как ФУНДАМЕНТ: эффект живёт в данных
(PET_CONFIG), а логика применения — одна. Эти проверки замыкают контракт, чтобы
любая будущая правка данных (новый питомец, другой cap/floor/цель) не могла тихо
его сломать. Ломается контракт → громкий фейл здесь, а не тихий баг в проде.

    python tests/pet_bonus_test.py

Контракт:
  * при полной сытости бонус == bonus_max_pct (обещанный максимум даётся);
  * при нулевой сытости бонус == max_pct * floor (НИКОГДА не 0, если floor>0) —
    заброс не наказывает механически, лишь снижает баф к полу;
  * бонус монотонно растёт с сытостью и не выходит за [floor..max];
  * бонус применяется к доходу плантации, но НЕ к чужой цели и НЕ без питомца;
  * база (без питомца) не меняется — фича чисто аддитивна.
"""
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TOKEN", "1")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://x:y@127.0.0.1:5432/z")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("RENDER_URL", "")
sys.path.insert(0, ROOT)

import bot  # noqa: E402
from config import PET_CONFIG  # noqa: E402
from game_models import Player  # noqa: E402


def check_bonus_scaling_contract():
    """Для каждого питомца с бонусом: max при сытости 100, пол (>0) при 0, монотонность."""
    bad = []
    for key, cfg in PET_CONFIG.items():
        max_pct = cfg.get("bonus_max_pct", 0)
        target = cfg.get("bonus_target")
        if max_pct <= 0 or not target:
            continue
        floor = cfg.get("hunger_floor", 0)
        p = Player(user_id=1, pet=cfg["name"])

        p.pet_hunger = 100
        full = bot._pet_bonus_pct(p, target)
        if abs(full - max_pct) > 1e-9:
            bad.append(f"[{key}] сытость 100 → {full}, ожидался максимум {max_pct}")

        p.pet_hunger = 0
        empty = bot._pet_bonus_pct(p, target)
        expected_floor = max_pct * (floor / 100.0)
        if abs(empty - expected_floor) > 1e-9:
            bad.append(f"[{key}] сытость 0 → {empty}, ожидался пол {expected_floor}")
        if floor > 0 and empty <= 0:
            bad.append(f"[{key}] заброшенный питомец обнулил бонус (floor={floor}) — наказание за паузу")

        prev = -1.0
        for h in range(0, 101, 5):
            p.pet_hunger = h
            b = bot._pet_bonus_pct(p, target)
            if b < prev - 1e-9:
                bad.append(f"[{key}] бонус не монотонен: сытость {h} → {b} < предыдущего {prev}")
            if b < expected_floor - 1e-9 or b > max_pct + 1e-9:
                bad.append(f"[{key}] бонус {b} вне диапазона [{expected_floor}..{max_pct}] при сытости {h}")
            prev = b
    assert not bad, "Контракт масштабирования бонуса питомца нарушен:\n  " + "\n  ".join(bad)


def check_bonus_targeting_and_additivity():
    """Бонус бьёт только по своей цели, только при наличии питомца, база не тронута."""
    bad = []
    now = datetime.now()
    # L4 (100 OAC/ч), собрано 3ч назад → база 300
    base_p = Player(user_id=1, passive_level=4, passive_collected=now - timedelta(hours=3))
    base_earned, _, _ = bot._plant_pending_player(base_p, now)
    if base_earned != 300:
        bad.append(f"база без питомца изменилась: {base_earned} != 300 (фича должна быть аддитивной)")

    dog = PET_CONFIG.get("dog", {})
    if dog.get("bonus_target") == "plantation" and dog.get("bonus_max_pct", 0) > 0:
        fed = Player(user_id=2, passive_level=4, passive_collected=now - timedelta(hours=3),
                     pet=dog["name"], pet_hunger=100)
        fed_earned, _, _ = bot._plant_pending_player(fed, now)
        expect = int(300 * (1 + dog["bonus_max_pct"] / 100.0))
        if fed_earned != expect:
            bad.append(f"сытый питомец: {fed_earned} != {expect}")
        # Бонус не применяется к чужой цели
        if bot._pet_bonus_pct(fed, "farm") != 0.0:
            bad.append("бонус утёк на чужую цель 'farm'")

    # Нет плантации → ноль независимо от питомца (бонус ничего не выдумывает)
    no_plant = Player(user_id=3, passive_level=0, pet=dog.get("name", ""), pet_hunger=100)
    e, _, _ = bot._plant_pending_player(no_plant, now)
    if e != 0:
        bad.append(f"без плантации доход не ноль: {e}")
    assert not bad, "Таргетинг/аддитивность бонуса питомца нарушены:\n  " + "\n  ".join(bad)


def main():
    passed = []
    check_bonus_scaling_contract()
    passed.append("бонус: max при сытости, пол>0 при забросе, монотонность, диапазон")
    check_bonus_targeting_and_additivity()
    passed.append("бонус: только своя цель, только с питомцем, база аддитивна")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты бонуса питомца пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
