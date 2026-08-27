"""Инварианты навигации и подписей кнопок на двух хаб-экранах игры.

Главное меню и «🌍 Мир» — единственные экраны, через которые проходит ЛЮБОЙ
маршрут игрока, поэтому именно на них сложилась конвенция подписи:

    «›» в конце  = тап ОТКРЫВАЕТ экран
    без «›»      = тап СОВЕРШАЕТ действие сразу

Конвенция не декоративная: подпись — это обещание результата, и когда кнопка
«🌾 Собрать урожай · 300 OAC» вместо сбора открывала экран, где урожай надо
было собирать вторым тапом, она обещала то, чего не делала (Nielsen, «match
between system and the real world»). Хуже всего это било по idle-крючку —
самому сильному поводу вернуться в игру.

Глубже этих двух хабов конвенция другая (там подписи без «›»), поэтому тест
намеренно ограничен ими: расширять его вниз нельзя, не переписав ~60 кнопок.

    python tests/navigation_ux_test.py
"""
import asyncio
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
sys.path.insert(0, os.path.join(ROOT, "tests"))

import audit_regression_test as t  # noqa: E402  (переиспользуем проверенные фейки)
import bot  # noqa: E402
from game_models import Player  # noqa: E402
from game_content import RANK_LORE, WHISPERS, WHISPERS_GUILD  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

# Куда ведёт callback: открыть экран (NAV) или выполнить действие (ACT).
# Значения выведены из самих обработчиков, а не из подписей — иначе тест
# проверял бы подпись подписью.
NAV = {
    "craft", "guild_info", "progress_hub", "top", "world_hub", "daily_quest_hub",
    "collect", "altar_hub", "ritual", "repent", "all_features",
    "pet_preview", "pet_locked", "luck", "lab_start", "shop",
}
ACT = {"farm", "do_smoke", "claim_reward", "plant_harvest"}
# Кнопки-возврат живут по своей конвенции («🔙»), к ним «›» неприменима.
EXEMPT_PREFIX = "🔙"


def _states():
    """Состояния игрока, покрывающие ВСЕ адаптивные слоты обоих хабов."""
    now = datetime.now()
    return [
        ("новичок без гильдии", dict(
            exists=True, balance=0, total_earned=0, farm_count=0, craft_count=0,
            onboarding_step=-1, guild=None, login_streak=1)),
        ("ветеран BLACK, урожай готов", dict(
            exists=True, balance=6000, total_earned=9000, farm_count=60, craft_count=25,
            onboarding_step=-1, guild="BLACK", login_streak=5, passive_level=3, blunts=4,
            last_farm=now - timedelta(hours=9), passive_collected=now - timedelta(hours=4))),
        ("ветеран WHITE, урожай пуст (слот апгрейда)", dict(
            exists=True, balance=9000, total_earned=9000, onboarding_step=-1,
            guild="WHITE", passive_level=3, blunts=4, pet="dog",
            last_farm=now - timedelta(hours=9), passive_collected=now)),
        ("эндгейм: Некромант, макс-плантация, Алтарь", dict(
            exists=True, balance=120000, total_earned=160000, onboarding_step=-1,
            guild="WHITE", login_streak=45, passive_level=10, pet="dog", blunts=20,
            prestige=8, last_repent=now - timedelta(hours=13),
            last_farm=now - timedelta(hours=1), passive_collected=now)),
        ("онбординг не пройден", dict(
            exists=True, balance=100, total_earned=100, onboarding_step=2, guild=None)),
    ]


async def _main_menu_buttons(kw):
    p = Player(user_id=1, **kw)
    ctx = t.make_ctx(p)
    _text, kb = await bot.build_main_menu(p, ctx, t.FakeContext(ctx))
    return [b for row in kb.inline_keyboard for b in row]


async def _world_hub_buttons(kw):
    p = Player(user_id=1, **kw)
    ctx = t.make_ctx(p)
    u, c = t.FakeUpdate("world_hub", uid=1), t.FakeContext(ctx)
    await bot.world_hub(u, c)
    _text, kw_markup = u.callback_query.message.edit_calls[-1]
    return [b for row in kw_markup["reply_markup"].inline_keyboard for b in row]


