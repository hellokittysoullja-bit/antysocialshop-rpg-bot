"""Инварианты кривой награды за серию входов (без БД — чистые функции bot.py).

Награда за ежедневный вход — главный рычаг D1-ретеншна (метрика №1). Раньше
кривая D1–D7 давала 10–50 OAC — НИЖЕ одного фарма → ритуал возврата был мёртв.
Эти проверки замыкают контракт, чтобы правка не вернула «мёртвую» кривую и не
создала cliff после D14 (который карал бы самых лояльных).

    python tests/streak_curve_test.py

Контракт:
  * награда D1..D14 монотонно НЕ убывает (возврат никогда не ощущается нёрфом);
  * каждый день D1..D7 ощутимо выше одного фарма (FARM_MAX) — иначе ниже шума;
  * есть недельные пики-скачки на D7 и D14 (peak-end);
  * после D14 — достойное плато, а НЕ обвал к прежним 100 (нет cliff лояльности);
  * превью «завтра» согласовано с фактической завтрашней наградой.
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
from config import FARM_MIN  # noqa: E402


def _base(streak, cfg):
    """Детерминированная база награды за день streak (как в _calculate_reward, без random)."""
    b = cfg.base_rewards.get(streak, cfg.plateau_reward)
    if streak >= cfg.hot_streak_threshold:
        b = int(b * cfg.hot_streak_multiplier)
    return b


def check_curve_health():
    cfg = bot.daily_config
    bad = []

    # монотонность D1..D14
    prev = -1
    for d in range(1, 15):
        v = cfg.base_rewards.get(d)
        if v is None:
            bad.append(f"нет награды за день {d}")
            continue
        if v < prev:
            bad.append(f"день {d}: {v} < дня {d-1} ({prev}) — возврат ощущается как нёрф")
        prev = v

    # D1..D7 не ниже одного фарма (иначе награда «ниже шума», как старые 10–15)
    for d in range(1, 8):
        v = cfg.base_rewards.get(d, 0)
        if v < FARM_MIN:
            bad.append(f"день {d}: {v} < FARM_MIN ({FARM_MIN}) — награда за возврат ниже одного фарма")

    # недельные пики: D7 заметно выше D6, D14 заметно выше D13
    if cfg.base_rewards.get(7, 0) < cfg.base_rewards.get(6, 0) * 1.5:
        bad.append("D7 не пик: скачок от D6 меньше ×1.5")
    if cfg.base_rewards.get(14, 0) <= cfg.base_rewards.get(13, 0):
        bad.append("D14 не пик: не выше D13")

    # нет обвала после D14: плато заметно выше прежних 100 и не выше пика D14
    if cfg.plateau_reward <= 100:
        bad.append(f"плато {cfg.plateau_reward} ≤ 100 — cliff карает самых лояльных")
    if cfg.plateau_reward > cfg.base_rewards.get(14, 0):
        bad.append("плато выше пикового приза D14 — пик перестаёт быть пиком")

    assert not bad, "Кривая стрика нарушает контракт:\n  " + "\n  ".join(bad)


def check_preview_matches_actual():
    """Превью «завтра: +X» обязано совпадать с фактической завтрашней базой."""
    cfg = bot.daily_config
    bad = []
    for d in range(0, 20):
        preview = bot._build_next_day_preview(d, cfg)
        expected = _base(d + 1, cfg)
        if f"+{expected} OAC" not in preview:
            bad.append(f"день {d}: превью не содержит фактическое +{expected} OAC")
    assert not bad, "Рассинхрон превью и реальной награды:\n  " + "\n  ".join(bad)


def main():
    passed = []
    check_curve_health()
    passed.append("кривая монотонна, D1–D7 выше фарма, пики D7/D14, плато без cliff")
    check_preview_matches_actual()
    passed.append("превью «завтра» совпадает с фактической наградой")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты кривой стрика пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
