"""Render engine for the metacognition.hq reel: captions, concept-web, chrome, loop."""
import json, math, os, subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from arabic_reshaper import reshape
from bidi.algorithm import get_display
import imageio_ffmpeg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H, FPS = 1080, 1920, 30
BG = (10, 8, 6)
GOLD = (233, 180, 74)
GOLD_HI = (255, 228, 158)
GOLD_LO = (122, 88, 32)
WARM = (246, 234, 210)
DIM = (146, 132, 108)

_fcache = {}


def font(kind, w, size):
    k = (kind, w, size)
    if k not in _fcache:
        _fcache[k] = ImageFont.truetype(f"{ROOT}/assets/fonts/{kind}-{w}.ttf", size)
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


# ---------------------------------------------------------------- text utils
def text_img(text, fnt, fill, spacing=0):
    """Tight RGBA image of text."""
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
    """Vertical gold-gradient text."""
    mask = text_img(text, fnt, (255, 255, 255), spacing)
    a = np.array(mask.getchannel("A"), np.float32) / 255.0
    hh = mask.height
    g = np.linspace(0, 1, hh)[:, None]
    top = np.array(hi, np.float32)
    bot = np.array(lo, np.float32)
    col = top[None, None, :] * (1 - g)[..., None] + bot[None, None, :] * g[..., None]
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


