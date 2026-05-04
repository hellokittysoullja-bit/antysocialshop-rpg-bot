from PIL import Image, ImageDraw
import os

os.makedirs("blunt_pics", exist_ok=True)

def draw_blunt(rarity):
    w, h = 512, 512
    colors = {
        "common": ("#2d5a27", "#4caf50"),
        "rare": ("#1a3a5c", "#2196f3"),
        "epic": ("#3e1f47", "#9c27b0"),
        "legendary": ("#4a3f00", "#ffd700")
    }
    bg, el = colors[rarity]
    img = Image.new("RGB", (w,h), bg)
    d = ImageDraw.Draw(img)

    # рамка
    for i in range(5):
        d.rectangle([i,i,w-i-1,h-i-1], outline=el)

    # упрощённый косяк
    d.ellipse([180,150, 332,280], fill=el)
    d.rectangle([180,220, 332,360], fill=el)
    d.polygon([220,360, 292,360, 256,430], fill="#8b4513")

    # текст редкости
    try:
        from PIL import ImageFont
        fnt = ImageFont.truetype("arial.ttf", 30)
    except:
        fnt = ImageFont.load_default()
    txt = rarity.upper()
    b = d.textbbox((0,0), txt, font=fnt)
    tw = b[2]-b[0]
    d.text(((w-tw)/2, 450), txt, fill=el, font=fnt)

    path = f"blunt_pics/blunt_{rarity}.jpg"
    img.save(path)
    print(f"✅ Готово: {path}")

for r in ["common","rare","epic","legendary"]:
    draw_blunt(r)
