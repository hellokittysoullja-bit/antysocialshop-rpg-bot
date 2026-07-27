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
        "glow": (38, 74, 60), "frame": (112, 152, 128), "accent": (188, 214, 192),
        "label": "ОБЫЧНЫЙ", "pips": 1, "rings": 0, "foil": 0.0, "stars": 26,
        "rays": 0, "rosette": 3, "band": 7.0, "art_y1": 0.575,
        "burst": False, "seal": False, "edition": False,
    },
    "rare": {
        "glow": (30, 62, 116), "frame": (104, 140, 190), "accent": (196, 214, 238),
        "label": "РЕДКИЙ", "pips": 2, "rings": 1, "foil": 0.0, "stars": 46,
        "rays": 14, "rosette": 4, "band": 9.0, "art_y1": 0.590,
        "burst": False, "seal": False, "edition": False,
    },
    "epic": {
        "glow": (78, 48, 110), "frame": (150, 112, 196), "accent": (214, 192, 238),
        "label": "ЭПИЧЕСКИЙ", "pips": 3, "rings": 2, "foil": 0.16, "stars": 72,
        "rays": 20, "rosette": 6, "band": 11.5, "art_y1": 0.605,
        "burst": True, "seal": False, "edition": False,
    },
    "legendary": {
        "glow": (140, 96, 34), "frame": (214, 172, 96), "accent": (244, 226, 186),
        "label": "ЛЕГЕНДАРНЫЙ", "pips": 4, "rings": 3, "foil": 0.26, "stars": 104,
        "rays": 28, "rosette": 8, "band": 14.0, "art_y1": 0.625,
        "burst": True, "seal": True, "edition": True,
    },
}

# Немонотонная рампа металла: провал в середине светлого поля — «горизонт
# отражения». Линейный градиент читается как пластик, этот — как металл.
_METAL_RAMP = (0.22, 0.60, 1.00, 0.38, 0.82, 0.30, 0.55, 0.18)

_BG_CORE = (34, 28, 48)
_BG_EDGE = (9, 8, 14)
_ART_BG = (11, 9, 17)

# Обёртка скрутки — одна на всех редкостях: предмет должен узнаваться сразу,
# а статус нести огранка и ореол. Диапазон расширен, чтобы было чем лепить объём.
_WRAP_LIGHT = (146, 118, 86)
_WRAP_MID = (86, 66, 48)
_WRAP_DARK = (26, 20, 17)
_EMBER = (255, 150, 48)
# Дым холодный — третий тон карты. Тёплый дым делал легендарку монохромной
# (золото и огонь в одном хью), а холодный делает уголёк по-настоящему горячим.
_SMOKE_COOL = (86, 84, 108)
_SMOKE_WARM = (168, 110, 64)

_SCALE = 3
_W, _H = 640, 896          # пропорция 2.5:3.5 — канон коллекционной карты

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


