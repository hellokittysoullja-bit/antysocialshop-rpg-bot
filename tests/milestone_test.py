"""Инварианты трекера «ближайшая веха» мид-гейма (без БД — чистые функции bot.py).

Между редкими рангами (5k→20k→50k) мид-гейм держится на плотном слое вех из
ACHIEVEMENTS. Трекер показывает ближайшую невзятую как goal-gradient. Эти
проверки замыкают контракт, чтобы будущая правка данных (новое достижение, новое
поле-счётчик) не сломала его тихо.

    python tests/milestone_test.py

Контракт:
  * у КАЖДОГО поля из ACHIEVEMENT_CONDITIONS есть человекочитаемая единица —
    иначе трекер напишет «ещё 5 » с пустотой на конце;
  * _nearest_milestone всегда возвращает невзятую, ещё НЕ достигнутую (<100%)
    веху с максимальным % — «цель впереди», а не почти-выданную;
  * взятие ближайшей вехи каскадирует к следующей (лестница малых побед);
  * без незакрытых счётных вех возвращается None (не падает).
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
from game_content import ACHIEVEMENTS, ACHIEVEMENT_CONDITIONS  # noqa: E402
from game_models import Player  # noqa: E402


def check_every_condition_field_has_unit():
    """Каждое поле-счётчик достижения обязано иметь единицу для строки «ещё N …»."""
    bad = []
    for ach_id, (field, _target) in ACHIEVEMENT_CONDITIONS.items():
        if not bot._MILESTONE_UNITS.get(field):
            bad.append(f"[{ach_id}] поле {field!r} без единицы в _MILESTONE_UNITS "
                       f"→ трекер напишет «ещё N » с пустотой")
    assert not bad, "Поля вех без единицы измерения:\n  " + "\n  ".join(bad)


def check_nearest_is_valid_and_frontmost():
    """Ближайшая веха — невзятая, <100%, и именно максимум по % среди кандидатов."""
    bad = []
    # Мид-гейм игрок: близок к нескольким счётным вехам
    p = Player(user_id=1, balance=8000, farm_count=96, craft_count=48,
               smoke_count=90, passive_level=4, login_streak=6)
    awarded = {"farm_1", "craft_1", "smoke_1", "balance_1000"}
    ms = bot._nearest_milestone(p, awarded)
    assert ms is not None, "ожидалась ближайшая веха для мид-гейм игрока"
    ach, cur, tgt, pct = ms

    if ach["id"] in awarded:
        bad.append("вернулась уже взятая веха")
    if cur >= tgt:
        bad.append(f"веха уже достигнута ({cur}/{tgt}) — это не «цель впереди»")

    # честный максимум по % среди всех невзятых незавершённых счётных
    best_pct = -1.0
    for a in ACHIEVEMENTS:
        aid = a["id"]
        if aid == "lunar_lord" or aid in awarded:
            continue
        cond = ACHIEVEMENT_CONDITIONS.get(aid)
        if not cond:
            continue
        f, t = cond
        c = getattr(p, f, 0) or 0
        if t <= 0 or c >= t:
            continue
        best_pct = max(best_pct, c / t * 100)
    if abs(pct - best_pct) > 1e-9:
        bad.append(f"выбран не максимум по %: {pct} != {best_pct}")

    # каскад: взяли ближайшую → появляется следующая с не большим %
    awarded2 = set(awarded) | {ach["id"]}
    ms2 = bot._nearest_milestone(p, awarded2)
    if ms2 and ms2[3] > pct + 1e-9:
        bad.append("после взятия ближайшей следующая имеет больший % (не каскад)")
    assert not bad, "Инвариант ближайшей вехи нарушен:\n  " + "\n  ".join(bad)


def check_none_when_nothing_left():
    """Все счётные вехи взяты → None, без исключения."""
    countable = {aid for aid in ACHIEVEMENT_CONDITIONS}
    p = Player(user_id=2)
    assert bot._nearest_milestone(p, countable) is None, "ожидался None, когда незакрытых вех нет"


def main():
    passed = []
    check_every_condition_field_has_unit()
    passed.append("у каждого поля-вехи есть единица измерения")
    check_nearest_is_valid_and_frontmost()
    passed.append("ближайшая веха: невзятая, <100%, максимум по %, каскадирует")
    check_none_when_nothing_left()
    passed.append("нет незакрытых вех → None без падения")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты трекера вех пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
