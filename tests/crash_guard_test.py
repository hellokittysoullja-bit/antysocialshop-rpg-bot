"""Страж классов прод-крашей, которые уже кусали живую игру.

Каждая проверка здесь — не абстрактная гигиена, а защёлка против КОНКРЕТНОГО
бага, что реально ломал бота в проде и был найден вручную/случайно. Тест
превращает такой тихий класс в громкий фейл на CI.

    python tests/crash_guard_test.py

БД/Redis не нужны — только AST-разбор исходников.
"""
import ast
import os
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


def main():
    passed = []
    check_duplicate_dict_keys()
    passed.append("нет дублей ключей в словарях (страж бага «мёртвая Удача»)")
    check_conn_used_after_async_with()
    passed.append("нет 'conn' вне своего async with (страж бага progress_hub)")

    for name in passed:
        print(f"  OK  {name}")
    print(f"\nКрэш-страж пройден: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
