"""Процедурный рендер коллекционной карточки именного бланта.

Настоящая коллекционность рождается, когда КАЖДЫЙ блант визуально уникален —
а не когда все «обычные» на одну картинку. Здесь генерируется карточка-реликвия,
детерминированно выведенная из хэша конкретного бланта (item["hash"]): один и тот
же блант всегда даёт один и тот же арт, но два разных бланта — разный. Картинка
рисуется ОДИН раз на создание, кэшируется как Telegram file_id и переиспользуется.

── Дизайн-позиция ───────────────────────────────────────────────────────────
Карта должна читаться как НАПЕЧАТАННЫЙ предмет, а не как вывод скрипта. Всё
ниже — следствия этой позиции, и каждое проверяемо:

* Рамка — ОБЪЁМНОЕ ТЕЛО, а не линия. Полоса с немонотонной металлической
  рампой (провал яркости в середине светлого поля = горизонт отражения). Именно
  это отличает золото от жёлтой краски; обводка толщиной 1px не отличает.
* Свет одноисточниковый: верх/лево получают светлый край рампы, низ/право —
  тёмный. Без этого «выдавленности» не возникает.
* Иерархия по прищур-тесту: арт → имя → редкость → мета. Замер яркости зон на
  размытом кадре — единственная честная проверка; глазу здесь верить нельзя.
* Ровно ОДНА точка максимального контраста на карту — уголёк. Пикселей ярче
  L=230 должно быть меньше 0.1%, и все они у уголька.
* Редкость кодируется СТРУКТУРНО: площадь арта, архитектура рамки, наличие
  элементов, которых у низких тиров НЕТ вовсе (печать, нумерация тиража,
  прорыв рамки дымом). Цвет — последний по важности носитель (~8% мужчин
  не различают его).
* Композиция строится ОТ точки интереса (уголёк на пересечении третей), а не
  от центра: пять систем, концентричных одной точке, дают мишень, из которой
  глазу некуда двигаться.
* Плотность 70/30: почти вся площадь несёт микродеталь низкого контраста,
  чистое поле — только вокруг фокуса. Пустая тёмная зона читается как
  «не доделали».

Технические следствия, без которых остальное не работает:
* _SCALE=3: при 2× линия width=1 после LANCZOS превращается в полупрозрачный
  серый волосок, и любая филигрань физически не переживает даунсэмпл.
* Зерно и все случайные величины берутся из seed карты — иначе один и тот же
  блант даёт разные карты, что ломает сам смысл «паспорта предмета».

Зависит только от Pillow. Если Pillow нет или рендер падает — вызывающий код
откатывается на текст (карточка — украшение, не критичный путь).

    render_blunt_card(item, owner_name) -> bytes (JPEG)
"""
from __future__ import annotations

import io
import math
import os
import random
import re

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# ── Палитры редкости ────────────────────────────────────────────────
# Ни один канал не равен 255: чистый максимум зарезервирован за угольком.
# Иначе золото рамки спорит с огнём, и источник света перестаёт читаться.
# Насыщенность держим в 0.25–0.40 — выше начинается «неоновая вывеска».
#
#   pips    — делений статуса (редкость читается без цвета)
#   rings   — колец угловой филиграни
#   foil    — сила фольги (локальной, по маске орнамента)
#   stars   — плотность звёздного поля
#   rays    — лучи от уголька
#   rosette — слоёв гильоше
#   band    — ширина металлической полосы рамки, px при 640
#   art_y1  — низ окна арта в долях высоты (площадь арта = ступень редкости)
#   burst   — прорыв дыма за рамку (привилегия топ-редкостей)
#   seal    — печать-сигил (элемент, которого у низких тиров НЕТ вовсе)
#   edition — нумерация тиража вместо простого серийника
_RARITY = {
    "common": {
        "glow": (24, 116, 78), "frame": (78, 184, 134), "accent": (184, 246, 211),
        "label": "ОБЫЧНЫЙ", "pips": 1, "rings": 0, "foil": 0.04, "stars": 38,
        "rays": 8, "rosette": 3, "band": 9.0, "art_y1": 0.608,
        "burst": False, "seal": False, "edition": False,
    },
    "rare": {
        "glow": (26, 86, 186), "frame": (72, 142, 226), "accent": (179, 226, 255),
        "label": "РЕДКИЙ", "pips": 2, "rings": 1, "foil": 0.10, "stars": 58,
        "rays": 18, "rosette": 4, "band": 11.0, "art_y1": 0.614,
        "burst": False, "seal": False, "edition": False,
    },
    "epic": {
        "glow": (124, 38, 188), "frame": (177, 82, 226), "accent": (242, 184, 255),
        "label": "ЭПИЧЕСКИЙ", "pips": 3, "rings": 2, "foil": 0.24, "stars": 86,
        "rays": 28, "rosette": 6, "band": 13.0, "art_y1": 0.620,
        "burst": True, "seal": False, "edition": False,
    },
    "legendary": {
        "glow": (198, 102, 16), "frame": (232, 164, 48), "accent": (255, 232, 150),
        "label": "ЛЕГЕНДАРНЫЙ", "pips": 4, "rings": 3, "foil": 0.34, "stars": 126,
        "rays": 38, "rosette": 8, "band": 16.0, "art_y1": 0.630,
        "burst": True, "seal": True, "edition": True,
    },
}

# Немонотонная рампа металла: провал в середине светлого поля — «горизонт
# отражения». Линейный градиент читается как пластик, этот — как металл.
_METAL_RAMP = (0.22, 0.60, 1.00, 0.38, 0.82, 0.30, 0.55, 0.18)

_BG_CORE = (30, 21, 44)
_BG_EDGE = (5, 4, 10)
_ART_BG = (6, 5, 12)

# Обёртка скрутки — одна на всех редкостях: предмет должен узнаваться сразу,
# а статус нести огранка и ореол. Диапазон расширен, чтобы было чем лепить объём.
_WRAP_LIGHT = (198, 132, 68)
_WRAP_MID = (104, 58, 30)
_WRAP_DARK = (24, 12, 8)
_EMBER = (255, 150, 48)
# Дым холодный — третий тон карты. Тёплый дым делал легендарку монохромной
# (золото и огонь в одном хью), а холодный делает уголёк по-настоящему горячим.
_SMOKE_COOL = (86, 84, 108)
_SMOKE_WARM = (168, 110, 64)

_SCALE = 3
_W, _H = 640, 896          # пропорция 2.5:3.5 — канон коллекционной карты

# Версия визуала. Карточка кэшируется как Telegram file_id прямо на предмете и
# больше НИКОГДА не перерисовывается — file_id для своего бота не протухает. Без
# счётчика версии любая правка рендера доходила только до предметов, созданных
# после деплоя: у игрока со собранной коллекцией карты навсегда оставались
# старыми. Это ровно та когорта, ради которой рендер и улучшают.
#
# Поднимать при КАЖДОМ изменении, меняющем картинку. История файла — пять
# переписываний подряд, шестое неизбежно, поэтому инвалидация сделана ленивой
# (сверка версии при показе), а не разовой миграцией: миграцию пришлось бы
# повторять каждый раз, а этот счётчик работает сам и навсегда.
ART_VERSION = 6

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu",)


# ── Утилиты ─────────────────────────────────────────────────────────

def _font(name: str, size: int):
    for d in _FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _sanitize(text: str, limit: int = 34) -> str:
    """Оставляем только то, что рисует DejaVu (кириллица/латиница/цифры/пунктуация).

    Эмодзи и прочие не-BMP символы Pillow с DejaVu рисует «тофу»-квадратами."""
    text = str(text or "")
    keep = re.sub(r"[^0-9A-Za-zЀ-ӿ ,.\-!?'«»\"()]+", "", text)
    keep = re.sub(r"\s+", " ", keep).strip()
    return keep[:limit] or "Безымянный"


def _sc(col, k):
    """Умножить цвет на коэффициент с клэмпом."""
    return tuple(max(0, min(255, int(c * k))) for c in col)


def _mix(a, b, t):
    return tuple(max(0, min(255, int(a[i] * (1 - t) + b[i] * t))) for i in range(3))


def _dim(layer, k):
    """Приглушить слой перед screen: свет должен быть локальным, а не заливать кадр."""
    return layer.point(lambda v: int(v * k))


def _text_w(draw, s, font, tracking=0):
    """Ширина с трекингом. Последний глиф трекинг НЕ добавляет — иначе строка
    визуально уезжает влево при центрировании."""
    if not tracking:
        return draw.textlength(s, font=font)
    return sum(draw.textlength(ch, font=font) for ch in s) + tracking * max(0, len(s) - 1)


