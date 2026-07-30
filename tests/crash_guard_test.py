"""Страж классов прод-крашей, которые уже кусали живую игру.

Каждая проверка здесь — не абстрактная гигиена, а защёлка против КОНКРЕТНОГО
бага, что реально ломал бота в проде и был найден вручную/случайно. Тест
превращает такой тихий класс в громкий фейл на CI.

    python tests/crash_guard_test.py

БД/Redis не нужны — только AST-разбор исходников.
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["bot.py", "game_content.py", "game_models.py", "services.py",
           "config.py", "repository.py", "infra.py", "main.py"]


def check_duplicate_dict_keys():
    """Дубль ключа в dict-литерале: второй молча затирает первый.

    Ровно это убило весь хаб «Удача»: LUCK_CONFIG["mines"] был объявлен дважды,
    второй словарь потерял поле "cost" → KeyError на каждый тап, хаб мёртв.
    Python не предупреждает о таком — только этот тест.
    """
    bad = []
    for mod in MODULES:
        path = os.path.join(ROOT, mod)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            seen = {}
            for k in node.keys:
                if k is None:  # {**spread}
                    continue
                try:
                    key = ast.literal_eval(k)
                except Exception:
                    continue  # динамический ключ — пропускаем
                if isinstance(key, (str, int, float, bool, tuple)):
                    if key in seen:
                        bad.append(f"{mod}:{k.lineno} дубль ключа {key!r} "
                                   f"(первый на :{seen[key]})")
                    seen[key] = k.lineno
    assert not bad, "Дубли ключей в словарях (второй затирает первый):\n  " + "\n  ".join(bad)


def check_conn_used_after_async_with():
    """'conn'/'conn2' используется вне ЛЮБОГО связывающего его `async with`.

    Ровно это роняло progress_hub_handler: запрос conn.fetchrow(...) стоял ПОСЛЕ
    закрытия `async with ctx.db_pool.acquire() as conn` → "connection has been
    released back to the pool" для любого игрока не из топ-10.

    Исключаем ложные срабатывания: имя, которое является параметром какой-либо
    (в т.ч. вложенной) функции — это чужое соединение, переданное снаружи.
    """
    NAMES = {"conn", "conn2"}
    bad = []
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    def line_range(n):
        los = [x.lineno for x in ast.walk(n) if hasattr(x, "lineno")]
        return (min(los), max(los))

    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))]:
        # имена-параметры всех функций в этом поддереве (свои и вложенные)
        params = set()
        for sub in ast.walk(fn):
            if isinstance(sub, (ast.AsyncFunctionDef, ast.FunctionDef)):
                for a in sub.args.args + sub.args.kwonlyargs:
                    params.add(a.arg)
        # диапазоны with-блоков, связывающих conn/conn2
        with_ranges = []
        for node in ast.walk(fn):
            if isinstance(node, ast.AsyncWith):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name) and item.optional_vars.id in NAMES:
                        with_ranges.append((item.optional_vars.id, *line_range(node)))
        if not with_ranges:
            continue
        for u in ast.walk(fn):
            if isinstance(u, ast.Name) and u.id in NAMES and isinstance(u.ctx, ast.Load):
                if u.id in params:
                    continue  # это параметр (чужое соединение) — легитимно
                inside = any(nm == u.id and lo <= u.lineno <= hi for (nm, lo, hi) in with_ranges)
                if not inside:
                    bad.append(f"bot.py:{u.lineno} '{u.id}' используется вне своего `async with`")
    assert not bad, "Соединение используется после возврата в пул:\n  " + "\n  ".join(bad)


def check_achievement_rewards_parseable():
    """Каждая награда достижения должна полностью распарситься reward-DSL.

    Баг «Лунный лорд»: reward="Уникальный фон 🌀" начиналось с «Уникальный», а не
    «Фон » → парсер (_award_achievement_rewards / _give_reward) молча ронял её в
    else-ветку, и высший completionist-приз (закрыть ВСЕ достижения) не давал
    ничего. DSL допускает ровно: '+N OAC', 'Титул X', 'Фон X', 'Рамка X'
    (через запятую). Любая иная формулировка = тихо потерянная награда → тут
    падаем громко.
    """
    os.environ.setdefault("TOKEN", "1")
    os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://x:y@127.0.0.1:5432/z")
    os.environ.setdefault("REDIS_URL", "")
    os.environ.setdefault("ADMIN_ID", "0")
    os.environ.setdefault("RENDER_URL", "")
    sys.path.insert(0, ROOT)
    import re
    from game_content import ACHIEVEMENTS

    def part_ok(part):
        if part.startswith("+") and "OAC" in part:
            return re.search(r"\+(\d+)", part.replace(" ", "")) is not None
        return part.startswith(("Титул ", "Фон ", "Рамка "))

    bad = []
    for a in ACHIEVEMENTS:
        reward = a.get("reward", "")
        if not reward:
            continue
        for part in (p.strip() for p in reward.split(",") if p.strip()):
            if not part_ok(part):
                bad.append(f"[{a['id']}] непарсибельная часть награды: {part!r}")
    assert not bad, ("Награды достижений, которые парсер молча теряет:\n  "
                     + "\n  ".join(bad))



def check_war_actions_exist():
    """Каждое WarAction.X, упомянутое в bot.py, обязано существовать и иметь цену.

    Страж бага «тяга не приносит очков»: do_smoke вызывал WarAction.SMOKE,
    которого в enum не было. AttributeError ловился общим `except Exception`
    и уходил в лог — механика молча не работала месяцами, а второе по частоте
    действие игры не давало гильдии ничего.
    """
    import re
    from services import WarAction, WarConfig
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    used = set(re.findall(r"WarAction\.([A-Z_]+)", src))
    # Членство в enum проверяем ДО WarConfig(): если действие пропало из enum,
    # но осталось в таблице очков, конструктор упадёт первым и спрячет причину.
    missing = sorted(n for n in used if not hasattr(WarAction, n))
    assert not missing, ("bot.py ссылается на несуществующие WarAction: "
                         + ", ".join(missing))
    points = WarConfig().points
    priceless = sorted(n for n in used if WarAction[n] not in points)
    assert not priceless, ("У этих WarAction нет цены в WarConfig (add_score "
                           "бросит UnknownWarActionError): " + ", ".join(priceless))



def check_earnings_reach_status_ledger():
    """Любое начисление OAC обязано попасть в total_earned.

    Статус (ранг, топ, «Полярная звезда», гейты) живёт на total_earned.
    Дельту автоматически ловит repository.atomic_update, но пути, которые
    сохраняются прямым save(), проходят мимо — и награда двигала бы кошелёк,
    не двигая ранг. Так тихо потерялись бы награды достижений (~19 900 OAC
    суммарно) и бонус за выбор стороны у любого, кто уже что-то тратил.

    Правило: если начисление баланса живёт ВНЕ замыкания, переданного в
    atomic_update, рядом обязано быть явное начисление total_earned.
    """
    import ast
    path = os.path.join(ROOT, "bot.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()

    atomic = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "atomic_update"):
            for a in n.args:
                if isinstance(a, ast.Name):
                    atomic.add(a.id)

    funcs = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = max(getattr(x, "lineno", n.lineno) for x in ast.walk(n))
            funcs.append((n.name, n.lineno, end))

    def owner(ln):
        best = None
        for name, s, e in funcs:
            if s <= ln <= e and (best is None or s > best[1]):
                best = (name, s, e)
        return best[0] if best else "?"

    bad = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        credits = ".balance +=" in s or (".balance = " in s and "or 0) +" in s)
        if not credits:
            continue
        if owner(i) in atomic:
            continue                      # дельту поймает atomic_update
        # Окно широкое: между начислением и учётом статуса часто стоит
        # объяснительный комментарий.
        window = "\n".join(lines[i - 1:i + 9])
        if "total_earned" not in window:
            bad.append(f"bot.py:{i} в {owner(i)}(): {s[:70]}")
    assert not bad, ("Начисление OAC вне транзакции без учёта в total_earned "
                     "(кошелёк вырастет, ранг — нет):\n  " + "\n  ".join(bad))


def check_no_calls_to_undefined_names():
    """Вызов имени, которого нигде нет в модуле, — гарантированный NameError.

    Ровно это молчало 20 дней: `_send_http_message` вызывалась в 4 местах
    (джекпот, ранг-ап, итоги войны, Час Удачи) и не была определена НИГДЕ —
    ни как функция, ни как импорт. try/except вокруг каждого вызова глотал
    исключение молча, поэтому ни разработка, ни прод-логи не подняли тревогу:
    весь публичный социальный слой игры не работал, а выглядело всё исправным.

    Проверка — over-approximation: «определено» значит «есть как def функции/
    класса, импорт, module-level присваивание, ИЛИ используется как параметр/
    локальная переменная где угодно в файле». Это специально мягче честного
    scope-анализа — чтобы не поднимать шум на легальных локальных именах,
    ценой пропуска экзотических случаев. Ловит именно этот класс бага: имя,
    которого в файле нет вообще ни в какой роли.
    """
    import builtins as _builtins

    bad = []
    for mod in MODULES:
        path = os.path.join(ROOT, mod)
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)

        defined = set(dir(_builtins))
        called = []  # (lineno, name)

        # Исключение — УЗКОЕ НАМЕРЕННО: только буквальный `except NameError`,
        # идиом «вызови, если функция вообще существует» (см. repository.py:
        # invalidate_menu_cache — зарезервированный хук на будущее).
        #
        # `except Exception` / голый `except:` НЕ исключаем, хотя они тоже
        # технически глотают NameError, — потому что ЭТО и есть форма, в которой
        # настоящий баг прятался 20 дней (_send_http_message падала внутри
        # `except Exception` в happy_hour_trigger и _safe_send_guild_message, и
        # ни разу не всплыла). Если разрешить широкий except гасить проверку,
        # страж перестаёт ловить ровно тот баг, ради которого написан.
        guarded_call_ids = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            catches_name_error = any(
                (isinstance(h.type, ast.Name) and h.type.id == "NameError")
                or (isinstance(h.type, ast.Tuple) and any(
                    isinstance(e, ast.Name) and e.id == "NameError" for e in h.type.elts))
                for h in node.handlers
            )
            if catches_name_error:
                for n in node.body:
                    for c in ast.walk(n):
                        if isinstance(c, ast.Call):
                            guarded_call_ids.add(id(c))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        defined.add(t.id)
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
                t = node.target
                if isinstance(t, ast.Name):
                    defined.add(t.id)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    defined.add(a.asname or a.name)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, (ast.For, ast.comprehension)):
                target = node.target if isinstance(node, ast.For) else node.target
                for n in ast.walk(target):
                    if isinstance(n, ast.Name):
                        defined.add(n.id)
            elif isinstance(node, ast.withitem) and node.optional_vars:
                for n in ast.walk(node.optional_vars):
                    if isinstance(n, ast.Name):
                        defined.add(n.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
                defined.update(node.names)
            elif isinstance(node, ast.Lambda):
                for a in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                    defined.add(a.arg)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if id(node) not in guarded_call_ids:
                    called.append((node.lineno, node.func.id))

        for lineno, name in called:
            if name not in defined:
                bad.append(f"{mod}:{lineno}: вызов неопределённого имени `{name}`")

    assert not bad, ("Вызов имени, которого нет в файле, — гарантированный "
                     "NameError в рантайме:\n  " + "\n  ".join(bad))


def check_named_blunt_calls_pass_ctx():
    """`create_named_blunt` без ctx — гарантированный ValueError, съедающий транзакцию.

    Функция первым же действием делает `if ctx is None: raise ValueError`. Все её
    вызовы сидят внутри `atomic_update`, поэтому исключение не просто теряет
    блант — оно откатывает ВСЮ транзакцию вокруг. Так молча не работали два
    приза сразу: легендарка за реферал (реферер терял ещё и +50 OAC, титул и
    связку с приглашённым) и «Философский Камень» — джекпот алхимии, у которого
    откат съедал списанные бланты и попытку.

    Обе копии выглядели правдоподобно и жили рядом с корректными вызовами —
    глазами такое не ловится, поэтому проверка статическая.
    """
    path = os.path.join(ROOT, "bot.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if name != "create_named_blunt":
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        # ctx может прийти и как **kwargs — такой вызов не считаем дефектным
        has_splat = any(k.arg is None for k in node.keywords)
        if "ctx" not in kwargs and not has_splat:
            bad.append(f"bot.py:{node.lineno}")
    assert not bad, (
        "create_named_blunt вызывается без ctx → ValueError и откат транзакции "
        "(приз не выдаётся, потраченное не возвращается):\n  " + "\n  ".join(bad))


def check_advertised_odds_match_real_roll():
    """Шанс, показанный игроку, обязан совпадать с шансом в коде.

    Продукт запрещает фейковый дефицит. Нарушение уже жило в проде: экран крафта
    сообщал «Обнаружен у 3.5% игроков» при фактических 55%, а легендарку объявлял
    «0.17%» при реальных 2% — в 12 раз реже правды. Числа были захардкожены и
    поданы как статистика, то есть как факт о мире.

    Расхождение такого рода не ловится ни компилятором, ни ревью: обе части
    выглядят правдоподобно и лежат в разных концах файла. Поэтому сверяем их
    машинно — пороги ролла из create_named_blunt против таблицы, которую видит
    игрок. Разъедутся при любой правке баланса → тест упадёт, и подкрутить
    вероятность молча, забыв про текст, станет невозможно.
    """
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()

    # пороги ролла: `if r < 0.02: rarity = "legendary"` и т.д.
    thresholds = {}
    for m in re.finditer(r"r\s*<\s*([0-9.]+)\s*:\s*rarity\s*=\s*[\"'](\w+)[\"']", src):
        thresholds[m.group(2)] = float(m.group(1))
    assert thresholds, "не найдены пороги ролла редкости в create_named_blunt"

    # кумулятивные пороги → вероятность каждого тира
    order = sorted(thresholds.items(), key=lambda kv: kv[1])
    real, prev = {}, 0.0
    for name, th in order:
        real[name] = th - prev
        prev = th
    real["common"] = round(1.0 - prev, 10)

    # то, что видит игрок: ("ЛЕГЕНДАРНЫЙ", "2%") и хвостовой common в .get(...)
    shown = {r: int(p) for r, p in
             re.findall(r"[\"'](\w+)[\"']:\s*\(\s*[\"'][^\"']+[\"']\s*,\s*[\"'](\d+)%[\"']\s*\)", src)}
    m_common = re.search(r"\.get\(rarity,\s*\(\s*[\"'][^\"']+[\"']\s*,\s*[\"'](\d+)%[\"']\s*\)\)", src)
    if m_common:
        shown["common"] = int(m_common.group(1))
    # Fail closed — намеренно. Если таблицу не удалось разобрать, значит экран
    # крафта переписали в другой форме, и сверить показанное с реальным больше
    # нечем. Молчаливый пропуск здесь означал бы, что запрет на фейковый дефицит
    # снова держится на честном слове, — поэтому неразобранная форма считается
    # провалом, а не «нечего проверять». Переписал экран → почини и эту сверку.
    assert shown, (
        "не удалось разобрать таблицу шансов на экране крафта: сверка с реальным "
        "роллом невозможна. Если формат вывода менялся — обнови разбор здесь, "
        "иначе запрет на фейковый дефицит перестаёт проверяться.")

    bad = []
    for tier, pct in shown.items():
        expected = round(real.get(tier, -1) * 100)
        if expected != pct:
            bad.append(f"{tier}: игроку показано {pct}%, реальный шанс {expected}%")
    assert not bad, ("Заявленный шанс расходится с реальным (фейковый дефицит):\n  "
                     + "\n  ".join(bad))


def main():
    passed = []
    check_no_calls_to_undefined_names()
    passed.append("нет вызовов неопределённых имён (страж «_send_http_message 20 дней молчала»)")
    check_duplicate_dict_keys()
    passed.append("нет дублей ключей в словарях (страж бага «мёртвая Удача»)")
    check_conn_used_after_async_with()
    passed.append("нет 'conn' вне своего async with (страж бага progress_hub)")
    check_achievement_rewards_parseable()
    passed.append("все награды достижений парсятся DSL (страж бага «Уникальный фон»)")
    check_war_actions_exist()
    passed.append("все WarAction из bot.py существуют и имеют цену (страж «тяга без очков»)")
    check_earnings_reach_status_ledger()
    passed.append("все начисления OAC доходят до total_earned (страж «ранг не растёт»)")
    check_named_blunt_calls_pass_ctx()
    passed.append("create_named_blunt везде получает ctx (страж «приз не выдался, транзакция откатилась»)")
    check_advertised_odds_match_real_roll()
    passed.append("показанный шанс равен реальному роллу (страж фейкового дефицита)")

    for name in passed:
        print(f"  OK  {name}")
    print(f"\nКрэш-страж пройден: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