def _relic(draw, cx, cy, length, ang, rng, accent, u):
    """Скрутка: стилизованный иконический силуэт, вылепленный светом.

    Тело рисуется ПОЛОСАМИ с ламбертовым спадом поперёк толщины — три плоских
    полигона давали флаг, а не цилиндр (внутренний диапазон 2.6:1 против 5.5:1
    у освещённого тела). Плюс контровой свет по верхней грани: самый дешёвый и
    самый сильный признак объёма.
    """
    ca, sa = math.cos(ang), math.sin(ang)
    t0 = length * rng.uniform(0.050, 0.060)
    t1 = t0 * rng.uniform(0.62, 0.74)
    half = length / 2

    def P(uu, v):
        return (cx + uu * ca - v * sa, cy + uu * sa + v * ca)

    # тело: 14 полос, ключ сверху-слева
    N = 14
    for j in range(N):
        n0 = -1.0 + 2.0 * j / N
        n1 = -1.0 + 2.0 * (j + 1) / N
        n = (n0 + n1) / 2
        sh = 0.22 + 0.98 * max(0.0, math.cos((n + 0.40) * 1.45)) ** 1.6
        col = list(_sc(_WRAP_MID, sh))
        if abs(n + 0.40) < 0.10:                      # спекуляр — узкая полоса
            col = [min(255, col[k] + (58, 52, 40)[k]) for k in range(3)]
        if n > 0.55:                                  # тёплый отскок от уголька
            b = (n - 0.55) / 0.45
            col = [min(255, col[k] + int((34, 16, 5)[k] * b)) for k in range(3)]
        draw.polygon([P(-half, n0 * t0), P(half, n0 * t1),
                      P(half, n1 * t1), P(-half, n1 * t0)], fill=tuple(col))

    # контровой свет по верхней грани: холодный у мундштука → горячий у уголька
    NS = 18
    cool = _sc(accent, 0.5)
    for i in range(NS):
        a0, a1 = i / NS, (i + 1) / NS
        u0, u1 = -half + length * a0, -half + length * a1
        tt0 = t0 + (t1 - t0) * a0
        tt1 = t0 + (t1 - t0) * a1
        mm = ((a0 + a1) / 2) ** 2.2
        c = _mix(cool, (214, 168, 118), mm)
        draw.line([P(u0, -tt0 * 0.97), P(u1, -tt1 * 0.97)], fill=c, width=max(1, int(u * 1.3)))
    # нижняя грань — глухая тень
    draw.line([P(-half, t0 * 0.98), P(half, t1 * 0.98)], fill=(22, 16, 14),
              width=max(1, int(u * 1.2)))

    # швы обёртки: смесь с цветом бумаги, иначе на легендарке предмет
    # обесцвечивается золотом в «бетонную плиту»
    seam = _mix(accent, _WRAP_LIGHT, 0.5)
    ns = rng.randint(5, 8)
    for i in range(ns):
        uu = -half * 0.72 + (length * 0.85) * (i / max(1, ns - 1))
        tt = t0 + (t1 - t0) * ((uu + half) / length)
        sk = tt * 0.55
        draw.line([P(uu - sk, -tt * 0.9), P(uu + sk, tt * 0.9)],
                  fill=_sc(seam, 0.8), width=max(1, int(u * 1.1)))

    bu = -half * rng.uniform(0.60, 0.76)
    draw.line([P(bu, -t0 * 0.98), P(bu, t0 * 0.98)], fill=seam, width=max(2, int(u * 2.0)))

    # пепел: градиент вдоль оси + тлеющие трещины (не серый колпачок)
    au = half * 0.86
    NA = 4
    for j in range(NA):
        b0, b1 = j / NA, (j + 1) / NA
        aa0 = au + (half - au) * b0
        aa1 = au + (half - au) * b1
        c = _mix((74, 62, 56), (34, 28, 26), b0)
        draw.polygon([P(aa0, -t1 * 0.98), P(aa1, -t1 * 0.95),
                      P(aa1, t1 * 0.95), P(aa0, t1 * 0.98)], fill=c)
    for _ in range(rng.randint(2, 3)):
        uu = rng.uniform(au, half * 0.99)
        draw.line([P(uu, -t1 * 0.5), P(uu, t1 * 0.5)], fill=(206, 92, 30), width=max(1, int(u)))

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