def _draw_tracked(draw, xy, s, font, fill, tracking=0):
    """Текст с межбуквенным интервалом. Капс без трекинга читается как ошибка вёрстки,
    а Pillow трекинг не умеет — рисуем поглифно."""
    x, y = xy
    if not tracking:
        draw.text((x, y), s, font=font, fill=fill)
        return
    for ch in s:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def _vgrad(size, top, bottom):
    """Вертикальный градиент (считается узкой полосой и растягивается)."""
    w, h = size
    strip = Image.new("RGB", (1, max(2, h // 4)))
    sp = strip.load()
    hh = strip.size[1]
    for y in range(hh):
        t = y / max(1, hh - 1)
        sp[0, y] = _mix(top, bottom, t)
    return strip.resize((w, h), Image.BILINEAR)


def _radial_bg(size, core, edge):
    """Радиальная виньетка: свет в центре, тьма к краям — глубина вместо плоскости.

    Считаем на маленьком холсте и растягиваем: per-pixel Python по полному
    размеру был бы непозволительно медленным."""
    w, h = size
    sw, sh = 80, 112
    small = Image.new("RGB", (sw, sh))
    px = small.load()
    cx, cy = sw / 2, sh * 0.38
    maxd = math.hypot(max(cx, sw - cx), max(cy, sh - cy))
    for yy in range(sh):
        for xx in range(sw):
            t = min(1.0, (math.hypot(xx - cx, yy - cy) / maxd) ** 1.35)
            px[xx, yy] = _mix(core, edge, t)
    return small.resize((w, h), Image.BICUBIC)


def _apply_grain(img, rng, strength=6):
    """Зерно плёнки — материальность. Без него градиент выглядит «пластиково».

    Зерно берётся из ТОГО ЖЕ seed, что и вся карта: Image.effect_noise тянет
    глобальный ГПСЧ, из-за чего один и тот же блант давал разные карты — а это
    ломает сам контракт коллекционности.

    Шум ЗНАКОПЕРЕМЕННЫЙ вокруг 128: складывать шум со средним 128 напрямую
    нельзя — это подмешало бы ~128 яркости на весь холст и вымыло бы карту.
    """
    w, h = img.size
    sw, sh = max(1, w // 2), max(1, h // 2)
    noise = Image.frombytes("L", (sw, sh), rng.randbytes(sw * sh))
    k = strength / 74.0
    noise = noise.point(lambda v: int(128 + (v - 128) * k))
    noise = noise.resize((w, h), Image.BILINEAR)
    return ImageChops.add(img, Image.merge("RGB", (noise, noise, noise)),
                          scale=1.0, offset=-128)


def _linen(size, rng, tint, u):
    """Фактура льняной бумаги: две встречные волновые системы.

    Даёт «ценную бумагу» по всей плоскости и убивает пластиковость градиента.
    Считается в четверти размера и растягивается — иначе слишком дорого.
    """
    w, h = size
    sw, sh = max(8, w // 4), max(8, h // 4)
    lay = Image.new("L", (sw, sh), 0)
    px = lay.load()
    ph1, ph2 = rng.uniform(0, 6.283), rng.uniform(0, 6.283)
    p1 = max(2.0, 5.0 * u / 4)
    p2 = max(2.0, 6.5 * u / 4)
    for y in range(sh):
        for x in range(sw):
            a = math.sin((x * 0.978 + y * 0.208) / p1 * 6.283 + ph1)
            b = math.sin((x * 0.978 - y * 0.208) / p2 * 6.283 + ph2)
            px[x, y] = int(128 + 58 * (a * 0.5 + b * 0.5))
    lay = lay.resize((w, h), Image.BILINEAR)
    col = Image.new("RGB", (w, h), tint)
    out = Image.new("RGB", (w, h), (0, 0, 0))
    out.paste(col, (0, 0), lay.point(lambda v: int(abs(v - 128) * 0.42)))
    return out


# ── Металл и рельеф ─────────────────────────────────────────────────

def _metal_frame(draw, x0, y0, x1, y1, band, base, wgt):
    """Рамка как ОБЪЁМНАЯ ПОЛОСА с немонотонной металлической рампой.

    Рисуется вложенными контурами — по одному на стоп рампы. Верх/лево получают
    светлый вариант стопа, низ/право — тёмный: свет одноисточниковый, иначе
    «выдавленности» не возникает и полоса читается плоской краской.
    """
    n = len(_METAL_RAMP)
    step = max(1.0, band / n)
    for i, k in enumerate(_METAL_RAMP):
        o = i * step
        lit = _sc(base, k * 1.16)
        shd = _sc(base, k * 0.52)
        ax0, ay0, ax1, ay1 = x0 + o, y0 + o, x1 - o, y1 - o
        if ax1 <= ax0 or ay1 <= ay0:
            break
        draw.line([(ax0, ay0), (ax1, ay0)], fill=lit, width=wgt)   # верх
        draw.line([(ax0, ay0), (ax0, ay1)], fill=lit, width=wgt)   # лево
        draw.line([(ax0, ay1), (ax1, ay1)], fill=shd, width=wgt)   # низ
        draw.line([(ax1, ay0), (ax1, ay1)], fill=shd, width=wgt)   # право


def _inset_shadow(img, box, u, depth=0.55):
    """Внутренняя тень по верхней/левой кромке — зона читается УТОПЛЕННОЙ."""
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    if w <= 2 or h <= 2:
        return img
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rectangle([0, 0, w - 1, h - 1], outline=255, width=max(2, int(u * 5)))
    mask = mask.filter(ImageFilter.GaussianBlur(u * 4))
    # гасим нижнюю/правую половину — тень только от верхней и левой кромки
    fade = Image.new("L", (w, h), 0)
    fd = ImageDraw.Draw(fade)
    fd.polygon([(0, 0), (w, 0), (0, h)], fill=255)
    mask = ImageChops.multiply(mask, fade.filter(ImageFilter.GaussianBlur(u * 6)))
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    region = img.crop((x0, y0, x1, y1))
    region = Image.composite(dark, region, mask.point(lambda v: int(v * depth)))
    img.paste(region, (x0, y0))
    return img


def _drop_shadow(img, box, u, off=(4, 5), blur=7, depth=0.6):
    """Падающая тень под зоной — зона читается ВЫСТУПАЮЩЕЙ."""
    x0, y0, x1, y1 = [int(v) for v in box]
    lay = Image.new("L", img.size, 0)
    ImageDraw.Draw(lay).rectangle(
        [x0 + off[0] * u, y0 + off[1] * u, x1 + off[0] * u, y1 + off[1] * u], fill=255)
    lay = lay.filter(ImageFilter.GaussianBlur(blur * u))
    dark = Image.new("RGB", img.size, (0, 0, 0))
    return Image.composite(dark, img, lay.point(lambda v: int(v * depth)))


def _metal_text(img, xy, text, font, base, tracking, u, size):
    """Текст с металлической заливкой и тиснением.

    Единственный элемент, кроме уголька, которому позволен высокий контраст:
    имя обязано быть второй точкой притяжения. Плоская белая заливка этого не даёт.
    """
    d = ImageDraw.Draw(img)
    x, y = xy
    # тиснение: тёмная копия ниже-правее, светлая выше-левее
    _draw_tracked(d, (x + u * 2, y + u * 2), text, font, (4, 3, 7), tracking)
    _draw_tracked(d, (x - u, y - u), text, font, _sc(base, 0.55), tracking)
    # металлическая заливка через маску текста
    mask = Image.new("L", img.size, 0)
    _draw_tracked(ImageDraw.Draw(mask), (x, y), text, font, 255, tracking)
    grad = _vgrad(img.size, _sc(base, 1.30), _sc(base, 0.62))
    # блик-горизонт в верхней трети глифа (та же логика, что у рампы)
    band = Image.new("L", img.size, 0)
    ImageDraw.Draw(band).rectangle([0, y + size * 0.26, img.size[0], y + size * 0.42], fill=90)
    grad = ImageChops.add(grad, Image.merge("RGB", (band, band, band)))
    img.paste(grad, (0, 0), mask)


def _card_edge(card, rim, radius=17, inset=7):
    """Карта лежит на поверхности: скруглённый срез, падающая тень, кромка картона.

    Самый сильный сигнал «физический предмет» — не сам скос, а тень под картой.
    Тень считается в четверти холста: мягкой тени точность не нужна, а блюр там
    в ~16 раз дешевле. Кромка тёплая серо-бежевая — белая читалась бы обводкой,
    тёплая читается срезом картона.
    """
    w, h = card.size
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([inset, inset, w - 1 - inset, h - 1 - inset],
                                        radius=radius, fill=255)
    sm = m.resize((w // 4, h // 4), Image.BILINEAR)
    s = ImageChops.offset(sm, 0, 2).filter(ImageFilter.GaussianBlur(4)) \
        .resize((w, h), Image.BILINEAR)

    out = Image.new("RGB", (w, h), (7, 6, 10))
    out.paste(Image.new("RGB", (w, h), (0, 0, 0)), (0, 0), s.point(lambda v: int(v * 0.8)))
    out.paste(card, (0, 0), m)

    hi = Image.new("L", (w, h), 0)
    ImageDraw.Draw(hi).rounded_rectangle([inset, inset, w - 1 - inset, h - 1 - inset],
                                         radius=radius, outline=255, width=2)
    out.paste(Image.new("RGB", (w, h), (0, 0, 0)), (0, 0),
              ImageChops.subtract(hi, ImageChops.offset(hi, -1, -1)).point(lambda v: int(v * 0.85)))
    out.paste(Image.new("RGB", (w, h), rim), (0, 0),
              ImageChops.subtract(hi, ImageChops.offset(hi, 1, 1)).point(lambda v: int(v * 0.5)))
    return out


def _ornament_mask(img, u):
    """Маска «где есть орнамент»: разность кадра с его размытой копией.

    Нужна, чтобы фольга ложилась ТОЛЬКО на рамки, филигрань и буквы. Фольга по
    всему полю читается как неравномерная засветка — это антипремиум.
    """
    g = img.convert("L")
    hi = ImageChops.difference(g, g.filter(ImageFilter.GaussianBlur(u * 2)))
    hi = hi.point(lambda v: 255 if v > 10 else 0)
    return hi.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(u))


def _foil(size, rng, color, amount, mask):
    """Голографическая фольга: узкая полоса с хроматическим сдвигом каналов.

    Каналы R/G/B считаются с разной фазой → полоса переливается. Жёсткий профиль
    (^8) вместо мягкого: настоящая фольга — узкий высококонтрастный блик, а
    широкое мутное пятно читается как артефакт сжатия.
    """
    if amount <= 0:
        return None
    w, h = size
    sw, sh = 108, 150
    phase = rng.uniform(0, math.pi * 2)
    chans = []
    for dph in (-0.10, 0.0, 0.10):
        small = Image.new("L", (sw, sh))
        px = small.load()
        for yy in range(sh):
            for xx in range(sw):
                v = math.sin((xx / sw * 0.9 + yy / sh * 0.8) * math.pi + phase + dph * math.pi)
                px[xx, yy] = int(max(0.0, v) ** 8 * 255)
        chans.append(small.resize((w, h), Image.BICUBIC)
                     .filter(ImageFilter.GaussianBlur(w / 90)))
    tinted = [c.point(lambda v, k=color[i] / 255.0: int(v * k * amount))
              for i, c in enumerate(chans)]
    foil = Image.merge("RGB", tinted)
    black = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(foil, black, mask)


# ── Орнамент ────────────────────────────────────────────────────────

def _guilloche(draw, cx, cy, rng, color, R, layers, width, fade_center=True):
    """Гильоше-розетка (гипотрохоиды) — защитная сетка, как на банкноте.

    Главный источник плотной детализации. Две встречные системы (по и против
    часовой) с разной фазой дают муар — именно он читается как «дорого».
    Отношение радиусов берётся МАЛЫМ ЦЕЛЫМ: произвольное сворачивается в
    спутанный клубок вместо чистых лепестков.
    """
    for k in range(layers):
        Rk = R * (1.0 - 0.075 * k)
        ratio = rng.choice([11, 13, 14, 16, 17, 19])
        r = Rk / ratio
        d = r * rng.uniform(0.7, 1.3)
        sign = 1 if k % 2 == 0 else -1          # встречное вращение → муар
        ph = rng.uniform(0, math.pi * 2)
        steps = max(240, ratio * 30)
        pts = []
        for i in range(steps + 1):
            t = i / steps * ratio * 2 * math.pi
            x = (Rk - r) * math.cos(t + ph) + sign * d * math.cos((Rk - r) / r * t)
            y = (Rk - r) * math.sin(t + ph) - sign * d * math.sin((Rk - r) / r * t)
            pts.append((cx + x, cy + y))
        draw.line(pts, fill=color, width=width, joint="curve")


def _rays(draw, cx, cy, rng, color, count, r0, r1, width, sector=None):
    """Лучи от ИСТОЧНИКА (уголька). Симметричные спицы из пустого центра не
    читаются вообще — god-rays из реального источника читаются мгновенно."""
    if count <= 0:
        return
    base = rng.uniform(0, math.pi * 2) if sector is None else sector[0]
    span = math.pi * 2 if sector is None else sector[1]
    for i in range(count):
        a = base + span * (i / max(1, count - 1) - (0.5 if sector else 0))
        ln = r1 * (1.0 if i % 2 == 0 else 0.68)
        draw.line([(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
                   (cx + ln * math.cos(a), cy + ln * math.sin(a))],
                  fill=color, width=width)


def _energy_aura(size, cx, cy, rng, glow, accent, tier, radius, u):
    """Игровой ореол редкости: кольца, разрывы и кристаллические лучи.

    Гильоше сообщает «ценная печать», но почти исчезает в превью. Ореол держит
    статус на самом дешёвом масштабе: один круг у common, два у rare, разорванная
    корона у epic и полноценное солнечное ядро у legendary. Всё строится
    геометрией Pillow — никаких внешних картинок или генеративных моделей.
    """
    tier = max(1, min(4, int(tier)))
    w, h = size
    crisp = Image.new("RGB", size, (0, 0, 0))
    cd = ImageDraw.Draw(crisp)

    # Незамкнутые кольца выглядят энергией, а не мишенью. Каждый следующий тир
    # получает дополнительный физический контур, а не только более яркий цвет.
    for i in range(tier):
        rr = radius * (0.36 + i * 0.115)
        col = _sc(_mix(glow, accent, 0.48), 0.34 + i * 0.09)
        ww = max(2, int(u * (1.1 + i * 0.24)))
        start = int(rng.uniform(8, 70))
        gap = 54 - tier * 6
        cd.arc([cx - rr, cy - rr, cx + rr, cy + rr], start=start,
               end=start + 176 - gap, fill=col, width=ww)
        cd.arc([cx - rr, cy - rr, cx + rr, cy + rr], start=start + 198,
               end=start + 356 - gap // 2, fill=col, width=ww)
        for a in (math.radians(start), math.radians(start + 198)):
            x, y = cx + rr * math.cos(a), cy + rr * math.sin(a)
            _diamond(cd, x, y, u * (2.2 + tier * 0.7), accent, col, max(1, int(u)))

    # С epic появляются крупные осколки света; legendary получает плотную
    # корону. Они читаются даже после уменьшения карточки до 120 px.
    shards = max(0, (tier - 1) * 7)
    base = rng.uniform(0, math.pi * 2)
    for i in range(shards):
        a = base + 2 * math.pi * i / max(1, shards) + rng.uniform(-0.08, 0.08)
        r0 = radius * rng.uniform(0.43, 0.55)
        r1 = radius * rng.uniform(0.62, 0.92) * (1.12 if tier == 4 and i % 2 == 0 else 1.0)
        half_w = u * rng.uniform(2.0, 5.5) * (0.72 + tier * 0.12)
        tx, ty = -math.sin(a) * half_w, math.cos(a) * half_w
        p0 = (cx + math.cos(a) * r0, cy + math.sin(a) * r0)
        p1 = (cx + math.cos(a) * r1, cy + math.sin(a) * r1)
        cd.polygon([(p0[0] - tx, p0[1] - ty), p1, (p0[0] + tx, p0[1] + ty)],
                   fill=_sc(_mix(glow, accent, 0.60), 0.20 + tier * 0.055))

    # Мягкий bloom берётся из той же геометрии, поэтому свечение подчёркивает
    # форму, а не превращает фон в бесформенное цветное облако.
    soft = crisp.filter(ImageFilter.GaussianBlur(u * (7 + tier * 2)))
    return ImageChops.screen(crisp, _dim(soft, 0.72 if tier < 4 else 0.92))


def _corner_flourish(draw, x, y, sx, sy, size, rings, frame, accent, wgt):
    """Угловая филигрань. Примыкает к рамке, а не висит рядом: оторванные
    L-скобки читаются как наклеенный клипарт."""
    for k in range(max(1, rings + 1)):
        o = size * 0.17 * k
        ln = size - size * 0.20 * k
        if ln <= 0:
            continue
        col = accent if k == 0 else frame
        draw.line([(x + sx * o, y + sy * o), (x + sx * (o + ln), y + sy * o)],
                  fill=col, width=wgt)
        draw.line([(x + sx * o, y + sy * o), (x + sx * o, y + sy * (o + ln))],
                  fill=col, width=wgt)
    if rings >= 2:
        d = size * 0.30
        draw.line([(x + sx * d * 0.5, y + sy * d * 1.6), (x + sx * d * 1.6, y + sy * d * 0.5)],
                  fill=frame, width=max(1, wgt - 1))
    if rings >= 3:
        d = size * 0.20
        _diamond(draw, x + sx * d, y + sy * d, size * 0.075, accent, frame, wgt)


def _diamond(draw, cx, cy, r, fill, outline, wgt):
    pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
    if fill:
        draw.polygon(pts, fill=fill)
    draw.line(pts + [pts[0]], fill=outline, width=wgt, joint="curve")


def _rule(draw, x0, x1, y, frame, accent, u, gem=True):
    """Линейка-разделитель с ромбом по центру: роли текста разделяются
    ЛИНЕЙКОЙ, а не только отступом — иначе это бланк, а не карта."""
    mid = (x0 + x1) / 2
    g = u * 7 if gem else 0
    draw.line([(x0, y), (mid - g * 1.8, y)], fill=frame, width=max(1, int(u)))
    draw.line([(mid + g * 1.8, y), (x1, y)], fill=frame, width=max(1, int(u)))
    if gem:
        _diamond(draw, mid, y, g, None, accent, max(1, int(u)))


def _pips(draw, cx, y, count, total, accent, frame, u):
    """Деления статуса: редкость читается СТРУКТУРНО, без опоры на цвет.

    Ряд центрируется по ЦЕНТРУ МАСС, а не по геометрии: заполненное деление
    визуально тяжелее пустого (~2.5:1), поэтому при 1 из 4 геометрически
    центрированный ряд заваливается влево на ~6.5px — это видно глазом.
    """
    gap = u * 19
    r = u * 5.4
    xs = [-(total - 1) * gap / 2 + i * gap for i in range(total)]
    ws = [1.0 if i < count else 0.40 for i in range(total)]
    cx += -sum(w * x for w, x in zip(ws, xs)) / sum(ws)
    start = cx - (total - 1) * gap / 2
    for i in range(total):
        x = start + i * gap
        if i < count:
            _diamond(draw, x, y, r * 1.55, None, frame, max(1, int(u)))
            _diamond(draw, x, y, r * 0.9, accent, accent, max(1, int(u)))
        else:
            _diamond(draw, x, y, r * 0.9, None, _sc(frame, 0.55), max(1, int(u)))


def _seal(draw, cx, cy, r, rng, frame, accent, u):
    """Печать-сигил: элемент, которого у низких редкостей НЕТ ВООБЩЕ.

    Появление нового ОБЪЕКТА читается сильнее, чем усиление существующего."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=frame, width=max(2, int(u * 1.6)))
    draw.ellipse([cx - r * 0.82, cy - r * 0.82, cx + r * 0.82, cy + r * 0.82],
                 outline=_sc(frame, 0.7), width=max(1, int(u)))
    n = rng.choice([5, 7])
    rot = rng.uniform(0, math.pi)
    vs = [(cx + r * 0.66 * math.cos(rot + 2 * math.pi * i / n),
           cy + r * 0.66 * math.sin(rot + 2 * math.pi * i / n)) for i in range(n)]
    step = 2 if n == 5 else 3
    poly = [vs[(i * step) % n] for i in range(n)]
    draw.line(poly + [poly[0]], fill=accent, width=max(1, int(u * 1.2)), joint="curve")


def _microtext(draw, x0, x1, y, text, font, color, u):
    """Микротекст — «награда за приближение» и знак подлинности.

    На грани читаемости: функция декоративная, но именно она заставляет
    приблизить карту, а это главный триггер скриншота."""
    unit = text + "  ·  "
    w = draw.textlength(unit, font=font)
    if w <= 0:
        return
    n = int((x1 - x0) / w) + 1
    draw.text((x0, y), unit * n, font=font, fill=color)


# ── Предмет ─────────────────────────────────────────────────────────

def _smoke(draw, cx, cy, rng, accent, spread, height, ribbons, warm_frac=0.16):
    """Клубы дыма: восходящие ленты, вьющиеся по синусоиде. Уникальны по хэшу.

    Рисуется ЧАСТЫМИ мелкими кружками и обязательно размывается вызывающим
    кодом: без размытия лента читается как цепочка «сосисок», а не как дым.
    Тёплый только у самого источника, дальше холодный — это даёт карте третий
    тон и делает уголёк горячим по контрасту.
    """
    for _ in range(ribbons):
        x = cx + rng.uniform(-spread * 0.35, spread * 0.35)
        y = cy
        amp = rng.uniform(spread * 0.3, spread * 0.85)
        freq = rng.uniform(1.3, 3.0)
        phase = rng.uniform(0, math.pi * 2)
        rise = height * rng.uniform(0.65, 1.15)
        r0 = spread * rng.uniform(0.05, 0.10)
        steps = 150
        for i in range(steps):
            t = i / steps
            px = x + math.sin(phase + t * freq * math.pi) * amp * t
            py = y - rise * t
            r = r0 * (0.5 + 2.0 * t)
            fade = (1.0 - t) ** 1.35
            base = _SMOKE_WARM if t < warm_frac else _SMOKE_COOL
            if i % 34 == 0:
                base = accent
            col = _sc(base, 0.12 + 0.72 * fade)
            draw.ellipse([px - r, py - r, px + r, py + r], fill=col)


def _sparkles(draw, cx, cy, rng, spread, accent, count, u, direction=None):
    """Искры-блики, кластеризованные в конусе от источника: равномерная россыпь
    даёт визуальный шум с равным весом, кластер — акцент."""
    for _ in range(count):
        if direction is not None and rng.random() < 0.7:
            a = direction + rng.uniform(-0.6, 0.6)
            d = spread * rng.uniform(0.15, 0.9)
        else:
            a = rng.uniform(0, math.pi * 2)
            d = spread * rng.uniform(0.35, 1.05)
        x, y = cx + math.cos(a) * d, cy + math.sin(a) * d * 0.85
        near = 1.0 - min(1.0, d / max(1e-6, spread))
        s = u * rng.uniform(2.4, 6.5) * (0.5 + 0.9 * near)
        w = max(1, int(u * 1.1))
        col = _sc(accent, 0.45 + 0.55 * near)
        draw.line([(x - s, y), (x + s, y)], fill=col, width=w)
        draw.line([(x, y - s), (x, y + s)], fill=col, width=w)


def _relic(draw, cx, cy, length, ang, rng, accent, u, lit=1, pose=None):
    """Скрутка: стилизованный иконический силуэт, вылепленный светом.

    Тело рисуется ПОЛОСАМИ с ламбертовым спадом поперёк толщины — три плоских
    полигона давали флаг, а не цилиндр (внутренний диапазон 2.6:1 против 5.5:1
    у освещённого тела). Плюс контровой свет по верхней грани: самый дешёвый и
    самый сильный признак объёма.

    `lit` (+1/−1) — с какой стороны локальной оси лежит освещённая грань. При
    зеркальной позе (скрутка смотрит вверх-ВЛЕВО) локальная нормаль n<0
    указывает уже вниз-влево, и без переворота знака блик уезжал бы на нижнюю
    грань — свет стал бы бить снизу. Один источник на карте важнее позы, поэтому
    зеркалим не свет, а адресацию граней.

    `pose` — паспорт индивидуальности: рисунок обёртки и degree прогара. Это то,
    что отличает ОДИН блант от другого в превью 120px, где цвет редкости уже
    прочитан, а мелкая филигрань физически не видна.
    """
    pose = pose or {}
    tier = max(1, min(4, int(pose.get("tier", 1))))
    ca, sa = math.cos(ang), math.sin(ang)
    # Толщина — главный фикс силуэта. Старые 5–6% длины читались как сигарета;
    # 7.8–9.1% дают тяжёлый blunt/cigar-профиль, который выглядит наградой.
    t0 = length * rng.uniform(0.078, 0.091) * pose.get("girth", 1.0)
    t1 = t0 * rng.uniform(0.69, 0.79)
    half = length / 2

    def P(uu, v):
        return (cx + uu * ca - v * sa, cy + uu * sa + v * ca)

    def cross_section(uu, thick, long_r, fill, outline=None, width=1):
        pts = []
        for i in range(26):
            a = 2 * math.pi * i / 26
            pts.append(P(uu + math.cos(a) * long_r, math.sin(a) * thick))
        draw.polygon(pts, fill=fill)
        if outline:
            draw.line(pts + [pts[0]], fill=outline, width=width, joint="curve")

    # Тёмный торец кладётся ДО тела: половину эллипса перекроют полосы, и он
    # будет восприниматься настоящим срезом цилиндра, а не наклейкой поверх.
    cross_section(-half, t0, t0 * 0.34, _WRAP_DARK, _sc(accent, 0.32),
                  max(1, int(u * 1.1)))

    # тело: 14 полос, ключ сверху-слева
    N = 14
    for j in range(N):
        n0 = -1.0 + 2.0 * j / N
        n1 = -1.0 + 2.0 * (j + 1) / N
        nl = ((n0 + n1) / 2) * lit               # нормаль в системе источника света
        sh = max(0.0, math.cos((nl + 0.40) * 1.45)) ** 1.55
        col = list(_mix(_WRAP_DARK, _WRAP_LIGHT, 0.08 + sh * 0.83))
        if abs(nl + 0.40) < 0.10:                     # спекуляр — узкая полоса
            col = [min(255, col[k] + (70, 52, 32)[k]) for k in range(3)]
        if nl > 0.55:                                 # тёплый отскок от уголька
            b = (nl - 0.55) / 0.45
            col = [min(255, col[k] + int((34, 16, 5)[k] * b)) for k in range(3)]
        draw.polygon([P(-half, n0 * t0), P(half, n0 * t1),
                      P(half, n1 * t1), P(-half, n1 * t0)], fill=tuple(col))

    # контровой свет по освещённой грани: холодный у мундштука → горячий у уголька
    NS = 18
    cool = _sc(accent, 0.5)
    for i in range(NS):
        a0, a1 = i / NS, (i + 1) / NS
        u0, u1 = -half + length * a0, -half + length * a1
        tt0 = t0 + (t1 - t0) * a0
        tt1 = t0 + (t1 - t0) * a1
        mm = ((a0 + a1) / 2) ** 2.2
        c = _mix(cool, (214, 168, 118), mm)
        draw.line([P(u0, -tt0 * 0.97 * lit), P(u1, -tt1 * 0.97 * lit)],
                  fill=c, width=max(1, int(u * 1.3)))
    # теневая грань — глухая тень
    draw.line([P(-half, t0 * 0.98 * lit), P(half, t1 * 0.98 * lit)], fill=(22, 16, 14),
              width=max(1, int(u * 1.2)))

    # ── Рисунок обёртки: один из четырёх «почерков скрутки» ──
    # Швы — самая крупная фактура на теле предмета, единственная деталь скрутки,
    # переживающая уменьшение до превью. Пока рисунок был один на всех, два
    # разных бланта в коллекции читались как одна карточка с разным именем.
    # Смесь с цветом бумаги обязательна: на легендарке чистое золото
    # обесцвечивает предмет в «бетонную плиту».
    seam = _mix(accent, _WRAP_LIGHT, 0.5)
    style = pose.get("seam_style", "rings")
    sw = max(1, int(u * 1.1))

    def _seam_at(uu, skew, k=0.9, width=sw):
        tt = t0 + (t1 - t0) * ((uu + half) / length)
        sk = tt * skew
        draw.line([P(uu - sk, -tt * k), P(uu + sk, tt * k)], fill=_sc(seam, 0.8), width=width)

    ns = rng.randint(5, 8)
    span = length * 0.85
    if style == "braid":
        # плетение: встречные диагонали дают ромбическую сетку
        for i in range(ns):
            uu = -half * 0.72 + span * (i / max(1, ns - 1))
            _seam_at(uu, 0.85)
            _seam_at(uu, -0.85)
    elif style == "spiral":
        # спираль: один наклон, шаг чаще — «туго закрученная»
        for i in range(ns + 2):
            uu = -half * 0.72 + span * (i / max(1, ns + 1))
            _seam_at(uu, 1.25)
    elif style == "banded":
        # редкие широкие пояса — «грубая, кустарная» скрутка
        for i in range(max(2, ns - 3)):
            uu = -half * 0.66 + span * 0.9 * (i / max(1, ns - 4))
            _seam_at(uu, 0.18, k=1.0, width=max(2, int(u * 2.2)))
    else:  # "rings" — исходный ровный шов
        for i in range(ns):
            uu = -half * 0.72 + span * (i / max(1, ns - 1))
            _seam_at(uu, 0.55)

    # Натуральные прожилки листа: редкая крупная фактура делает оболочку
    # материальной и отличает дорогой blunt от гладкой коричневой трубки.
    vein = _mix(_WRAP_LIGHT, accent, 0.18)
    for q in (-0.42, 0.02, 0.43):
        pts = []
        for i in range(15):
            a = i / 14
            uu = -half * 0.76 + length * 0.70 * a
            tt = t0 + (t1 - t0) * ((uu + half) / length)
            vv = tt * (q + math.sin(a * math.pi * 2 + q * 4) * 0.07)
            pts.append(P(uu, vv))
        draw.line(pts, fill=_sc(vein, 0.44), width=max(1, int(u * 0.75)))

    # Коллекционная бандероль — новый предметный маркер статуса. Она впервые
    # появляется на rare, усложняется на epic и получает камень на legendary.
    if tier >= 2:
        bc = -half * (0.13 if tier == 2 else 0.05)
        bw = length * (0.035 + tier * 0.005)
        band_dark = _sc(accent, 0.30)
        band_mid = _mix(_sc(accent, 0.62), _WRAP_LIGHT, 0.28)
        draw.polygon([P(bc - bw, -t0 * 0.98), P(bc + bw, -t0 * 0.91),
                      P(bc + bw, t0 * 0.91), P(bc - bw, t0 * 0.98)], fill=band_dark)
        for k, kk in enumerate((0.22, 0.78)):
            uu = bc - bw + 2 * bw * kk
            draw.line([P(uu, -t0 * 0.95), P(uu, t0 * 0.95)],
                      fill=_sc(band_mid, 0.72 + k * 0.25), width=max(1, int(u * 1.5)))
        draw.line([P(bc, -t0 * 0.88), P(bc, t0 * 0.88)],
                  fill=_sc(accent, 0.88), width=max(1, int(u * (1.4 + tier * 0.25))))
        if tier == 4:
            gx, gy = P(bc, -t0 * 0.06 * lit)
            _diamond(draw, gx, gy, t0 * 0.24, _sc(accent, 0.95), (255, 244, 206),
                     max(1, int(u * 1.1)))

    bu = -half * rng.uniform(0.60, 0.76)
    draw.line([P(bu, -t0 * 0.98), P(bu, t0 * 0.98)], fill=seam, width=max(2, int(u * 2.0)))

    # пепел: градиент вдоль оси + тлеющие трещины (не серый колпачок).
    # Длина прогара — вторая ось индивидуальности и единственная, что несёт
    # ЛОР: «едва зажжён» против «скурен почти до пальцев» — разные истории
    # одного предмета, читаемые мгновенно и без единого слова.
    au = half * pose.get("ash", 0.86)
    # Пепел рисуется НАВСТРЕЧУ жару, а не от него: раньше градиент светлел у
    # основания и темнел к самому угольку — то есть ровно там, где уголь обязан
    # раскаляться. При коротком прогаре это скрывал bloom уголька; при длинном
    # (а теперь он бывает длинным) сегмент читался плоской серой накладкой.
    # Здесь холодный спёкшийся пепел у тела → раскалённый уголь у кончика, с
    # осыпающейся кромкой: крошка сужается и рвётся, а не идёт ровной трубой.
    # Пепел строится ПРОДОЛЬНЫМИ волокнами, а не поперечными ломтями. Ломти
    # давали идеально прямую линию прогара — предмет выглядел как деталь с
    # отпиленным концом. Бумага так не горит: линия огня рваная, и каждое
    # волокно догорает на свою глубину. Разброс старта по волокнам — и есть
    # тот самый рваный фронт.
    #
    # Тона держим ГОРЯЧИМИ с запасом: поверх скрутки ляжет холодный передний
    # дым (screen), и всё, что здесь выглядит «в самый раз», после него уходит
    # в синевато-серую плиту. Компенсируем на входе.
    M = 9
    _ash_cold, _ash_warm = (66, 57, 53), (196, 96, 40)
    for mm in range(M):
        v0 = -1.0 + 2.0 * mm / M
        v1 = -1.0 + 2.0 * (mm + 1) / M
        # каждое волокно догорело на свою глубину → фронт огня неровный
        start = au + (half - au) * rng.uniform(-0.22, 0.20)
        NA = 6
        for j in range(NA):
            b0, b1 = j / NA, (j + 1) / NA
            aa0 = start + (half - start) * b0
            aa1 = start + (half - start) * b1
            heat = b0 ** 1.35                             # жар копится у кончика
            c = list(_mix(_ash_cold, _ash_warm, heat))
            d = rng.uniform(0.84, 1.14)                   # спёкшийся уголь неоднороден
            c = [max(0, min(255, int(v * d))) for v in c]
            # крошка осыпается: к кончику волокно тоньше
            k0 = (0.99 - 0.14 * b0) * rng.uniform(0.94, 1.02)
            k1 = (0.99 - 0.14 * b1) * rng.uniform(0.94, 1.02)
            draw.polygon([P(aa0, v0 * t1 * k0), P(aa1, v0 * t1 * k1),
                          P(aa1, v1 * t1 * k1), P(aa0, v1 * t1 * k0)], fill=tuple(c))
    # тлеющие трещины — только в горячей половине, иначе «светится вся сигара»
    for _ in range(rng.randint(3, 5)):
        b = rng.uniform(0.45, 0.97)
        uu = au + (half - au) * b
        sp = t1 * rng.uniform(0.35, 0.72)
        glow = _mix((214, 96, 32), (255, 190, 88), b)
        draw.line([P(uu, -sp), P(uu, sp)], fill=glow, width=max(1, int(u)))

    # Раскалённый торец: тёмное кольцо, горячая сердцевина и трещины. Круглый
    # bloom остаётся снаружи, но теперь под ним есть реальная конструкция.
    cross_section(half, t1 * 0.98, t1 * 0.30, (82, 28, 14), (242, 108, 32),
                  max(2, int(u * 1.5)))
    tx, ty = P(half, 0)
    cross_section(half + t1 * 0.04, t1 * 0.62, t1 * 0.18,
                  (224, 82, 24), (255, 184, 80), max(1, int(u)))
    for a in (-1.9, -0.7, 0.45, 1.65):
        draw.line([(tx, ty), P(half + math.cos(a) * t1 * 0.13,
                               math.sin(a) * t1 * 0.76)],
                  fill=(255, 196, 96), width=max(1, int(u * 0.9)))

    return P(half, 0) + (t1,)


def _ember(art, ex, ey, tip_t, ang, u):
    """Уголёк — единственная точка максимального контраста на карте.

    Три ступени bloom с РАСТУЩИМ отношением blur/radius: при отношении около
    единицы гауссиан даёт не спад, а диск с мягким краем — отсюда прежний
    «пончик». Ядра идут монотонно к белому, иначе средний слой светится ярче
    внешнего и читается как кольцо.
    """
    for rad, blur, k, col in ((tip_t * 1.6, u * 4.0, 0.85, (255, 150, 48)),
                              (tip_t * 3.0, u * 13.0, 0.42, (255, 120, 36)),
                              (tip_t * 7.0, u * 34.0, 0.18, (196, 86, 28))):
        lay = Image.new("RGB", art.size, (0, 0, 0))
        ImageDraw.Draw(lay).ellipse([ex - rad, ey - rad, ex + rad, ey + rad], fill=col)
        art = ImageChops.screen(art, _dim(lay.filter(ImageFilter.GaussianBlur(blur)), k))
    ad = ImageDraw.Draw(art)
    for rr, col in ((tip_t * 1.00, (232, 116, 32)),
                    (tip_t * 0.62, (255, 206, 130)),
                    (tip_t * 0.30, (255, 248, 228))):
        ad.ellipse([ex - rr, ey - rr, ex + rr, ey + rr], fill=col)
    return art


# ── Сборка ──────────────────────────────────────────────────────────

def _pose(rng, pal):
    """Поза карты — то, что отличает ОДИН блант от другого на превью.

    Коллекция существует только там, где два предмета различимы. Раньше вся
    вариативность лежала в шуме: звёздное поле, завитки дыма, серийник. Всё это
    исчезает при уменьшении до превью, и владелец десяти именных блантов видел
    десять одинаковых карточек с разными подписями — новизны нет, а значит нет
    и повода скрутить одиннадцатый.

    Различимость обязана жить в СИЛУЭТЕ и КРУПНОЙ форме — единственном, что
    переживает даунскейл: направление скрутки, наклон, длина, толщина, точка
    уголька, прогар, рисунок обёртки, характер дыма, устройство фона.

    Чего здесь намеренно НЕТ — цвета. Цвет остаётся носителем РЕДКОСТИ: если
    его сделать ещё и носителем индивидуальности, тир перестанет читаться, а
    редкость важнее личности. Поэтому личность кодируется только структурой.
    """
    mirrored = rng.random() < 0.5
    if mirrored:
        # скрутка смотрит вверх-ВЛЕВО: самый сильный различитель силуэта,
        # видимый даже там, где не читается уже ничего
        ang = math.radians(rng.uniform(-152, -134))
        fx = rng.uniform(0.335, 0.395)
        lit = -1
    else:
        ang = math.radians(rng.uniform(-46, -28))
        fx = rng.uniform(0.605, 0.665)
        lit = 1
    return {
        "mirrored": mirrored,
        "ang": ang,
        "lit": lit,
        "fx": fx,
        "fy": rng.uniform(0.295, 0.375),
        # длина/толщина — множители ПОВЕРХ ступени редкости: тир сохраняет
        # свой порядок величин, личность играет внутри него
        "len_k": rng.uniform(0.90, 1.07),
        "girth": rng.uniform(0.90, 1.12),
        "ash": rng.uniform(0.66, 0.93),
        "seam_style": rng.choice(("rings", "spiral", "braid", "banded")),
        "ribbons": rng.randint(2, 4),
        "smoke_k": rng.uniform(0.85, 1.20),
        # фон: центр розетки и плотность поля — крупная структура заднего плана
        "ros_x": rng.uniform(0.36, 0.58),
        "ros_y": rng.uniform(0.36, 0.54),
        "ros_k": rng.uniform(0.80, 1.02),
        "star_k": rng.uniform(0.85, 1.15),
        "tier": pal["pips"],
    }


def render_blunt_card(item: dict, owner_name: str = "") -> bytes:
    """JPEG-байты коллекционной карточки для бланта `item`. Детерминирован по hash."""
    rarity = item.get("rarity", "common")
    pal = _RARITY.get(rarity, _RARITY["common"])
    glow, frame, accent = pal["glow"], pal["frame"], pal["accent"]

    seed_src = str(item.get("hash") or item.get("id") or item.get("rare_number") or "0")
    hexs = re.sub(r"[^0-9a-fA-F]", "", seed_src)
    seed = int(hexs, 16) if hexs else abs(hash(seed_src))
    rng = random.Random(seed)
    # Поза тянется ПЕРВОЙ из потока: так индивидуальность карты определяется
    # старшими битами хэша и не зависит от того, сколько случайных величин
    # израсходует по пути тот или иной тир (иначе редкость незаметно смещала бы
    # позу, и «личность» бланта менялась бы вместе с апгрейдом тира).
    pose = _pose(rng, pal)

    W, H = _W * _SCALE, _H * _SCALE
    u = W / 640.0
    thin = max(2, int(u * 0.85))    # минимальная толщина орнамента: тоньше не переживает даунсэмпл

    # подложка: виньетка тонируется цветом редкости (6% — незаметно как цвет,
    # но подложка перестаёт спорить с огранкой). Лён/фольга/зерно — ПОСЛЕ ресайза.
    img = _radial_bg((W, H), _mix(_BG_CORE, glow, 0.14), _mix(_BG_EDGE, glow, 0.055))

    # ── геометрия зон ──
    band = pal["band"] * u
    m = int(u * 17)
    aw_x0, aw_x1 = int(W * 0.062), int(W * 0.938)
    aw_y0, aw_y1 = int(H * 0.088), int(H * pal["art_y1"])
    Wa, Ha = aw_x1 - aw_x0, aw_y1 - aw_y0
    art_r = Wa * 0.5

    # ══ ОКНО АРТА ══
    art = _radial_bg((Wa, Ha), _mix((18, 13, 27), glow, 0.19), _ART_BG)

    # план 0 — дальний фон: розетка + звёзды, слегка расфокусирован.
    # Лёгкое расфокусирование фона — самый убедительный признак глубины.
    bg = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    bd = ImageDraw.Draw(bg)
    # розетка смещена от уголька → мишень исчезает; её центр индивидуален,
    # поэтому «погода» заднего плана у каждой карты своя
    ros_cx, ros_cy = Wa * pose["ros_x"], Ha * pose["ros_y"]
    ros_r = art_r * pose["ros_k"]
    _guilloche(bd, ros_cx, ros_cy, rng, _sc(frame, 0.42), ros_r * 0.88, pal["rosette"], thin)
    _guilloche(bd, ros_cx, ros_cy, rng, _sc(accent, 0.26), ros_r * 0.56,
               max(1, pal["rosette"] - 2), thin)
    _guilloche(bd, ros_cx, ros_cy, rng, _sc(frame, 0.85), ros_r * 0.72, 1, thin)
    for _ in range(int(pal["stars"] * pose["star_k"])):
        # кластеризация: равномерный uniform даёт шум, а не звёздное поле
        if rng.random() < 0.55:
            sx = min(Wa, max(0, rng.gauss(Wa * 0.5, Wa * 0.22)))
            sy = min(Ha, max(0, rng.gauss(Ha * 0.45, Ha * 0.24)))
        else:
            sx, sy = rng.uniform(0, Wa), rng.uniform(0, Ha)
        cal = rng.choice([0.35, 0.6, 1.0])
        rr = max(1, u * (0.8 if cal < 0.5 else 1.3 if cal < 0.8 else 1.9))
        bd.ellipse([sx - rr, sy - rr, sx + rr, sy + rr], fill=_sc(accent if cal > 0.8 else glow, cal))
    art = ImageChops.screen(art, _dim(bg.filter(ImageFilter.GaussianBlur(u * 1.6)), 0.72))

    # ── композиция строится ОТ точки интереса ──
    # Точка интереса, наклон и длина — из позы: пересечение третей остаётся
    # пересечением третей (левое или правое), но КАКОЕ именно и под каким
    # углом входит скрутка — паспорт конкретного бланта.
    ember_x, ember_y = Wa * pose["fx"], Ha * pose["fy"]
    ang = pose["ang"]
    item_len = art_r * (1.62 if pal["burst"] else 1.45) * pose["len_k"]

    # Удержание в кадре. Уголёк закреплён на пересечении третей — это якорь
    # композиции, двигать его нельзя. Поэтому при неудачном сочетании наклона и
    # длины укорачиваем ХВОСТ: обрезанный рамкой мундштук читается как брак
    # печати, а не как приём, и рушит обещание «это напечатанный предмет».
    _ca, _sa = math.cos(ang), math.sin(ang)
    _pad = art_r * 0.06
    _lim = [item_len]
    if _ca > 1e-6:
        _lim.append((ember_x - _pad) / _ca)
    elif _ca < -1e-6:
        _lim.append((Wa - _pad - ember_x) / -_ca)
    if _sa < -1e-6:                       # скрутка всегда смотрит вверх → хвост вниз
        _lim.append((Ha - _pad - ember_y) / -_sa)
    item_len = max(art_r * 0.75, min(_lim))
    half = item_len / 2
    cx_item = ember_x - half * math.cos(ang)
    cy_item = ember_y - half * math.sin(ang)

    # Главный «loot»-сигнал: структурный ореол редкости за предметом. В отличие
    # от мелкой филиграни он остаётся читаемым в Telegram-превью и делает тир
    # эмоционально понятным до того, как игрок успел прочитать подпись.
    aura = _energy_aura((Wa, Ha), cx_item, cy_item, rng, glow, accent,
                        pal["pips"], art_r * 0.86, u)
    art = ImageChops.screen(art, aura)

    # заполняющий свет — холодный, цвета редкости, из центра розетки
    fillh = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    fr = art_r * 0.75
    ImageDraw.Draw(fillh).ellipse([ros_cx - fr, ros_cy - fr, ros_cx + fr, ros_cy + fr],
                                  fill=_sc(glow, 0.8))
    art = ImageChops.screen(art, _dim(fillh.filter(ImageFilter.GaussianBlur(u * 46)), 0.16))

    # ключевой свет — тёплый, ИЗ уголька
    key = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    hr = art_r * 0.58
    ImageDraw.Draw(key).ellipse([ember_x - hr, ember_y - hr, ember_x + hr, ember_y + hr],
                                fill=_mix(glow, (255, 146, 60), 0.55))
    art = ImageChops.screen(art, _dim(key.filter(ImageFilter.GaussianBlur(u * 32)), 0.52))

    # god-rays из источника, сектором в сторону дыма
    if pal["rays"]:
        rl = Image.new("RGB", (Wa, Ha), (0, 0, 0))
        _rays(ImageDraw.Draw(rl), ember_x, ember_y, rng, _mix(glow, _EMBER, 0.5),
              pal["rays"] // 2, art_r * 0.10, art_r * 0.55, thin,
              sector=(-math.pi / 2, math.pi * 1.1))
        art = ImageChops.screen(art, _dim(rl.filter(ImageFilter.GaussianBlur(u * 2.2)), 0.5))

    # атмосферный карман: предмет садится в тень, а не лежит поверх решётки.
    # Без него на легендарке предмет растворяется в собственном орнаменте.
    pw, ph = item_len * 0.80, item_len * 0.30
    em = Image.new("L", (int(pw * 2), int(ph * 2)), 0)
    ImageDraw.Draw(em).ellipse([0, 0, pw * 2 - 1, ph * 2 - 1], fill=255)
    em = em.rotate(-math.degrees(ang), expand=True, resample=Image.BICUBIC)
    em = em.filter(ImageFilter.GaussianBlur(u * 22)).point(lambda v: int(v * 0.55))
    pocket = Image.new("L", (Wa, Ha), 0)
    pocket.paste(em, (int(cx_item - em.width / 2), int(cy_item - em.height / 2)))
    art = ImageChops.multiply(art, ImageChops.invert(
        Image.merge("RGB", (pocket, pocket, pocket))))

    # дым ЗА предметом (перекрытие → дым обвивает, а не лежит сверху)
    sm_back = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    _smoke(ImageDraw.Draw(sm_back), ember_x, ember_y, rng, accent,
           art_r * 0.38 * pose["smoke_k"], art_r * 0.98, 2)
    art = ImageChops.screen(art, _dim(sm_back.filter(ImageFilter.GaussianBlur(u * 5)), 0.82))

    # контактная тень под предметом → отрыв от плоскости
    sh = Image.new("L", (Wa, Ha), 0)
    shd = ImageDraw.Draw(sh)
    ca_, sa_ = math.cos(ang), math.sin(ang)
    tt = item_len * 0.055
    shd.polygon([(cx_item - half * ca_ + tt * sa_, cy_item - half * sa_ - tt * ca_),
                 (cx_item + half * ca_ + tt * sa_, cy_item + half * sa_ - tt * ca_),
                 (cx_item + half * ca_ - tt * sa_, cy_item + half * sa_ + tt * ca_),
                 (cx_item - half * ca_ - tt * sa_, cy_item - half * sa_ + tt * ca_)], fill=255)
    sh = ImageChops.offset(sh, int(u * 7), int(u * 9)).filter(ImageFilter.GaussianBlur(u * 11))
    art = ImageChops.multiply(art, ImageChops.invert(
        Image.merge("RGB", (sh.point(lambda v: int(v * 0.55)),) * 3)))

    # предмет
    ad = ImageDraw.Draw(art)
    ex, ey, tip_t = _relic(ad, cx_item, cy_item, item_len, ang, rng, accent, u,
                           lit=pose["lit"], pose=pose)

    # дым ПЕРЕД предметом
    sm_fr = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    _smoke(ImageDraw.Draw(sm_fr), ex, ey, rng, accent,
           art_r * 0.36 * pose["smoke_k"], art_r * 1.05, pose["ribbons"])
    smoke_blur = sm_fr.filter(ImageFilter.GaussianBlur(u * 4.5))
    art = ImageChops.screen(art, smoke_blur)

    # уголёк — поверх дыма
    art = _ember(art, ex, ey, tip_t, ang, u)

    # искры кластером в сторону дыма
    _sparkles(ImageDraw.Draw(art), ex, ey, rng, art_r * 0.8, accent,
              rng.randint(6 + pal["pips"] * 2, 9 + pal["pips"] * 4), u,
              direction=-math.pi / 2)

    # передний план: боке у краёв → камера с малой ГРИП
    fg = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    fd = ImageDraw.Draw(fg)
    for _ in range(rng.randint(4, 7)):
        fx = rng.choice([rng.uniform(0, Wa * 0.22), rng.uniform(Wa * 0.78, Wa)])
        fy = rng.uniform(0, Ha)
        frr = u * rng.uniform(3.0, 8.0)
        c = _EMBER if rng.random() < 0.45 else accent
        fd.ellipse([fx - frr, fy - frr, fx + frr, fy + frr], fill=_sc(c, rng.uniform(0.3, 0.65)))
    art = ImageChops.screen(art, fg.filter(ImageFilter.GaussianBlur(u * 3.2)))

    img.paste(art, (aw_x0, aw_y0))

    # прорыв дыма за рамку — привилегия топ-редкостей.
    # У дыма нет края, поэтому прорыв не требует контура и тени.
    if pal["burst"]:
        bh = int(Ha * 0.42)
        bl = Image.new("RGB", (Wa, bh), (0, 0, 0))
        _smoke(ImageDraw.Draw(bl), ex, bh, rng, accent, art_r * 0.34, bh * 0.95, 3)
        bl = bl.filter(ImageFilter.GaussianBlur(u * 6))
        grad = Image.new("L", (Wa, bh))
        gp = grad.load()
        for yy in range(bh):
            v = int(140 * (yy / max(1, bh - 1)) ** 1.6)
            for xx in range(0, Wa, 1):
                gp[xx, yy] = v
        strip = img.crop((aw_x0, aw_y0 - bh, aw_x1, aw_y0))
        strip = ImageChops.screen(strip, Image.composite(
            bl, Image.new("RGB", (Wa, bh), (0, 0, 0)), grad))
        img.paste(strip, (aw_x0, aw_y0 - bh))

    # окно утоплено: тень по верхней/левой кромке
    img = _inset_shadow(img, (aw_x0, aw_y0, aw_x1, aw_y1), u, 0.5)
    draw = ImageDraw.Draw(img)
    _metal_frame(draw, aw_x0 - band * 0.62, aw_y0 - band * 0.62,
                 aw_x1 + band * 0.62, aw_y1 + band * 0.62, band * 0.62, frame, thin)
    for cxx, cyy in ((aw_x0, aw_y0), (aw_x1, aw_y0), (aw_x0, aw_y1), (aw_x1, aw_y1)):
        _diamond(draw, cxx, cyy, u * 7, _ART_BG, accent, thin)

    # ══ ВНЕШНЯЯ ОГРАНКА ══
    _metal_frame(draw, m, m, W - m, H - m, band, frame, thin)
    fl = W * 0.072
    inner = m + band + u * 4
    for x, y, sx, sy in ((inner, inner, 1, 1), (W - inner, inner, -1, 1),
                         (inner, H - inner, 1, -1), (W - inner, H - inner, -1, -1)):
        _corner_flourish(draw, x, y, sx, sy, fl, pal["rings"], _sc(frame, 0.8), accent, thin)

    # ══ МЕТКА РЕДКОСТИ ══
    f_label = _font("DejaVuSans-Bold.ttf", int(20 * u))
    label, tr = pal["label"], u * 4.6
    lw = _text_w(draw, label, f_label, tr)
    ly_t = H * 0.043
    badge_pad_x, badge_pad_y = u * 38, u * 11
    bx0, bx1 = (W - lw) / 2 - badge_pad_x, (W + lw) / 2 + badge_pad_x
    by0, by1 = ly_t - badge_pad_y, ly_t + int(20 * u) + badge_pad_y
    draw.rounded_rectangle([bx0, by0, bx1, by1], radius=u * 9,
                           fill=_mix((7, 5, 12), glow, 0.12),
                           outline=_sc(frame, 0.62), width=max(2, thin))
    draw.rounded_rectangle([bx0 + u * 4, by0 + u * 4, bx1 - u * 4, by1 - u * 4],
                           radius=u * 6, outline=_sc(accent, 0.32), width=max(1, thin - 1))
    _draw_tracked(draw, ((W - lw) / 2, ly_t), label, f_label, _sc(accent, 0.86), tr)
    gy = ly_t + int(10 * u)
    pad = u * 18
    _rule(draw, W * 0.20, (W - lw) / 2 - pad, gy, _sc(frame, 0.75), accent, u, gem=False)
    _rule(draw, (W + lw) / 2 + pad, W * 0.80, gy, _sc(frame, 0.75), accent, u, gem=False)
    _diamond(draw, W * 0.20 - u * 11, gy, u * 5, None, frame, thin)
    _diamond(draw, W * 0.80 + u * 11, gy, u * 5, None, frame, thin)

    # ══ КАРТУШ С ИМЕНЕМ (выступает) ══
    px0, px1 = W * 0.098, W * 0.902
    cy0, cy1 = H * 0.668, H * 0.755
    img = _drop_shadow(img, (px0, cy0, px1, cy1), u)
    draw = ImageDraw.Draw(img)
    img.paste(_vgrad((int(px1 - px0), int(cy1 - cy0)), (26, 21, 36), (10, 8, 15)),
              (int(px0), int(cy0)))
    draw = ImageDraw.Draw(img)
    _metal_frame(draw, px0, cy0, px1, cy1, band * 0.46, frame, thin)
    _diamond(draw, px0, (cy0 + cy1) / 2, u * 9, (13, 10, 19), accent, thin)
    _diamond(draw, px1, (cy0 + cy1) / 2, u * 9, (13, 10, 19), accent, thin)

    name = _sanitize(item.get("name", "Безымянный"))
    size = int(38 * u)
    f_name = _font("DejaVuSans-Bold.ttf", size)
    avail = (px1 - px0) - u * 52
    txt = name.upper()
    while size > int(17 * u) and draw.textlength(txt, font=f_name) > avail:
        size = int(size * 0.94)
        f_name = _font("DejaVuSans-Bold.ttf", size)
    # если даже минимальный кегль не влезает — режем с многоточием, СОХРАНЯЯ
    # закрывающую кавычку: обрезка вида «Вздох без пары читается как баг
    if draw.textlength(txt, font=f_name) > avail:
        base = name.upper()
        while base and draw.textlength(f"{base}…", font=f_name) > avail:
            base = base[:-1]
        txt = f"{base}…"
    nw = draw.textlength(txt, font=f_name)
    ny = (cy0 + cy1) / 2 - size * 0.66
    _metal_text(img, ((W - nw) / 2, ny), txt, f_name,
                _mix((244, 240, 250), accent, 0.18), 0, u, size)
    draw = ImageDraw.Draw(img)

    # ══ ПЛОТНЫЙ СЛУЖЕБНЫЙ БЛОК ══
    ix0, ix1 = W * 0.135, W * 0.865
    _rule(draw, ix0, ix1, H * 0.782, _sc(frame, 0.62), accent, u)

    # Подвал намеренно мелкий. Раньше три подвальных блока занимали 45–47%
    # ширины при имени в 63% — имя превосходило их всего в 1.35×, и в прищуре
    # подвал перевешивал картуш («чек из магазина»). Ступени шкалы разнесены:
    # имя T6 / метка T3 / ник T2 / мета и бренд T1 — три голоса, а не пять шёпотов.
    f_meta = _font("DejaVuSansMono.ttf", int(13 * u))
    if pal["edition"] and item.get("serial") is not None:
        # Номер реестра — РЕАЛЬНЫЙ (nft_registry.serial, см. create_named_blunt
        # в bot.py), а не выдуманный. Раньше здесь был `(seed % 999) + 1` —
        # число из хэша, поданное как «N из 999», при абсолютно неограниченном
        # крафте легендарок (2% с любой попытки, без предела тиража). Это ровно
        # тот фабрикованный дефицит, который проект запрещает себе в других
        # местах (см. развёрнутый комментарий у честных шансов бланта в
        # handle_named_name). Знаменателя больше нет: последовательный номер
        # реестра честен и без него — врать не нужно, чтобы выглядеть редким.
        meta = f"№{item['serial']:04d} · РЕЕСТР   {str(item.get('hash', '0x????'))[:10]}"
    else:
        meta = f"{item.get('rare_number', '?-????')}   {str(item.get('hash', '0x????'))[:10]}"
    mw = _text_w(draw, meta, f_meta, u * 1.1)
    _draw_tracked(draw, ((W - mw) / 2, H * 0.797), meta, f_meta, _sc(frame, 1.05), u * 1.1)

    _pips(draw, W / 2, H * 0.845, pal["pips"], 4, accent, frame, u)

    # Владелец: ярлык капсом + ник. «Первый владелец: X» одним кеглем было
    # предложением в подвале — самой дешёвой деталью карты. Два регистра в одну
    # строку читаются как поле документа. Тень убрана: субпиксельная тень при
    # мелком кегле не читается как тень, только замыливает штрих.
    f_lbl = _font("DejaVuSans-Bold.ttf", int(13 * u))
    f_small = _font("DejaVuSans.ttf", int(16 * u))
    owner = _sanitize(owner_name, 20) if owner_name else ""
    if owner:
        lbl, ltr = "ВЛАДЕЛЕЦ", u * 2.6
        lw2 = _text_w(draw, lbl, f_lbl, ltr)
        ow = draw.textlength(owner, font=f_small)
        gapw = u * 16
        total = lw2 + gapw + ow
        ox = (W - total) / 2
        oy = H * 0.872
        _draw_tracked(draw, (ox, oy + (16 - 13) * u * 0.56), lbl, f_lbl, (138, 132, 152), ltr)
        draw.text((ox + lw2 + gapw, oy), owner, font=f_small, fill=(200, 194, 216))

    # печать-сигил — только у топ-редкости
    if pal["seal"]:
        _seal(draw, W * 0.845, H * 0.862, u * 22, rng, frame, accent, u)

    _rule(draw, ix0, ix1, H * 0.905, _sc(frame, 0.5), accent, u)

    f_brand = _font("DejaVuSans-Bold.ttf", int(13 * u))
    brand, btr = "КОДЕКС ИСКАЖЕНИЯ", u * 3.4
    bw = _text_w(draw, brand, f_brand, btr)
    _draw_tracked(draw, ((W - bw) / 2, H * 0.917), brand, f_brand, _sc(frame, 0.95), btr)

    # микротекст — закрывает пустоту, добавляет знак подлинности и «награду за приближение»
    f_micro = _font("DejaVuSansMono.ttf", max(6, int(5.4 * u)))
    _microtext(draw, m + band * 1.6, W - m - band * 1.6, H * 0.952,
               f"КОДЕКС ИСКАЖЕНИЯ · {str(item.get('hash', ''))[:10]}",
               f_micro, _sc(frame, 0.34), u)

    # ══ ПОСТОБРАБОТКА — ПОСЛЕ РЕСАЙЗА ══
    # Материальность (лён, фольга, кромка, зерно) считается на финальном
    # разрешении, а не на 3×. Две причины, обе решающие:
    #  * втрое дешевле — per-pixel циклы идут по площади в 9 раз меньшей;
    #  * РЕЗЧЕ — LANCZOS-даунскейл размывает фактуру, посчитанную до него,
    #    превращая зерно и волокно в мыло. Текстура обязана пережить ресайз.
    img = img.resize((_W, _H), Image.LANCZOS)
    uf = 1.0    # единица на финальном разрешении

    img = ImageChops.screen(img, _linen((_W, _H), rng, _sc(frame, 0.5), uf))
    foil = _foil((_W, _H), rng, accent, pal["foil"], _ornament_mask(img, uf))
    if foil is not None:
        img = ImageChops.screen(img, foil)
    img = _card_edge(img, _sc(accent, 0.92))
    img = _apply_grain(img, rng, 5)
    out = io.BytesIO()
    # JPEG, а не PNG: зерно плёнки — шум, он убивает PNG-сжатие (карта весила
    # ~800 КБ). Telegram всё равно пережимает фото в JPEG.
    out.name = "blunt.jpg"
    img.save(out, format="JPEG", quality=90, optimize=True, progressive=True)
    out.seek(0)
    return out.getvalue()


# ── Витрина коллекции ───────────────────────────────────────────────
# Коллекция существует только тогда, когда предметы видно ВМЕСТЕ. Пока экран
# «Кодекс» был текстовым списком, уникальные силуэты не с чем было сравнить —
# а новизна работает исключительно на сравнении. Отсюда витрина: один кадр,
# где собрание видно целиком, и пустые слоты честно показывают, что место есть.
#
# Почему один кадр, а не альбом из настоящих карточек: полная карта стоит ~220 МБ
# пикового RSS и ~2с, шесть карт — это шесть таких проходов и гарантированный OOM.
# Витрина рисует упрощённые плитки за ОДИН проход при меньшем масштабе: тяжёлые
# слои (фольга, лён, гильоше, тройной bloom) плитке не нужны — на 300px они
# всё равно не читаются. Общее с картой — поза: силуэт плитки совпадает с
# силуэтом настоящей карточки, иначе витрина врала бы о содержимом коллекции.

_WALL_SCALE = 2
_TILE_W, _TILE_H = 300, 400
_WALL_COLS = 3
_WALL_GAP, _WALL_MARGIN, _WALL_HEAD = 14, 18, 64


def _wall_tile(tile, item, u):
    """Одна плитка витрины: занятая (предмет) или пустая (слот под предмет)."""
    W, H = tile.size
    d = ImageDraw.Draw(tile)

    if item is None:
        # Пустой слот — не «дырка», а обещание. Приглушённый, пунктирный,
        # без цвета редкости: он ничего не обещает конкретного, только место.
        d.rectangle([0, 0, W - 1, H - 1], fill=(15, 13, 21))
        dash, gap_ = int(u * 9), int(u * 7)
        edge = _sc((92, 86, 112), 0.5)
        for x in range(int(u * 6), W - int(u * 6), dash + gap_):
            d.line([(x, int(u * 6)), (min(x + dash, W - int(u * 6)), int(u * 6))], fill=edge)
            d.line([(x, H - int(u * 6)), (min(x + dash, W - int(u * 6)), H - int(u * 6))], fill=edge)
        for y in range(int(u * 6), H - int(u * 6), dash + gap_):
            d.line([(int(u * 6), y), (int(u * 6), min(y + dash, H - int(u * 6)))], fill=edge)
            d.line([(W - int(u * 6), y), (W - int(u * 6), min(y + dash, H - int(u * 6)))], fill=edge)
        f = _font("DejaVuSerif-Bold.ttf", int(46 * u))
        q = "?"
        qw = d.textlength(q, font=f)
        d.text(((W - qw) / 2, H * 0.40), q, font=f, fill=(66, 62, 84))
        f2 = _font("DejaVuSans.ttf", int(13 * u))
        t = "место свободно"
        tw = d.textlength(t, font=f2)
        d.text(((W - tw) / 2, H * 0.60), t, font=f2, fill=(84, 79, 102))
        return tile

    rarity = item.get("rarity", "common")
    pal = _RARITY.get(rarity, _RARITY["common"])
    glow, frame, accent = pal["glow"], pal["frame"], pal["accent"]

    seed_src = str(item.get("hash") or item.get("id") or "0")
    hexs = re.sub(r"[^0-9a-fA-F]", "", seed_src)
    seed = int(hexs, 16) if hexs else abs(hash(seed_src))
    rng = random.Random(seed)
    pose = _pose(rng, pal)      # та же поза, что и на полной карте

    tile.paste(_radial_bg((W, H), _mix(_BG_CORE, glow, 0.18), _mix(_BG_EDGE, glow, 0.07)), (0, 0))
    d = ImageDraw.Draw(tile)

    # окно арта
    ax0, ay0 = int(u * 12), int(u * 12)
    ax1, ay1 = W - int(u * 12), int(H * 0.66)
    Wa, Ha = ax1 - ax0, ay1 - ay0
    art = _radial_bg((Wa, Ha), _mix((17, 12, 25), glow, 0.20), _ART_BG)
    art_r = Wa * 0.5

    ex_, ey_ = Wa * pose["fx"], Ha * pose["fy"]
    ang = pose["ang"]
    ln = art_r * 1.5 * pose["len_k"]
    ca, sa = math.cos(ang), math.sin(ang)
    pad = art_r * 0.06
    lim = [ln]
    if ca > 1e-6:
        lim.append((ex_ - pad) / ca)
    elif ca < -1e-6:
        lim.append((Wa - pad - ex_) / -ca)
    if sa < -1e-6:
        lim.append((Ha - pad - ey_) / -sa)
    ln = max(art_r * 0.7, min(lim))

    # тёплый ключ из уголька — плитка должна читаться как та же сцена
    key = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    hr = art_r * 0.7
    ImageDraw.Draw(key).ellipse([ex_ - hr, ey_ - hr, ex_ + hr, ey_ + hr],
                                fill=_mix(glow, (255, 146, 60), 0.5))
    art = ImageChops.screen(art, _dim(key.filter(ImageFilter.GaussianBlur(u * 16)), 0.55))

    cxi = ex_ - (ln / 2) * ca
    cyi = ey_ - (ln / 2) * sa
    art = ImageChops.screen(art, _energy_aura(
        (Wa, Ha), cxi, cyi, rng, glow, accent, pal["pips"], art_r * 0.78, u))
    ad = ImageDraw.Draw(art)
    tx, ty, tip_t = _relic(ad, cxi, cyi, ln, ang, rng, accent, u,
                           lit=pose["lit"], pose=pose)
    sm = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    _smoke(ImageDraw.Draw(sm), tx, ty, rng, accent, art_r * 0.3, art_r * 0.8, 2)
    art = ImageChops.screen(art, _dim(sm.filter(ImageFilter.GaussianBlur(u * 3)), 0.7))
    art = _ember(art, tx, ty, tip_t, ang, u)

    tile.paste(art, (ax0, ay0))
    d = ImageDraw.Draw(tile)
    _metal_frame(d, ax0, ay0, ax1, ay1, pal["band"] * u * 0.42, frame, max(1, int(u)))

    # имя
    f = _font("DejaVuSans-Bold.ttf", int(19 * u))
    nm = _sanitize(item.get("name", "Безымянный"), 22)
    txt = nm.upper()
    avail = W - int(u * 26)
    size = int(19 * u)
    while size > int(11 * u) and d.textlength(txt, font=f) > avail:
        size = int(size * 0.92)
        f = _font("DejaVuSans-Bold.ttf", size)
    if d.textlength(txt, font=f) > avail:
        nm = nm.upper()
        while nm and d.textlength(f"{nm}…", font=f) > avail:
            nm = nm[:-1]
        txt = f"{nm}…"
    tw = d.textlength(txt, font=f)
    d.text(((W - tw) / 2, H * 0.70), txt, font=f, fill=(234, 228, 240))

    # метка редкости + пипсы — тир читается и без цвета
    fl = _font("DejaVuSans-Bold.ttf", int(11 * u))
    lw = _text_w(d, pal["label"], fl, u * 2.2)
    _draw_tracked(d, ((W - lw) / 2, H * 0.795), pal["label"], fl, _sc(accent, 0.85), u * 2.2)
    # Имя формы — набор нельзя собирать, не видя, что именно у тебя в руках.
    # Берётся из той же позы, которой нарисован силуэт выше, поэтому подпись и
    # картинка не могут разойтись.
    weave = _WEAVE_NAMES.get(pose["seam_style"], "")
    facing = _FACING_NAMES.get(bool(pose["mirrored"]), "")
    if weave:
        ff = _font("DejaVuSans.ttf", int(11 * u))
        ft = f"{weave} {facing}"
        fw = d.textlength(ft, font=ff)
        d.text(((W - fw) / 2, H * 0.845), ft, font=ff, fill=(132, 126, 150))
    _pips(d, W / 2, H * 0.905, pal["pips"], 4, accent, frame, u)
    _metal_frame(d, int(u * 3), int(u * 3), W - int(u * 3), H - int(u * 3),
                 pal["band"] * u * 0.3, _sc(frame, 0.75), max(1, int(u)))
    return tile


def render_collection_wall(items, owner_name: str = "", slots: int = 6,
                           owned_tiers: int = None) -> bytes:
    """JPEG-витрина коллекции: сетка плиток + честно пустые слоты.

    `items` — список предметов (лучшие идут первыми, порядок задаёт вызывающий).
    Показываем не больше `slots`; недостающее добивается пустыми слотами, чтобы
    неполнота коллекции была ВИДНА, а не подразумевалась.

    `owned_tiers` считается по ВСЕЙ коллекции и передаётся снаружи: витрина
    показывает лишь верхушку, и счёт по видимым плиткам занижал бы собранное
    у игрока с большой коллекцией.
    """
    items = list(items or [])[:slots]
    rows = (slots + _WALL_COLS - 1) // _WALL_COLS
    u = float(_WALL_SCALE)
    tw, th = int(_TILE_W * u), int(_TILE_H * u)
    gap, mg, head = int(_WALL_GAP * u), int(_WALL_MARGIN * u), int(_WALL_HEAD * u)
    W = _WALL_COLS * tw + (_WALL_COLS - 1) * gap + 2 * mg
    H = rows * th + (rows - 1) * gap + 2 * mg + head

    img = _radial_bg((W, H), (30, 25, 42), (8, 7, 12))
    d = ImageDraw.Draw(img)

    if owned_tiers is None:
        owned_tiers = len({it.get("rarity") for it in items if it})
    f = _font("DejaVuSans-Bold.ttf", int(21 * u))
    title = "КОДЕКС ИСКАЖЕНИЯ"
    tr = u * 4.0
    twd = _text_w(d, title, f, tr)
    _draw_tracked(d, ((W - twd) / 2, mg * 0.55), title, f, (214, 198, 168), tr)
    f2 = _font("DejaVuSans.ttf", int(14 * u))
    # Санитайзить ЦЕЛУЮ строку нельзя: _sanitize оставляет только то, что рисует
    # DejaVu, и вырезает «/» вместе с «·» — счёт «4/4» молча превращался в «44».
    # Чистим только пользовательский ник, служебный текст собираем после.
    who = _sanitize(owner_name, 22) if owner_name else ""
    sub = f"{who} · собрано редкостей {owned_tiers}/4" if who else f"собрано редкостей {owned_tiers}/4"
    sw = d.textlength(sub, font=f2)
    d.text(((W - sw) / 2, mg * 0.55 + int(26 * u)), sub, font=f2, fill=(150, 143, 168))

    for i in range(slots):
        r, c = divmod(i, _WALL_COLS)
        x = mg + c * (tw + gap)
        y = mg + head + r * (th + gap)
        tile = Image.new("RGB", (tw, th), (12, 10, 17))
        tile = _wall_tile(tile, items[i] if i < len(items) else None, u)
        img.paste(tile, (x, y))

    img = img.resize((W // _WALL_SCALE, H // _WALL_SCALE), Image.LANCZOS)
    img = _apply_grain(img, random.Random(1), 4)
    out = io.BytesIO()
    out.name = "codex.jpg"
    img.save(out, format="JPEG", quality=88, optimize=True, progressive=True)
    out.seek(0)
    return out.getvalue()


# ── Формы скрутки: конечный перечислимый набор ──────────────────────
# Коллекционирование живёт только там, где набор ИЗВЕСТЕН, КОНЕЧЕН и видно, чего
# в нём не хватает. Набор из четырёх редкостей закрывается почти сразу и потом
# не зовёт никуда, а бесконечная процедурная вариация силуэтов неперечислима —
# «у меня 7 из 12» про неё сказать нельзя, и собирать её невозможно.
#
# Между этими крайностями уже лежал готовый набор, просто безымянный: поза даёт
# 4 плетения × 2 направления = 8 дискретных форм, строго по 12.5% каждая.
# Здесь они получают имена — и невидимая техническая деталь становится целью.
# Ничего не добавлено в механику: форма выводится из того же хэша, что и
# картинка, поэтому у КАЖДОГО уже выданного бланта форма есть с рождения и
# никакой миграции не требуется.
_WEAVE_NAMES = {
    "rings": "Кольца", "spiral": "Спираль",
    "braid": "Плетение", "banded": "Пояса",
}
# Направление скрутки — самый крупный различитель силуэта, его и называем.
_FACING_NAMES = {False: "Восхода", True: "Заката"}

FORMS_TOTAL = len(_WEAVE_NAMES) * len(_FACING_NAMES)


def form_of(item) -> dict:
    """Форма скрутки предмета: {id, name}. Детерминирована по тому же хэшу, что и арт.

    Возвращает ровно то, что игрок ВИДИТ на карточке, — иначе набор описывал бы
    не те предметы, что лежат в коллекции.
    """
    rarity = (item or {}).get("rarity", "common")
    pal = _RARITY.get(rarity, _RARITY["common"])
    seed_src = str((item or {}).get("hash") or (item or {}).get("id") or "0")
    hexs = re.sub(r"[^0-9a-fA-F]", "", seed_src)
    seed = int(hexs, 16) if hexs else abs(hash(seed_src))
    pose = _pose(random.Random(seed), pal)
    weave, facing = pose["seam_style"], bool(pose["mirrored"])
    keys = sorted(_WEAVE_NAMES)
    fid = keys.index(weave) * 2 + (1 if facing else 0)
    return {"id": fid,
            "name": f"{_WEAVE_NAMES[weave]} {_FACING_NAMES[facing]}"}


def all_forms() -> list:
    """Полный набор форм по порядку id — чтобы показать и недостающие."""
    out = []
    for i, weave in enumerate(sorted(_WEAVE_NAMES)):
        for j, facing in enumerate((False, True)):
            out.append({"id": i * 2 + j,
                        "name": f"{_WEAVE_NAMES[weave]} {_FACING_NAMES[facing]}"})
    return out
