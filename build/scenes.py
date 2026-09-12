"""Scene foregrounds for the reel. Each fn(ctx) draws on ctx['fr'] (RGBA)."""
import math
from PIL import Image, ImageDraw, ImageFilter
from engine import (ROOT, W, H, GOLD, GOLD_HI, GOLD_LO, WARM, DIM, BG,
                    font, clamp, ease, ease_out, ease_back, lerp,
                    text_img, gold_text, glow_disc, rounded_card, res)

_cache = {}


def cached(key, fn):
    if key not in _cache:
        _cache[key] = fn()
    return _cache[key]


# ------------------------------------------------------------------ pieces
def draw_gauge(d, cx, cy, r, val, label, sub, hot=True):
    a0, sweep = 135, 270
    col = GOLD if hot else (120, 108, 88)
    d.arc([cx - r, cy - r, cx + r, cy + r], a0, a0 + sweep, fill=(70, 60, 45), width=13)
    if val > 0.01:
        d.arc([cx - r, cy - r, cx + r, cy + r], a0, a0 + sweep * clamp(val), fill=col, width=13)
    for i in range(11):
        a = math.radians(a0 + sweep * i / 10)
        d.line([cx + math.cos(a) * (r - 16), cy + math.sin(a) * (r - 16),
                cx + math.cos(a) * (r - 26), cy + math.sin(a) * (r - 26)],
               fill=(120, 105, 80), width=3)
    a = math.radians(a0 + sweep * clamp(val))
    d.line([cx, cy, cx + math.cos(a) * (r - 34), cy + math.sin(a) * (r - 34)], fill=GOLD_HI, width=6)
    d.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], fill=GOLD_HI)
    t = text_img(label, font("en", 800, 44), GOLD_HI if hot else DIM)
    d2 = None
    return t, sub


