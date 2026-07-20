"""Процедурный рендер коллекционной карточки именного бланта.

Настоящая коллекционность рождается, когда КАЖДЫЙ блант визуально уникален —
а не когда все «обычные» на одну картинку. Здесь генерируется карточка-эмблема,
детерминированно выведенная из хэша конкретного бланта (item["hash"]): один и тот
же блант всегда даёт один и тот же арт, но два разных бланта — разный. Стиль
задаётся редкостью (цвет/рамка/сияние). Картинка рисуется ОДИН раз на создание,
кэшируется как Telegram file_id и переиспользуется.

Зависит только от Pillow. Если Pillow нет или рендер падает — вызывающий код
должен откатиться на текст (карточка — украшение, не критичный путь).

    render_blunt_card(item, owner_name) -> bytes (PNG)
"""
from __future__ import annotations

import io
import math
import os
import random
import re

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ── Палитра по редкости ─────────────────────────────────────────────
# (base_bg, glow/accent, frame, светлый акцент для деталей, человекочит. метка)
_RARITY = {
    "common":    {"glow": (54, 120, 70),   "frame": (96, 176, 112), "accent": (150, 220, 160), "label": "ОБЫЧНЫЙ"},
    "rare":      {"glow": (30, 90, 180),    "frame": (70, 140, 230), "accent": (150, 200, 255), "label": "РЕДКИЙ"},
    "epic":      {"glow": (110, 40, 160),   "frame": (170, 90, 230), "accent": (215, 160, 255), "label": "ЭПИЧЕСКИЙ"},
    "legendary": {"glow": (200, 130, 20),   "frame": (255, 196, 60), "accent": (255, 230, 150), "label": "ЛЕГЕНДАРНЫЙ"},
}
_BG_TOP = (24, 20, 34)      # тёмный фон карточки (верх)
_BG_BOTTOM = (12, 10, 18)   # низ

_SCALE = 2                  # рисуем в 2× и уменьшаем → сглаживание
_W, _H = 640, 900           # финальный размер карточки

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu",)


def _font(name: str, size: int):
    for d in _FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _sanitize(text: str, limit: int = 34) -> str:
    """Оставляем только то, что рисует DejaVu (кириллица/латиница/цифры/пунктуация).

    Эмодзи и прочие не-BMP символы Pillow с DejaVu рисует «тофу»-квадратами —
    вырезаем их, чтобы имя на карточке было чистым."""
    text = str(text or "")
    keep = re.sub(r"[^0-9A-Za-zЀ-ӿ ,.\-!?'«»\"()]+", "", text)
    keep = re.sub(r"\s+", " ", keep).strip()
    return (keep[:limit] or "Безымянный")


def _wrap(draw, text, font, max_w):
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
    return lines[:2]  # максимум 2 строки на карточке


def _vgrad(size, top, bottom):
    """Вертикальный градиент фона."""
    w, h = size
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        base.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return base.resize((w, h))


