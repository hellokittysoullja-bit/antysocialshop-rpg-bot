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
        for _ in range(150):
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
        for _ in range(150):
            text, _kb = await bot.build_main_menu(p2, ctx2, c2)
            seen2.add(text.split("\n")[0].replace("<i>", "").replace("</i>", ""))
        assert seen2 == set(WHISPERS) | set(WHISPERS_GUILD), (
            f"вступившему недоступна часть пула: "
            f"{(set(WHISPERS) | set(WHISPERS_GUILD)) - seen2}")
    asyncio.run(_run())


def check_same_destination_same_label():
    """Одна и та же кнопка называется одинаково везде, где встречается.

    Ритуал/Исповедь живут и в главном меню, и на экране Гильдии, и ведут на
    один и тот же экран выбора профиля риска. Когда «›» добавили только в
    меню, один пункт стал давать два разных обещания в двух местах.
    Кулдаун-суффикс «(3 ч 20 мин)» — легитимная разница состояния, а не имени,
    поэтому сравниваем базовую часть подписи.
    """
    async def _run():
        now = datetime.now()
        for guild, cb in (("BLACK", "ritual"), ("WHITE", "repent")):
            kw = dict(exists=True, balance=6000, total_earned=9000,
                      onboarding_step=-1, guild=guild, blunts=4,
                      last_farm=now - timedelta(hours=9), passive_collected=now,
                      passive_level=3)
            menu = [b for b in await _main_menu_buttons(kw) if b.callback_data == cb]
            p = Player(user_id=1, **kw)
            ctx = t.make_ctx(p)
            u, c = t.FakeUpdate("guild_info", uid=1), t.FakeContext(ctx)
            await bot.guild_info_callback(u, c)
            _txt, mk = u.callback_query.message.edit_calls[-1]
            gi = [b for row in mk["reply_markup"].inline_keyboard for b in row
                  if b.callback_data == cb]
            assert menu and gi, f"{cb}: кнопка найдена не на обоих экранах"

            def base(x):           # отрезаем суффикс состояния «(N ч M мин)»
                return x.split("(")[0].strip().rstrip("›").strip()
            assert base(menu[0].text) == base(gi[0].text), (
                f"{cb}: в меню {menu[0].text!r}, в Гильдии {gi[0].text!r}")
            assert gi[0].text.rstrip().endswith("›"), (
                f"{cb} на экране Гильдии открывает экран, но подпись без «›»: "
                f"{gi[0].text!r}")
    asyncio.run(_run())


def check_player_addressed_informally_everywhere():
    """Игра обращается к игроку на «ты» — без срывов в официальное «Вы».

    Игра целиком построена на «ты» («Странник», «твой путь»), но в десяти
    местах — включая главное меню («Пока вас не было») и пик Лабиринта
    («Вы тяжело ранены») — обращение срывалось на официальное. Смена
    регистра посреди опыта рвёт то самое второе лицо, на котором держится
    отождествление игрока с персонажем.

    «вы оба» (игрок + приглашённый друг) — законное множественное, не
    обращение, поэтому исключено явно.
    """
    import re
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    # [^"\n]* — без переводов строки: иначе «строка» склеивается через несколько
    # строк кода и в неё попадают комментарии, которые игроку не показываются
    # (ровно так этот тест сначала и упал — на комментарии про «Пока вас не было»).
    # Официальность прячется не только в местоимении: «Попробуйте позже» —
    # ровно то же обращение на «Вы», только глаголом. Половина сообщений об
    # ошибках говорила «попробуй», половина — «Попробуйте»; ловим обе формы.
    formal = re.compile(r'"[^"\n]*(?:\bВы\b|\bВас\b|\bВаш|\bвашем\b|\bвашу\b|\bвашего\b'
                        r'|\bвас не было\b'
                        r'|[Пп]опробуйте|[Нн]ажмите|[Пп]одождите|[Вв]ведите'
                        r'|[Вв]ыберите|[Пп]роверьте|[Оо]братитесь|[Нн]апишите'
                        r'|[Оо]тправьте|[Ии]спользуйте|[Уу]бедитесь)[^"\n]*"')
    hits = [m.group(0) for m in formal.finditer(src) if "вы оба" not in m.group(0)]
    assert not hits, ("официальное обращение вернулось в текст игры "
                      f"({len(hits)} шт.): {hits[:5]}")


