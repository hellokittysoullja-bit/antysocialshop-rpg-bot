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

JPEG_MAGIC = b"\xff\xd8\xff"
# Карта уходит фото-сообщением в Telegram: держим её лёгкой, иначе отправка
# тормозит, а зерно плёнки раздувает файл (на PNG было ~830 КБ).
MAX_BYTES = 400_000


def _mk(name, rarity, h):
    return {"name": name, "rarity": rarity, "hash": h,
            "rare_number": f"{rarity[0].upper()}-1234", "id": f"blunt_{h}"}


def check_all_rarities_valid_image():
    bad = []
    for r in ("common", "rare", "epic", "legendary"):
        blob = blunt_art.render_blunt_card(_mk("Крик Бездны", r, "0xabc123def456"), "ghost")
        if not blob.startswith(JPEG_MAGIC):
            bad.append(f"{r}: не JPEG")
        if len(blob) < 3000:
            bad.append(f"{r}: подозрительно маленькая картинка ({len(blob)}B)")
        if len(blob) > MAX_BYTES:
            bad.append(f"{r}: слишком тяжёлая ({len(blob)}B > {MAX_BYTES}) — Telegram будет тормозить")
    assert not bad, "Рендер карточки сломан:\n  " + "\n  ".join(bad)


def check_rarity_hierarchy_not_color_only():
    """Редкость обязана читаться СТРУКТУРНО, а не только цветом (~8% дальтоников).

    Проверяем сам контракт данных: у более высокого тира строго больше делений
    статуса (пипсов), а орнамент/фольга не убывают. Если кто-то в будущем
    сведёт различие тиров к одному цвету — тест упадёт.
    """
    order = ("common", "rare", "epic", "legendary")
    bad = []
    prev = None
    for r in order:
        cfg = blunt_art._RARITY[r]
        if prev is not None:
            if cfg["pips"] <= prev["pips"]:
                bad.append(f"{r}: пипсов не больше, чем у предыдущего тира")
            if cfg["rings"] < prev["rings"]:
                bad.append(f"{r}: орнамент беднее предыдущего тира")
            if cfg["foil"] < prev["foil"]:
                bad.append(f"{r}: фольга слабее предыдущего тира")
            if cfg["stars"] <= prev["stars"]:
                bad.append(f"{r}: звёздное поле не плотнее предыдущего тира")
        prev = cfg
    assert not bad, "Иерархия редкости держится только на цвете:\n  " + "\n  ".join(bad)


def check_deterministic():
    a = blunt_art.render_blunt_card(_mk("Шёпот", "epic", "0xdeadbeef0000"), "x")
    b = blunt_art.render_blunt_card(_mk("Шёпот", "epic", "0xdeadbeef0000"), "x")
    assert a == b, "рендер недетерминирован: один блант дал разные карты"


def check_unique_by_hash():
    a = blunt_art.render_blunt_card(_mk("Крик", "legendary", "0x1111111111111111"), "x")
    b = blunt_art.render_blunt_card(_mk("Крик", "legendary", "0x2222222222222222"), "x")
    assert a != b, "разный хэш дал одинаковую карту — нет уникальности коллекции"


# Порог различимости превью. Замер на 15 парах одного тира:
#   до введения позы  — 3.98 (композиция была жёстко зашита, «уникальность»
#                       держалась на шуме звёзд и завитках дыма)
#   после             — 10.87
# 7.0 лежит между режимами с запасом в обе стороны: случайный дрейф не уронит,
# а откат к общей композиции — уронит сразу.
MIN_THUMB_DIFF = 7.0


def check_perceptually_unique():
    """Различимость обязана пережить УМЕНЬШЕНИЕ до превью.

    Побайтовое неравенство (check_unique_by_hash) — негодный сторож этого
    обещания: любой зерновой шум делает файлы разными, и тест горит зелёным,
    пока владелец коллекции видит десять одинаковых карточек с разными
    подписями. Именно так и было: композиция (точка уголька, наклон, длина)
    была константой, а различались только звёзды, дым и серийник — всё, что
    исчезает первым при уменьшении.

    Поэтому меряем то, что видит глаз в ленте: кадры сводятся к 60×84, и
    сравнивается средняя яркостная разница. Тир фиксирован — внутри ОДНОГО
    тира карты обязаны различаться силуэтом, иначе коллекции не существует.
    """
    from PIL import Image, ImageChops, ImageStat
    import io as _io

    names = ["Тень Пепла", "Голос Праха", "Сны Мертвеца", "Ржавый Клык", "Пепел Зари"]
    thumbs = []
    for i, n in enumerate(names):
        blob = blunt_art.render_blunt_card(_mk(n, "common", f"0x{i + 1:016x}aa"), "Странник")
        thumbs.append(Image.open(_io.BytesIO(blob)).convert("L").resize((60, 84), Image.LANCZOS))

    diffs = []
    for i in range(len(thumbs)):
        for j in range(i + 1, len(thumbs)):
            d = ImageChops.difference(thumbs[i], thumbs[j])
            diffs.append(ImageStat.Stat(d).mean[0])

    avg = sum(diffs) / len(diffs)
    assert avg >= MIN_THUMB_DIFF, (
        f"карты одного тира неразличимы в превью: средняя разница {avg:.2f} "
        f"< {MIN_THUMB_DIFF}. Коллекция держится на том, что предметы видно "
        f"РАЗНЫМИ; уникальность в шуме, исчезающем при уменьшении, не считается."
    )


