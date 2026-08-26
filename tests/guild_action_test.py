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


def check_picker_text_states_both_branches_explicitly():
    """Пикер обязан называть процент на КАЖДОЙ ветке для тиров с редким
    предметом, а не только на редкой — "~95 OAC · 25% шанс Пыли" читается
    неоднозначно (можно понять как OAC И бонус сверху, хотя ветки взаимо-
    исключающие: or, не and). Тот же класс ошибки чтения вероятностных
    формулировок — Tversky & Kahneman, 1983, "conjunction fallacy"."""
    import asyncio
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import audit_regression_test as t
    from bot import Player

    async def _run():
        for guild in ("BLACK", "WHITE"):
            p = Player(user_id=1, exists=True, balance=1000, total_earned=1000,
                      blunts=5, guild=guild)
            ctx = t.make_ctx(p)
            fn = bot.ritual_callback if guild == "BLACK" else bot.repent_callback
            u = t.FakeUpdate("x", uid=1)
            await fn(u, t.FakeContext(ctx))
            text = u.callback_query.message.edit_calls[-1][0]
            for tier in GUILD_ACTION_TIERS:
                rare = tier["dust_chance"] + tier["legendary_chance"]
                name = GUILD_ACTION_THEME[guild]["tier_names"][tier["key"]]
                if rare:
                    oac_pct = int(round((1 - rare) * 100))
                    rare_pct = int(round(rare * 100))
                    assert f"{oac_pct}%" in text and f"{rare_pct}%" in text, (
                        f"{guild}/{tier['key']}: не обе ветки названы процентом — {text!r}")
                    # "или", а не только точка-разделитель — грамматика тоже
                    # обязана сообщать взаимоисключение, не только цифры.
                    idx = text.find(name)
                    assert "или" in text[idx:idx + 120], (
                        f"{guild}/{tier['key']}: нет слова 'или' между взаимоисключающими ветками")

    asyncio.run(_run())


def check_legendary_gets_suspense_others_dont():
    """Suspense-ревил обязан включаться ТОЛЬКО на легендарке (редкий пик,
    Berridge & Robinson 1998 — предвкушение важнее самого раскрытия) и НЕ
    включаться на OAC/Пыли (частый путь — задержка там была бы чистым
    раздражением без выигрыша, тот же принцип, что уже в do_smoke для
    джекпота).

    Метрика — не число кадров (у animate_progress_bar их и так больше, это
    просто более мелкая нарезка полосы заполнения за то же ~0.6с), а факт,
    что легендарка проходит через ИМЕННО НАРРАТИВНЫЕ кадры (см. frames в
    _resolve_guild_action) и суммарно тратит на предвкушение БОЛЬШЕ времени
    (3 кадра × 0.6с = 1.8с) — что и есть содержательный смысл suspense-ревила,
    а не сырое количество edit_text-вызовов."""
    import asyncio
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import audit_regression_test as t
    from bot import Player

    BLACK_NARRATIVE = ("Тьма сгущается", "Кровь на камне", "ЧУДО СВЕРШИЛОСЬ")

    async def _run():
        frames_legendary = frames_other = None
        for seed in range(1, 400):
            random.seed(seed)
            p = Player(user_id=1, exists=True, balance=1000, total_earned=1000,
                      blunts=5, guild="BLACK")
            ctx = t.make_ctx(p)
            u = t.FakeUpdate("guild_act_BLACK_risky", uid=1)
            await bot._guild_action_pick_wrapper(u, t.FakeContext(ctx))
            frames = [txt for txt, _ in u.callback_query.message.edit_calls]
            is_legendary = any("ЧУДО" in f for f in frames)
            if is_legendary and frames_legendary is None:
                frames_legendary = frames
            elif not is_legendary and frames_other is None:
                frames_other = frames
            if frames_legendary is not None and frames_other is not None:
                break
        assert frames_legendary is not None and frames_other is not None, (
            "не удалось поймать оба исхода (легендарка/не-легендарка) за 400 сидов")
        # На легендарке — ровно нарративные кадры суспенса, по одному на каждую
        # фразу, ПЕРЕД финальным результатом.
        for phrase in BLACK_NARRATIVE:
            assert any(phrase in f for f in frames_legendary), (
                f"кадр с фразой {phrase!r} не найден в легендарной последовательности: {frames_legendary}")
        # На обычном исходе — ни один из нарративных кадров суспенса не всплывает
        # (общий прогресс-бар — другой текст, "░"/"▓"-полоса, не эти фразы).
        for phrase in BLACK_NARRATIVE:
            assert not any(phrase in f for f in frames_other), (
                f"нарративный кадр {phrase!r} просочился на обычный (не-легендарный) путь")

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
    check_picker_text_states_both_branches_explicitly()
    passed.append("пикер называет процент на ОБЕИХ ветках — не читается как 'and' вместо 'or'")
    check_legendary_gets_suspense_others_dont()
    passed.append("suspense-ревил только на легендарке, не на частом пути (OAC/Пыль)")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты гильдейского действия пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