def chip(w, h, title, sub, fill=(26, 20, 13, 235), outline=GOLD):
    _t1 = gold_text(title, font("en", 800, 46))
    _t2 = text_img(sub, font("en", 500, 27), DIM, spacing=1) if sub else None
    w = max(w, _t1.width + 70, (_t2.width + 60) if _t2 else 0)
    im = rounded_card(w, h, 26, fill, outline + (200,), 3)
    d = ImageDraw.Draw(im)
    t1 = gold_text(title, font("en", 800, 46))
    im.paste(t1, ((w - t1.width) // 2, 26), t1)
    if sub:
        t2 = text_img(sub, font("en", 500, 27), DIM, spacing=1)
        im.paste(t2, ((w - t2.width) // 2, 26 + t1.height + 8), t2)
    return im


# ------------------------------------------------------------------ scenes
def sc_hook(c):
    R, fr, ts = c["R"], c["fr"], c["ts"]
    d = ImageDraw.Draw(fr)
    g = cached("hookglow", lambda: glow_disc(1100, (255, 186, 90, 200), 420, 150))
    pulse = 0.75 + 0.25 * math.sin(ts * 2.2)
    ga = g.copy()
    ga.putalpha(ga.getchannel("A").point(lambda v: int(v * pulse)))
    fr.alpha_composite(ga, (540 - 550, 950 - 550))
    s = ease_back(ts / 0.7)
    size = int(840 * s)
    if size > 4:
        em = R.emblem.resize((size, size), Image.LANCZOS)
        fr.alpha_composite(em, (540 - size // 2, 950 - size // 2))
    # orbiting sparks
    for i in range(26):
        a = ts * (0.5 + 0.11 * (i % 5)) + i * 2.399
        rx, ry = 470 + 30 * math.sin(i), 430 + 26 * math.cos(i * 2)
        x, y = 540 + math.cos(a) * rx, 950 + math.sin(a) * ry * 0.92
        dep = 0.55 + 0.45 * math.sin(a)
        r = 2 + 3 * dep
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 214, 130, int(190 * dep)))
    # ring pulses
    for k in range(2):
        pt = (ts * 0.55 + k * 0.5) % 1.0
        rr = 380 + pt * 330
        al = int(120 * (1 - pt))
        d.ellipse([540 - rr, 950 - rr * 0.94, 540 + rr, 950 + rr * 0.94],
                  outline=(233, 180, 74, al), width=3)


def _paper_card():
    w, h = 780, 1000
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], 18, fill=(235, 224, 200, 255))
    d.rounded_rectangle([26, 26, w - 27, h - 27], 10, outline=(60, 48, 30, 200), width=3)
    d.rounded_rectangle([40, 40, w - 41, h - 41], 6, outline=(150, 120, 60, 160), width=2)
    t = text_img("AMERICAN PSYCHOLOGIST · 1979", font("en", 700, 26), (90, 70, 40), spacing=3)
    im.paste(t, ((w - t.width) // 2, 92), t)
    d.line([90, 150, w - 90, 150], fill=(90, 70, 40, 180), width=2)
    title = ["Metacognition and", "Cognitive Monitoring"]
    y = 210
    for ln in title:
        t = text_img(ln, font("en", 800, 62), (28, 22, 14))
        im.paste(t, ((w - t.width) // 2, y), t)
        y += 84
    t = text_img("A New Area of Cognitive-Developmental Inquiry", font("en", 500, 27), (90, 70, 40))
    im.paste(t, ((w - t.width) // 2, y + 16), t)
    d.line([140, y + 90, w - 140, y + 90], fill=(90, 70, 40, 140), width=2)
    t = text_img("JOHN H. FLAVELL", font("en", 700, 40), (28, 22, 14), spacing=4)
    im.paste(t, ((w - t.width) // 2, y + 130), t)
    t = text_img("Stanford University", font("en", 500, 26), (90, 70, 40))
    im.paste(t, ((w - t.width) // 2, y + 190), t)
    # fake abstract lines
    import random
    rnd = random.Random(3)
    yy = y + 260
    while yy < h - 190:
        lw = rnd.randint(int(w * 0.5), int(w * 0.72))
        d.line([(w - lw) // 2, yy, (w + lw) // 2, yy], fill=(70, 56, 36, 90), width=5)
        yy += 26
    # gold seal
    d.ellipse([w - 210, h - 210, w - 70, h - 70], fill=(196, 148, 62, 255))
    d.ellipse([w - 196, h - 196, w - 84, h - 84], outline=(90, 66, 26, 255), width=4)
    t = text_img("THE PAPER", font("en", 800, 26), (40, 28, 10), spacing=2)
    im.paste(t, (w - 140 - t.width // 2, h - 158), t)
    t = text_img("VOL 34 · NO 10", font("en", 600, 18), (40, 28, 10), spacing=1)
    im.paste(t, (w - 140 - t.width // 2, h - 122), t)
    return im


def sc_article(c):
    R, fr, ts, sd = c["R"], c["fr"], c["ts"], c["sd"]
    d = ImageDraw.Draw(fr)
    big = cached("y1979", lambda: gold_text("1979", font("en", 800, 300), spacing=8))
    ba = big.copy()
    ba.putalpha(ba.getchannel("A").point(lambda v: int(v * 0.16)))
    fr.alpha_composite(ba, (W // 2 - big.width // 2, 430))
    card = cached("paper", _paper_card)
    p = ease_out(ts / 0.6)
    y = lerp(1500, 640, p) + 14 * math.sin(ts * 1.3)
    rot = lerp(-13, -5, p) + 1.2 * math.sin(ts * 0.9)
    cc = card.rotate(rot, expand=True, resample=Image.BICUBIC)
    sh = Image.new("RGBA", cc.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([10, 26, cc.width - 10, cc.height - 6], 20, fill=(0, 0, 0, 150))
    sh = sh.filter(ImageFilter.GaussianBlur(18))
    fr.alpha_composite(sh, (W // 2 - cc.width // 2 + 8, int(y) + 6))
    fr.alpha_composite(cc, (W // 2 - cc.width // 2, int(y)))
    if ts > 1.5:
        st = cached("stamp", lambda: _stamp())
        k = ease_back((ts - 1.5) / 0.4)
        sw = int(st.width * k)
        if sw > 4:
            ss = st.resize((sw, int(st.height * k)), Image.BILINEAR)
            fr.alpha_composite(ss, (120, 1420))
    # shimmer sweep
    ph = (ts % 2.6) / 2.6
    sx = int(-300 + ph * (W + 600))
    shim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shim)
    ds.polygon([(sx, 0), (sx + 150, 0), (sx - 120, H), (sx - 270, H)], fill=(255, 226, 150, 26))
    fr.alpha_composite(shim)


def _stamp():
    w, h = 620, 150
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([4, 4, w - 5, h - 5], 18, outline=(233, 180, 74, 235), width=5)
    d.rounded_rectangle([16, 16, w - 17, h - 17], 12, outline=(233, 180, 74, 140), width=2)
    t = gold_text("THE PAPER THAT STARTED IT", font("en", 800, 40), spacing=2)
    im.paste(t, ((w - t.width) // 2, (h - t.height) // 2 - 4), t)
    return im.rotate(-8, expand=True, resample=Image.BICUBIC)


def sc_word(c):
    R, fr, ts, sd = c["R"], c["fr"], c["ts"], c["sd"]
    d = ImageDraw.Draw(fr)
    phase_b = 8.6
    if ts > phase_b - 0.8:
        a = clamp((ts - (phase_b - 0.8)) / 0.9)
        br = R.brain_full.copy()
        br.putalpha(br.getchannel("A").point(lambda v: int(v * a * 0.9)))
        fr.alpha_composite(br)
    if ts < phase_b + 0.4:
        m = cached("chipmeta", lambda: chip(430, 170, "META", "above · beyond"))
        g = cached("chipcog", lambda: chip(560, 170, "COGNITION", "thinking"))
        p1 = ease_out(ts / 0.55)
        p2 = ease_out((ts - 0.35) / 0.55)
        merge = ease(clamp((ts - 3.4) / 0.7))
        xm = lerp(-460, 90, p1) + merge * 130
        xg = lerp(W + 60, 450, p2) - merge * 130
        y = 640 + 10 * math.sin(ts * 1.4)
        fade = 1 - ease(clamp((ts - 4.0) / 0.5))
        if fade > 0:
            for img, x in ((m, xm), (g, xg)):
                ii = img.copy()
                ii.putalpha(ii.getchannel("A").point(lambda v: int(v * fade)))
                fr.alpha_composite(ii, (int(x), int(y)))
        if ts > 3.6:
            flash = 1 - clamp((ts - 3.6) / 0.5)
            if flash > 0:
                fr.alpha_composite(glow_disc(900, (255, 220, 140, int(200 * flash)), 300, 90),
                                   (540 - 450, 720 - 450))
        if ts > 3.9:
            word = cached("metaword", lambda: gold_text("METACOGNITION", font("en", 800, 72), spacing=2))
            n = len("METACOGNITION")
            for i in range(n):
                a = ease((ts - 3.9 - i * 0.055) / 0.3)
                if a <= 0:
                    continue
                ch_img = cached(f"mw{i}", lambda i=i: gold_text("METACOGNITION"[i], font("en", 800, 72)))
                xoff = sum(cached(f"mw{j}", lambda j=j: gold_text("METACOGNITION"[j], font("en", 800, 72))).width + 2
                           for j in range(i))
                ii = ch_img.copy()
                ii.putalpha(ii.getchannel("A").point(lambda v: int(v * a)))
                fr.alpha_composite(ii, (int(540 - word.width / 2 + xoff), int(660 - (1 - a) * 30)))
            t = text_img("thinking about thinking", font("en", 500, 40), WARM, spacing=2)
            a = ease((ts - 5.0) / 0.5)
            if a > 0:
                t2 = t.copy()
                t2.putalpha(t2.getchannel("A").point(lambda v: int(v * a)))
                fr.alpha_composite(t2, (540 - t.width // 2, 800))
    else:
        tb = ts - phase_b
        eye = R.eye.resize((430, 430), Image.LANCZOS)
        y = 560 + 16 * math.sin(tb * 1.6)
        fr.alpha_composite(glow_disc(700, (255, 190, 95, 150), 240, 80), (540 - 350, int(y) + 215 - 350))
        fr.alpha_composite(eye, (540 - 215, int(y)))
        d.line([540, int(y) + 400, 540, 1180], fill=(233, 180, 74, 140), width=4)
        for i in range(6):
            yy = int(y) + 400 + (1180 - y - 400) * i / 6
            rr = 6 + 3 * math.sin(tb * 3 + i)
            d.ellipse([540 - rr, yy - rr, 540 + rr, yy + rr], fill=(255, 220, 140, 200))
        lab = cached("watchlab", lambda: chip(420, 96, "THE WATCHER", None))
        a = ease(tb / 0.6)
        li = lab.copy()
        li.putalpha(li.getchannel("A").point(lambda v: int(v * a)))
        fr.alpha_composite(li, (540 - 210, 1200))


def sc_halves(c):
    R, fr, ts, sd = c["R"], c["fr"], c["ts"], c["sd"]
    d = ImageDraw.Draw(fr)
    head = cached("hhead", lambda: chip(560, 110, "METACOGNITION", None))
    fr.alpha_composite(head, (540 - 280, 470))
    cards = [("know", "KNOWLEDGE", "knowing how your mind works", 90, 0.25),
             ("control", "CONTROL", "steering it in real time", 590, 0.55)]
    for key, title, sub, x, t0 in cards:
        im = cached("hc" + key, lambda title=title, sub=sub: chip(400, 210, title, sub))
        p = ease_back((ts - t0) / 0.5)
        if p <= 0:
            continue
        y = 700 + (1 - ease_out((ts - t0) / 0.5)) * -60
        ii = im.resize((int(im.width * p), int(im.height * p)), Image.BILINEAR)
        d.line([540, 580, x + 200, int(y) + 40], fill=(233, 180, 74, 150), width=3)
        fr.alpha_composite(ii, (x + (400 - ii.width) // 2, int(y + (210 - ii.height) // 2)))
    toks = ["plan", "monitor", "evaluate"]
    for i, tk in enumerate(toks):
        tt = R.find_token(tk, chunk_id="c5")
        if not tt:
            continue
        t0 = tt[0] - c["t0"]
        if ts < t0:
            continue
        p = ease_back((ts - t0) / 0.4)
        im = cached("pchip" + tk, lambda tk=tk: _pchip(tk.upper()))
        x = 130 + i * 300
        ii = im.resize((int(im.width * p), int(im.height * p)), Image.BILINEAR)
        fr.alpha_composite(ii, (x + (260 - ii.width) // 2, 1180 + (150 - ii.height) // 2))
    tt = R.find_token("skill", chunk_id="c5")
    if tt and ts > tt[0] - c["t0"]:
        lab = cached("whole", lambda: gold_text("THAT'S THE WHOLE SKILL", font("en", 800, 52), spacing=3))
        a = 0.85 + 0.15 * math.sin(ts * 5)
        li = lab.copy()
        li.putalpha(li.getchannel("A").point(lambda v: int(v * a)))
        fr.alpha_composite(li, (540 - lab.width // 2, 1380))


def _pchip(title):
    w, h = 260, 150
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], 30, fill=(233, 180, 74, 245))
    d.rounded_rectangle([6, 6, w - 7, h - 7], 26, outline=(255, 236, 180, 200), width=2)
    t = text_img(title, font("en", 800, 40), (24, 17, 8), spacing=2)
    im.paste(t, ((w - t.width) // 2, (h - t.height) // 2 - 2), t)
    return im


def _notes_card():
    w, h = 560, 720
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], 20, fill=(24, 19, 13, 240), outline=(233, 180, 74, 160), width=3)
    t = gold_text("MY NOTES · BIO 101", font("en", 700, 34), spacing=2)
    im.paste(t, (40, 36), t)
    d.line([40, 100, w - 40, 100], fill=(233, 180, 74, 120), width=2)
    import random
    rnd = random.Random(11)
    y = 150
    while y < h - 60:
        lw = rnd.randint(int(w * 0.55), int(w * 0.8))
        pts = [(xx, y + 3 * math.sin(xx * 0.09) + 1.5 * math.sin(xx * 0.23)) for xx in range(40, 40 + lw, 6)]
        d.line(pts, fill=(214, 190, 140, 150), width=4, joint="curve")
        y += 44
    hl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle([36, 236, w - 120, 282], 8, fill=(233, 180, 74, 70))
    im.alpha_composite(hl)
    return im


def sc_trap(c):
    R, fr, ts, sd = c["R"], c["fr"], c["ts"], c["sd"]
    d = ImageDraw.Draw(fr)
    notes = cached("notes", _notes_card)
    p = ease_out(ts / 0.5)
    nx, ny = 60, 560
    ni = notes.copy()
    ni.putalpha(ni.getchannel("A").point(lambda v: int(v * p)))
    fr.alpha_composite(ni, (nx, int(ny + (1 - p) * 80)))
    t = R.find_token("smooth", chunk_id="c6")
    conf = 0.0
    if t:
        conf = ease(clamp((ts - (t[0] - c["t0"])) / 1.6)) * 0.92
    blank = R.find_token("blank", chunk_id="c7")
    bt = (blank[0] - c["t0"]) if blank else 999
    if ts > bt:
        wob = math.exp(-(ts - bt) * 2.2) * math.sin((ts - bt) * 14)
        conf = lerp(conf, 0.18, ease(clamp((ts - bt) / 0.8))) + wob * 0.08
    GX1, GY1, GX2, GY2 = 830, 700, 830, 1190
    d.arc([GX1 - 140, GY1 - 140, GX1 + 140, GY1 + 140], 135, 405, fill=(70, 60, 45), width=13)
    if conf > 0.01:
        d.arc([GX1 - 140, GY1 - 140, GX1 + 140, GY1 + 140], 135, 135 + 270 * clamp(conf), fill=GOLD, width=13)
    a = math.radians(135 + 270 * clamp(conf))
    d.line([GX1, GY1, GX1 + math.cos(a) * 104, GY1 + math.sin(a) * 104], fill=GOLD_HI, width=6)
    d.ellipse([GX1 - 10, GY1 - 10, GX1 + 10, GY1 + 10], fill=GOLD_HI)
    lab = text_img(f"FEELS LIKE  {int(conf * 100)}%", font("en", 800, 34), GOLD_HI)
    fr.alpha_composite(lab, (GX1 - lab.width // 2, GY1 + 158))
    know = 0.38
    d.arc([GX2 - 140, GY2 - 140, GX2 + 140, GY2 + 140], 135, 405, fill=(70, 60, 45), width=13)
    d.arc([GX2 - 140, GY2 - 140, GX2 + 140, GY2 + 140], 135, 135 + 270 * know, fill=(122, 108, 84), width=13)
    a = math.radians(135 + 270 * know)
    d.line([GX2, GY2, GX2 + math.cos(a) * 104, GY2 + math.sin(a) * 104], fill=(190, 172, 140), width=6)
    d.ellipse([GX2 - 10, GY2 - 10, GX2 + 10, GY2 + 10], fill=(190, 172, 140))
    lab = text_img(f"ACTUALLY  {int(know * 100)}%", font("en", 800, 34), DIM)
    fr.alpha_composite(lab, (GX2 - lab.width // 2, GY2 + 158))
    if ts > bt - 0.2:
        ex = cached("exam", _exam_card)
        p = ease_out((ts - (bt - 0.2)) / 0.45)
        wq = int(ex.width * max(0.02, p))
        ei = ex.resize((wq, ex.height), Image.BILINEAR)
        fr.alpha_composite(ei, (60 + (560 - wq) // 2, 560))
    if ts > bt + 0.6:
        lab = cached("illusion", lambda: gold_text("THE ILLUSION OF COMPETENCE", font("en", 800, 46), spacing=2))
        a = ease((ts - bt - 0.6) / 0.5)
        li = lab.copy()
        li.putalpha(li.getchannel("A").point(lambda v: int(v * a)))
        fr.alpha_composite(li, (540 - lab.width // 2, 1470))


def _exam_card():
    w, h = 560, 720
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], 20, fill=(238, 228, 206, 250))
    t = text_img("EXAM · QUESTION 3", font("en", 800, 36), (30, 24, 15), spacing=2)
    im.paste(t, (44, 48), t)
    d.line([44, 110, w - 44, 110], fill=(60, 48, 30, 180), width=3)
    q = text_img("“…apply this to a new case?”", font("en", 600, 34), (60, 48, 30))
    im.paste(q, (44, 150), q)
    t = gold_text("?", font("en", 800, 340))
    im.paste(t, (w // 2 - t.width // 2, 260), t)
    return im


def sc_methods(c):
    R, fr, ts, sd = c["R"], c["fr"], c["ts"], c["sd"]
    d = ImageDraw.Draw(fr)
    panel = rounded_card(940, 800, 34, (16, 12, 9, 214), (233, 180, 74, 120), 2)
    fr.alpha_composite(panel, (70, 600))
    t = gold_text("WHAT ACTUALLY WORKS", font("en", 800, 46), spacing=2)
    fr.alpha_composite(t, (540 - t.width // 2, 646))
    t = text_img("utility ratings · Dunlosky et al., 2013", font("en", 500, 26), DIM, spacing=1)
    fr.alpha_composite(t, (540 - t.width // 2, 726))
    rows = [("PRACTICE TESTING", 0.92, True, "practice"),
            ("SPACED PRACTICE", 0.88, True, "spaced"),
            ("REREADING", 0.25, False, "rereading"),
            ("HIGHLIGHTING", 0.22, False, "highlighting")]
    y = 800
    for name, val, hot, tok in rows:
        tt = R.find_token(tok)
        t0 = (tt[0] - c["t0"]) if tt else 0.4
        p = ease_out(clamp((ts - t0) / 0.9))
        lab = text_img(name, font("en", 700, 30), WARM if hot else DIM, spacing=1)
        fr.alpha_composite(lab, (110, y + 6))
        d.rounded_rectangle([450, y, 880, y + 52], 26, fill=(48, 40, 30, 200))
        bw = int((880 - 450) * val * p)
        if bw > 6:
            col = (233, 180, 74, 240) if hot else (110, 98, 78, 220)
            d.rounded_rectangle([450, y, 450 + bw, y + 52], 26, fill=col)
            d.rounded_rectangle([454, y + 4, 450 + bw - 4, y + 24], 12,
                                fill=(255, 236, 180, 70 if hot else 30))
        if p > 0.95:
            tag = text_img("HIGH" if hot else "LOW", font("en", 800, 30),
                           GOLD_HI if hot else (140, 126, 102), spacing=2)
            fr.alpha_composite(tag, (985 - tag.width, y + 8))
        y += 118
    t = text_img("feels easy  ≠  learns deep", font("en", 600, 34), WARM, spacing=1)
    a = ease(clamp((ts - (sd - 2.2)) / 0.5)) if sd > 2.4 else 1
    ti = t.copy()
    ti.putalpha(ti.getchannel("A").point(lambda v: int(v * a)))
    fr.alpha_composite(ti, (540 - t.width // 2, 1300))


def sc_loop(c):
    R, fr, ts, sd = c["R"], c["fr"], c["ts"], c["sd"]
    d = ImageDraw.Draw(fr)
    cx, cy, rx, ry = 540, 1010, 350, 140
    segs = 90
    pts = []
    for i in range(segs + 1):
        a = 2 * math.pi * i / segs
        dep = math.sin(a)
        pts.append((cx + math.cos(a) * rx, cy + math.sin(a) * ry, dep))
    for i in range(segs):
        x0, y0, dp0 = pts[i]
        x1, y1, dp1 = pts[i + 1]
        dep = (dp0 + dp1) / 2
        wdt = int(4 + 7 * (dep * 0.5 + 0.5))
        al = int(70 + 130 * (dep * 0.5 + 0.5))
        d.line([x0, y0, x1, y1], fill=(233, 180, 74, al), width=wdt)
    stations = [("PLAN", -math.pi / 2, "plan"), ("MONITOR", math.pi / 6, "monitor"),
                ("EVALUATE", math.pi * 5 / 6, "evaluate")]
    for name, a, tok in stations:
        tt = R.find_token(tok, chunk_id="c8") or R.find_token(tok, chunk_id="c5")
        lit = tt and ts >= tt[0] - c["t0"]
        x, y = cx + math.cos(a) * rx, cy + math.sin(a) * ry
        dep = math.sin(a) * 0.5 + 0.5
        r = 16 + 10 * dep
        if lit:
            fr.alpha_composite(glow_disc(180, (255, 200, 110, 190), 60, 30), (int(x) - 90, int(y) - 90))
        d.ellipse([x - r, y - r, x + r, y + r], fill=GOLD if lit else (86, 72, 50))
        d.ellipse([x - r * 0.45, y - r * 0.45, x + r * 0.45, y + r * 0.45],
                  fill=GOLD_HI if lit else (140, 122, 90))
        lab = text_img(name, font("en", 800, 40), GOLD_HI if lit else DIM, spacing=3)
        ly = y - 90 if math.sin(a) < 0 else y + 46
        fr.alpha_composite(lab, (int(x - lab.width / 2), int(ly)))
    ang = -math.pi / 2 + ts * 1.05
    for k in range(22):
        aa = ang - k * 0.055
        x, y = cx + math.cos(aa) * rx, cy + math.sin(aa) * ry
        r = 9 * (1 - k / 26)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 226, 150, int(230 * (1 - k / 22))))
    eye = R.eye.resize((300, 300), Image.LANCZOS)
    fr.alpha_composite(eye, (540 - 150, 880))
    t = cached("yw", lambda: gold_text("YOU, WATCHING YOU", font("en", 800, 40), spacing=3))
    fr.alpha_composite(t, (540 - t.width // 2, 1330))


def sc_hq(c):
    R, fr, ts, sd = c["R"], c["fr"], c["ts"], c["sd"]
    d = ImageDraw.Draw(fr)
    fr.alpha_composite(glow_disc(1300, (255, 186, 90, 170), 500, 170), (540 - 650, 900 - 650))
    s = ease_back(ts / 0.7)
    size = int(940 * s)
    if size > 4:
        em = R.emblem.resize((size, size), Image.LANCZOS)
        fr.alpha_composite(em, (540 - size // 2, 880 - size // 2))
    handle = "@metacognition.hq"
    n = int(len(handle) * clamp((ts - 0.9) / 0.9))
    if n:
        t = gold_text(handle[:n], font("en", 700, 62), spacing=2)
        fr.alpha_composite(t, (540 - 320, 1420))
        if int(ts * 3) % 2 == 0 and n < len(handle):
            d.rectangle([540 - 320 + t.width + 8, 1430, 540 - 320 + t.width + 18, 1495], fill=GOLD_HI)
    t = cached("reel1", lambda: chip(360, 92, "REEL 01", None))
    a = ease((ts - 0.3) / 0.5)
    ti = t.copy()
    ti.putalpha(ti.getchannel("A").point(lambda v: int(v * a)))
    fr.alpha_composite(ti, (540 - 180, 500))


def sc_cta(c):
    R, fr, ts, sd = c["R"], c["fr"], c["ts"], c["sd"]
    d = ImageDraw.Draw(fr)
    t1 = cached("cta1", lambda: gold_text("TRAIN THE WATCHER", font("en", 800, 74), spacing=2))
    t2 = cached("cta2", lambda: gold_text("IN YOUR HEAD", font("en", 800, 74), spacing=2))
    a = ease(ts / 0.5)
    for i, t in enumerate((t1, t2)):
        ti = t.copy()
        ti.putalpha(ti.getchannel("A").point(lambda v: int(v * a)))
        fr.alpha_composite(ti, (540 - t.width // 2, 620 + i * 96))
    em = R.emblem.resize((520, 520), Image.LANCZOS)
    fr.alpha_composite(em, (540 - 260, 880))
    pill = cached("fpill", _follow_pill)
    sc = 1 + 0.035 * math.sin(ts * 4.2)
    pw, ph = int(pill.width * sc), int(pill.height * sc)
    pi = pill.resize((pw, ph), Image.BILINEAR)
    fr.alpha_composite(glow_disc(800, (255, 196, 100, 130), 260, 90), (540 - 400, 1480 - 400))
    fr.alpha_composite(pi, (540 - pw // 2, 1480 - ph // 2))
    t = text_img("LESSON 01 IS LIVE ↑", font("en", 600, 34), WARM, spacing=3)
    fr.alpha_composite(t, (540 - t.width // 2, 1600))
    fade = clamp((ts - (sd - 1.0)) / 1.0)
    if fade > 0:
        ov = Image.new("RGBA", (W, H), (5, 4, 3, int(235 * fade)))
        fr.alpha_composite(ov)
        if fade > 0.5:
            g = glow_disc(700, (255, 190, 95, int(200 * fade)), 240, 90)
            fr.alpha_composite(g, (540 - 350, 960 - 350))
            e2 = R.emblem.resize((430, 430), Image.LANCZOS)
            e2.putalpha(e2.getchannel("A").point(lambda v: int(v * fade)))
            fr.alpha_composite(e2, (540 - 215, 760))
            t = cached("endtag", lambda: gold_text("METACOGNITION · HQ", font("en", 800, 54), spacing=6))
            t.putalpha(t.getchannel("A").point(lambda v: int(v * fade)))
            fr.alpha_composite(t, (540 - t.width // 2, 1240))


def _follow_pill():
    w, h = 680, 128
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], 64, fill=(233, 180, 74, 250))
    d.rounded_rectangle([6, 6, w - 7, h - 7], 58, outline=(255, 240, 190, 210), width=3)
    t = text_img("FOLLOW  @metacognition.hq", font("en", 800, 40), (22, 16, 8), spacing=1)
    im.paste(t, ((w - t.width) // 2, (h - t.height) // 2 - 2), t)
    return im


SCENES = {"hook": sc_hook, "article": sc_article, "word": sc_word, "halves": sc_halves,
          "trap": sc_trap, "methods": sc_methods, "loop": sc_loop, "hq": sc_hq, "cta": sc_cta}
BGKIND = {"hook": "base", "article": "base", "word": "base", "halves": "brain",
          "trap": "desk", "methods": "base", "loop": "base", "hq": "base", "cta": "base"}
