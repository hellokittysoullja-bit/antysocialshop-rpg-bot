"""Регрессии качества игрового опыта.

Запуск: TOKEN=123456:test RENDER_URL=http://localhost python3 tests/game_experience_test.py
Проверяет обещания игроку, а не только техническую корректность: осмысленный
прогресс, прозрачность вероятностей и отсутствие таймерного давления.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("TOKEN", "123456:game-experience-test-token")
os.environ.setdefault("RENDER_URL", "http://localhost")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://localhost/test")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot
from game_content import CRAFT_MEDALS, FARM_MEDALS, SMOKE_MEDALS

PASSED = 0


def check(condition, description):
    global PASSED
    if not condition:
        raise AssertionError(description)
    PASSED += 1
    print(f"  OK  {description}")


def test_thematic_progression():
    all_names = [name for _threshold, name, _reward in FARM_MEDALS + CRAFT_MEDALS + SMOKE_MEDALS]
    forbidden = {"🥉 Бронза", "🥈 Серебро", "🥇 Золото", "💎 Платина"}
    check(not forbidden.intersection(all_names), "у трёх базовых петель есть собственная тематическая идентичность")
    check(len(set(all_names)) == len(all_names), "награды трёх базовых петель не дублируют друг друга")


def test_transparent_rewards():
    craft = bot._format_craft_menu_text(100, 0, 0, "🧵 Первый Свиток", 1, 0)
    check("5% шанс" in craft, "скрытый шанс второго бланта раскрыт до решения о крафте")
    check("55%" in craft and "2%" in craft, "шансы редкости именного крафта показаны до траты")

    source = Path(bot.__file__).read_text(encoding="utf-8")
    smoke_slice = source[source.index("async def smoke_callback"):source.index("async def do_smoke")]
    check("2% джекпот" in smoke_slice and "25% осечка" in smoke_slice,
          "шансы затяжки показаны до расходования бланта")
    check("защищённой тяги" in smoke_slice or "гарантированно даст улов" in smoke_slice,
          "защита от плохой серии объяснена игроку")


def test_autonomy_and_no_fomo_pressure():
    source = Path(bot.__file__).read_text(encoding="utf-8")
    choice_slice = source[source.index("async def choose_play_path"):source.index("# ====== ФУНКЦИЯ ПЕРЕДАЧИ")]
    for route in ("collector", "builder", "explorer"):
        check(f'"{route}"' in choice_slice, f"доступен добровольный маршрут: {route}")
    check("ориентир, а не обязательство" in choice_slice, "выбранный маршрут не превращается в необратимый класс")
    legacy_fomo = "БОНУС ЗА СКОРОСТЬ" in source or "пока не поздно" in source
    check(not legacy_fomo, "в именном крафте нет пятиминутного давления ради бонуса")


def main():
    test_thematic_progression()
    test_transparent_rewards()
    test_autonomy_and_no_fomo_pressure()
    print(f"Game Experience quality gate пройден: {PASSED} проверок")


if __name__ == "__main__":
    main()
