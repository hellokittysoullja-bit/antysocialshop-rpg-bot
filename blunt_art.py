"""Процедурный рендер коллекционной карточки именного бланта.

Настоящая коллекционность рождается, когда КАЖДЫЙ блант визуально уникален —
а не когда все «обычные» на одну картинку. Здесь генерируется карточка-реликвия,
детерминированно выведенная из хэша конкретного бланта (item["hash"]): один и тот
же блант всегда даёт один и тот же арт, но два разных бланта — разный. Картинка
рисуется ОДИН раз на создание, кэшируется как Telegram file_id и переиспользуется.

── Дизайн-позиция (интенциональность, не аккуратность) ──────────────────────
Формат — коллекционная TCG-карта, а не постер: отдельное ОКНО АРТА в огранке,
именная плашка-картуш под ним, служебная зона внизу. Такое разделение и есть то,
что делает карточку «дорогой»: у каждой зоны своя работа, они не перетекают.

Конкретные НЕдефолтные решения:

* Гильоше-розетка в окне арта — переплетённые гипотрохоиды, как защитная сетка
  на банкноте. Это главный источник «много деталей» и мгновенного считывания
  «дорогой сертификат». Узор уникален на блант.
* Предмет нарисован стилизованно и иконически (скрутка + тлеющий уголёк + дым),
  без натурализма: карта передаёт вещь, а не инструктирует.
* Бумага скрутки одного цвета на всех редкостях, а редкость несёт ОРЕОЛ и
  огранка. Так предмет узнаётся мгновенно, а статус читается отдельным слоем.
* Редкость кодируется СТРУКТУРНО, а не только цветом (~8% мужчин не различают
  цвета): деления статуса, плотность филиграни, лучи-корона, фольга, звёзды.
* Иерархия по прищур-тесту: сначала арт, затем ИМЯ в картуше, затем мета.
* Фон не чистый чёрный, а сдвинут в холодный фиолет: чистый #000 на больших
  плоскостях режет глаз и выглядит дёшево.
* Типографика: капс метки с ПОЛОЖИТЕЛЬНЫМ трекингом (Pillow не умеет трекинг —
  рисуем поглифно), мета моноширинным, чтобы цифры не «прыгали».

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

# ── Иерархия редкости: цвет + СТРУКТУРА ─────────────────────────────
#   glow/frame/accent — палитра ореола и огранки
#   pips   — делений статуса (редкость читается без цвета)
#   rings  — плотность угловой филиграни
#   foil   — фольговый отблеск (материальность топ-редкостей)
#   stars  — плотность звёздного поля
#   rays   — лучи-корона за предметом
#   rosette— слоёв гильоше в окне арта
_RARITY = {
    "common": {
        "glow": (46, 104, 64), "frame": (104, 170, 118), "accent": (168, 226, 178),
        "label": "ОБЫЧНЫЙ", "pips": 1, "rings": 0, "foil": 0.0, "stars": 26,
        "rays": 0, "rosette": 2,
    },
    "rare": {
        "glow": (32, 84, 168), "frame": (86, 148, 232), "accent": (168, 208, 255),
        "label": "РЕДКИЙ", "pips": 2, "rings": 1, "foil": 0.0, "stars": 46,
        "rays": 16, "rosette": 3,
    },
    "epic": {
        "glow": (104, 42, 158), "frame": (176, 104, 234), "accent": (224, 176, 255),
        "label": "ЭПИЧЕСКИЙ", "pips": 3, "rings": 2, "foil": 0.16, "stars": 72,
        "rays": 24, "rosette": 4,
    },
    "legendary": {
        "glow": (188, 122, 18), "frame": (255, 198, 72), "accent": (255, 236, 170),
        "label": "ЛЕГЕНДАРНЫЙ", "pips": 4, "rings": 3, "foil": 0.34, "stars": 104,
        "rays": 32, "rosette": 5,
    },
}

# Фон — не чистый чёрный, сдвинут в холодный фиолет (дорогая тёмная тема).
_BG_CORE = (34, 28, 48)
_BG_EDGE = (9, 8, 14)
_ART_BG = (17, 14, 26)      # окно арта темнее подложки — «углубление»

# Обёртка скрутки — одна на всех редкостях: предмет должен узнаваться сразу,
# а статус нести ореолом и огранкой. Тёмный лист, а не белая бумага: светлый
# цилиндр читается слишком буквально, тёмная свёртка — как артефакт.
_WRAP_LIGHT = (108, 85, 62)
_WRAP_MID = (72, 55, 40)
_WRAP_DARK = (40, 30, 23)
_EMBER = (255, 150, 48)

_SCALE = 2
_W, _H = 640, 896          # пропорция 2.5:3.5 — канон коллекционной карты

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu",)


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

    Эмодзи и прочие не-BMP символы Pillow с DejaVu рисует «тофу»-квадратами —
    вырезаем их, чтобы имя на карточке было чистым."""
    text = str(text or "")
    keep = re.sub(r"[^0-9A-Za-zЀ-ӿ ,.\-!?'«»\"()]+", "", text)
    keep = re.sub(r"\s+", " ", keep).strip()
    return keep[:limit] or "Безымянный"


