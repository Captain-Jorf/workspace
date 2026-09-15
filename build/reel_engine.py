"""Episode-aware render engine — English-only, Technology × Metacognition.

Visual system: dark marble + drifting lattice mesh + 6-node concept web (labels from script),
animated diagram widgets per beat (confidence meter, code visual, decision tree, evidence card,
prediction graph, human-AI network), English karaoke captions only (lower-middle, LTR, big font,
high contrast, safe zone above caption/IG buttons). No Persian layer.

Freed FA space used for bigger diagram/code visual/evidence card/confidence meter/decision tree/
prediction-vs-result graph/human↔AI network. Keeps mesh network style but tech-relevant per episode.

Used by render_auto.py and poster_auto.py.
"""
import bisect
import json
import math
import os
import random
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
common.assert_content_language_en()
W, H, FPS = 1080, 1920, 30
GOLD = (233, 180, 74)
GOLD_HI = (255, 228, 158)
GOLD_LO = (122, 88, 32)
WARM = (246, 234, 210)
DIM = (150, 136, 112)
INK = (24, 17, 8)
CODE_BG = (18, 20, 24)
CODE_GREEN = (120, 220, 120)
CODE_BLUE = (120, 180, 255)

_fcache = {}

def font(kind, w, size):
    k = (kind, w, size)
    if k not in _fcache:
        # kind en only now, but keep fallback
        path = f"{ROOT}/assets/fonts/{kind}-{w}.ttf"
        if not os.path.exists(path):
            # fallback to en
            path = f"{ROOT}/assets/fonts/en-{w}.ttf"
        _fcache[k] = ImageFont.truetype(path, size)
    return _fcache[k]

def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))

def ease(t):
    t = clamp(t)
    return t * t * (3 - 2 * t)

def ease_out(t):
    t = clamp(t)
    return 1 - (1 - t) ** 3

def ease_back(t):
    t = clamp(t)
    c = 1.7
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2

def lerp(a, b, t):
    return a + (b - a) * t

