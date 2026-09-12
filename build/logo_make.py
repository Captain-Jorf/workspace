"""Procedural reconstruction of the metacognition.hq emblem:
black-marble oval ring + gold neural dendrites + central golden watcher eye.
Outputs: assets/img/logo_emblem.png (transparent), assets/img/logo_eye.png
"""
import math, random
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

S = 1600
CX = CY = S // 2
GOLD = (233, 180, 74)
GOLD_HI = (255, 226, 150)
GOLD_LO = (122, 88, 32)
BLACK = (10, 8, 6)


def radial(size, inner, outer, radius_frac=1.0, cx=None, cy=None):
    """Radial gradient RGBA layer."""
    w = h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx = w / 2 if cx is None else cx
    cy = h / 2 if cy is None else cy
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / (max(w, h) / 2 * radius_frac)
    r = np.clip(r, 0, 1)
    inner = np.array(inner, np.float32)
    outer = np.array(outer, np.float32)
    img = inner[None, None, :] * (1 - r)[..., None] + outer[None, None, :] * r[..., None]
    return Image.fromarray(img.astype(np.uint8), "RGB")


def glow(size, color, radius, blur):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = size / 2
    d.ellipse([c - radius, c - radius, c + radius, c + radius], fill=color)
    return im.filter(ImageFilter.GaussianBlur(blur))