def _text_w(draw, s, font, tracking=0):
    if not tracking:
        return draw.textlength(s, font=font)
    return sum(draw.textlength(ch, font=font) for ch in s) + tracking * max(0, len(s) - 1)


def _draw_tracked(draw, xy, s, font, fill, tracking=0):
    """Текст с межбуквенным интервалом. Капс без трекинга читается как ошибка вёрстки."""
    x, y = xy
    if not tracking:
        draw.text((x, y), s, font=font, fill=fill)
        return
    for ch in s:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def _wrap(draw, text, font, max_w, max_lines=2):
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def _dim(layer, k):
    """Приглушить слой перед screen: свет должен быть локальным, а не заливать кадр."""
    return layer.point(lambda v: int(v * k))


def _radial_bg(size, core, edge):
    """Радиальная виньетка: свет в центре, тьма к краям — глубина вместо плоскости.

    Считаем на маленьком холсте и растягиваем: per-pixel Python по полному
    размеру был бы непозволительно медленным."""
    w, h = size
    sw, sh = 80, 112
    small = Image.new("RGB", (sw, sh))
    px = small.load()
    cx, cy = sw / 2, sh * 0.40
    maxd = math.hypot(max(cx, sw - cx), max(cy, sh - cy))
    for yy in range(sh):
        for xx in range(sw):
            t = min(1.0, (math.hypot(xx - cx, yy - cy) / maxd) ** 1.35)
            px[xx, yy] = tuple(int(core[i] + (edge[i] - core[i]) * t) for i in range(3))
    return small.resize((w, h), Image.BICUBIC)