# text utils
def text_img(text, fnt, fill, spacing=0):
    pad = 12
    tmp = Image.new("RGBA", (8, 8))
    bb = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=fnt)
    w = bb[2] - bb[0] + (spacing * (len(text) - 1) if spacing else 0) + pad * 2
    h = bb[3] - bb[1] + pad * 2
    im = Image.new("RGBA", (max(w, 4), max(h, 4)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = pad - bb[0]
    if spacing:
        for c in text:
            d.text((x, pad - bb[1]), c, font=fnt, fill=fill)
            x += fnt.getbbox(c)[2] + spacing
    else:
        d.text((x, pad - bb[1]), text, font=fnt, fill=fill)
    return im

def gold_text(text, fnt, spacing=0, hi=GOLD_HI, lo=GOLD_LO):
    mask = text_img(text, fnt, (255, 255, 255), spacing)
    a = np.array(mask.getchannel("A"), np.float32) / 255.0
    g = np.linspace(0, 1, mask.height)[:, None]
    col = np.array(hi, np.float32)[None, None, :] * (1 - g)[..., None] + np.array(lo, np.float32)[None, None, :] * g[..., None]
    rgb = np.repeat(col, mask.width, axis=1).astype(np.uint8)
    out = Image.fromarray(rgb, "RGB").convert("RGBA")
    out.putalpha(Image.fromarray((a * 255).astype(np.uint8), "L"))
    return out

def glow_disc(size, color, radius, blur):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    c = size / 2
    ImageDraw.Draw(im).ellipse([c - radius, c - radius, c + radius, c + radius], fill=color)
    return im.filter(ImageFilter.GaussianBlur(blur))

def rounded_card(w, h, r, fill, outline=None, ow=2):
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=fill)
    if outline:
        d.rounded_rectangle([ow // 2, ow // 2, w - 1 - ow // 2, h - 1 - ow // 2], r, outline=outline, width=ow)
    return im

def with_alpha(im, a):
    if a >= 0.999:
        return im
    out = im.copy()
    out.putalpha(out.getchannel("A").point(lambda v: int(v * a)))
    return out

# Resources
class Reel:
    def __init__(self, epdir, pol, safe=False):
        self.ep = os.path.abspath(epdir)
        self.pol = pol
        self.L = pol["layout"]
        self.safe = safe
        self.script = json.load(open(f"{self.ep}/script.json", encoding="utf-8"))
        self.tl = json.load(open(f"{self.ep}/timing.json", encoding="utf-8"))
        self.audio_full = f"{self.ep}/full.wav"
        self.total = float(self.tl["total"])
        self.tags = self.script.get("scene_tags", {})
        self.web_labels = (self.script.get("web") or ["QUESTION", "PROBLEM", "IDEA", "EXAMPLE", "TRY", "YOU"])[:6]
        while len(self.web_labels) < 6:
            self.web_labels.append("·")
        vis = self.script.get("visuals") or {}
        self.vis = {"gauge": vis.get("gauge") or ["FEELS LIKE", "ACTUALLY"],
                    "chain": vis.get("chain") or self.web_labels[1:4],
                    "cards": vis.get("cards") or [self.web_labels[0], self.web_labels[3]],
                    "steps": vis.get("steps") or ["NOTICE", "TEST", "UPDATE"]}
        self.visual_direction = self.script.get("visual_direction", "") or self.script.get("meta", {}).get("technology_angle", "")
        # beats / cuts
        self.lines = []
        for ch in self.tl["chunks"]:
            for ln in ch["lines"]:
                self.lines.append(ln)
        self.cuts = []
        for ln in self.lines:
            b = ln.get("scene") or ln.get("beat") or "explain"
            if not self.cuts or self.cuts[-1][1] != b:
                self.cuts.append((ln["start"], b))
        if self.cuts:
            self.cuts[0] = (0.0, self.cuts[0][1])
        self.cut_t = [t for t, _ in self.cuts]
        self.beat_start = {}
        for t, b in self.cuts:
            self.beat_start.setdefault(b, t)
        self.beat_order = ["hook", "problem", "explain", "example", "technique", "ending"]
        bw, bh = 1296, 2304
        self.bg_base = self._marble().resize((bw, bh), Image.LANCZOS)
        self.bg_alt = {}
        for kind, fn, k in (("brain", "hero_brain.png", 0.62), ("desk", "hero_desk.png", 0.6)):
            p = f"{ROOT}/assets/img/{fn}"
            if os.path.exists(p):
                im = Image.open(p).convert("RGB").resize((bw, bh), Image.LANCZOS)
                self.bg_alt[kind] = Image.fromarray((np.array(im, np.float32) * k).astype(np.uint8))
        self.emblem = Image.open(f"{ROOT}/assets/img/logo_emblem.png").convert("RGBA")
        self.eye = Image.open(f"{ROOT}/assets/img/logo_eye.png").convert("RGBA")
        self.wmark = self.emblem.resize((96, 96), Image.LANCZOS)
        self.handle_img = text_img(self.script["meta"].get("handle", "@metacognition.hq"), font("en", 600, 28), GOLD, spacing=3)
        self.flash = glow_disc(1600, (255, 200, 110, 255), 620, 260).resize((W, H))
        self.layout = {"en": [], "fa": [], "policy": self.L, "font_fallbacks": 0}
        self._build_lattice()
        self._build_web()
        self._build_captions()
        self._build_tags()
        self._widget_cache = {}

    def _marble(self):
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        base = np.zeros((H, W, 3), np.float32)
        base[..., 0], base[..., 1], base[..., 2] = 10, 8, 6
        r = np.sqrt((xx - W / 2) ** 2 + ((yy - 80) * 1.9) ** 2) / 900
        base += (np.clip(1 - r, 0, 1) ** 2.1)[..., None] * np.array([64, 46, 22], np.float32)
        n = np.sin(xx * 0.006 + np.sin(yy * 0.004) * 2.6) + np.sin(yy * 0.009 + np.sin(xx * 0.005) * 1.8)
        n = (n - n.min()) / (n.max() - n.min())
        base += (n ** 4)[..., None] * np.array([16, 13, 9], np.float32)
        rv = np.sqrt(((xx - W / 2) / (W * 0.62)) ** 2 + ((yy - H / 2) / (H * 0.6)) ** 2)
        base *= (1 - 0.55 * np.clip(rv - 0.35, 0, 1))[..., None]
        return Image.fromarray(base.astype(np.uint8), "RGB")

    def bg_crop(self, kind, t, t0, sd, punch=0.0):
        big = self.bg_alt.get(kind, self.bg_base)
        pr = clamp((t - t0) / max(sd, 0.1))
        scale = (1.06 + 0.10 * pr) * (1 + punch)
        dx, dy = lerp(-34, 34, pr), lerp(-20, 26, pr)
        cw, ch = W * 1.2 / scale, H * 1.2 / scale
        cx, cy = 648 + dx, 1152 + dy
        box = (int(cx - cw / 2), int(cy - ch / 2), int(cx + cw / 2), int(cy + ch / 2))
        return big.crop(box).resize((W, H), Image.BILINEAR)

    def _build_lattice(self):
        rnd = random.Random(7)
        z0, z1 = self.L["scene_zone"]
        self.lat = [(rnd.uniform(40, W - 40), rnd.uniform(z0 - 60, z1 + 60), rnd.uniform(0, 6.28), rnd.uniform(0.15, 0.5)) for _ in range(46)]

    def draw_lattice(self, fr, t, alpha=1.0):
        d = ImageDraw.Draw(fr)
        pts = [(x + 26 * math.sin(t * s + p), y + 18 * math.cos(t * s * 0.8 + p)) for x, y, p, s in self.lat]
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                dx, dy = pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]
                dist = math.hypot(dx, dy)
                if dist < 230:
                    a = int(46 * (1 - dist / 230) * alpha)
                    if a > 3:
                        d.line([pts[i], pts[j]], fill=(233, 180, 74, a), width=1)
        for x, y in pts:
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(200, 160, 80, int(90 * alpha)))

    def _build_web(self):
        cx, cy, rx, ry = 540, 790, 310, 150
        angles = [-90, -30, 30, 90, 150, 210]
        self.nodes = []
        for i, (lab, ang) in enumerate(zip(self.web_labels, angles)):
            a = math.radians(ang)
            x, y = cx + math.cos(a) * rx, cy + math.sin(a) * ry
            beat = self.beat_order[i]
            self.nodes.append({"x": x, "y": y, "beat": beat,
                               "lab": text_img(lab, font("en", 600, 22), DIM, spacing=2),
                               "lab_on": text_img(lab, font("en", 700, 22), GOLD_HI, spacing=2),
                               "below": ang in (30, 90, 150)})
        self.web_center = (cx, cy)
        self.web_edges = [(i, (i + 1) % 6) for i in range(6)] + [(0, 3), (1, 4), (2, 5)]

    def node_alpha(self, i, t):
        b = self.nodes[i]["beat"]
        t0 = self.beat_start.get(b)
        if t0 is None:
            return 0.0
        return ease((t - t0) / 0.6)

    def draw_web(self, fr, t, cur_beat):
        d = ImageDraw.Draw(fr)
        cx, cy = self.web_center
        for i, j in self.web_edges:
            a = min(self.node_alpha(i, t), self.node_alpha(j, t))
            if a <= 0:
                continue
            ni, nj = self.nodes[i], self.nodes[j]
            hot = cur_beat in (ni["beat"], nj["beat"])
            x0, y0, x1, y1 = ni["x"], ni["y"], nj["x"], nj["y"]
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            dx, dy = x1 - x0, y1 - y0
            L = math.hypot(dx, dy) or 1
            bx, by = mx - dy / L * L * 0.14, my + dx / L * L * 0.14
            pts = []
            for k in range(int(20 * a) + 1):
                u = k / 20
                pts.append(((1 - u) ** 2 * x0 + 2 * (1 - u) * u * bx + u ** 2 * x1,
                            (1 - u) ** 2 * y0 + 2 * (1 - u) * u * by + u ** 2 * y1))
            if len(pts) > 1:
                d.line(pts, fill=(233, 180, 74, 150 if hot else 70), width=2)
        a_c = max(self.node_alpha(1, t), 0.0)
        if a_c > 0:
            eye = self._cached("eye_small", lambda: self.eye.resize((110, 110), Image.LANCZOS))
            fr.alpha_composite(with_alpha(eye, a_c * 0.95), (int(cx - 55), int(cy - 55)))
        for i, nd in enumerate(self.nodes):
            a = self.node_alpha(i, t)
            if a <= 0:
                continue
            hot = nd["beat"] == cur_beat
            x, y = nd["x"], nd["y"]
            r = (10 if hot else 6) * a
            if hot:
                g = self._cached("nodeglow", lambda: glow_disc(110, (255, 200, 110, 150), 30, 16))
                fr.alpha_composite(g, (int(x) - 55, int(y) - 55))
            d.ellipse([x - r, y - r, x + r, y + r], fill=GOLD if hot else (130, 110, 74))
            lab = nd["lab_on"] if hot else nd["lab"]
            ly = y + 14 if nd["below"] else y - 14 - lab.height
            if cur_beat == "ending" and i == 3:
                continue
            fr.alpha_composite(with_alpha(lab, a * (1.0 if hot else 0.8)), (int(x - lab.width / 2), int(ly)))

    def _cached(self, key, fn):
        if key not in self._widget_cache:
            self._widget_cache[key] = fn()
        return self._widget_cache[key]

    # English karaoke captions — lower-middle, big font, high contrast, safe zone above caption/IG buttons
    def _build_captions(self):
        L = self.L
        self.cap_lines = []
        for ln in self.lines:
            words = ln["words"]
            chosen = None
            for size in (L["en_font_size"], 50, 46, 42, 38):
                f = font("en", 600, size)
                asc, desc = f.getmetrics()
                lh = asc + desc + 14
                rows, cur, cw = [], [], 0
                def word_img(wd, fill, f=f, asc=asc, lh=lh):
                    bb = f.getbbox(wd)
                    im = Image.new("RGBA", (bb[2] - bb[0] + 24, lh), (0, 0, 0, 0))
                    ImageDraw.Draw(im).text((12, 7 + asc), wd, font=f, fill=fill, anchor="ls")
                    return im
                for wd in words:
                    wi, gi, di = word_img(wd["w"], WARM), word_img(wd["w"], GOLD_HI), word_img(wd["w"], (200, 190, 170))
                    ww = wi.width + 12
                    if cw + ww > L["en_max_width"] and cur:
                        rows.append(cur)
                        cur, cw = [], 0
                    cur.append((wd, (wi, gi, di), cw))
                    cw += ww
                if cur:
                    rows.append(cur)
                if len(rows) <= L["en_max_rows"] or size == 38:
                    chosen = (rows, size, lh)
                    break
            rows, size, lh = chosen
            row_h = L["en_row_height"]
            width = max((r[-1][2] + r[-1][1][0].width) for r in rows)
            top = L["en_top"]
            bottom = top + len(rows) * row_h
            self.cap_lines.append({"rows": rows, "start": ln["start"], "end": ln["end"], "size": size, "lh": lh, "width": width, "top": top, "bottom": bottom})
            self.layout["en"].append({"text": ln["text"], "start": ln["start"], "end": ln["end"], "rows": len(rows), "font": size, "direction": "ltr", "bbox": [int((W - width) // 2), top, int((W + width) // 2), bottom]})

    def draw_captions(self, fr, t):
        i = bisect.bisect_right([l["start"] for l in self.cap_lines], t) - 1
        if i < 0:
            return
        ln = self.cap_lines[i]
        if t > ln["end"] + 0.35 and i + 1 < len(self.cap_lines):
            return
        a = ease((t - ln["start"]) / 0.16)
        sw = int(ln["width"]) + 70
        sh = int(len(ln["rows"]) * self.L["en_row_height"]) + 18
        scrim = self._cached(("scrim", sw, sh), lambda: rounded_card(sw, sh, 26, (8, 6, 4, 132)))
        fr.alpha_composite(with_alpha(scrim, a), ((W - sw) // 2, ln["top"] - 10))
        y = ln["top"]
        for row in ln["rows"]:
            rw = row[-1][2] + row[-1][1][0].width
            x = (W - rw) // 2
            for wd, (wi, gi, di), off in row:
                wx = x + off
                if t >= wd["end"]:
                    im, al = wi, 0.92 * a
                elif t >= wd["start"]:
                    im, al = gi, a
                    g = self._cached("wordglow", lambda: glow_disc(140, (255, 200, 110, 110), 44, 26))
                    fr.alpha_composite(g, (int(wx + wi.width / 2) - 70, int(y + wi.height / 2) - 70))
                else:
                    im, al = di, 0.55 * a
                fr.alpha_composite(with_alpha(im, al), (int(wx), int(y)))
            y += self.L["en_row_height"]

    def _build_tags(self):
        self.tag_imgs = {k: gold_text(v, font("en", 700, 26), spacing=5) for k, v in self.tags.items()}

    def draw_chrome(self, fr, t, cur_beat, t0):
        d = ImageDraw.Draw(fr)
        p = clamp(t / self.total)
        d.rectangle([0, 0, int(W * p), 7], fill=GOLD)
        fr.alpha_composite(with_alpha(self.wmark, 0.9), (W - 124, 48))
        tag = self.tag_imgs.get(cur_beat)
        if tag is not None:
            a = ease((t - t0) / 0.3)
            fr.alpha_composite(with_alpha(tag, a), (64 - int((1 - a) * 30), self.L["en_top"] - 66))
            d.line([64, self.L["en_top"] - 22, 64 + int(tag.width * a), self.L["en_top"] - 22], fill=(233, 180, 74, 160), width=2)
        hi = self.handle_img
        fr.alpha_composite(hi, ((W - hi.width) // 2, self.L["fa_bottom"] + 12 if "fa_bottom" in self.L else H - 120))

    def line_times(self, beat):
        return [(ln["start"], ln["end"]) for ln in self.lines if (ln.get("scene") or ln.get("beat")) == beat]

    WZ = (500, 1300)

    def widget(self, fr, t, beat, ts, sd):
        fn = {"hook": self.w_hook, "problem": self.w_problem, "explain": self.w_explain, "example": self.w_example, "technique": self.w_technique, "ending": self.w_ending}.get(beat)
        if fn:
            fn(fr, t, ts, sd)

    def w_hook(self, fr, t, ts, sd):
        d = ImageDraw.Draw(fr)
        g = self._cached("hookglow", lambda: glow_disc(1000, (255, 186, 90, 190), 380, 140))
        pulse = 0.72 + 0.28 * math.sin(ts * 2.2)
        fr.alpha_composite(with_alpha(g, pulse), (540 - 500, 930 - 500))
        s = ease_back(ts / 0.7)
        size = int(640 * s)
        if size > 4:
            em = self.emblem.resize((size, size), Image.LANCZOS)
            fr.alpha_composite(em, (540 - size // 2, 930 - size // 2))
        for i in range(22):
            a = ts * (0.5 + 0.11 * (i % 5)) + i * 2.399
            rx, ry = 360 + 26 * math.sin(i), 330 + 22 * math.cos(i * 2)
            x, y = 540 + math.cos(a) * rx, 930 + math.sin(a) * ry
            dep = 0.55 + 0.45 * math.sin(a)
            r = 2 + 3 * dep
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 214, 130, int(190 * dep)))
        for k in range(2):
            pt = (ts * 0.55 + k * 0.5) % 1.0
            rr = 300 + pt * 260
            d.ellipse([540 - rr, 930 - rr * 0.94, 540 + rr, 930 + rr * 0.94], outline=(233, 180, 74, int(120 * (1 - pt))), width=3)

    def _gauge(self, d, cx, cy, r, val, hot):
        a0, sweep = 135, 270
        d.arc([cx - r, cy - r, cx + r, cy + r], a0, a0 + sweep, fill=(70, 60, 45), width=12)
        if val > 0.01:
            d.arc([cx - r, cy - r, cx + r, cy + r], a0, a0 + sweep * clamp(val), fill=GOLD if hot else (150, 132, 100), width=12)
        a = math.radians(a0 + sweep * clamp(val))
        d.line([cx, cy, cx + math.cos(a) * (r - 30), cy + math.sin(a) * (r - 30)], fill=GOLD_HI if hot else (200, 180, 140), width=5)
        d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=GOLD_HI if hot else (200, 180, 140))

    def w_problem(self, fr, t, ts, sd):
        # Confidence meter + code visual for tech
        d = ImageDraw.Draw(fr)
        vd = self.visual_direction.lower()
        if "code" in vd or "coding" in vd or "debug" in vd:
            self._draw_code_visual(fr, t, ts, 200, 950, 680, 320)
        else:
            lab_a, lab_b = self.vis["gauge"][:2]
            feel = ease_out(ts / 1.4) * 0.9 + 0.03 * math.sin(ts * 3)
            real = ease_out((ts - 1.0) / 1.8) * 0.36
            for cx, val, lab, hot in ((330, feel, lab_a, True), (750, real, lab_b, False)):
                self._gauge(d, cx, 1110, 96, val, hot)
                li = self._cached(("glab", lab, hot), lambda lab=lab, hot=hot: text_img(lab, font("en", 800, 28), GOLD_HI if hot else DIM, spacing=2))
                fr.alpha_composite(li, (cx - li.width // 2, 1216))
            if ts > 2.2:
                a = ease((ts - 2.2) / 0.5)
                gap = self._cached("gaplab", lambda: gold_text("THE GAP", font("en", 800, 34), spacing=4))
                fr.alpha_composite(with_alpha(gap, a), (540 - gap.width // 2, 1080))
                d.line([430, 1110, 650, 1110], fill=(233, 180, 74, int(150 * a)), width=2)

    def w_explain(self, fr, t, ts, sd):
        # Evidence card / decision tree / human-AI network depending on tech angle
        vd = self.visual_direction.lower()
        if "decision" in vd or "tree" in vd or "product" in vd:
            self._draw_decision_tree(fr, t, ts)
        elif "evidence" in vd or "research" in vd:
            self._draw_evidence_card(fr, t, ts)
        elif "human" in vd and "ai" in vd:
            self._draw_human_ai_network(fr, t, ts)
        else:
            # Chain mechanism
            d = ImageDraw.Draw(fr)
            labs = self.vis["chain"][:3]
            times = self.line_times("explain")
            starts = [s for s, _ in times][:3]
            while len(starts) < 3:
                starts.append(starts[-1] + 2.0 if starts else self.beat_start.get("explain", 0))
            xs = [200, 540, 880]
            for i, (lab, x) in enumerate(zip(labs, xs)):
                a = ease((t - starts[i]) / 0.5)
                if a <= 0:
                    continue
                chip = self._cached(("chain", lab), lambda lab=lab: self._chip(lab, 250, 92))
                if i > 0:
                    pa = ease((t - starts[i]) / 0.7)
                    x0, x1 = xs[i - 1] + 125, x - 125
                    d.line([x0, 1120, x0 + (x1 - x0) * pa, 1120], fill=(233, 180, 74, 200), width=4)
                    if pa > 0.9:
                        d.polygon([(x1, 1120), (x1 - 18, 1110), (x1 - 18, 1130)], fill=GOLD)
                sc = 0.9 + 0.1 * a
                ci = chip.resize((int(chip.width * sc), int(chip.height * sc)), Image.BILINEAR)
                fr.alpha_composite(with_alpha(ci, a), (x - ci.width // 2, 1120 - ci.height // 2))
            if ts > 1.0:
                ph = (ts * 0.35) % 1.0
                px = 325 + ph * 430
                d.ellipse([px - 7, 1113, px + 7, 1127], fill=(255, 228, 158, 220))

    def w_example(self, fr, t, ts, sd):
        vd = self.visual_direction.lower()
        if "prediction" in vd or "graph" in vd:
            self._draw_prediction_graph(fr, t, ts)
        elif "code" in vd:
            self._draw_code_visual(fr, t, ts, 180, 900, 720, 380)
        else:
            d = ImageDraw.Draw(fr)
            la, lb = self.vis["cards"][:2]
            p1, p2 = ease_out(ts / 0.6), ease_out((ts - 0.5) / 0.6)
            ca = self._cached(("card", la), lambda: self._card(la, (34, 28, 20, 235), DIM))
            cb = self._cached(("card", lb), lambda: self._card(lb, (40, 31, 16, 240), GOLD_HI))
            ya = 1000 + (1 - p1) * 80
            yb = 1000 + (1 - p2) * 80
            fr.alpha_composite(with_alpha(ca, p1), (int(90), int(ya)))
            fr.alpha_composite(with_alpha(cb, p2), (int(590), int(yb)))
            if p2 > 0.95:
                k = 0.5 + 0.5 * math.sin(ts * 3)
                d.rounded_rectangle([586, 996, 986, 1236], 24, outline=(255, 228, 158, int(60 + 120 * k)), width=3)
                vs = self._cached("vs", lambda: gold_text("VS", font("en", 800, 34), spacing=3))
                fr.alpha_composite(vs, (540 - vs.width // 2, 1100))

    def w_technique(self, fr, t, ts, sd):
        # Steps + confidence meter / code
        d = ImageDraw.Draw(fr)
        labs = self.vis["steps"][:3]
        times = self.line_times("technique")
        starts = [s for s, _ in times][:3]
        while len(starts) < 3:
            starts.append((starts[-1] + 2.5) if starts else self.beat_start.get("technique", 0))
        xs = [190, 540, 890]
        d.line([xs[0], 1112, xs[-1], 1112], fill=(90, 76, 52, 200), width=6)
        for i, (lab, x) in enumerate(zip(labs, xs)):
            lit = t >= starts[i]
            a = ease((t - starts[i]) / 0.45) if lit else 0.0
            if i > 0 and lit:
                d.line([xs[i - 1], 1112, xs[i - 1] + (x - xs[i - 1]) * ease((t - starts[i]) / 0.6), 1112], fill=(233, 180, 74, 220), width=6)
            r = 30 + 6 * a
            d.ellipse([x - r, 1112 - r, x + r, 1112 + r], fill=GOLD if lit else (60, 50, 36), outline=(255, 228, 158, 200) if lit else (120, 104, 72, 200), width=3)
            num = self._cached(("stepn", i, lit), lambda i=i, lit=lit: text_img(str(i + 1), font("en", 800, 30), INK if lit else DIM))
            fr.alpha_composite(num, (x - num.width // 2, 1112 - num.height // 2))
            li = self._cached(("stepl", lab, lit), lambda lab=lab, lit=lit: text_img(lab, font("en", 800, 28), GOLD_HI if lit else DIM, spacing=2))
            fr.alpha_composite(with_alpha(li, 0.9 if lit else 0.6), (x - li.width // 2, 1160))
            if lit and a > 0.9:
                d.line([x - 60, 1215, x - 45, 1230, x - 18, 1200], fill=(255, 228, 158, 230), width=5, joint="curve")

    def w_ending(self, fr, t, ts, sd):
        d = ImageDraw.Draw(fr)
        cx, cy, rx, ry = 540, 1120, 330, 105
        for i in range(90):
            a0, a1 = 2 * math.pi * i / 90, 2 * math.pi * (i + 1) / 90
            dep = (math.sin(a0) + math.sin(a1)) / 2
            d.line([cx + math.cos(a0) * rx, cy + math.sin(a0) * ry, cx + math.cos(a1) * rx, cy + math.sin(a1) * ry], fill=(233, 180, 74, int(70 + 130 * (dep * 0.5 + 0.5))), width=int(4 + 6 * (dep * 0.5 + 0.5)))
        stations = [("PLAN", -math.pi / 2, 0.0), ("MONITOR", math.pi / 6, 0.8), ("EVALUATE", math.pi * 5 / 6, 1.6)]
        for name, a, t0 in stations:
            lit = ts >= t0
            x, y = cx + math.cos(a) * rx, cy + math.sin(a) * ry
            r = 14 + 8 * (math.sin(a) * 0.5 + 0.5)
            if lit:
                fr.alpha_composite(self._cached("stglow", lambda: glow_disc(150, (255, 200, 110, 170), 48, 26)), (int(x) - 75, int(y) - 75))
            d.ellipse([x - r, y - r, x + r, y + r], fill=GOLD if lit else (86, 72, 50))
            lab = self._cached(("st", name, lit), lambda name=name, lit=lit: text_img(name, font("en", 800, 30), GOLD_HI if lit else DIM, spacing=3))
            ly = y - 70 if math.sin(a) < 0 else y + 30
            fr.alpha_composite(lab, (int(x - lab.width // 2), int(ly)))
        ang = -math.pi / 2 + ts * 1.05
        for k in range(20):
            aa = ang - k * 0.055
            x, y = cx + math.cos(aa) * rx, cy + math.sin(aa) * ry
            r = 8 * (1 - k / 24)
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 226, 150, int(230 * (1 - k / 20))))
        eye = self._cached("eye_mid", lambda: self.eye.resize((150, 150), Image.LANCZOS))
        fr.alpha_composite(eye, (540 - 75, cy - 75))

    # New tech visuals
    def _draw_code_visual(self, fr, t, ts, x, y, w, h):
        card = rounded_card(w, h, 18, (18, 20, 24, 230), (233, 180, 74, 90), 2)
        d = ImageDraw.Draw(card)
        # Fake code lines
        code_lines = ["def solve():", "    # explain before accept", "    result = ai.complete()", "    assert test(result)", "    return result"]
        for i, ln in enumerate(code_lines[:5]):
            col = CODE_GREEN if "assert" in ln or "def" in ln else CODE_BLUE if "ai" in ln else WARM
            f = font("en", 500, 26)
            d.text((20, 20 + i * 34), ln, font=f, fill=col)
        # Glow cursor
        cursor_x = 20 + int((ts * 80) % (w - 40))
        d.rectangle([cursor_x, 20 + 2 * 34, cursor_x + 2, 20 + 3 * 34], fill=GOLD)
        fr.alpha_composite(card, (x, y))

    def _draw_evidence_card(self, fr, t, ts):
        card = rounded_card(760, 280, 22, (26, 20, 13, 235), (233, 180, 74, 180), 2)
        d = ImageDraw.Draw(card)
        title = text_img("EVIDENCE", font("en", 800, 28), GOLD_HI, spacing=2)
        card.alpha_composite(title, (30, 20))
        lines = ["Source: Bansal et al. (2021)", "Tier: B — peer-reviewed", "Claim: calibration matters", "No fake stats"]
        for i, ln in enumerate(lines):
            d.text((30, 70 + i * 36), ln, font=font("en", 500, 24), fill=WARM)
        fr.alpha_composite(card, (160, 980))

    def _draw_decision_tree(self, fr, t, ts):
        d = ImageDraw.Draw(fr)
        # Root
        nodes = [(540, 960, "DECIDE"), (320, 1100, "OPTION A"), (760, 1100, "OPTION B"), (200, 1240, "COST?"), (440, 1240, "GAIN?"), (640, 1240, "RISK?"), (880, 1240, "NOW?")]
        edges = [(0,1),(0,2),(1,3),(1,4),(2,5),(2,6)]
        for i,j in edges:
            x0,y0,_ = nodes[i]
            x1,y1,_ = nodes[j]
            prog = clamp((ts - i*0.3)/0.6)
            if prog > 0:
                x2 = x0 + (x1 - x0) * prog
                y2 = y0 + (y1 - y0) * prog
                d.line([x0,y0,x2,y2], fill=(233,180,74,200), width=3)
        for i,(x,y,label) in enumerate(nodes):
            prog = clamp((ts - i*0.2)/0.5)
            if prog <=0:
                continue
            chip = self._cached(("dt", label), lambda label=label: self._chip(label, 160, 60))
            fr.alpha_composite(with_alpha(chip, prog), (int(x - 80), int(y - 30)))

    def _draw_human_ai_network(self, fr, t, ts):
        d = ImageDraw.Draw(fr)
        # Two clusters
        human = (300, 1080)
        ai = (780, 1080)
        # Nodes
        for cx,cy in [human, ai]:
            g = glow_disc(200, (255,200,110,90), 60, 20)
            fr.alpha_composite(g, (int(cx-100), int(cy-100)))
            d.ellipse([cx-30, cy-30, cx+30, cy+30], fill=GOLD if cx==300 else CODE_BLUE)
            label = "HUMAN" if cx==300 else "AI"
            li = text_img(label, font("en", 800, 26), GOLD_HI, spacing=2)
            fr.alpha_composite(li, (cx - li.width//2, cy+40))
        # Connection with pulse
        prog = (math.sin(ts*1.5)+1)/2
        mx = human[0] + (ai[0]-human[0]) * prog
        d.line([human[0], human[1], ai[0], ai[1]], fill=(100,100,80,120), width=2)
        d.ellipse([mx-8, human[1]-8, mx+8, human[1]+8], fill=GOLD_HI)

    def _draw_prediction_graph(self, fr, t, ts):
        card = rounded_card(760, 320, 22, (18,20,24,230), (233,180,74,90), 2)
        d = ImageDraw.Draw(card)
        d.text((30,20), "PREDICTION vs RESULT", font=font("en", 700, 26), fill=GOLD_HI)
        # Axes
        d.line([40, 280, 720, 280], fill=(100,100,100,200), width=2)
        d.line([40, 60, 40, 280], fill=(100,100,100,200), width=2)
        # Two lines: predicted (dashed) and actual (solid)
        pred = [(40 + i*60, 260 - i*10 - 20*math.sin(i)) for i in range(10)]
        actual = [(40 + i*60, 250 - i*8 - 10*math.cos(i*0.5)) for i in range(10)]
        prog = int(clamp(ts/2.0) * len(pred))
        if prog >1:
            d.line(pred[:prog], fill=(150,150,150,180), width=2)
            d.line(actual[:prog], fill=GOLD, width=3)
        fr.alpha_composite(card, (160, 940))

    def _chip(self, title, w, h):
        t1 = gold_text(title, font("en", 800, 30), spacing=2)
        w = max(w, t1.width + 50)
        im = rounded_card(w, h, 22, (26, 20, 13, 235), (233, 180, 74, 200), 3)
        im.alpha_composite(t1, ((w - t1.width) // 2, (h - t1.height) // 2))
        return im

    def _card(self, title, fill, col):
        w, h = 400, 240
        im = rounded_card(w, h, 24, fill, (233, 180, 74, 140), 2)
        d = ImageDraw.Draw(im)
        import random
        rnd = random.Random(len(title))
        y = 150
        while y < h - 30:
            lw = rnd.randint(int(w * 0.35), int(w * 0.7))
            d.line([40, y, 40 + lw, y], fill=(150, 130, 95, 110), width=5)
            y += 24
        words = title.split()
        lines = [" ".join(words[:2]), " ".join(words[2:])] if len(words) > 2 else [title]
        yy = 34
        for ln in lines:
            if not ln:
                continue
            ti = text_img(ln, font("en", 800, 34), col, spacing=1)
            im.alpha_composite(ti, ((w - ti.width) // 2, yy))
            yy += ti.height - 6
        return im

    def beat_at(self, t):
        i = bisect.bisect_right(self.cut_t, t) - 1
        return max(0, i)

    def frame(self, t):
        si = self.beat_at(t)
        beat, t0 = self.cuts[si][1], self.cut_t[si]
        sd = (self.cut_t[si + 1] if si + 1 < len(self.cut_t) else self.total) - t0
        ts = t - t0
        punch = flash = 0.0
        if not self.safe:
            for ct in self.cut_t[1:]:
                dt = abs(t - ct)
                if dt < 0.30:
                    k = 1 - dt / 0.30
                    punch, flash = max(punch, 0.02 * k), max(flash, k)
        kind = {"explain": "brain", "example": "desk"}.get(beat, "base")
        fr = self.bg_crop(kind, t, t0, sd, punch).convert("RGBA")
        if si > 0 and not self.safe:
            pk = {"explain": "brain", "example": "desk"}.get(self.cuts[si - 1][1], "base")
            if pk != kind and ts < 0.35:
                fr = Image.blend(self.bg_crop(pk, t, t0, sd, punch).convert("RGBA"), fr, ease(ts / 0.35))
        self.draw_lattice(fr, t, 0.35 if beat == "hook" else 1.0)
        if beat != "hook":
            self.draw_web(fr, t, beat)
        self.widget(fr, t, beat, ts, sd)
        self.draw_captions(fr, t)
        self.draw_chrome(fr, t, beat, t0)
        if flash > 0:
            fr.alpha_composite(with_alpha(self.flash, 0.3 * flash))
        return fr.convert("RGB")

    def background_only(self, t):
        si = self.beat_at(t)
        beat, t0 = self.cuts[si][1], self.cut_t[si]
        sd = (self.cut_t[si + 1] if si + 1 < len(self.cut_t) else self.total) - t0
        kind = {"explain": "brain", "example": "desk"}.get(beat, "base")
        fr = self.bg_crop(kind, t, t0, sd).convert("RGBA")
        self.draw_lattice(fr, t)
        if beat != "hook":
            self.draw_web(fr, t, beat)
        self.widget(fr, t, beat, t - t0, sd)
        return fr.convert("RGB")
