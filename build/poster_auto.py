"""Poster / cover for an auto episode — English-only, Technology × Metacognition.

1080x1920 (9:16) + 1080x1350 (4:5 crop that keeps hook, web and handle).
Stand-in emblem only, English-only, no Persian layer.

usage: python3 build/poster_auto.py --ep content/episodes/auto-<tag> --out-dir output
"""
import argparse
import math
import os
import sys
import textwrap

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import text_norm
from reel_engine import Reel, W, H, GOLD, GOLD_HI, WARM, font, text_img, gold_text, glow_disc, rounded_card, with_alpha

def build(reel, out_dir, tag):
    # Issue #32 §1: poster text is visible text too — normalize and glyph-gate
    # it against the ACTUAL production font cmap BEFORE drawing, fail closed.
    meta = reel.script.get("meta", {}) or {}
    extras = [
        f"{meta.get('pillar', '')} · {meta.get('content_date', tag)}",
        str(meta.get("technology_angle", "") or ""),
        str(reel.visual_direction or ""),
        str((reel.script.get("caption") or {}).get("hook", "") or ""),
    ]
    issues = text_norm.glyph_gate_issues(reel.script, extra_texts=extras)
    if issues:
        raise RuntimeError("poster glyph gate blocked before draw: "
                           + " · ".join(issues[:6]))
    fr = reel.bg_crop("base", 0, 0, 1).convert("RGBA")
    reel.draw_lattice(fr, 3.0, 0.8)
    d = ImageDraw.Draw(fr)
    # web fully lit
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
        fr.alpha_composite(with_alpha(lab, 0.9), (int(x - lab.width // 2), int(ly)))
    fr.alpha_composite(glow_disc(1100, (255, 186, 90, 170), 380, 150), (540 - 550, cy - 550))
    em = reel.emblem.resize((520, 520), Image.LANCZOS)
    fr.alpha_composite(em, (540 - 260, cy - 260))
    # top: pillar + technology_angle + date
    meta = reel.script["meta"]
    tagline = text_norm.normalize_text(
        f"{meta.get('pillar', 'AI_JUDGMENT')} · {meta.get('content_date', tag)}")
    tech_angle = text_norm.normalize_text(meta.get('technology_angle', '') or "")[:50]
    t = text_img(tagline, font("en", 700, 30), GOLD, spacing=6)
    fr.alpha_composite(t, (540 - t.width // 2, 280))
    if tech_angle:
        t2 = text_img(tech_angle, font("en", 600, 26), WARM, spacing=2)
        fr.alpha_composite(t2, (540 - t2.width // 2, 320))
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
    # Evidence card / visual hint below emblem (English-only, bigger diagram)
    vd = reel.visual_direction or ""
    if vd:
        card = rounded_card(760, 120, 18, (18, 20, 24, 200), (233, 180, 74, 70), 2)
        d2 = ImageDraw.Draw(card)
        d2.text((20, 20), vd[:60], font=font("en", 500, 22), fill=WARM)
        fr.alpha_composite(card, (160, 1320))
    t = gold_text("METACOGNITION FOR THE AI AGE", font("en", 700, 32), spacing=4)
    fr.alpha_composite(t, (540 - t.width // 2, 1500))
    t = text_img("@metacognition.hq", font("en", 600, 34), GOLD, spacing=3)
    fr.alpha_composite(t, (540 - t.width // 2, 1556))
    out = fr.convert("RGB")
    p1 = os.path.join(out_dir, f"auto-{tag}_poster.jpg")
    p2 = os.path.join(out_dir, f"auto-{tag}_poster_4x5.jpg")
    out.save(p1, quality=88)
    out.crop((0, 262, 1080, 1612)).save(p2, quality=88)
    print(f"[poster] {p1}\n[poster] {p2}")
    return p1, p2

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ep", required=True)
    ap.add_argument("--out-dir", default=os.path.join(common.ROOT, "output"))
    a = ap.parse_args()
    common.assert_content_language_en()
    reel = Reel(a.ep, common.policy())
    tag = os.path.basename(os.path.abspath(a.ep)).replace("auto-", "")
    os.makedirs(a.out_dir, exist_ok=True)
    build(reel, a.out_dir, tag)

if __name__ == "__main__":
    main()
