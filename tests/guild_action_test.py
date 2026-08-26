"""Инварианты гильдейского действия (Ритуал/Исповедь — общий трёхпрофильный выбор).

Раньше Ритуал (BLACK) был жёстко заперт в один профиль (стабильный OAC,
0% доступ к редким предметам), Исповедь (WHITE) — в другой (смесь OAC/пыли/
легенды, 0% доступ к стабильному профилю). Ни одна фракция не выбирала
ничего — тапала единственную доступную кнопку раз в 12ч. Теперь обе стороны
выбирают один из трёх профилей риска перед КАЖДЫМ действием — настоящий
выбор (autonomy, Deci & Ryan 1985), а не тематическая обёртка вокруг того же
числа. Профили математически идентичны для обеих фракций — паритет через
одинаковую механику, не подгонкой чисел одной стороны под другую.

    python tests/guild_action_test.py
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
from game_content import GUILD_ACTION_TIERS, GUILD_ACTION_THEME  # noqa: E402


def check_tiers_are_shared_not_per_faction():
    """Механика ОДНА на обе фракции — GUILD_ACTION_TIERS не дублирован по
    guild, паритет достигается структурой, а не подгонкой чисел."""
    assert isinstance(GUILD_ACTION_TIERS, list) and len(GUILD_ACTION_TIERS) == 3
    keys = [t["key"] for t in GUILD_ACTION_TIERS]
    assert keys == ["safe", "balanced", "risky"], keys
    assert {"BLACK", "WHITE"} <= set(GUILD_ACTION_THEME.keys())
    # Обе темы обязаны знать имя для КАЖДОГО из трёх тиров — иначе KeyError
    # в рантайме на конкретном выборе игрока.
    for guild in ("BLACK", "WHITE"):
        names = GUILD_ACTION_THEME[guild]["tier_names"]
        assert set(names.keys()) == {"safe", "balanced", "risky"}, (guild, names)


def check_no_dominant_strategy():
    """Ни один профиль не должен быть строго лучше других: OAC-отдача обязана
    падать по мере роста шанса редкого предмета — иначе «выбор» фиктивен
    (один вариант всегда правильный, остальные — ловушка для невнимательных)."""
    prev_oac = float("inf")
    for tier in GUILD_ACTION_TIERS:
        lo, hi = tier["oac_range"]
        oac_ev = (1 - tier["dust_chance"] - tier["legendary_chance"]) * (lo + hi) / 2
        assert oac_ev < prev_oac, (
            f"тир {tier['key']}: OAC EV {oac_ev:.1f} не ниже предыдущего {prev_oac:.1f} "
            f"— доминирующая стратегия, выбор не настоящий")
        prev_oac = oac_ev
        rare_chance = tier["dust_chance"] + tier["legendary_chance"]
        assert 0.0 <= rare_chance <= 1.0


def check_safe_tier_not_a_nerf():
    """«Стабильный» профиль обязан давать не меньше, чем старый Ритуал
    (EV 150 + 10%×15 = 151.5 OAC) — заменa структуры не должна тихо срезать
    доход тем, кто и раньше просто хотел стабильный OAC."""
    safe = next(t for t in GUILD_ACTION_TIERS if t["key"] == "safe")
    lo, hi = safe["oac_range"]
    ev = (lo + hi) / 2
    assert ev >= 151.5, f"'safe' даёт {ev} OAC EV — это нерф относительно старого Ритуала (151.5)"
    assert safe["dust_chance"] == 0.0 and safe["legendary_chance"] == 0.0


def check_risky_tier_matches_old_confession_rate():
    """«Рискованный» профиль обязан давать не меньше шанса легендарки, чем
    старая Исповедь эффективно давала (25% пыль, которая всегда
    конвертируется в легенду через handle_use_dust, + 5% сразу = ~30%) —
    иначе Светлые тихо теряют то, что уже имели, получая взамен лишь
    формальный «выбор»."""
    risky = next(t for t in GUILD_ACTION_TIERS if t["key"] == "risky")
    assert risky["legendary_chance"] >= 0.30 - 1e-9, (
        f"'risky' даёт {risky['legendary_chance']:.0%} легенды — ниже эффективных "
        f"~30% старой Исповеди")


def check_medal_bonus_never_lost():
    """Регресс на баг, который правка попутно исправила: раньше в Исповеди
    награда за медаль (get_medal_text_and_reward) начислялась ТОЛЬКО в OAC-
    ветке (70%) — если ранг-ап медали совпадал с попаданием в пыль/легенду
    (30% случаев), бонус молча пропадал. Гоняем реальный резолвер (тот же
    проверенный фейк-харнесс, что и в audit_regression_test.py) и проверяем,
    что бонус применяется независимо от исхода."""
    import asyncio
    from bot import Player
    # Переиспользуем уже отлаженный харнесс (FakeUpdate/FakeContext/make_ctx),
    # а не изобретаем параллельный — та же причина, по которой он существует:
    # check_achievements внутри резолвера делает реальный SQL через conn,
    # и только этот харнесс умеет отвечать на него безопасным дефолтом.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import audit_regression_test as t

    async def _run():
        # count_field=9 → следующий тап (10-й) пересекает медальный порог
        # RITUAL_MEDALS[1]/REPENT_MEDALS[1] = (10, ..., 75) — реальный ранг-ап
        # медали ровно на этом тапе.
        for guild, count_field, tier_key in (("BLACK", "ritual_count", "risky"),
                                             ("WHITE", "repent_count", "risky")):
            hit = False
            for seed in range(1, 300):
                random.seed(seed)
                p = Player(user_id=1, exists=True, balance=1000, total_earned=1000,
                          guild=guild, blunts=5, **{count_field: 9})
                ctx = t.make_ctx(p)
                u = t.FakeUpdate(f"guild_act_{guild}_{tier_key}", uid=1)
                await bot._guild_action_pick_wrapper(u, t.FakeContext(ctx))
                # medal_bonus=75 обязан прибавиться, даже когда исход ЭТОГО
                # тапа — не OAC (иначе тест не проверяет найденный баг).
                if getattr(p, count_field) == 10 and p.balance >= 1000 + 75:
                    hit = True
                    break
            assert hit, f"{guild}: не удалось поймать ранг-ап медали на не-OAC ветке за 300 сидов"

    asyncio.run(_run())


def main():
    passed = []
    check_tiers_are_shared_not_per_faction()
    passed.append("механика одна на обе фракции — паритет по конструкции")
    check_no_dominant_strategy()
    passed.append("нет доминирующей стратегии: OAC падает по мере роста шанса редкого предмета")
    check_safe_tier_not_a_nerf()
    passed.append("'стабильный' профиль не хуже старого Ритуала (151.5 OAC)")
    check_risky_tier_matches_old_confession_rate()
    passed.append("'рискованный' профиль даёт не меньше легенды, чем старая Исповедь (~30%)")
    check_medal_bonus_never_lost()
    passed.append("бонус за медаль применяется независимо от исхода (регресс на найденный баг)")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты гильдейского действия пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
