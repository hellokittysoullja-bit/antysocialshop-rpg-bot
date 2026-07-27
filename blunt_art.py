"""Процедурный рендер коллекционной карточки именного бланта.

Настоящая коллекционность рождается, когда КАЖДЫЙ блант визуально уникален —
а не когда все «обычные» на одну картинку. Здесь генерируется карточка-реликвия,
детерминированно выведенная из хэша конкретного бланта (item["hash"]): один и тот
же блант всегда даёт один и тот же арт, но два разных бланта — разный. Картинка
рисуется ОДИН раз на создание, кэшируется как Telegram file_id и переиспользуется.

── Дизайн-позиция (интенциональность, не аккуратность) ──────────────────────
Карточка должна за 300 мс сказать: «оккультная реликвия из андеграунда», а не
«сгенерированный отчёт». Отсюда конкретные НЕдефолтные решения:

* Иерархия по прищур-тесту: первым проступает ИМЯ (это то, что создал игрок),
  затем метка редкости, затем мета. Метка намеренно мелкая и разреженная —
  она статус, а не заголовок; конкуренция за primary убивает иерархию.
* Редкость кодируется СТРУКТУРНО, а не только цветом (цвет — не единственный
  носитель смысла: ~8% мужчин не различают): число пипсов, плотность орнамента,
  двойная рамка, фольга, плотность звёздного поля.
* Материальность: радиальная виньетка + зерно плёнки + bloom-свечение +
  фольговый градиент. Плоская заливка читается как дёшево; свет и текстура —
  как дорого.
* Фон не чистый чёрный, а сдвинут в холодный фиолет: чистый #000 на больших
  плоскостях режет глаз и выглядит дёшево.
* Типографика: капс метки с ПОЛОЖИТЕЛЬНЫМ трекингом, имя — крупная антиква,
  мета — моноширинный (цифры не «прыгают»).

Зависит только от Pillow. Если Pillow нет или рендер падает — вызывающий код
откатывается на текст (карточка — украшение, не критичный путь).

    render_blunt_card(item, owner_name) -> bytes (PNG)
"""
from __future__ import annotations

import io
import math
import os
import random
import re

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# ── Иерархия редкости: цвет + СТРУКТУРА ─────────────────────────────
#   glow    — цвет свечения за эмблемой
#   frame   — основная рамка
#   accent  — яркие детали (штрих сигила, узлы, метка)
#   pips    — сколько делений статуса (редкость читается без цвета)
#   rings   — плотность орнамента углов
#   foil    — фольговый отблеск (материальность топ-редкостей)
#   stars   — плотность звёздного поля
_RARITY = {
    "common": {
        "glow": (46, 104, 64), "frame": (104, 170, 118), "accent": (168, 226, 178),
        "label": "ОБЫЧНЫЙ", "pips": 1, "rings": 0, "foil": 0.0, "stars": 26,
    },
    "rare": {
        "glow": (32, 84, 168), "frame": (86, 148, 232), "accent": (168, 208, 255),
        "label": "РЕДКИЙ", "pips": 2, "rings": 1, "foil": 0.0, "stars": 46,
    },
    "epic": {
        "glow": (104, 42, 158), "frame": (176, 104, 234), "accent": (224, 176, 255),
        "label": "ЭПИЧЕСКИЙ", "pips": 3, "rings": 2, "foil": 0.16, "stars": 72,
    },
    "legendary": {
        "glow": (188, 122, 18), "frame": (255, 198, 72), "accent": (255, 236, 170),
        "label": "ЛЕГЕНДАРНЫЙ", "pips": 4, "rings": 3, "foil": 0.34, "stars": 104,
    },
}

# Фон — не чистый чёрный, сдвинут в холодный фиолет (дорогая тёмная тема).
_BG_CORE = (34, 28, 48)     # центр (светлее — фигура)
_BG_EDGE = (9, 8, 14)       # края (виньетка — глаз ведётся к центру)

_SCALE = 2                  # рисуем в 2× и уменьшаем → сглаживание краёв
_W, _H = 640, 900

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
    """Ширина строки с учётом трекинга (Pillow сам его не умеет)."""
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


def _radial_bg(size, core, edge):
    """Радиальная виньетка: свет в центре, тьма к краям — глубина вместо плоскости.

    Считаем на маленьком холсте и растягиваем: per-pixel Python по полному
    размеру был бы непозволительно медленным."""
    w, h = size
    sw, sh = 80, 112
    small = Image.new("RGB", (sw, sh))
    px = small.load()
    cx, cy = sw / 2, sh * 0.44
    maxd = math.hypot(max(cx, sw - cx), max(cy, sh - cy))
    for yy in range(sh):
        for xx in range(sw):
            d = math.hypot(xx - cx, yy - cy) / maxd
            t = min(1.0, d ** 1.35)
            px[xx, yy] = tuple(int(core[i] + (edge[i] - core[i]) * t) for i in range(3))
    return small.resize((w, h), Image.BICUBIC)