def _sigil(draw, cx, cy, rng, glow, accent, radius):
    """Симметричная эмблема-«сигил», семя — из хэша бланта (уникальна на блант)."""
    points = rng.randint(5, 9)
    # звёздный полигон
    step = rng.choice([2, 3, 4])
    verts = []
    rot = rng.uniform(0, math.pi)
    for i in range(points):
        a = rot + 2 * math.pi * i / points
        verts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    order = [(i * step) % points for i in range(points)]
    poly = [verts[i] for i in order]
    draw.line(poly + [poly[0]], fill=accent, width=max(2, radius // 60))

    # концентрические кольца
    for k in range(rng.randint(2, 4)):
        rr = radius * (0.35 + 0.22 * k)
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=glow, width=max(1, radius // 90))

    # радиальные лучи с акцентами
    rays = rng.randint(6, 12)
    for i in range(rays):
        a = 2 * math.pi * i / rays + rot
        r0 = radius * 0.15
        r1 = radius * rng.uniform(0.7, 1.05)
        col = accent if i % 2 == 0 else glow
        draw.line([(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
                   (cx + r1 * math.cos(a), cy + r1 * math.sin(a))],
                  fill=col, width=max(1, radius // 80))

    # сияющие узлы на вершинах полигона
    for (vx, vy) in verts:
        rr = max(3, radius // 22)
        draw.ellipse([vx - rr, vy - rr, vx + rr, vy + rr], fill=accent)


def render_blunt_card(item: dict, owner_name: str = "") -> bytes:
    """PNG-байты коллекционной карточки для бланта `item`. Детерминирован по hash."""
    rarity = item.get("rarity", "common")
    pal = _RARITY.get(rarity, _RARITY["common"])
    glow, frame, accent = pal["glow"], pal["frame"], pal["accent"]

    # семя из хэша → воспроизводимая уникальность
    seed_src = str(item.get("hash") or item.get("id") or item.get("rare_number") or "0")
    seed = int(re.sub(r"[^0-9a-fA-F]", "", seed_src) or "0", 16) if re.search(r"[0-9a-fA-F]", seed_src) else abs(hash(seed_src))
    rng = random.Random(seed)

    W, H = _W * _SCALE, _H * _SCALE
    img = _vgrad((W, H), _BG_TOP, _BG_BOTTOM).convert("RGB")

    # сияние редкости за эмблемой
    glow_layer = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gcx, gcy = W // 2, int(H * 0.42)
    gr = int(W * 0.42)
    gd.ellipse([gcx - gr, gcy - gr, gcx + gr, gcy + gr], fill=glow)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(W // 12))
    img = Image.blend(img, Image.composite(glow_layer, img, glow_layer.convert("L").point(lambda v: min(255, v))), 0.0)
    img.paste(Image.blend(img, glow_layer, 0.35), (0, 0))

    draw = ImageDraw.Draw(img)

    # процедурная эмблема
    _sigil(draw, gcx, gcy, rng, glow, accent, radius=int(W * 0.30))

    # рамка редкости (двойная у легендарного)
    m = int(W * 0.035)
    draw.rectangle([m, m, W - m, H - m], outline=frame, width=max(3, W // 120))
    if rarity in ("epic", "legendary"):
        m2 = m + W // 45
        draw.rectangle([m2, m2, W - m2, H - m2], outline=accent, width=max(2, W // 220))

    # верхний баннер — метка редкости
    f_label = _font("DejaVuSans-Bold.ttf", int(30 * _SCALE))
    label = pal["label"]
    lw = draw.textlength(label, font=f_label)
    draw.text(((W - lw) / 2, int(H * 0.07)), label, font=f_label, fill=accent)

    # разделитель
    draw.line([(W * 0.22, H * 0.115), (W * 0.78, H * 0.115)], fill=frame, width=max(1, W // 320))

    # имя бланта (центр-низ)
    name = _sanitize(item.get("name", "Безымянный"))
    f_name = _font("DejaVuSerif-Bold.ttf", int(44 * _SCALE))
    lines = _wrap(draw, f"«{name}»", f_name, W - 2 * m - W // 12)
    y = int(H * 0.66)
    for ln in lines:
        w = draw.textlength(ln, font=f_name)
        # лёгкая тень для читаемости поверх эмблемы
        draw.text(((W - w) / 2 + 2, y + 2), ln, font=f_name, fill=(0, 0, 0))
        draw.text(((W - w) / 2, y), ln, font=f_name, fill=(245, 240, 250))
        y += int(52 * _SCALE)

    # мета: серийный номер + хэш
    f_meta = _font("DejaVuSansMono.ttf", int(24 * _SCALE))
    serial = str(item.get("rare_number", "?-????"))
    short_hash = str(item.get("hash", "0x????"))[:12]
    meta = f"#{serial}   {short_hash}"
    mw = draw.textlength(meta, font=f_meta)
    draw.text(((W - mw) / 2, int(H * 0.80)), meta, font=f_meta, fill=frame)

    # владелец + бренд (низ)
    f_small = _font("DejaVuSans.ttf", int(22 * _SCALE))
    owner = _sanitize(owner_name, 20) if owner_name else ""
    if owner:
        ot = f"Первый владелец: {owner}"
        ow = draw.textlength(ot, font=f_small)
        draw.text(((W - ow) / 2, int(H * 0.86)), ot, font=f_small, fill=(190, 185, 200))
    f_brand = _font("DejaVuSans-Bold.ttf", int(22 * _SCALE))
    brand = "КОДЕКС ИСКАЖЕНИЯ · ANTYSOCIALSHOP"
    bw = draw.textlength(brand, font=f_brand)
    draw.text(((W - bw) / 2, int(H * 0.91)), brand, font=f_brand, fill=frame)

    img = img.resize((_W, _H), Image.LANCZOS)
    out = io.BytesIO()
    out.name = "blunt.png"
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out.getvalue()