def sphere(r, bright=True):
    """Small 3D-looking gold (or black-gold) bulb."""
    s = int(r * 2 + 8)
    base = radial(s, (255, 232, 170) if bright else (60, 48, 30),
                  (150, 105, 35) if bright else (8, 6, 4), 1.0, cx=s * 0.38, cy=s * 0.34)
    base = base.convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse([4, 4, s - 4, s - 4], fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    d = ImageDraw.Draw(out)
    d.ellipse([s * 0.28, s * 0.22, s * 0.46, s * 0.40], fill=(255, 250, 230, 160))
    return out


def branch(d, x0, y0, ang, length, width, gen, rnd, tips):
    """Recursive gold dendrite filament."""
    pts = []
    x, y, a = x0, y0, ang
    seg = max(6, int(length / 14))
    for i in range(14):
        a += rnd.uniform(-0.16, 0.16)
        x += math.cos(a) * seg
        y += math.sin(a) * seg
        pts.append((x, y))
    for i in range(len(pts) - 1):
        t = i / len(pts)
        w = max(1.2, width * (1 - t * 0.85))
        col = tuple(int(a + (b - a) * t) for a, b in zip(GOLD_HI, GOLD_LO))
        d.line([pts[i], pts[i + 1]], fill=col, width=int(w))
    ex, ey = pts[-1]
    tips.append((ex, ey, rnd.uniform(12, 24) if gen == 0 else rnd.uniform(7, 14)))
    if gen < 1:
        for da in (-0.7, 0.65):
            mid = pts[int(len(pts) * rnd.uniform(0.45, 0.7))]
            branch(d, mid[0], mid[1], ang + da + rnd.uniform(-0.2, 0.2),
                   length * rnd.uniform(0.42, 0.6), width * 0.6, gen + 1, rnd, tips)


def make_eye(size=760):
    """Almond watcher-eye with golden iris, transparent bg."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    c = size / 2
    g = glow(size, (255, 190, 90, 150), size * 0.30, size * 0.09)
    im.alpha_composite(g)
    d = ImageDraw.Draw(im)
    hw, hh = size * 0.46, size * 0.235
    # almond via two arcs
    top = [ (c - hw + 2*hw*t, c - hh * math.sin(math.pi * t)) for t in np.linspace(0, 1, 60)]
    bot = [ (c - hw + 2*hw*t, c + hh * math.sin(math.pi * t)) for t in np.linspace(1, 0, 60)]
    almond = top + bot
    d.polygon(almond, fill=(12, 10, 8, 255))
    d.line(almond, fill=GOLD, width=max(4, size // 110), joint="curve")
    inner = [(c - hw * 0.94 + 2 * hw * 0.94 * t, c - hh * 0.86 * math.sin(math.pi * t)) for t in np.linspace(0, 1, 60)] + \
            [(c - hw * 0.94 + 2 * hw * 0.94 * t, c + hh * 0.86 * math.sin(math.pi * t)) for t in np.linspace(1, 0, 60)]
    d.line(inner, fill=(150, 110, 45), width=max(2, size // 260), joint="curve")
    # iris
    ir = size * 0.205
    iris = radial(int(ir * 2), (255, 216, 122), (128, 88, 26), 1.0).convert("RGBA")
    m = Image.new("L", iris.size, 0)
    ImageDraw.Draw(m).ellipse([0, 0, ir * 2, ir * 2], fill=255)
    im.paste(iris, (int(c - ir), int(c - ir)), m)
    d = ImageDraw.Draw(im)
    rnd = random.Random(7)
    for i in range(110):  # iris fibres
        a = rnd.uniform(0, 2 * math.pi)
        r0, r1 = ir * rnd.uniform(0.30, 0.42), ir * rnd.uniform(0.8, 0.98)
        col = (255, 236, 170, rnd.randint(70, 160)) if i % 2 else (96, 62, 16, rnd.randint(60, 140))
        d.line([c + math.cos(a) * r0, c + math.sin(a) * r0, c + math.cos(a) * r1, c + math.sin(a) * r1],
               fill=col, width=2)
    d.ellipse([c - ir, c - ir, c + ir, c + ir], outline=(30, 20, 8, 255), width=max(4, size // 120))
    pr = ir * 0.42
    d.ellipse([c - pr, c - pr, c + pr, c + pr], fill=(5, 4, 3, 255))
    d.ellipse([c - pr, c - pr, c + pr, c + pr], outline=(200, 150, 60, 120), width=2)
    # upper lid shadow
    sh = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ds = ImageDraw.Draw(sh)
    ds.polygon(almond, fill=(0, 0, 0, 120))
    sh = sh.filter(ImageFilter.GaussianBlur(size * 0.03))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(almond, fill=255)
    sh.putalpha(Image.composite(sh.getchannel("A"), Image.new("L", (size, size), 0), mask))
    cut = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    cut.paste(sh, (0, int(-size * 0.02)))
    im.alpha_composite(cut)
    d = ImageDraw.Draw(im)
    d.ellipse([c + ir * 0.16, c - ir * 0.55, c + ir * 0.55, c - ir * 0.16], fill=(255, 255, 248, 235))
    d.ellipse([c - ir * 0.58, c + ir * 0.26, c - ir * 0.36, c + ir * 0.48], fill=(255, 244, 210, 110))
    return im


def make_emblem():
    rnd = random.Random(42)
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rx, ry = 700, 722
    th = 96
    # ---- marble ring
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dr = ImageDraw.Draw(ring)
    dr.ellipse([CX - rx, CY - ry, CX + rx, CY + ry], fill=(36, 28, 19, 255))
    dr.ellipse([CX - rx + th, CY - ry + th, CX + rx - th, CY + ry - th], fill=(0, 0, 0, 0))
    # marble noise streaks
    noise = np.zeros((S, S), np.float32)
    yy, xx = np.mgrid[0:S, 0:S]
    noise = (np.sin(xx * 0.013 + np.sin(yy * 0.011) * 3.1) + np.sin(yy * 0.017 + np.sin(xx * 0.008) * 2.2))
    noise = (noise - noise.min()) / (noise.max() - noise.min())
    arr = np.array(ring).astype(np.float32)
    streak = (noise ** 2.2)[..., None] * np.array([78, 64, 46, 0], np.float32)
    arr[..., :3] += streak[..., :3] * (arr[..., 3:4] > 0)
    ring = Image.fromarray(arr.astype(np.uint8), "RGBA")
    dr = ImageDraw.Draw(ring)
    for e, w, col in ((6, 5, GOLD), (th + 4, 3, (150, 110, 45)), (th // 2, 2, (90, 66, 28))):
        dr.ellipse([CX - rx + e, CY - ry + e, CX + rx - e, CY + ry - e], outline=col, width=w)
    # seams (gold tabs N/S/E/W)
    for ang in (90, 270, 0, 180):
        a = math.radians(ang)
        px, py = CX + math.cos(a) * (rx - th / 2), CY + math.sin(a) * (ry - th / 2)
        w_, h_ = (26, th + 26) if ang in (90, 270) else (th + 26, 26)
        dr.rounded_rectangle([px - w_ / 2, py - h_ / 2, px + w_ / 2, py + h_ / 2], 8, fill=GOLD)
        dr.rounded_rectangle([px - w_ / 2 + 5, py - h_ / 2 + 5, px + w_ / 2 - 5, py + h_ / 2 - 5],
                             6, fill=(120, 88, 36))
    im.alpha_composite(ring)
    # ---- inner disc + warm core glow
    disc = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dm = Image.new("L", (S, S), 0)
    ImageDraw.Draw(dm).ellipse([CX - rx + th, CY - ry + th, CX + rx - th, CY + ry - th], fill=255)
    grad = radial(S, (46, 33, 18), (9, 7, 5)).convert("RGBA")
    disc.paste(grad, (0, 0), dm)
    im.alpha_composite(disc)
    im.alpha_composite(glow(S, (255, 186, 88, 165), 330, 120))
    # ---- dendrites
    dend = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dend)
    tips = []
    for i in range(16):
        ang = math.radians(i * 22.5 + rnd.uniform(-5, 5))
        r0 = rnd.uniform(150, 210)
        branch(dd, CX + math.cos(ang) * r0, CY + math.sin(ang) * r0 * 1.02, ang,
               rnd.uniform(300, 400), 10, 0, rnd, tips)
    dend_g = dend.filter(ImageFilter.GaussianBlur(6))
    im.alpha_composite(dend_g)
    im.alpha_composite(dend_g)
    im.alpha_composite(dend)
    for (x, y, r) in tips:
        b = sphere(int(r), bright=rnd.random() > 0.35)
        im.alpha_composite(b, (int(x - b.width / 2), int(y - b.height / 2)))
    # ---- eye
    eye = make_eye(780)
    im.alpha_composite(eye, (CX - 390, CY - 390))
    return im, eye


if __name__ == "__main__":
    emb, eye = make_emblem()
    emb.save("assets/img/logo_emblem.png")
    eye.save("assets/img/logo_eye.png")
    prev = emb.copy()
    bg = Image.new("RGBA", (S, S), (10, 8, 6, 255))
    bg.alpha_composite(prev)
    bg.convert("RGB").save("assets/img/logo_preview.jpg", quality=90)
    print("emblem saved", emb.size)