# ---------------------------------------------------------------- resources
class Res:
    def __init__(self):
        self.script = json.load(open(f"{ROOT}/content/script.json"))
        self.tl = json.load(open(f"{ROOT}/audio/timing.json"))
        self.total = self.tl["total"]
        self.tags = self.script["scene_tags"]
        # scene cut times
        self.cuts = []          # (time, scene)
        for ch in self.tl["chunks"]:
            for ln in ch["lines"]:
                if not self.cuts or self.cuts[-1][1] != ln["scene"]:
                    self.cuts.append((ln["start"], ln["scene"]))
        self.scene_start = {s: t for t, s in self.cuts}
        self.scenes = [s for _, s in self.cuts]
        # backgrounds (oversized for ken burns)
        bw, bh = 1296, 2304
        self.bg_base = self._base_bg().resize((bw, bh), Image.LANCZOS)
        brain = Image.open(f"{ROOT}/assets/img/hero_brain.png").convert("RGB").resize((bw, bh), Image.LANCZOS)
        self.bg_brain = Image.fromarray((np.array(brain, np.float32) * 0.82).astype(np.uint8))
        desk = Image.open(f"{ROOT}/assets/img/hero_desk.png").convert("RGB").resize((bw, bh), Image.LANCZOS)
        self.bg_desk = Image.fromarray((np.array(desk, np.float32) * 0.8).astype(np.uint8))
        self.emblem = Image.open(f"{ROOT}/assets/img/logo_emblem.png").convert("RGBA")
        self.eye = Image.open(f"{ROOT}/assets/img/logo_eye.png").convert("RGBA")
        bf = Image.open(f"{ROOT}/assets/img/hero_brain.png").convert("RGB").resize((W, H), Image.LANCZOS)
        bfa = np.array(bf, np.float32) * 0.86
        self.brain_full = Image.fromarray(bfa.astype(np.uint8), "RGB").convert("RGBA")
        self.wmark = self.emblem.resize((104, 104), Image.LANCZOS)
        self.handle_img = text_img("@metacognition.hq", font("en", 600, 30), GOLD, spacing=3)
        self.flash = glow_disc(1600, (255, 200, 110, 255), 620, 260).resize((W, H))
        self._build_captions()
        self._build_fa()
        self._build_tags()
        self._build_web()

    # ---- base marble background
    def _base_bg(self):
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        base = np.zeros((H, W, 3), np.float32)
        base[..., 0], base[..., 1], base[..., 2] = 10, 8, 6
        # top spotlight
        r = np.sqrt((xx - W / 2) ** 2 + ((yy - 80) * 1.9) ** 2) / 900
        spot = np.clip(1 - r, 0, 1) ** 2.1
        base += spot[..., None] * np.array([64, 46, 22], np.float32)
        # marble veins
        n = np.sin(xx * 0.006 + np.sin(yy * 0.004) * 2.6) + np.sin(yy * 0.009 + np.sin(xx * 0.005) * 1.8)
        n = (n - n.min()) / (n.max() - n.min())
        base += (n ** 4)[..., None] * np.array([16, 13, 9], np.float32)
        # vignette
        rv = np.sqrt(((xx - W / 2) / (W * 0.62)) ** 2 + ((yy - H / 2) / (H * 0.6)) ** 2)
        base *= (1 - 0.55 * np.clip(rv - 0.35, 0, 1))[..., None]
        return Image.fromarray(base.astype(np.uint8), "RGB")

    # ---- english karaoke captions
    def _build_captions(self):
        f = font("en", 600, 50)
        asc, desc = f.getmetrics()
        lh = asc + desc + 16

        def word_img(wd, fill):
            bb = f.getbbox(wd)
            wimg = Image.new("RGBA", (bb[2] - bb[0] + 24, lh), (0, 0, 0, 0))
            ImageDraw.Draw(wimg).text((12, 8 + asc), wd, font=f, fill=fill, anchor="ls")
            return wimg
        self.cap_lines = []     # flat list, timed
        for ch in self.tl["chunks"]:
            for ln in ch["lines"]:
                words = ln["words"]
                # wrap into rows <= 950px
                rows, cur, cw = [], [], 0
                imgs = {}
                for wd in words:
                    wi = word_img(wd["w"], WARM)
                    gi = word_img(wd["w"], GOLD_HI)
                    imgs[id(wd)] = (wi, gi)
                    ww = wi.width + 14
                    if cw + ww > 950 and cur:
                        rows.append(cur)
                        cur, cw = [], 0
                    cur.append((wd, imgs[id(wd)], cw))
                    cw += ww
                if cur:
                    rows.append(cur)
                self.cap_lines.append({"rows": rows, "start": ln["start"], "end": ln["end"],
                                       "scene": ln["scene"], "width": max(sum(1 for _ in r) and
                                       (r[-1][2] + r[-1][1][0].width) for r in rows)})

    # ---- persian subtitle pills (mixed FA/EN runs, auto-fit width)
    def _build_fa(self):
        import re as _re
        self.fa_lines = []
        for ch in self.tl["chunks"]:
            for fa in ch["fa"]:
                txt = fa["text"]
                for size in (56, 50, 44, 38, 32):
                    ff, fe = font("fa", 500, size), font("en", 600, int(size * 0.92))
                    runs = _re.findall(r"[A-Za-z0-9@./%'+-]+|[^A-Za-z0-9@./%'+-]+", txt)
                    vis = []
                    for r in runs:
                        if _re.fullmatch(r"[A-Za-z0-9@./%'+-]+", r):
                            vis.insert(0, ("en", text_img(r, fe, WARM)))
                        else:
                            vis.insert(0, ("fa", text_img(get_display(reshape(r)), ff, WARM)))
                    vis.reverse()
                    vis = vis[::-1] if False else vis
                    # visual order: for RTL paragraph, first logical run is rightmost
                    vis = list(reversed(vis))
                    tw = sum(im.width for _, im in vis) - 6 * len(vis)
                    if tw <= 940:
                        break
                th = max(im.height for _, im in vis)
                canv = Image.new("RGBA", (tw + 4, th), (0, 0, 0, 0))
                x = 0
                for kind, im in vis:
                    canv.paste(im, (x, (th - im.height) // 2), im)
                    x += im.width - 6
                pw, ph = canv.width + 96, canv.height + 44
                pill = rounded_card(pw, ph, 30, (12, 10, 8, 196), (233, 180, 74, 70), 2)
                pill.paste(canv, ((pw - canv.width) // 2, (ph - canv.height) // 2), canv)
                d = ImageDraw.Draw(pill)
                d.line([26, 5, pw - 26, 5], fill=(233, 180, 74, 120), width=2)
                self.fa_lines.append({"img": pill, "start": fa["start"], "end": fa["end"]})

    # ---- scene tags
    def _build_tags(self):
        self.tag_imgs = {}
        for sid, txt in self.tags.items():
            self.tag_imgs[sid] = gold_text(txt, font("en", 700, 30), spacing=5)

    # ---- concept web
    def _build_web(self):
        N = dict(
            center=("METACOGNITION", 540, 640, "hook"),
            flavell=("FLAVELL · 1979", 208, 486, "article"),
            paper=("THE PAPER", 872, 486, "article"),
            meta=("META · ABOVE", 236, 780, "word"),
            cog=("COGNITION · THINKING", 844, 780, "word"),
            watcher=("THE WATCHER", 540, 452, "word"),
            know=("KNOWLEDGE", 236, 1010, "halves"),
            control=("CONTROL", 844, 1010, "halves"),
            plan=("PLAN", 320, 1250, "halves"),
            monitor=("MONITOR", 540, 1318, "halves"),
            evaluate=("EVALUATE", 760, 1250, "halves"),
            trap=("FLUENCY TRAP", 196, 1180, "trap"),
            reread=("REREADING ✕", 216, 1400, "methods"),
            test=("PRACTICE TESTING", 864, 1180, "methods"),
            spaced=("SPACED PRACTICE", 864, 1400, "methods"),
            hq=("HQ · TRAIN THE WATCHER", 540, 1470, "hq"),
        )
        E = [("center", "flavell"), ("center", "paper"), ("flavell", "paper"),
             ("center", "meta"), ("center", "cog"), ("meta", "watcher"), ("cog", "watcher"),
             ("center", "know"), ("center", "control"), ("control", "plan"),
             ("control", "monitor"), ("control", "evaluate"), ("know", "plan"),
             ("center", "trap"), ("trap", "reread"), ("center", "test"),
             ("test", "spaced"), ("reread", "test"), ("center", "hq"),
             ("know", "trap"), ("evaluate", "hq")]
        self.nodes = {}
        for k, (label, x, y, sc) in N.items():
            lab = text_img(label, font("en", 600, 21), DIM, spacing=2)
            lab_on = text_img(label, font("en", 700, 21), GOLD_HI, spacing=2)
            self.nodes[k] = dict(x=x, y=y, sc=sc, lab=lab, lab_on=lab_on)
        self.edges = E

    def find_word(self, chunk_idx, line_idx, word_idx):
        ln = self.tl["chunks"][chunk_idx]["lines"][line_idx]
        wd = ln["words"][word_idx]
        return wd["start"], wd["end"]

    def find_token(self, tok, chunk_id=None):
        """First (start,end) of a word token across timeline."""
        for ch in self.tl["chunks"]:
            if chunk_id and ch["id"] != chunk_id:
                continue
            for ln in ch["lines"]:
                for wd in ln["words"]:
                    if wd["w"].strip(".,:!?").lower() == tok.strip(".,:!?").lower():
                        return wd["start"], wd["end"]
        return None


RES = None


def res():
    global RES
    if RES is None:
        RES = Res()
    return RES
