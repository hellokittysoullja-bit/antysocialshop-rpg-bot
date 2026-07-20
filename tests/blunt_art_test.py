"""Инварианты процедурного рендера карточки бланта (tests/blunt_art_test.py).

Карточка — движок «настоящего коллекционирования»: каждый блант обязан быть
визуально УНИКАЛЕН и ВОСПРОИЗВОДИМ (один и тот же блант — одна и та же карта),
а рендер — не падать на кривых данных (пустое имя, эмодзи, нет хэша), потому что
это украшение, а не критичный путь.

    python tests/blunt_art_test.py

Пропускается (skip), если Pillow не установлен — прод так же мягко деградирует.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import blunt_art
except Exception as e:  # Pillow отсутствует — как и в проде, мягко выходим
    print(f"  SKIP  blunt_art недоступен ({e}) — прод деградирует на текст")
    raise SystemExit(0)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _mk(name, rarity, h):
    return {"name": name, "rarity": rarity, "hash": h,
            "rare_number": f"{rarity[0].upper()}-1234", "id": f"blunt_{h}"}


def check_all_rarities_valid_png():
    bad = []
    for r in ("common", "rare", "epic", "legendary"):
        png = blunt_art.render_blunt_card(_mk("Крик Бездны", r, "0xabc123def456"), "ghost")
        if not png.startswith(PNG_MAGIC):
            bad.append(f"{r}: не PNG")
        if len(png) < 3000:
            bad.append(f"{r}: подозрительно маленький PNG ({len(png)}B)")
    assert not bad, "Рендер карточки сломан:\n  " + "\n  ".join(bad)


def check_deterministic():
    a = blunt_art.render_blunt_card(_mk("Шёпот", "epic", "0xdeadbeef0000"), "x")
    b = blunt_art.render_blunt_card(_mk("Шёпот", "epic", "0xdeadbeef0000"), "x")
    assert a == b, "рендер недетерминирован: один блант дал разные карты"


def check_unique_by_hash():
    a = blunt_art.render_blunt_card(_mk("Крик", "legendary", "0x1111111111111111"), "x")
    b = blunt_art.render_blunt_card(_mk("Крик", "legendary", "0x2222222222222222"), "x")
    assert a != b, "разный хэш дал одинаковую карту — нет уникальности коллекции"


def check_robust_to_bad_data():
    bad = []
    cases = [
        {"rarity": "common"},                                  # нет имени/хэша
        {"name": "🔥🎰💀", "rarity": "rare"},                   # только эмодзи
        {"name": "", "rarity": "legendary", "hash": ""},        # пустые
        {"name": "x" * 200, "rarity": "epic", "hash": "zzz"},   # мусорный хэш, длинное имя
        {"name": "Обычное Имя", "rarity": "unknown_tier", "hash": "0xabc"},  # неизв. редкость
    ]
    for c in cases:
        try:
            png = blunt_art.render_blunt_card(c, "")
            if not png.startswith(PNG_MAGIC):
                bad.append(f"{c}: не PNG")
        except Exception as e:
            bad.append(f"{c}: упал ({e})")
    assert not bad, "Рендер падает на кривых данных (должен деградировать мягко):\n  " + "\n  ".join(bad)


def main():
    passed = []
    check_all_rarities_valid_png()
    passed.append("все 4 редкости → валидный PNG")
    check_deterministic()
    passed.append("детерминизм: один блант → та же карта")
    check_unique_by_hash()
    passed.append("уникальность: разный хэш → разная карта")
    check_robust_to_bad_data()
    passed.append("устойчивость к кривым данным (пустое/эмодзи/мусор)")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты карточки бланта пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