def check_nav_buttons_marked_and_actions_not():
    """«›» стоит ровно там, где тап открывает экран, и нигде больше.

    Регресс, который это ловит: Ритуал/Исповедь перестали быть мгновенным
    действием (появился выбор профиля риска) — но подпись осталась от
    действия, и кнопка молча начала обещать не то, что делает.
    """
    async def _run():
        for label, kw in _states():
            for source, getter in (("главное меню", _main_menu_buttons),
                                   ("хаб «Мир»", _world_hub_buttons)):
                for btn in await getter(kw):
                    cb = btn.callback_data
                    text = btn.text.rstrip()
                    if text.startswith(EXEMPT_PREFIX) or cb in ("menu", "noop"):
                        continue
                    where = f"[{label} · {source}] {text!r} -> {cb}"
                    if cb in NAV:
                        assert text.endswith("›"), (
                            f"{where}: открывает экран, но подпись без «›» — "
                            f"кнопка обещает мгновенное действие")
                    elif cb in ACT:
                        assert not text.endswith("›"), (
                            f"{where}: совершает действие, но подпись с «›» — "
                            f"кнопка обещает экран")
                    else:
                        raise AssertionError(
                            f"{where}: callback не классифицирован. Добавь его в NAV "
                            f"или ACT — молча пропустив, тест перестанет быть стражем")
    asyncio.run(_run())


def check_no_doubled_leading_emoji():
    """Ведущий эмодзи подписи не повторяется внутри неё же.

    Ровно этот баг уже ловили дважды: «⚔️⚔️ Ветеран» в строке приветствия и
    «🕯️ Алтарь: 🕯️ Послушник Алтаря» на кнопке Алтаря — оба раза причина одна:
    к строке, которая УЖЕ несёт свой эмодзи, приписали второй сверху.
    """
    async def _run():
        for label, kw in _states():
            for source, getter in (("главное меню", _main_menu_buttons),
                                   ("хаб «Мир»", _world_hub_buttons)):
                for btn in await getter(kw):
                    text = btn.text
                    lead = text.split(" ", 1)[0]
                    if not lead or lead.isascii():
                        continue
                    rest = text[len(lead):]
                    assert lead not in rest, (
                        f"[{label} · {source}] {text!r}: ведущий эмодзи {lead!r} "
                        f"повторяется в той же подписи")
    asyncio.run(_run())


def check_harvest_button_actually_harvests():
    """Кнопка урожая обязана ВЫПОЛНЯТЬ сбор, а не вести на экран.

    Она называет точную сумму («🌾 Собрать урожай · 300 OAC»), поэтому любой
    другой исход тапа — прямой обман ожидания на самом сильном крючке
    возврата в игру.
    """
    async def _run():
        now = datetime.now()
        kw = dict(exists=True, balance=1000, total_earned=5000, onboarding_step=-1,
                  guild="BLACK", passive_level=3, blunts=2,
                  last_farm=now - timedelta(hours=9),
                  passive_collected=now - timedelta(hours=4))
        btns = await _main_menu_buttons(kw)
        harvest = [b for b in btns if "рожай" in b.text]
        assert harvest, f"кнопка урожая не найдена среди {[b.text for b in btns]}"
        assert harvest[0].callback_data == "plant_harvest", (
            f"кнопка урожая ведёт на {harvest[0].callback_data!r} вместо сбора")

        # И сбор действительно происходит: баланс растёт одним тапом.
        p = Player(user_id=1, **kw)
        ctx = t.make_ctx(p)
        before = p.balance
        u, c = t.FakeUpdate("plant_harvest", uid=1), t.FakeContext(ctx)
        await bot.plant_harvest_handler(u, c)
        after = (await ctx.repo.get_by_id(1)).balance
        assert after > before, (
            f"тап по «Собрать урожай» не изменил баланс ({before} -> {after})")
    asyncio.run(_run())


