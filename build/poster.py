"""Poster / reel cover for metacognition.hq — 1080x1920 + 1080x1350 crop."""
import math, os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from engine import (ROOT, W, H, GOLD, GOLD_HI, GOLD_LO, WARM, DIM, font,
                    text_img, gold_text, glow_disc, rounded_card, res)

R = res()


def build():
    fr = R.bg_base.copy().convert("RGBA")
    d = ImageDraw.Draw(fr)
    # full lit web as backdrop
    skip = {k for k, nd in R.nodes.items()
            if math.hypot(nd["x"] - 540, nd["y"] + 120 - 980) < 560 or k == "hq"}
    for a, b in R.edges:
        if a in skip or b in skip:
            continue
        na, nb = R.nodes[a], R.nodes[b]
        pts = []
        x0, y0, x1, y1 = na["x"], na["y"] + 120, nb["x"], nb["y"] + 120
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy) or 1
        cx, cy = mx - dy / L * L * 0.16, my + dx / L * L * 0.16
        for i in range(25):
            u = i / 24
            pts.append(((1 - u) ** 2 * x0 + 2 * (1 - u) * u * cx + u ** 2 * x1,
                        (1 - u) ** 2 * y0 + 2 * (1 - u) * u * cy + u ** 2 * y1))
        for i in range(24):
            d.line([pts[i], pts[i + 1]], fill=(233, 180, 74, 80), width=2)
    for k, nd in R.nodes.items():
        x, y = nd["x"], nd["y"] + 120
        if math.hypot(x - 540, y - 980) < 560 or k == "hq":
            continue
        d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=GOLD)
        lab = nd["lab_on"]
        li = lab.copy()
        li.putalpha(li.getchannel("A").point(lambda v: int(v * 0.85)))
        fr.alpha_composite(li, (int(x - lab.width / 2), int(y + 14)))
    # glow + emblem
    fr.alpha_composite(glow_disc(1500, (255, 186, 90, 190), 560, 190), (540 - 750, 980 - 750))
    em = R.emblem.resize((1000, 1000), Image.LANCZOS)
    fr.alpha_composite(em, (540 - 500, 980 - 500))
    # top block
    t = text_img("REEL 01 · WHY THIS NAME?", font("en", 700, 34), GOLD, spacing=6)
    fr.alpha_composite(t, (540 - t.width // 2, 150))
    d.line([340, 220, 740, 220], fill=(233, 180, 74, 160), width=2)
    t1 = gold_text("WHY", font("en", 800, 150), spacing=10)
    t2 = gold_text("METACOGNITION?", font("en", 800, 96), spacing=4)
    fr.alpha_composite(t1, (540 - t1.width // 2, 260))
    fr.alpha_composite(t2, (540 - t2.width // 2, 420))
    # persian question under emblem
    from arabic_reshaper import reshape
    from bidi.algorithm import get_display
    fa = "چرا اسم این پیج metacognition است؟"
    import re as _re
    runs = _re.findall(r"[A-Za-z0-9@./%'+-]+|[^A-Za-z0-9@./%'+-]+", fa)
    vis = []
    for r in runs:
        if _re.fullmatch(r"[A-Za-z0-9@./%'+-]+", r):
            vis.insert(0, text_img(r, font("en", 600, 52), WARM))
        else:
            vis.insert(0, text_img(get_display(reshape(r)), font("fa", 500, 56), WARM))
    tw = sum(im.width for im in vis) - 6 * len(vis)
    x = 540 - tw // 2
    for im in vis:
        fr.alpha_composite(im, (x, 1620))
        x += im.width - 6
    t = gold_text("TRAIN THE WATCHER IN YOUR HEAD", font("en", 700, 40), spacing=4)
    fr.alpha_composite(t, (540 - t.width // 2, 1720))
    t = text_img("@metacognition.hq", font("en", 600, 40), GOLD, spacing=3)
    fr.alpha_composite(t, (540 - t.width // 2, 1810))
    out = fr.convert("RGB")
    out.save(f"{ROOT}/output/poster_metacognition_hq.png")
    out.crop((0, 240, 1080, 1590)).save(f"{ROOT}/output/poster_4x5.png")
    print("poster saved")


if __name__ == "__main__":
    build()
