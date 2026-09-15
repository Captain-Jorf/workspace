"""Poster / cover for an auto episode: 1080x1920 (9:16) + 1080x1350 (4:5 crop that
keeps the hook, the web and the handle inside the crop). Stand-in emblem only.

usage: python3 build/poster_auto.py --ep content/episodes/auto-<tag> --out-dir output
"""
import argparse
import math
import os
import sys
import textwrap

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from reel_engine import Reel, W, H, GOLD, GOLD_HI, WARM, font, text_img, gold_text, glow_disc, \
    fa_display, fa_wrap, rounded_card, with_alpha  # noqa: E402


def build(reel, out_dir, tag):
    fr = reel.bg_crop("base", 0, 0, 1).convert("RGBA")
    reel.draw_lattice(fr, 3.0, 0.8)
    d = ImageDraw.Draw(fr)
    # web fully lit (poster shows the whole concept map)
    cx, cy = 540, 1010
    pts = []
    for i, nd in enumerate(reel.nodes):
        pts.append((nd["x"], nd["y"] + 220))
    for i, j in reel.web_edges:
        d.line([pts[i], pts[j]], fill=(233, 180, 74, 90), width=2)
    for i, nd in enumerate(reel.nodes):
        x, y = pts[i]
        d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=GOLD)
        lab = nd["lab_on"]
        ly = y + 14 if nd["below"] else y - 14 - lab.height
        fr.alpha_composite(with_alpha(lab, 0.9), (int(x - lab.width / 2), int(ly)))
    fr.alpha_composite(glow_disc(1100, (255, 186, 90, 170), 380, 150), (540 - 550, cy - 550))
    em = reel.emblem.resize((520, 520), Image.LANCZOS)
    fr.alpha_composite(em, (540 - 260, cy - 260))
    # top: pillar tag + hook (wrapped)
    meta = reel.script["meta"]
    tagline = f"{meta.get('pillar', 'THINK')} · {meta.get('content_date', tag)}"
    t = text_img(tagline, font("en", 700, 30), GOLD, spacing=6)
    fr.alpha_composite(t, (540 - t.width // 2, 300))
    d.line([340, 360, 740, 360], fill=(233, 180, 74, 160), width=2)
    hook = reel.script["caption"]["hook"]
    lines = textwrap.wrap(hook, 24)[:4]
    size = 74 if len(lines) <= 3 else 62
    y = 400
    for ln in lines:
        ti = gold_text(ln, font("en", 800, size), spacing=1)
        if ti.width > 980:
            ti = gold_text(ln, font("en", 800, 54), spacing=1)
        fr.alpha_composite(ti, (540 - ti.width // 2, y))
        y += int(size * 1.25)
    # persian hook under the emblem: one pill, right-aligned rows
    fa_hook = reel.script["chunks"][0]["fa"][0] if reel.script["chunks"][0].get("fa") else ""
    if fa_hook:
        ff = font("fa", 600, 42)
        rows = fa_wrap(fa_hook, ff, 860)[:2]
        imgs = [text_img(fa_display(r), ff, WARM) for r in rows]
        pw = max(i.width for i in imgs) + 56
        rh = 66
        ph = len(imgs) * rh + 20
        pill = rounded_card(pw, ph, 24, (12, 10, 8, 175), (233, 180, 74, 70), 2)
        for k, im in enumerate(imgs):
            pill.alpha_composite(im, (pw - 28 - im.width, 10 + k * rh + (rh - im.height) // 2))
        fr.alpha_composite(pill, (540 - pw // 2, 1330))
    t = gold_text("TRAIN THE WATCHER IN YOUR HEAD", font("en", 700, 34), spacing=4)
    fr.alpha_composite(t, (540 - t.width // 2, 1500))
    t = text_img("@metacognition.hq", font("en", 600, 34), GOLD, spacing=3)
    fr.alpha_composite(t, (540 - t.width // 2, 1556))
    out = fr.convert("RGB")
    p1 = os.path.join(out_dir, f"auto-{tag}_poster.jpg")
    p2 = os.path.join(out_dir, f"auto-{tag}_poster_4x5.jpg")
    out.save(p1, quality=88)
    out.crop((0, 262, 1080, 1612)).save(p2, quality=88)       # 1080x1350: tag, hook, emblem, FA hook, handle kept
    print(f"[poster] {p1}\n[poster] {p2}")
    return p1, p2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ep", required=True)
    ap.add_argument("--out-dir", default=os.path.join(common.ROOT, "output"))
    a = ap.parse_args()
    reel = Reel(a.ep, common.policy())
    tag = os.path.basename(os.path.abspath(a.ep)).replace("auto-", "")
    os.makedirs(a.out_dir, exist_ok=True)
    build(reel, a.out_dir, tag)


if __name__ == "__main__":
    main()