def _apply_grain(img, rng, strength=7):
    """Зерно плёнки — материальность. Без него градиент выглядит «пластиково».

    Зерно берётся из ТОГО ЖЕ seed, что и вся карта: Image.effect_noise тянет
    глобальный ГПСЧ, из-за чего один и тот же блант давал разные карты — а это
    ломает сам контракт коллекционности (карта должна быть паспортом предмета).

    Шум генерится в половинном разрешении и растягивается (быстро), и он
    ЗНАКОПЕРЕМЕННЫЙ вокруг 128: складывать шум со средним 128 напрямую нельзя —
    это подмешало бы ~128 яркости на весь холст и вымыло бы карту в серое.
    """
    w, h = img.size
    sw, sh = max(1, w // 2), max(1, h // 2)
    noise = Image.frombytes("L", (sw, sh), rng.randbytes(sw * sh))
    k = strength / 74.0  # ужимаем равномерный 0..255 (σ≈74) до ±strength
    noise = noise.point(lambda v: int(128 + (v - 128) * k))
    noise = noise.resize((w, h), Image.BILINEAR)
    return ImageChops.add(img, Image.merge("RGB", (noise, noise, noise)),
                          scale=1.0, offset=-128)


def _dim(layer, k):
    """Приглушить слой перед screen: свет должен быть локальным, а не заливать кадр."""
    return layer.point(lambda v: int(v * k))


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


def _starfield(draw, w, h, rng, count, accent, glow):
    """Пылевое поле — воздух и глубина. Плотность растёт с редкостью."""
    for _ in range(count):
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        r = rng.choice([1, 1, 1, 2, 2, 3]) * (w / 640)
        col = accent if rng.random() < 0.3 else glow
        draw.ellipse([x - r, y - r, x + r, y + r], fill=col)


def _corner_ornaments(draw, w, h, m, rings, frame, accent):
    """Филигрань в углах: чем выше редкость, тем богаче огранка."""
    if rings <= 0:
        return
    L = int(w * 0.085)
    wgt = max(2, w // 260)
    for sx, sy, ax, ay in ((1, 1, m, m), (-1, 1, w - m, m),
                           (1, -1, m, h - m), (-1, -1, w - m, h - m)):
        for k in range(rings):
            off = int(w * 0.022) * (k + 1)
            ln = L - k * int(w * 0.018)
            if ln <= 0:
                continue
            col = accent if k == 0 else frame
            draw.line([(ax + sx * off, ay + sy * off),
                       (ax + sx * (off + ln), ay + sy * off)], fill=col, width=wgt)
            draw.line([(ax + sx * off, ay + sy * off),
                       (ax + sx * off, ay + sy * (off + ln))], fill=col, width=wgt)


def _sigil(draw, cx, cy, rng, glow, accent, frame, radius):
    """Гравированная эмблема-сигил. Семя — из хэша, поэтому уникальна на блант.

    Слои: полупрозрачное ядро → концентрические кольца → лучи → звёздный
    полигон → сияющие узлы. Многослойность = ощущение гравюры, а не вайрфрейма.
    """
    points = rng.randint(5, 9)
    step = rng.choice([2, 3, 4])
    rot = rng.uniform(0, math.pi)
    wgt = max(2, int(radius / 55))

    # ядро — приглушённая заливка, чтобы центр не «дырявился» и не выжигался
    core_r = radius * 0.26
    core_col = tuple(int(c * 0.55) for c in glow)
    draw.ellipse([cx - core_r, cy - core_r, cx + core_r, cy + core_r], fill=core_col)

    # концентрические кольца
    for k in range(rng.randint(2, 4)):
        rr = radius * (0.42 + 0.20 * k)
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                     outline=frame, width=max(1, wgt // 2))

    # радиальные лучи
    rays = rng.randint(8, 16)
    for i in range(rays):
        a = 2 * math.pi * i / rays + rot
        r0, r1 = radius * 0.16, radius * rng.uniform(0.72, 1.08)
        col = accent if i % 2 == 0 else frame
        draw.line([(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
                   (cx + r1 * math.cos(a), cy + r1 * math.sin(a))],
                  fill=col, width=max(1, wgt // 2))

    # звёздный полигон — «подпись» бланта
    verts = [(cx + radius * math.cos(rot + 2 * math.pi * i / points),
              cy + radius * math.sin(rot + 2 * math.pi * i / points))
             for i in range(points)]
    poly = [verts[(i * step) % points] for i in range(points)]
    draw.line(poly + [poly[0]], fill=accent, width=wgt, joint="curve")

    # сияющие узлы на вершинах (компактные — крупные превращаются в белые кляксы)
    for (vx, vy) in verts:
        rr = max(2, radius // 34)
        draw.ellipse([vx - rr * 1.7, vy - rr * 1.7, vx + rr * 1.7, vy + rr * 1.7],
                     fill=tuple(int(c * 0.6) for c in glow))
        draw.ellipse([vx - rr, vy - rr, vx + rr, vy + rr], fill=accent)


def _pips(draw, cx, y, count, total, accent, frame, unit):
    """Деления статуса: редкость читается СТРУКТУРНО, без опоры на цвет."""
    gap = unit * 15
    r = unit * 4.2
    start = cx - (total - 1) * gap / 2
    for i in range(total):
        x = start + i * gap
        if i < count:
            draw.ellipse([x - r * 1.85, y - r * 1.85, x + r * 1.85, y + r * 1.85], fill=frame)
            draw.ellipse([x - r, y - r, x + r, y + r], fill=accent)
        else:
            draw.ellipse([x - r, y - r, x + r, y + r], outline=frame, width=max(1, int(unit * 1.2)))


def render_blunt_card(item: dict, owner_name: str = "") -> bytes:
    """PNG-байты коллекционной карточки для бланта `item`. Детерминирован по hash."""
    rarity = item.get("rarity", "common")
    pal = _RARITY.get(rarity, _RARITY["common"])
    glow, frame, accent = pal["glow"], pal["frame"], pal["accent"]

    # семя из хэша → воспроизводимая уникальность
    seed_src = str(item.get("hash") or item.get("id") or item.get("rare_number") or "0")
    hexs = re.sub(r"[^0-9a-fA-F]", "", seed_src)
    seed = int(hexs, 16) if hexs else abs(hash(seed_src))
    rng = random.Random(seed)

    W, H = _W * _SCALE, _H * _SCALE
    u = W / 640.0  # единица масштаба

    # ── фон: радиальная виньетка ──
    img = _radial_bg((W, H), _BG_CORE, _BG_EDGE)

    # ── свечение редкости за эмблемой (приглушённая bloom-подложка) ──
    # Полнояркий screen залил бы кадр молоком: карта обязана остаться ТЁМНОЙ,
    # свет — только локальный ореол вокруг сигила.
    gcx, gcy = W // 2, int(H * 0.40)
    halo = Image.new("RGB", (W, H), (0, 0, 0))
    hd = ImageDraw.Draw(halo)
    gr = int(W * 0.34)
    hd.ellipse([gcx - gr, gcy - gr, gcx + gr, gcy + gr], fill=glow)
    halo = halo.filter(ImageFilter.GaussianBlur(int(W * 0.075)))
    img = ImageChops.screen(img, _dim(halo, 0.42))

    # ── звёздная пыль ──
    dust = Image.new("RGB", (W, H), (0, 0, 0))
    _starfield(ImageDraw.Draw(dust), W, H, rng, pal["stars"], accent, glow)
    img = ImageChops.screen(img, _dim(dust.filter(ImageFilter.GaussianBlur(u * 0.5)), 0.75))

    # ── фольговый отблеск ПОД сигилом (материальность топ-редкостей) ──
    foil = _foil((W, H), rng, accent, pal["foil"])
    if foil is not None:
        img = ImageChops.screen(img, foil)

    # ── сигил на отдельном слое + bloom ──
    sig = Image.new("RGB", (W, H), (0, 0, 0))
    _sigil(ImageDraw.Draw(sig), gcx, gcy, rng, glow, accent, frame, radius=int(W * 0.29))
    img = ImageChops.screen(img, _dim(sig.filter(ImageFilter.GaussianBlur(u * 4)), 0.55))
    img = ImageChops.screen(img, sig)  # чёткий штрих поверх ореола

    # ── зерно плёнки (последним — по всему кадру, знакопеременное) ──
    img = _apply_grain(img, rng, 7)

    draw = ImageDraw.Draw(img)

    # ── рамки: внешняя + внутренний волосок (огранка, не «бордер») ──
    m = int(W * 0.038)
    draw.rectangle([m, m, W - m, H - m], outline=frame, width=max(3, int(u * 3.2)))
    m2 = m + int(u * 11)
    draw.rectangle([m2, m2, W - m2, H - m2], outline=glow, width=max(1, int(u * 1.2)))
    if rarity in ("epic", "legendary"):
        m3 = m - int(u * 7)
        draw.rectangle([m3, m3, W - m3, H - m3], outline=accent, width=max(1, int(u * 1.6)))

    _corner_ornaments(draw, W, H, m2 + int(u * 6), pal["rings"], frame, accent)

    # ── метка редкости: мелкая, разреженная — статус, а не заголовок ──
    f_label = _font("DejaVuSans-Bold.ttf", int(21 * u))
    label, tr = pal["label"], u * 4.2
    lw = _text_w(draw, label, f_label, tr)
    _draw_tracked(draw, ((W - lw) / 2, H * 0.078), label, f_label, accent, tr)

    # тонкие направляющие по бокам метки (continuity — глаз ведётся по линии)
    ly = H * 0.078 + int(11 * u)
    pad = u * 16
    draw.line([(W * 0.20, ly), ((W - lw) / 2 - pad, ly)], fill=frame, width=max(1, int(u)))
    draw.line([((W + lw) / 2 + pad, ly), (W * 0.80, ly)], fill=frame, width=max(1, int(u)))

    # ── ИМЯ — primary. Крупная антиква, тень для читаемости поверх сигила ──
    name = _sanitize(item.get("name", "Безымянный"))
    size = int(50 * u)
    f_name = _font("DejaVuSerif-Bold.ttf", size)
    avail = W - 2 * m2 - int(u * 40)
    lines = _wrap(draw, f"«{name}»", f_name, avail)
    while size > int(26 * u) and any(draw.textlength(l, font=f_name) > avail for l in lines):
        size = int(size * 0.9)
        f_name = _font("DejaVuSerif-Bold.ttf", size)
        lines = _wrap(draw, f"«{name}»", f_name, avail)

    y = H * 0.645
    for ln in lines:
        lw2 = draw.textlength(ln, font=f_name)
        x = (W - lw2) / 2
        draw.text((x + u * 2.2, y + u * 2.2), ln, font=f_name, fill=(6, 5, 10))   # тень
        draw.text((x, y), ln, font=f_name, fill=(248, 244, 252))                  # основной
        y += size * 1.16

    # разделитель под именем
    dy = y + u * 14
    draw.line([(W * 0.34, dy), (W * 0.66, dy)], fill=frame, width=max(1, int(u)))

    # ── мета: моноширинный (цифры не «прыгают») ──
    f_meta = _font("DejaVuSansMono.ttf", int(22 * u))
    serial = str(item.get("rare_number", "?-????"))
    short_hash = str(item.get("hash", "0x????"))[:12]
    meta = f"#{serial}   {short_hash}"
    mw = _text_w(draw, meta, f_meta, u * 1.1)
    _draw_tracked(draw, ((W - mw) / 2, dy + u * 22), meta, f_meta, frame, u * 1.1)

    # ── деления статуса ──
    _pips(draw, W / 2, H * 0.845, pal["pips"], 4, accent, frame, u)

    # ── владелец + бренд (подвал; serial position — низ запоминается) ──
    f_small = _font("DejaVuSans.ttf", int(21 * u))
    owner = _sanitize(owner_name, 20) if owner_name else ""
    if owner:
        ot = f"Первый владелец: {owner}"
        ow = draw.textlength(ot, font=f_small)
        oy = H * 0.876
        # тень: подвал лежит на виньетке, без неё контраст падает ниже читаемого
        draw.text(((W - ow) / 2 + u * 1.6, oy + u * 1.6), ot, font=f_small, fill=(6, 5, 10))
        draw.text(((W - ow) / 2, oy), ot, font=f_small, fill=(214, 208, 226))

    f_brand = _font("DejaVuSans-Bold.ttf", int(18 * u))
    brand, btr = "КОДЕКС ИСКАЖЕНИЯ", u * 5.0
    bw = _text_w(draw, brand, f_brand, btr)
    _draw_tracked(draw, ((W - bw) / 2, H * 0.918), brand, f_brand, frame, btr)

    img = img.resize((_W, _H), Image.LANCZOS)
    out = io.BytesIO()
    # JPEG, а не PNG: зерно плёнки — шум, он убивает PNG-сжатие (карта весила
    # ~800 КБ). Для фотографического арта JPEG даёт тот же вид в ~8 раз легче,
    # а Telegram всё равно пережимает фото в JPEG.
    out.name = "blunt.jpg"
    img.save(out, format="JPEG", quality=90, optimize=True, progressive=True)
    out.seek(0)
    return out.getvalue()