def check_one_destination_one_icon():
    """У одной цели — один значок во всех подписях, что на неё ведут.

    Значок кнопки — самый быстрый признак «куда это ведёт»: его видно раньше,
    чем прочитан текст. Когда одно и то же место помечено 🍀 в одном месте и
    🎲 в другом (или 💍 против 🌿 у крафта), признак перестаёт работать —
    игрок вынужден каждый раз дочитывать подпись (Nielsen, consistency &
    standards; узнавание вместо припоминания).

    Сокращать слова можно («Лабиринт» вместо «Лабиринт Искажения»), а вот
    заменять их другими — нет: «Магазин» и «Лавка Фабрики» читаются как два
    разных места. Поэтому здесь проверяется значок, а общее опорное слово —
    в check_one_destination_one_word ниже.
    """
    import re
    import collections
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    pat = re.compile(r'(?:InlineKeyboardButton|_btn)\(\s*"([^"]*)"\s*,\s*'
                     r'callback_data="([a-z_0-9]+)"')
    icons = collections.defaultdict(set)
    for m in pat.finditer(src):
        label, cb = m.group(1), m.group(2)
        if label.startswith("🔙") or cb in ("menu", "noop"):
            continue          # кнопки-возврат живут по своей конвенции
        lead = label.split(" ", 1)[0]
        if lead and not lead.isascii():
            icons[cb].add(lead)
    # Осознанные исключения: значок несёт СОСТОЯНИЕ, а не место.
    STATEFUL = {
        "altar_hub",    # 🌫️ «в тумане» против 🕯️ открытого
        "collect",      # 🌱 «посади» (пусто) против 🪴 существующей плантации
        "do_smoke",     # 🔥 на пороге Забоя против обычного 💨
        "farm",         # 🍬 всегда, но подписи контекстные — значок один, см. assert
        "craft_normal",
    }
    bad = {cb: sorted(v) for cb, v in icons.items()
           if len(v) > 1 and cb not in STATEFUL}
    assert not bad, f"одна цель помечена разными значками: {bad}"


def check_one_destination_one_word():
    """У одной цели во всех подписях есть общее опорное слово.

    Регресс, который это ловит: обучение звало «🍬 Собрать первый урожай», а
    та же механика во всей игре называется «фарм» — причём «собрать урожай»
    занято ДРУГОЙ механикой (Плантацией). Первая кнопка игры выдавала игроку
    словарь, который дальше означал не то, чему его научили.
    """
    import re
    import collections
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    pat = re.compile(r'(?:InlineKeyboardButton|_btn)\(\s*"([^"]*)"\s*,\s*'
                     r'callback_data="([a-z_0-9]+)"')
    words = collections.defaultdict(list)
    for m in pat.finditer(src):
        label, cb = m.group(1), m.group(2)
        if label.startswith("🔙") or cb in ("menu", "noop"):
            continue
        # оставляем только буквенные корни, без значков/цифр/суффиксов состояния
        base = label.split("(")[0].split("·")[0]
        # Основа в 4 буквы, а не всё слово: русский склоняется, и «Удача»
        # против «Зал Удачи» — это одно слово в разных падежах, а не два
        # разных имени. С 5 буквами тест падал именно на этом («удача» против
        # «удачи») — то есть ловил грамматику вместо расхождения смысла.
        toks = {w.lower()[:4] for w in re.findall(r"[А-Яа-яЁё]{4,}", base)}
        if toks:
            words[cb].append((label, toks))
    # Исключения — только там, где расхождение ОСМЫСЛЕННО, и каждое названо
    # поимённо: широкое исключение молча гасит настоящие находки (именно так
    # первая версия этого теста пропустила обучение, звавшее «Собрать первый
    # урожай» вместо «Фармить»).
    CTA = {
        # Пик Забоя намеренно кричит другим текстом: «🔥 ЕЩЁ ОДНА — И ЗАБОЙ!».
        "do_smoke",
        # На экране крафта подпись противопоставляет варианты («Обычный блант»
        # против «Именной блант»), а не называет действие.
        "craft_normal",
        # Реферальная кнопка — призыв («Позвать друга» / «Подари другу лучший
        # старт»), формулировка зависит от места.
        "invite_friend",
        # «Пропустить шаг» против «Пропустить обучение» — разный охват.
        "skip_onboarding",
    }
    bad = {}
    for cb, entries in words.items():
        if cb in CTA or len(entries) < 2:
            continue
        common = set.intersection(*[t for _l, t in entries])
        if not common:
            bad[cb] = sorted({l for l, _t in entries})
    assert not bad, f"у одной цели нет ни одного общего слова в подписях: {bad}"