def check_pose_is_deterministic_and_varied():
    """Поза — паспорт предмета: та же по хэшу, но разная между хэшами.

    Проверяем обе половины контракта разом, потому что ломаются они порознь:
    сорванный детерминизм превращает «мой блант» в случайную картинку при
    каждой отправке, а сорванная вариативность возвращает общую композицию.
    """
    import random as _random

    def pose_for(h):
        rng = _random.Random(int(h, 16))
        return blunt_art._pose(rng, blunt_art._RARITY["common"])

    assert pose_for("0xabc123") == pose_for("0xabc123"), \
        "поза недетерминирована: один блант дал бы разные силуэты"

    poses = [pose_for(f"0x{i:08x}") for i in range(24)]
    # Направление скрутки — самый крупный различитель силуэта. Если вдруг все
    # карты смотрят в одну сторону, коллекция снова становится однообразной.
    mirrored = sum(1 for p in poses if p["mirrored"])
    assert 4 <= mirrored <= 20, \
        f"зеркальная поза вырождена ({mirrored}/24): силуэты смотрят в одну сторону"
    assert len({p["seam_style"] for p in poses}) >= 3, \
        "рисунок обёртки почти не варьируется — тело предмета одинаково у всех"
    assert len({round(p["ash"], 2) for p in poses}) >= 15, \
        "прогар почти не варьируется — пропала самая читаемая деталь истории предмета"


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
            blob = blunt_art.render_blunt_card(c, "")
            if not blob.startswith(JPEG_MAGIC):
                bad.append(f"{c}: не JPEG")
        except Exception as e:
            bad.append(f"{c}: упал ({e})")
    assert not bad, "Рендер падает на кривых данных (должен деградировать мягко):\n  " + "\n  ".join(bad)


def check_collection_wall():
    """Витрина: единственное место, где уникальность силуэтов вообще проявляется.

    Пока «Кодекс» был текстовым списком, разные силуэты не с чем было сравнить —
    а новизна работает только на сравнении. Витрина обязана: не падать на любых
    данных (это украшение, а не критичный путь), оставаться лёгкой для Telegram,
    честно показывать пустые слоты и — главное — рисовать плитки теми же позами,
    что и полные карточки, иначе она врёт о содержимом коллекции.
    """
    bad = []
    cases = [
        ("пусто", []),
        ("None в списке", [None]),
        ("нет hash/rarity", [{"name": "В"}]),
        ("эмодзи + длинное имя", [{"name": "🔥" * 5 + "Длинноеимя" * 6,
                                   "rarity": "legendary", "hash": "zzz"}]),
        ("переполнение слотов", [_mk(f"Б{i}", "epic", f"0x{i:04x}") for i in range(12)]),
    ]
    for label, items in cases:
        try:
            blob = blunt_art.render_collection_wall(items, "Странник", owned_tiers=2)
            if not blob.startswith(JPEG_MAGIC):
                bad.append(f"{label}: не JPEG")
            if len(blob) > MAX_BYTES:
                bad.append(f"{label}: слишком тяжёлая ({len(blob)}B)")
        except Exception as e:
            bad.append(f"{label}: упала ({e})")
    assert not bad, "Витрина коллекции сломана:\n  " + "\n  ".join(bad)

    # Детерминизм: одна и та же коллекция → та же витрина (иначе кэш file_id
    # по составу страницы отдавал бы картинку, не соответствующую содержимому).
    items = [_mk("Крик", "legendary", "0x1111"), _mk("Шёпот", "rare", "0x2222")]
    a = blunt_art.render_collection_wall(items, "X", owned_tiers=2)
    b = blunt_art.render_collection_wall(items, "X", owned_tiers=2)
    assert a == b, "витрина недетерминирована — кэш по составу станет врать"

    # Счёт собранного берётся снаружи: по видимым плиткам он занижал бы правду
    # у игрока с коллекцией больше витрины.
    one = blunt_art.render_collection_wall(items, "X", owned_tiers=1)
    four = blunt_art.render_collection_wall(items, "X", owned_tiers=4)
    assert one != four, "owned_tiers не влияет на витрину — счётчик собранного decorативен"


def check_wall_is_cheaper_than_cards():
    """Витрина обязана быть ДЕШЕВЛЕ, чем набор полных карточек.

    Смысл витрины ровно в этом: показать шесть предметов, не заплатив за шесть
    полных рендеров (~220 МБ пикового RSS каждый — верный OOM на малом
    инстансе). Если однажды витрина догонит карточку по стоимости, её незачем
    держать отдельно — тест поймает такой дрейф.
    """
    import time

    items = [_mk(f"Б{i}", "epic", f"0x{i:04x}") for i in range(6)]
    t0 = time.time()
    blunt_art.render_collection_wall(items, "X", owned_tiers=3)
    wall = time.time() - t0

    t0 = time.time()
    blunt_art.render_blunt_card(_mk("Один", "epic", "0xabcd"), "X")
    card = time.time() - t0

    assert wall < card, (
        f"витрина из 6 плиток ({wall:.2f}с) не быстрее ОДНОЙ карточки ({card:.2f}с) — "
        f"смысл отдельного лёгкого рендера потерян")


