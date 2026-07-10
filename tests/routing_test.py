"""Тест маршрутизации кнопок: каждая callback_data должна иметь обработчик.

Ловит «мёртвые» кнопки (как были luck_berserk, mines_bet_, choice_,
skip_onboarding), которые выдают «Неизвестная команда». БД/Redis не нужны —
парсит исходник bot.py и сверяет с реальными реестрами.

    python tests/routing_test.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TOKEN", "123:DUMMY")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://botuser:botpass@127.0.0.1:5432/botdb")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("RENDER_URL", "")

import bot

# callback_data, которые обрабатываются ВНУТРИ хендлеров по query.data,
# а не через реестры (осознанные исключения).
INTERNAL = {"noop"}

BOT_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def main() -> int:
    exact = set(bot.EXACT_HANDLERS)
    callbacks = set(bot.CALLBACKS)
    prefixes = set(bot.PREFIX_HANDLERS)
    src = open(BOT_PY, encoding="utf-8").read()

    static = set(re.findall(r'callback_data\s*=\s*"([^"{]+)"', src))
    fprefixes = {
        f.split("{")[0]
        for f in re.findall(r'callback_data\s*=\s*f"([^"]*)"', src)
        if f.split("{")[0]
    }

    def routed(cb: str) -> bool:
        return (cb in INTERNAL or cb in exact or cb in callbacks
                or any(cb.startswith(p) for p in prefixes))

    def routed_prefix(pre: str) -> bool:
        return (pre in INTERNAL or pre in exact or pre in callbacks
                or any(pre.startswith(p) or p.startswith(pre) for p in prefixes))

    broken_static = sorted(c for c in static if not routed(c))
    broken_prefix = sorted(p for p in fprefixes if not routed_prefix(p))

    assert not broken_static, f"Кнопки без маршрута (статические): {broken_static}"
    assert not broken_prefix, f"Кнопки без маршрута (динамические): {broken_prefix}"

    print(f"  OK  все callback_data маршрутизируются "
          f"({len(static)} статических + {len(fprefixes)} динамических)")
    print(f"\nТест роутинга пройден: 1/1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