def render_blunt_card(item: dict, owner_name: str = "") -> bytes:
    """JPEG-байты коллекционной карточки для бланта `item`. Детерминирован по hash."""
    rarity = item.get("rarity", "common")
    pal = _RARITY.get(rarity, _RARITY["common"])
    glow, frame, accent = pal["glow"], pal["frame"], pal["accent"]

    seed_src = str(item.get("hash") or item.get("id") or item.get("rare_number") or "0")
    hexs = re.sub(r"[^0-9a-fA-F]", "", seed_src)
    seed = int(hexs, 16) if hexs else abs(hash(seed_src))
    rng = random.Random(seed)

    W, H = _W * _SCALE, _H * _SCALE
    u = W / 640.0
    thin = max(2, int(u * 0.85))    # минимальная толщина орнамента: тоньше не переживает даунсэмпл

    # подложка: виньетка тонируется цветом редкости (6% — незаметно как цвет,
    # но подложка перестаёт спорить с огранкой). Лён/фольга/зерно — ПОСЛЕ ресайза.
    img = _radial_bg((W, H), _mix(_BG_CORE, glow, 0.06), _mix(_BG_EDGE, glow, 0.03))

    # ── геометрия зон ──
    band = pal["band"] * u
    m = int(u * 17)
    aw_x0, aw_x1 = int(W * 0.062), int(W * 0.938)
    aw_y0, aw_y1 = int(H * 0.088), int(H * pal["art_y1"])
    Wa, Ha = aw_x1 - aw_x0, aw_y1 - aw_y0
    art_r = Wa * 0.5

    # ══ ОКНО АРТА ══
    art = Image.new("RGB", (Wa, Ha), _ART_BG)

    # план 0 — дальний фон: розетка + звёзды, слегка расфокусирован.
    # Лёгкое расфокусирование фона — самый убедительный признак глубины.
    bg = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    bd = ImageDraw.Draw(bg)
    ros_cx, ros_cy = Wa * 0.46, Ha * 0.44      # смещена от уголька → мишень исчезает
    _guilloche(bd, ros_cx, ros_cy, rng, _sc(frame, 0.42), art_r * 0.88, pal["rosette"], thin)
    _guilloche(bd, ros_cx, ros_cy, rng, _sc(accent, 0.26), art_r * 0.56,
               max(1, pal["rosette"] - 2), thin)
    _guilloche(bd, ros_cx, ros_cy, rng, _sc(frame, 0.85), art_r * 0.72, 1, thin)
    for _ in range(pal["stars"]):
        # кластеризация: равномерный uniform даёт шум, а не звёздное поле
        if rng.random() < 0.55:
            sx = min(Wa, max(0, rng.gauss(Wa * 0.5, Wa * 0.22)))
            sy = min(Ha, max(0, rng.gauss(Ha * 0.45, Ha * 0.24)))
        else:
            sx, sy = rng.uniform(0, Wa), rng.uniform(0, Ha)
        cal = rng.choice([0.35, 0.6, 1.0])
        rr = max(1, u * (0.8 if cal < 0.5 else 1.3 if cal < 0.8 else 1.9))
        bd.ellipse([sx - rr, sy - rr, sx + rr, sy + rr], fill=_sc(accent if cal > 0.8 else glow, cal))
    art = ImageChops.screen(art, _dim(bg.filter(ImageFilter.GaussianBlur(u * 1.6)), 0.62))

    # ── композиция строится ОТ точки интереса ──
    ember_x, ember_y = Wa * 0.635, Ha * 0.335          # верхнее правое пересечение третей
    ang = math.radians(rng.uniform(-40, -32))           # настоящая диагональ
    item_len = art_r * (1.62 if pal["burst"] else 1.45)
    half = item_len / 2
    cx_item = ember_x - half * math.cos(ang)
    cy_item = ember_y - half * math.sin(ang)

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
           art_r * 0.34, art_r * 0.95, 2)
    art = ImageChops.screen(art, _dim(sm_back.filter(ImageFilter.GaussianBlur(u * 5)), 0.7))

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
    ex, ey, tip_t = _relic(ad, cx_item, cy_item, item_len, ang, rng, accent, u)

    # дым ПЕРЕД предметом
    sm_fr = Image.new("RGB", (Wa, Ha), (0, 0, 0))
    _smoke(ImageDraw.Draw(sm_fr), ex, ey, rng, accent, art_r * 0.36, art_r * 1.05, 3)
    smoke_blur = sm_fr.filter(ImageFilter.GaussianBlur(u * 4.5))
    art = ImageChops.screen(art, smoke_blur)

    # уголёк — поверх дыма
    art = _ember(art, ex, ey, tip_t, ang, u)

    # искры кластером в сторону дыма
    _sparkles(ImageDraw.Draw(art), ex, ey, rng, art_r * 0.8, accent,
              rng.randint(7, 12), u, direction=-math.pi / 2)

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
    f_label = _font("DejaVuSans-Bold.ttf", int(19 * u))
    label, tr = pal["label"], u * 4.6
    lw = _text_w(draw, label, f_label, tr)
    ly_t = H * 0.043
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
    size = int(40 * u)
    f_name = _font("DejaVuSerif-Bold.ttf", size)
    avail = (px1 - px0) - u * 52
    txt = f"«{name}»"
    while size > int(17 * u) and draw.textlength(txt, font=f_name) > avail:
        size = int(size * 0.94)
        f_name = _font("DejaVuSerif-Bold.ttf", size)
    # если даже минимальный кегль не влезает — режем с многоточием, СОХРАНЯЯ
    # закрывающую кавычку: обрезка вида «Вздох без пары читается как баг
    if draw.textlength(txt, font=f_name) > avail:
        base = name
        while base and draw.textlength(f"«{base}…»", font=f_name) > avail:
            base = base[:-1]
        txt = f"«{base}…»"
    nw = draw.textlength(txt, font=f_name)
    ny = (cy0 + cy1) / 2 - size * 0.66
    _metal_text(img, ((W - nw) / 2, ny), txt, f_name, (236, 230, 242), 0, u, size)
    draw = ImageDraw.Draw(img)

    # ══ ПЛОТНЫЙ СЛУЖЕБНЫЙ БЛОК ══
    ix0, ix1 = W * 0.135, W * 0.865
    _rule(draw, ix0, ix1, H * 0.782, _sc(frame, 0.62), accent, u)

    # Подвал намеренно мелкий. Раньше три подвальных блока занимали 45–47%
    # ширины при имени в 63% — имя превосходило их всего в 1.35×, и в прищуре
    # подвал перевешивал картуш («чек из магазина»). Ступени шкалы разнесены:
    # имя T6 / метка T3 / ник T2 / мета и бренд T1 — три голоса, а не пять шёпотов.
    f_meta = _font("DejaVuSansMono.ttf", int(13 * u))
    if pal["edition"]:
        # нумерация тиража: знаменатель делает редкость измеримой
        n = (seed % 999) + 1
        meta = f"{n:03d} / 999   {str(item.get('hash', '0x????'))[:10]}"
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