def check_whispers_never_point_at_locked_content():
    """Шёпот не зовёт туда, куда игрок не может пойти.

    Алтари (ритуал/исповедь) и война гильдий закрыты без вступления в гильдию,
    а шёпот — первая строка самого посещаемого экрана.
    """
    async def _run():
        random.seed(11)
        p = Player(user_id=1, exists=True, balance=0, total_earned=0,
                   onboarding_step=-1, guild=None)
        ctx = t.make_ctx(p)
        c = t.FakeContext(ctx)
        seen, prev, repeats = set(), None, 0
        for _ in range(400):
            text, _kb = await bot.build_main_menu(p, ctx, c)
            w = text.split("\n")[0].replace("<i>", "").replace("</i>", "")
            seen.add(w)
            if w == prev:
                repeats += 1
            prev = w
        leaked = seen & set(WHISPERS_GUILD)
        assert not leaked, f"игрок без гильдии видит недоступный контент: {leaked}"
        assert repeats == 0, f"шёпот повторился подряд {repeats} раз"
        assert seen == set(WHISPERS), f"часть общих шёпотов недостижима: {set(WHISPERS) - seen}"

        # У вступившего пул шире — мир оживает после главного шага вовлечения.
        p2 = Player(user_id=2, exists=True, balance=0, total_earned=0,
                    onboarding_step=-1, guild="BLACK")
        ctx2 = t.make_ctx(p2)
        c2 = t.FakeContext(ctx2)
        seen2 = set()
        for _ in range(400):
            text, _kb = await bot.build_main_menu(p2, ctx2, c2)
            seen2.add(text.split("\n")[0].replace("<i>", "").replace("</i>", ""))
        assert seen2 == set(WHISPERS) | set(WHISPERS_GUILD), (
            f"вступившему недоступна часть пула: "
            f"{(set(WHISPERS) | set(WHISPERS_GUILD)) - seen2}")
    asyncio.run(_run())


def check_rank_promises_match_real_gates():
    """Полярная звезда не обещает того, что уже доступно бесплатно.

    RANK_LORE «⚔️ Ветеран» обещал «🪴 Открыта Плантация-империя», хотя
    Плантация не гейтится рангом нигде в коде — и на ТОМ ЖЕ экране Рекруту
    показана кнопка «🌱 Посади Плантацию». Обещание, которое нечем
    подкрепить, обесценивает и остальные: Полярная звезда — главный
    долгосрочный ориентир игры.
    """
    unlock = RANK_LORE["⚔️ Ветеран"]["unlock"]
    assert "лантаци" not in unlock, (
        f"Ветеран снова обещает Плантацию, которая доступна с первого дня: {unlock!r}")

    # Плантация действительно открыта Рекруту — иначе обещание было бы честным
    # и убирать его было бы ошибкой.
    async def _run():
        p = Player(user_id=1, exists=True, balance=0, total_earned=0,
                   onboarding_step=-1, guild=None)
        ctx = t.make_ctx(p)
        _text, kb = await bot.build_main_menu(p, ctx, t.FakeContext(ctx))
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any("лантаци" in x for x in labels), (
            f"Плантация больше НЕ доступна новичку — обещание Ветерана стало "
            f"правдой, верни его в RANK_LORE. Кнопки: {labels}")
    asyncio.run(_run())

    # Реальные гейты Ветерана обязаны быть названы.
    assert "итомец" in unlock and "лхими" in unlock, (
        f"Ветеран не называет свои реальные гейты (питомец, Алхимия): {unlock!r}")


def main():
    passed = []
    check_nav_buttons_marked_and_actions_not()
    passed.append("«›» ровно там, где тап открывает экран, и нигде больше")
    check_no_doubled_leading_emoji()
    passed.append("ведущий эмодзи не дублируется внутри подписи")
    check_harvest_button_actually_harvests()
    passed.append("кнопка урожая реально собирает урожай одним тапом")
    check_whispers_never_point_at_locked_content()
    passed.append("шёпот не зовёт в закрытый контент и не повторяется подряд")
    check_rank_promises_match_real_gates()
    passed.append("обещания рангов совпадают с реальными гейтами")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты навигации и подписей пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