def check_no_dead_end_screens():
    """С любого экрана есть путь назад.

    Тупик — самый тяжёлый вид поломки навигации: игрок не «неудобно» себя
    чувствует, а физически застревает. Ловилось так: экран «🏅 Лидеры» —
    кнопка ПЕРВОГО уровня из главного меню — на пустом рейтинге отдавал
    голый текст вообще без клавиатуры, и выйти можно было только командой
    /menu, которой на экране нет.

    Исключены два экрана, где отсутствие «назад» — часть замысла, а не
    недосмотр: онбординг (ведёт вперёд, шаг пропускается своей кнопкой) и
    забег по Лабиринту (выход там внутриигровой — «сбежать», с ценой).
    """
    BACK = {"menu", "profile", "luck", "world_hub", "guild_info", "my_blunts",
            "lab_start", "progress_hub", "shop", "craft", "daily_quest_hub",
            "achievements_profile", "collect"}
    INTENTIONAL = {"defer_faction", "lab_enter_confirm"}

    async def _run():
        now = datetime.now()
        registry = dict(bot.CALLBACKS)
        registry.update(bot.EXACT_HANDLERS)
        for label, kw in _states() + [("пустой мир (рейтинг пуст)", dict(
                exists=True, balance=10, total_earned=10, onboarding_step=-1))]:
            for cb, fn in sorted(registry.items()):
                if cb in INTENTIONAL:
                    continue
                p = Player(user_id=1, **kw)
                ctx = t.make_ctx(p)
                u, c = t.FakeUpdate(cb, uid=1), t.FakeContext(ctx)
                try:
                    await fn(u, c)
                except Exception:
                    continue      # падения — забота других тестов, не этого
                calls = u.callback_query.message.edit_calls
                if not calls:
                    continue      # экран ничего не перерисовал (тост/алерт)
                mk = calls[-1][1].get("reply_markup")
                assert mk is not None, (
                    f"[{label}] экран {cb!r} отрисован вообще без клавиатуры — "
                    f"из него нет выхода")
                targets = {b.callback_data for row in mk.inline_keyboard
                           for b in row if b.callback_data}
                assert targets & BACK, (
                    f"[{label}] с экрана {cb!r} нет пути назад; "
                    f"кнопки ведут только в {sorted(targets)}")
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
    check_same_destination_same_label()
    passed.append("одна кнопка называется одинаково на всех экранах")
    check_player_addressed_informally_everywhere()
    passed.append("обращение к игроку везде на «ты», без срывов в «Вы»")
    check_one_destination_one_icon()
    passed.append("одна цель — один значок во всех ведущих на неё кнопках")
    check_one_destination_one_word()
    passed.append("у одной цели есть общее опорное слово в подписях")
    check_no_dead_end_screens()
    passed.append("ни одного тупика: с любого экрана есть путь назад")
    check_rank_promises_match_real_gates()
    passed.append("обещания рангов совпадают с реальными гейтами")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты навигации и подписей пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