def _apply_grain(img, rng, strength=7):
    """Зерно плёнки — материальность. Без него градиент выглядит «пластиково».

    Зерно берётся из ТОГО ЖЕ seed, что и вся карта: Image.effect_noise тянет
    глобальный ГПСЧ, из-за чего один и тот же блант давал разные карты — а это
    ломает сам контракт коллекционности (карта — паспорт предмета).

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


def _foil(size, rng, color, amount):
    """Диагональный фольговый отблеск — ощущение физической карточки."""
    if amount <= 0:
        return None
    w, h = size
    sw, sh = 96, 136
    small = Image.new("L", (sw, sh))
    px = small.load()
    phase = rng.uniform(0, math.pi * 2)
    for yy in range(sh):
        for xx in range(sw):
            # одна широкая диагональная полоса, а не рябь: полосатость читается
            # как артефакт, единичный блик — как физическая фольга
            v = math.sin((xx / sw * 0.85 + yy / sh * 0.75) * math.pi + phase)
            px[xx, yy] = int(max(0.0, v) ** 3 * 255)
    mask = small.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(w // 26))
    layer = Image.new("RGB", (w, h), color)
    out = Image.new("RGB", (w, h), (0, 0, 0))
    out.paste(layer, (0, 0), mask.point(lambda v: int(v * amount)))
    return out


# ── Орнамент ────────────────────────────────────────────────────────

def _guilloche(draw, cx, cy, rng, color, R, layers, width):
    """Гильоше-розетка (гипотрохоиды) — защитная сетка, как на банкноте.

    Главный источник плотной детализации: тонкие переплетённые кривые читаются
    как гравировка на ценной бумаге. Параметры из seed → узор уникален на блант.
    """
    for k in range(layers):
        Rk = R * (1.0 - 0.11 * k)
        # МАЛОЕ целое отношение даёт чистую «лепестковую» розетку; произвольное
        # (как было) сворачивается в спутанный клубок и читается как помарка
        ratio = rng.choice([5, 6, 7, 8, 9, 11])
        r = Rk / ratio
        d = r * rng.uniform(0.65, 1.25)
        turns = ratio
        steps = max(260, ratio * 52)
        pts = []
        for i in range(steps + 1):
            t = i / steps * turns * 2 * math.pi
            x = (Rk - r) * math.cos(t) + d * math.cos((Rk - r) / r * t)
            y = (Rk - r) * math.sin(t) - d * math.sin((Rk - r) / r * t)
            pts.append((cx + x, cy + y))
        draw.line(pts, fill=color, width=width, joint="curve")


def _rays(draw, cx, cy, rng, color, count, r0, r1, width):
    """Лучи-корона за предметом: подача «реликвии», плотность растёт с редкостью."""
    if count <= 0:
        return
    rot = rng.uniform(0, math.pi)
    for i in range(count):
        a = rot + 2 * math.pi * i / count
        ln = r1 * (1.0 if i % 2 == 0 else 0.72)
        draw.line([(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
                   (cx + ln * math.cos(a), cy + ln * math.sin(a))],
                  fill=color, width=width)


def _corner_flourish(draw, x, y, sx, sy, size, rings, frame, accent, wgt):
    """Угловая филигрань: уголки + диагональ + точка. Богатеет с редкостью."""
    for k in range(max(1, rings + 1)):
        off = size * 0.20 * k
        ln = size - size * 0.22 * k
        if ln <= 0:
            continue
        col = accent if k == 0 else frame
        draw.line([(x + sx * off, y + sy * off), (x + sx * (off + ln), y + sy * off)],
                  fill=col, width=wgt)
        draw.line([(x + sx * off, y + sy * off), (x + sx * off, y + sy * (off + ln))],
                  fill=col, width=wgt)
    if rings >= 2:
        d = size * 0.30
        draw.line([(x + sx * d * 0.5, y + sy * d * 1.5), (x + sx * d * 1.5, y + sy * d * 0.5)],
                  fill=frame, width=max(1, wgt - 1))
    if rings >= 1:
        d = size * 0.16
        rr = max(2, wgt)
        draw.ellipse([x + sx * d - rr, y + sy * d - rr, x + sx * d + rr, y + sy * d + rr],
                     fill=accent)


def _diamond(draw, cx, cy, r, fill, outline, wgt):
    """Ромб-огранка: разделители и держатель метки редкости."""
    pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
    if fill:
        draw.polygon(pts, fill=fill)
    draw.line(pts + [pts[0]], fill=outline, width=wgt, joint="curve")


def _pips(draw, cx, y, count, total, accent, frame, unit):
    """Деления статуса: редкость читается СТРУКТУРНО, без опоры на цвет."""
    gap = unit * 16
    r = unit * 4.4
    start = cx - (total - 1) * gap / 2
    for i in range(total):
        x = start + i * gap
        if i < count:
            _diamond(draw, x, y, r * 1.5, None, frame, max(1, int(unit)))
            _diamond(draw, x, y, r * 0.85, accent, accent, max(1, int(unit)))
        else:
            _diamond(draw, x, y, r * 0.85, None, frame, max(1, int(unit)))


# ── Предмет ─────────────────────────────────────────────────────────

def _smoke(draw, cx, cy, rng, glow, accent, spread, height, ribbons):
    """Клубы дыма: восходящие ленты, вьющиеся по синусоиде. Уникальны по хэшу.

    Рисуется ЧАСТЫМИ мелкими кружками и обязательно размывается вызывающим
    кодом: без размытия и с крупным шагом лента читается как цепочка «сосисок»,
    а не как дым. Мягкость здесь — не украшение, а условие правдоподобия.
    """
    for _ in range(ribbons):
        x = cx + rng.uniform(-spread * 0.5, spread * 0.5)
        y = cy + rng.uniform(-height * 0.05, height * 0.12)
        amp = rng.uniform(spread * 0.22, spread * 0.62)
        freq = rng.uniform(1.3, 3.0)
        phase = rng.uniform(0, math.pi * 2)
        rise = height * rng.uniform(0.60, 1.10)
        r0 = spread * rng.uniform(0.035, 0.068)
        steps = 130
        for i in range(steps):
            t = i / steps
            px = x + math.sin(phase + t * freq * math.pi) * amp * t
            py = y - rise * t
            r = r0 * (0.5 + 1.9 * t)
            fade = (1.0 - t) ** 1.4
            base = accent if i % 26 == 0 else glow
            col = tuple(int(c * (0.10 + 0.58 * fade)) for c in base)
            draw.ellipse([px - r, py - r, px + r, py + r], fill=col)


def _sparkles(draw, cx, cy, rng, spread, accent, glow, count, unit):
    """Мистические искры-блики (четырёхлучевые)."""
    for _ in range(count):
        a = rng.uniform(0, math.pi * 2)
        d = spread * rng.uniform(0.35, 1.05)
        x, y = cx + math.cos(a) * d, cy + math.sin(a) * d * 0.8
        s = unit * rng.uniform(2.6, 7.5)
        w = max(1, int(unit))
        col = accent if rng.random() < 0.6 else glow
        draw.line([(x - s, y), (x + s, y)], fill=col, width=w)
        draw.line([(x, y - s), (x, y + s)], fill=col, width=w)


def _relic(draw, cx, cy, length, rng, accent, unit):
    """Скрутка: стилизованный иконический силуэт + тлеющий уголёк.

    Намеренно НЕ натуралистично — вытянутый конус с пояском и угольком читается
    как предмет коллекции, а не как инструкция. Наклон и детали — из seed.
    """
    ang = math.radians(rng.uniform(-30, -16))
    ca, sa = math.cos(ang), math.sin(ang)
    t0 = length * rng.uniform(0.052, 0.064)   # у мундштука толще
    t1 = t0 * rng.uniform(0.62, 0.76)         # к угольку тоньше

    def P(u, v):
        return (cx + u * ca - v * sa, cy + u * sa + v * ca)

    half = length / 2
    body = [P(-half, -t0), P(half, -t1), P(half, t1), P(-half, t0)]
    draw.polygon(body, fill=_WRAP_MID)
    # верхняя грань освещена, нижняя в тени — один источник света,
    # иначе предмет читается плоской палкой
    draw.polygon([P(-half, -t0), P(half, -t1), P(half, -t1 * 0.25), P(-half, -t0 * 0.25)],
                 fill=_WRAP_LIGHT)
    draw.polygon([P(-half, t0 * 0.30), P(half, t1 * 0.30), P(half, t1), P(-half, t0)],
                 fill=_WRAP_DARK)
    draw.line(body + [body[0]], fill=(30, 22, 17), width=max(1, int(unit)))

    # спиральные швы обёртки — светятся цветом редкости (артефакт, не бумага)
    seam = tuple(int(c * 0.85) for c in accent)
    nseams = rng.randint(5, 8)
    for i in range(nseams):
        uu = -half * 0.72 + (length * 0.85) * (i / max(1, nseams - 1))
        tt = t0 + (t1 - t0) * ((uu + half) / length)
        sk = tt * 0.55  # наклон шва → ощущение витка вокруг цилиндра
        draw.line([P(uu - sk, -tt * 0.96), P(uu + sk, tt * 0.96)],
                  fill=seam, width=max(1, int(unit * 1.1)))

    # поясок у мундштука
    bu = -half * rng.uniform(0.60, 0.76)
    draw.line([P(bu, -t0 * 0.98), P(bu, t0 * 0.98)], fill=accent,
              width=max(2, int(unit * 2.0)))

    # руны-искры вдоль тела
    for _ in range(rng.randint(2, 4)):
        uu = rng.uniform(-half * 0.45, half * 0.60)
        tt = t0 + (t1 - t0) * ((uu + half) / length)
        gx, gy = P(uu, rng.uniform(-tt * 0.4, tt * 0.4))
        rr = max(1, unit * 1.5)
        draw.ellipse([gx - rr, gy - rr, gx + rr, gy + rr], fill=accent)

    # пепел на кончике
    au = half * 0.90
    draw.polygon([P(au, -t1 * 0.98), P(half, -t1 * 0.9), P(half, t1 * 0.9), P(au, t1 * 0.98)],
                 fill=(58, 54, 52))
    ex, ey = P(half, 0)
    return (ex, ey, t1)


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

    img = _radial_bg((W, H), _BG_CORE, _BG_EDGE)

    # ── геометрия зон ──
    m = int(W * 0.036)                       # внешнее поле
    aw_x0, aw_x1 = int(W * 0.088), int(W * 0.912)
    aw_y0, aw_y1 = int(H * 0.118), int(H * 0.588)
    acx, acy = (aw_x0 + aw_x1) // 2, (aw_y0 + aw_y1) // 2
    art_r = (aw_x1 - aw_x0) * 0.5

    # ── ОКНО АРТА: углубление + гильоше + корона + дым + предмет ──
    art = Image.new("RGB", (aw_x1 - aw_x0, aw_y1 - aw_y0), _ART_BG)
    ad = ImageDraw.Draw(art)
    lx, ly = acx - aw_x0, acy - aw_y0

    # гильоше-розетка — плотная гравировка (главный источник «деталей»)
    _guilloche(ad, lx, ly, rng, tuple(int(c * 0.42) for c in frame),
               R=art_r * 0.92, layers=pal["rosette"], width=max(1, int(u)))
    _guilloche(ad, lx, ly, rng, tuple(int(c * 0.26) for c in accent),
               R=art_r * 0.60, layers=max(1, pal["rosette"] - 1), width=max(1, int(u)))

    # корона лучей за предметом
    _rays(ad, lx, ly, rng, tuple(int(c * 0.45) for c in glow),
          pal["rays"], art_r * 0.30, art_r * 0.95, max(1, int(u)))

    # звёздная пыль внутри окна
    for _ in range(pal["stars"]):
        sx, sy = rng.uniform(0, art.width), rng.uniform(0, art.height)
        rr = rng.choice([1, 1, 2]) * u
        ad.ellipse([sx - rr, sy - rr, sx + rr, sy + rr],
                   fill=accent if rng.random() < 0.3 else glow)

    # ореол вокруг предмета — «свечение реликвии»
    # Ореол намеренно скромный: яркий пересвечивал центр, предмет выцветал в
    # серое пятно и терялся уголёк. Свет обязан ЛЕПИТЬ предмет, а не топить его.
    halo = Image.new("RGB", art.size, (0, 0, 0))
    hd = ImageDraw.Draw(halo)
    hr = art_r * 0.46
    hd.ellipse([lx - hr, ly - hr, lx + hr, ly + hr], fill=glow)
    art = ImageChops.screen(art, _dim(halo.filter(ImageFilter.GaussianBlur(u * 26)), 0.34))
    ad = ImageDraw.Draw(art)

    # предмет — рисуем ДО дыма, чтобы знать точку уголька
    item_len = art_r * 1.24
    ex, ey, tip_t = _relic(ad, lx - art_r * 0.06, ly + art_r * 0.14, item_len, rng, accent, u)

    # дым от уголька — свой слой с размытием, поверх предмета
    smoke = Image.new("RGB", art.size, (0, 0, 0))
    _smoke(ImageDraw.Draw(smoke), ex, ey, rng, glow, accent,
           spread=art_r * 0.36, height=art_r * 0.92, ribbons=rng.randint(4, 6))
    art = ImageChops.screen(art, smoke.filter(ImageFilter.GaussianBlur(u * 4.5)))

    # уголёк — САМАЯ яркая точка карты: свой bloom поверх дыма, иначе тлеющий
    # кончик тонет в ореоле и предмет выглядит потухшим
    eglow = Image.new("RGB", art.size, (0, 0, 0))
    egd = ImageDraw.Draw(eglow)
    er = max(tip_t * 2.6, u * 9)
    egd.ellipse([ex - er, ey - er, ex + er, ey + er], fill=_EMBER)
    art = ImageChops.screen(art, eglow.filter(ImageFilter.GaussianBlur(u * 7)))
    ad = ImageDraw.Draw(art)
    for rr, col in ((tip_t * 1.15, tuple(int(c * 0.8) for c in _EMBER)),
                    (tip_t * 0.72, _EMBER),
                    (tip_t * 0.34, (255, 236, 190))):
        ad.ellipse([ex - rr, ey - rr, ex + rr, ey + rr], fill=col)

    _sparkles(ad, lx, ly, rng, art_r * 0.85, accent, glow, rng.randint(6, 11), u)

    img.paste(art, (aw_x0, aw_y0))
    draw = ImageDraw.Draw(img)

    # рамка окна: двойной волосок + уголки (огранка «углубления»)
    draw.rectangle([aw_x0, aw_y0, aw_x1, aw_y1], outline=frame, width=max(2, int(u * 2.4)))
    draw.rectangle([aw_x0 - int(u * 5), aw_y0 - int(u * 5), aw_x1 + int(u * 5), aw_y1 + int(u * 5)],
                   outline=tuple(int(c * 0.55) for c in frame), width=max(1, int(u)))
    for cxx, cyy, sx, sy in ((aw_x0, aw_y0, 1, 1), (aw_x1, aw_y0, -1, 1),
                             (aw_x0, aw_y1, 1, -1), (aw_x1, aw_y1, -1, -1)):
        _diamond(draw, cxx, cyy, u * 6.5, _ART_BG, accent, max(1, int(u * 1.4)))

    # ── ВНЕШНЯЯ ОГРАНКА КАРТЫ ──
    draw.rectangle([m, m, W - m, H - m], outline=frame, width=max(3, int(u * 3.0)))
    m2 = m + int(u * 9)
    draw.rectangle([m2, m2, W - m2, H - m2], outline=tuple(int(c * 0.6) for c in glow),
                   width=max(1, int(u * 1.2)))
    if rarity in ("epic", "legendary"):
        m3 = m - int(u * 6)
        draw.rectangle([m3, m3, W - m3, H - m3], outline=accent, width=max(1, int(u * 1.5)))

    fl = W * 0.075
    for x, y, sx, sy in ((m2, m2, 1, 1), (W - m2, m2, -1, 1),
                         (m2, H - m2, 1, -1), (W - m2, H - m2, -1, -1)):
        _corner_flourish(draw, x, y, sx, sy, fl, pal["rings"], frame, accent, max(2, int(u * 2)))

    # ── МЕТКА РЕДКОСТИ (в ромбовидном держателе) ──
    f_label = _font("DejaVuSans-Bold.ttf", int(20 * u))
    label, tr = pal["label"], u * 4.4
    lw = _text_w(draw, label, f_label, tr)
    ly_t = H * 0.055
    _draw_tracked(draw, ((W - lw) / 2, ly_t), label, f_label, accent, tr)
    gy = ly_t + int(10 * u)
    pad = u * 18
    draw.line([(W * 0.20, gy), ((W - lw) / 2 - pad, gy)], fill=frame, width=max(1, int(u)))
    draw.line([((W + lw) / 2 + pad, gy), (W * 0.80, gy)], fill=frame, width=max(1, int(u)))
    _diamond(draw, W * 0.20 - u * 10, gy, u * 5, None, frame, max(1, int(u)))
    _diamond(draw, W * 0.80 + u * 10, gy, u * 5, None, frame, max(1, int(u)))

    # ── ИМЯ В КАРТУШЕ (primary) ──
    name = _sanitize(item.get("name", "Безымянный"))
    plate_y0, plate_y1 = H * 0.615, H * 0.700
    px0, px1 = W * 0.105, W * 0.895
    draw.rectangle([px0, plate_y0, px1, plate_y1], fill=(15, 12, 22))
    draw.rectangle([px0, plate_y0, px1, plate_y1], outline=frame, width=max(2, int(u * 1.8)))
    _diamond(draw, px0, (plate_y0 + plate_y1) / 2, u * 8, (15, 12, 22), accent, max(1, int(u * 1.4)))
    _diamond(draw, px1, (plate_y0 + plate_y1) / 2, u * 8, (15, 12, 22), accent, max(1, int(u * 1.4)))

    size = int(44 * u)
    f_name = _font("DejaVuSerif-Bold.ttf", size)
    avail = (px1 - px0) - u * 46
    lines = _wrap(draw, f"«{name}»", f_name, avail, max_lines=1)
    while size > int(20 * u) and draw.textlength(lines[0], font=f_name) > avail:
        size = int(size * 0.92)
        f_name = _font("DejaVuSerif-Bold.ttf", size)
        lines = _wrap(draw, f"«{name}»", f_name, avail, max_lines=1)
    nl = lines[0]
    nw = draw.textlength(nl, font=f_name)
    ny = (plate_y0 + plate_y1) / 2 - size * 0.62
    draw.text(((W - nw) / 2 + u * 2, ny + u * 2), nl, font=f_name, fill=(5, 4, 8))
    draw.text(((W - nw) / 2, ny), nl, font=f_name, fill=(248, 244, 252))

    # ── МЕТА: серийник + хэш (моноширинный — цифры не «прыгают») ──
    f_meta = _font("DejaVuSansMono.ttf", int(21 * u))
    meta = f"#{item.get('rare_number', '?-????')}   {str(item.get('hash', '0x????'))[:12]}"
    mw = _text_w(draw, meta, f_meta, u * 1.1)
    _draw_tracked(draw, ((W - mw) / 2, H * 0.727), meta, f_meta, frame, u * 1.1)

    # ── ДЕЛЕНИЯ СТАТУСА ──
    _pips(draw, W / 2, H * 0.792, pal["pips"], 4, accent, frame, u)

    # ── ПОДВАЛ: владелец + бренд ──
    f_small = _font("DejaVuSans.ttf", int(20 * u))
    owner = _sanitize(owner_name, 20) if owner_name else ""
    if owner:
        ot = f"Первый владелец: {owner}"
        ow = draw.textlength(ot, font=f_small)
        oy = H * 0.836
        draw.text(((W - ow) / 2 + u * 1.5, oy + u * 1.5), ot, font=f_small, fill=(5, 4, 8))
        draw.text(((W - ow) / 2, oy), ot, font=f_small, fill=(214, 208, 226))

    f_brand = _font("DejaVuSans-Bold.ttf", int(17 * u))
    brand, btr = "КОДЕКС ИСКАЖЕНИЯ", u * 5.0
    bw = _text_w(draw, brand, f_brand, btr)
    by = H * 0.893
    _draw_tracked(draw, ((W - bw) / 2, by), brand, f_brand, frame, btr)
    _diamond(draw, (W - bw) / 2 - u * 14, by + u * 8, u * 4, None, frame, max(1, int(u)))
    _diamond(draw, (W + bw) / 2 + u * 14, by + u * 8, u * 4, None, frame, max(1, int(u)))

    # ── фольга + зерно последними, по всему кадру ──
    foil = _foil((W, H), rng, accent, pal["foil"])
    if foil is not None:
        img = ImageChops.screen(img, foil)
    img = _apply_grain(img, rng, 7)

    img = img.resize((_W, _H), Image.LANCZOS)
    out = io.BytesIO()
    # JPEG, а не PNG: зерно плёнки — шум, он убивает PNG-сжатие (карта весила
    # ~800 КБ). Для фотографического арта JPEG даёт тот же вид в ~8 раз легче,
    # а Telegram всё равно пережимает фото в JPEG.
    out.name = "blunt.jpg"
    img.save(out, format="JPEG", quality=90, optimize=True, progressive=True)
    out.seek(0)
    return out.getvalue()