def check_forms_are_a_real_set():
    """Формы скрутки обязаны быть КОНЕЧНЫМ, РОВНЫМ и ЧЕСТНЫМ набором.

    Это единственная в игре долгая коллекционная цель: набор из 4 редкостей
    закрывается почти сразу (легендарка падает бесплатно за реферал, исповедь
    или алхимию) и после 4/4 не зовёт никуда. Набор форм держится на трёх
    свойствах, и каждое ломается по-своему:

    * КОНЕЧНОСТЬ — «7 из 8» можно сказать только про перечислимый набор;
    * РАВНОМЕРНОСТЬ — перекос превращает сбор последней формы в стену;
    * СОВПАДЕНИЕ С КАРТИНКОЙ — имя формы обязано описывать тот силуэт, который
      игрок видит, иначе набор описывает не те предметы, что лежат в коллекции.
    """
    import random as _random
    from collections import Counter

    catalog = blunt_art.all_forms()
    ids = [f["id"] for f in catalog]
    assert len(catalog) == blunt_art.FORMS_TOTAL, "каталог форм не равен заявленному размеру"
    assert sorted(ids) == list(range(blunt_art.FORMS_TOTAL)), "id форм не сплошные 0..N-1"
    assert len({f["name"] for f in catalog}) == blunt_art.FORMS_TOTAL, "имена форм повторяются"

    # детерминизм: форма — паспорт предмета, она не может «переехать»
    it = _mk("Крик", "epic", "0xabc123def456")
    assert blunt_art.form_of(it) == blunt_art.form_of(it), "форма недетерминирована"

    # форма не зависит от редкости: апгрейд тира не должен менять личность
    a = blunt_art.form_of(_mk("X", "common", "0xdeadbeef1234"))
    b = blunt_art.form_of(_mk("X", "legendary", "0xdeadbeef1234"))
    assert a == b, "форма зависит от редкости — один блант менял бы форму вместе с тиром"

    # равномерность: перекос делает последнюю форму стеной
    c = Counter(blunt_art.form_of(_mk("n", "common", f"0x{i * 2654435761:016x}"))["id"]
                for i in range(4000))
    assert len(c) == blunt_art.FORMS_TOTAL, f"выпадают не все формы: {sorted(c)}"
    lo, hi = min(c.values()), max(c.values())
    assert hi / lo < 1.5, f"формы выпадают неравномерно ({lo}..{hi}) — набор станет гриндом"

    # имя формы обязано совпадать с позой, которой нарисован силуэт
    bad = []
    for i in range(200):
        item = _mk("n", "rare", f"0x{i * 7919:016x}")
        seed = int(item["hash"][2:], 16)
        pose = blunt_art._pose(_random.Random(seed), blunt_art._RARITY["rare"])
        nm = blunt_art.form_of(item)["name"]
        want_dir = blunt_art._FACING_NAMES[bool(pose["mirrored"])]
        want_weave = blunt_art._WEAVE_NAMES[pose["seam_style"]]
        if not nm.startswith(want_weave) or not nm.endswith(want_dir):
            bad.append(f"{item['hash']}: имя «{nm}» ≠ поза ({want_weave}/{want_dir})")
    assert not bad, ("имя формы расходится с нарисованным силуэтом:\n  "
                     + "\n  ".join(bad[:5]))


def main():
    passed = []
    check_all_rarities_valid_image()
    passed.append("все 4 редкости → валидный JPEG нормального веса")
    check_rarity_hierarchy_not_color_only()
    passed.append("иерархия редкости структурная (пипсы/орнамент/фольга), не только цвет")
    check_deterministic()
    passed.append("детерминизм: один блант → та же карта")
    check_unique_by_hash()
    passed.append("уникальность: разный хэш → разная карта")
    check_perceptually_unique()
    passed.append("различимость переживает уменьшение до превью (силуэт, а не шум)")
    check_pose_is_deterministic_and_varied()
    passed.append("поза детерминирована по хэшу и не вырождена")
    check_robust_to_bad_data()
    passed.append("устойчивость к кривым данным (пустое/эмодзи/мусор)")
    check_collection_wall()
    passed.append("витрина коллекции: не падает, детерминирована, слоты честные")
    check_wall_is_cheaper_than_cards()
    passed.append("витрина из 6 плиток дешевле одной полной карточки")
    check_forms_are_a_real_set()
    passed.append("формы скрутки: конечный ровный набор, совпадающий с силуэтом")
    for name in passed:
        print(f"  OK  {name}")
    print(f"\nИнварианты карточки бланта пройдены: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
