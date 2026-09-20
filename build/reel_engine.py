"""Scene-plan-aware render engine — English-only, Technology × Metacognition.

Issue #24 (reel-2026-09-19) rework:

  * the renderer is driven by the deterministic visual plan
    (content/episodes/<ep>/visual_plan.json, built by build/visual_plan.py
    after the script passed Producer/Reviewer/Revision + pre-render text QA);
  * NO template is chosen by substring-matching a visual_direction string —
    the generic `def solve()` code card and its moving cursor are gone
    forever; a code visual can only appear as a justified, curated,
    syntax-coherent snippet in a CODING/LEARNING_TECH scene, and a caret only
    when the narration demonstrates code entry;
  * every scene is a distinct art-directed composition: a topic-specific
    procedural diagram (said-vs-did, evidence filter, hypothesis ladder,
    decision matrix, funnels, loops, gauges, timelines, ...), OR a
    photo-designated scene carrying a $0 attribution-free (CC0/PDM) Openverse
    photo with recorded provenance — graded into the matte-black/
    gold/amber/ivory brand world with Ken Burns motion, masked reveals and
    short crossfades. A normal reel plans a balanced mix of 2-3 distinct
    topic-relevant photos, the remaining content scenes as procedural
    diagrams, and brand assets only at hook/ending;
  * if a designated photo cannot be retrieved safely (offline, rate-limited,
    malformed, unreadable) the scene deterministically falls back to its own
    topic-specific procedural visual — the reel never fails on the network,
    and never reuses a repository hero image to compensate;
  * on load the engine RE-RUNS the deterministic visual-semantic gate on the
    plan: a blocked plan raises before a single frame is rendered (fail
    closed). The static fallback, the safe re-render and every recovery path
    go through this same gate.

Profile assets (logo_emblem, logo_eye) are brand references only: the emblem
opens, the eye closes. The repository hero images (hero_brain / hero_desk)
are NOT scene backgrounds — the pre-render gate blocks any non-brand
repository image as a scene visual.
"""
import bisect
import hashlib
import json
import math
import os
import random
import re
from collections import OrderedDict

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import layout_gate  # noqa: E402
import text_norm  # noqa: E402
import visual_plan as vp  # noqa: E402
common.assert_content_language_en()
W, H, FPS = 1080, 1920, 30
GOLD = vp.GOLD
GOLD_HI = vp.GOLD_HI
GOLD_LO = vp.GOLD_LO
AMBER = vp.AMBER
BRONZE = vp.BRONZE
IVORY = vp.IVORY
WARM = (246, 234, 210)
DIM = (150, 136, 112)
INK = (24, 17, 8)
CARD = vp.CARD_DARK

ZONE_W, ZONE_H = 1080, 840          # procedural composition zone
ZONE_Y = 470                        # top of the zone (clear of the subtitle band)
CANVAS_PAD = 1.10                   # Ken Burns headroom

_fcache = {}


def font(kind, w, size):
    k = (kind, w, size)
    if k not in _fcache:
        path = f"{ROOT}/assets/fonts/{kind}-{w}.ttf"
        if not os.path.exists(path):
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
        if not isinstance(vis, dict):  # issue #24: the plan-era "visuals" field is a hint list
            vis = {}
        self.vis = {"gauge": vis.get("gauge") or ["FEELS LIKE", "ACTUALLY"],
                    "chain": vis.get("chain") or self.web_labels[1:4],
                    "cards": vis.get("cards") or [self.web_labels[0], self.web_labels[3]],
                    "steps": vis.get("steps") or ["NOTICE", "TEST", "UPDATE"]}
        # Art direction TEXT (shown on the poster hint card). The renderer no
        # longer parses this string for templates — the gated visual plan is
        # the single source of truth for what is shown.
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
        self._photo_cache = {}
        self._last_frame = None
        # Declared layout items per scene (issue #32 §5): the renderer records
        # every rendered item's bounding box + z-layer; the layout gate runs on
        # them BEFORE frame 0, and final QA re-checks them from layout.json.
        self._scene_items = {}
        # ---- gated visual plan (fail closed: a blocked plan never renders) ----
        self.plan = None
        self.scene_times = []
        self.code_scenes_rendered = []
        self.cursor_scenes_rendered = []
        self.cursor_events = []
        if self.script.get("chunks"):
            self._load_plan()
        self._assert_visible_text_renderable()

    def _assert_visible_text_renderable(self):
        """Issue #32 §1/§3: BEFORE a single frame exists, every visible string
        (subtitles, on-screen labels, poster-bound text, handle, scene tags)
        must be normalized and have a glyph in the ACTUAL production font
        cmap. U+FFFD and any unsupported character are fail-closed here —
        never OS font fallback, never a guessed allowlist. Also enforces
        TTS/timing/display parity: the karaoke words must equal the
        normalized script narration token-for-token.
        """
        extras = []
        if self.plan:
            for sc in self.plan.get("scenes", []):
                spec = vp.C.get(sc.get("visual_category"), {})
                for ls in spec.get("label_sets") or []:
                    extras.extend(ls)
                extras.extend(sc.get("code_lines") or [])
        issues = text_norm.glyph_gate_issues(self.script, extra_texts=extras)
        if issues:
            raise RuntimeError("glyph coverage gate blocked the render before "
                               "frame 0: " + " · ".join(issues[:6]))
        for t in list(self.tags.values()) + [self.script.get("meta", {}).get("handle", "")]:
            if text_norm.normalize_text(str(t)) != str(t):
                raise RuntimeError(f"chrome text {t!r} is not normalized — "
                                   "TTS/timing/display parity would drift")
        parity = text_norm.parity_issues(self.script, self.tl)
        if parity:
            raise RuntimeError("TTS/timing/display parity broken before render: "
                               + " · ".join(parity[:4]))
        # Pre-render every procedural scene composition through the layout
        # gate (fail closed): a cluttered/colliding scene never reaches a
        # frame. Snapshots are cached, so rendering reuses them.
        if self.plan:
            for sc in self.plan.get("scenes", []):
                if sc.get("visual_category") in vp.BRAND_CATEGORIES:
                    continue
                if (sc.get("asset") or {}).get("kind") in ("repo", "external") \
                        and self._photo_for(sc) is not None:
                    continue
                self._scene_snapshot(sc)

    def _load_plan(self):
        plan_path = os.path.join(self.ep, "visual_plan.json")
        plan = common.load_json(plan_path)
        if not plan or not plan.get("scenes"):
            plan = vp.build_visual_plan(self.script, self.pol, allow_external=False)
        issues = vp.visual_semantic_issues(plan, self.script, self.pol)
        if issues:
            raise RuntimeError("visual plan blocked before render (pre-render "
                               "visual-semantic gate, re-run at render time): "
                               + " · ".join(issues))
        self.plan = plan
        self._build_scene_times()
        self.cursor_scenes_rendered = []
        self.cursor_events = []
        for sc in plan["scenes"]:
            if sc.get("code_justified") and vp.C.get(sc.get("visual_category"), {}).get("code"):
                self.code_scenes_rendered.append(sc["scene_id"])
            if sc.get("cursor_justified"):
                self.cursor_scenes_rendered.append(sc["scene_id"])
                self.cursor_events.append({
                    "scene_id": sc["scene_id"],
                    "cursor_drawn": True,
                    "expected_region": [296, ZONE_Y + 452, 300, ZONE_Y + 486],
                    "justified": True,
                    "blink_hz": 2.0,
                })

    def _build_scene_times(self):
        """Map plan scenes onto the REAL word-level timing boundaries."""
        groups = OrderedDict()
        for sc in self.plan["scenes"]:
            groups.setdefault(sc["beat"], []).append(sc)
        self.scene_times = []
        ends = list(self.cut_t[1:]) + [self.total]
        for (t0, beat), t1 in zip(self.cuts, ends):
            scs = groups.get(beat, [])
            if not scs:
                continue
            if len(scs) == 1:
                self.scene_times.append((scs[0], t0, t1))
                continue
            words = []
            for ln in self.lines:
                if (ln.get("scene") or ln.get("beat")) != beat:
                    continue
                for w in ln.get("words", []):
                    words.append((w["start"] + w["end"]) / 2.0)
            if len(words) < len(scs):
                for i, sc in enumerate(scs):
                    a = t0 + (t1 - t0) * i / len(scs)
                    b = t0 + (t1 - t0) * (i + 1) / len(scs)
                    self.scene_times.append((sc, a, b))
                continue
            bounds = [t0]
            total = len(words)
            for k in range(1, len(scs)):
                idx = min(total - 1, max(0, round(total * k / len(scs)) - 1))
                bounds.append(words[idx])
            bounds.append(t1)
            for i, sc in enumerate(scs):
                self.scene_times.append((sc, bounds[i], bounds[i + 1]))

    def _scene_at(self, t):
        if not self.scene_times:
            return None, 0.0, 0.0
        starts = [st for _, st, _ in self.scene_times]
        i = bisect.bisect_right(starts, t) - 1
        if i < 0:
            return None, 0.0, 0.0
        sc, a, b = self.scene_times[i]
        return sc, t - a, b - a

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
        big = self.bg_base
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

    def draw_brand_accent(self, fr, t):
        """Subtle brand motif (issue #32 §4): two faint gold arcs + a small
        eye mark. Opacity ≤ 0.12, always behind content, geometrically clear
        of the subtitle band, the scene zone and the handle zone — purely a
        brand accent, never decoration competing with the semantic visual."""
        ov = Image.new("RGBA", fr.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        # top arc: circle centred above the frame → arc only reaches y ≤ 190
        d.arc([540 - 620, -430 - 620, 540 + 620, -430 + 620],
              30, 150, fill=(233, 180, 74, 26), width=2)
        # bottom arc: circle centred below the frame → arc only reaches y ≥ 1650
        d.arc([540 - 700, 2350 - 700, 540 + 700, 2350 + 700],
              210, 330, fill=(233, 180, 74, 22), width=2)
        # eye mark: clear of subtitle band (≤~434), scene zone (470..1310),
        # tag, progress bar and handle zone
        eye = self._cached("accent_eye", lambda: self.eye.resize((64, 64), Image.LANCZOS))
        ov.alpha_composite(with_alpha(eye, 0.12), (64, 1500))
        fr.alpha_composite(ov)

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

    # --- English karaoke captions: LTR, big font, high contrast, inside the
    # --- Instagram safe zone (layout en_top band, validated by QA).
    # --- HARD constraint: at most layout.en_max_rows (3) rendered rows per cue.
    # --- A cue that would wrap to more rows is SPLIT at a natural phrase/sentence
    # --- boundary into consecutive sub-cues — timing is preserved because every
    # --- sub-cue keeps its own word-level TTS timings. Font size is NOT shrunk
    # --- to squeeze extra rows (readability over compaction).

    def _wrap_words(self, words, size):
        """Wrap timed words into rendered rows at font `size`.
        Returns (rows, size, line_height); rows are lists of (word, (warm, gold, dim imgs), x_offset)."""
        L = self.L
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
        return rows, size, lh

    @staticmethod
    def _boundary_rank(word):
        """How natural a cue split is right after `word`: sentence end > clause break > plain word."""
        w = word.rstrip("\"'”’)]}")
        if re.search(r"[.?!]$", w):
            return 2
        if re.search(r"[,:;:—–-]$|^(and|but|so|because|then|so that)$", word, re.I):
            return 1
        return 0

    def _split_words_for_rows(self, words):
        """Split a timed word list into consecutive parts so EACH part wraps to at
        most en_max_rows rows at the full subtitle font size.

        No split when the line already fits. Otherwise the split point prefers
        natural phrase/sentence boundaries closest to the character midpoint;
        word order and word-level timings are preserved (each part is a cue of
        its own, starting/ending at its own first/last word).
        """
        L = self.L
        size = L["en_font_size"]
        rows, _, _ = self._wrap_words(words, size)
        if len(rows) <= L["en_max_rows"]:
            return [words]
        total = sum(len(w["w"]) for w in words)
        # Best single split: both halves must fit in en_max_rows rows.
        best = None
        for i in range(1, len(words)):
            left, right = words[:i], words[i:]
            rl, _, _ = self._wrap_words(left, size)
            rr, _, _ = self._wrap_words(right, size)
            if len(rl) <= L["en_max_rows"] and len(rr) <= L["en_max_rows"]:
                acc = sum(len(w["w"]) for w in left)
                score = (-self._boundary_rank(words[i - 1]["w"]), abs(acc - total / 2.0), i)
                if best is None or score < best[0]:
                    best = (score, i)
        if best:
            i = best[1]
            return self._split_words_for_rows(words[:i]) + self._split_words_for_rows(words[i:])
        # Extremely long line: no single split makes both halves fit — split at
        # the most natural point near the midpoint and recurse on each half.
        if len(words) == 1:
            return [words]
        mid = total / 2.0
        acc, fallback = 0, None
        for i in range(1, len(words)):
            acc += len(words[i - 1]["w"])
            score = (-self._boundary_rank(words[i - 1]["w"]), abs(acc - mid), i)
            if fallback is None or score < fallback[0]:
                fallback = (score, i)
        i = fallback[1]
        return self._split_words_for_rows(words[:i]) + self._split_words_for_rows(words[i:])

    def _build_captions(self):
        L = self.L
        self.cap_lines = []
        for ln in self.lines:
            # Issue #32 §2: the burned-in words must already BE the normalized
            # representation (same as TTS/timing). Fail closed on drift —
            # normalizing here alone would desynchronize word timing.
            for wd in ln["words"]:
                if text_norm.normalize_text(wd["w"]) != wd["w"]:
                    raise RuntimeError(
                        f"subtitle word {wd['w']!r} is not normalized — the "
                        "burned-in text must derive from the same normalized "
                        "representation as TTS and word timing")
                if text_norm.contains_replacement_char(wd["w"]):
                    raise RuntimeError("U+FFFD reached the subtitle renderer — blocked")
            for sub in self._split_words_for_rows(ln["words"]):
                rows, size, lh = self._wrap_words(sub, L["en_font_size"])
                # Escape hatch only (pathological single word): shrinking never
                # re-joins cues; it just keeps an un-splittable word on screen.
                if len(rows) > L["en_max_rows"]:
                    for s2 in (50, 46, 42, 38):
                        rows, size, lh = self._wrap_words(sub, s2)
                        if len(rows) <= L["en_max_rows"]:
                            break
                text = " ".join(w["w"] for w in sub)
                row_h = L["en_row_height"]
                width = max((r[-1][2] + r[-1][1][0].width) for r in rows)
                top = L["en_top"]
                bottom = top + len(rows) * row_h
                start, end = sub[0]["start"], sub[-1]["end"]
                self.cap_lines.append({"rows": rows, "start": start, "end": end, "size": size, "lh": lh,
                                       "width": width, "top": top, "bottom": bottom, "text": text})
                self.layout["en"].append({"text": text, "start": start, "end": end, "rows": len(rows),
                                          "font": size, "direction": "ltr",
                                          "bbox": [int((W - width) // 2), top, int((W + width) // 2), bottom]})

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

    # -----------------------------------------------------------------------
    # Scene rendering (gated visual plan)
    # -----------------------------------------------------------------------

    def _kb_params(self, scene):
        """Deterministic Ken Burns parameters per scene (pan/zoom variety)."""
        h = int(hashlib.sha256(scene["scene_id"].encode("utf-8")).hexdigest()[:8], 16)
        zoom = 1.04 + (h % 5) * 0.01
        dx = -24 + (h % 7) * 8
        dy = -14 + (h % 5) * 7
        return zoom, dx, dy

    def _scene_snapshot(self, scene):
        """Pre-render a scene's full-state composition (cached per scene).
        Compositions are drawn in the 1080x840 design space, then centered on
        a padded canvas so Ken Burns can pan/zoom without exposing edges.

        Issue #32 §5: every composition DECLARES its items (bounding box +
        z-layer + kind) and the deterministic layout gate must pass BEFORE a
        frame is rendered — intersecting text, hidden labels, lines through
        text, unreadable microtext and excessive density all fail closed here.
        """
        key = ("scene", scene["scene_id"])
        if key in self._widget_cache:
            return self._widget_cache[key]
        inner = Image.new("RGBA", (ZONE_W, ZONE_H), (0, 0, 0, 0))
        d = ImageDraw.Draw(inner)
        comp = vp.C.get(scene["visual_category"], {}).get("comp")
        fn = getattr(self, f"_comp_{comp}", None) if comp else None
        if fn is None:  # defensive: unknown comp → neutral brand comparison
            fn = self._comp_dual
        items = fn(inner, d, scene) or []
        issues = layout_gate.check_items(items)
        if issues:
            raise RuntimeError(
                f"layout gate blocked scene {scene['scene_id']} "
                f"({scene['visual_category']}) before render: "
                + " · ".join(issues[:6]))
        self._scene_items[scene["scene_id"]] = items
        canvas = Image.new("RGBA", (int(ZONE_W * CANVAS_PAD), int(ZONE_H * CANVAS_PAD)), (0, 0, 0, 0))
        canvas.paste(inner, (int(ZONE_W * (CANVAS_PAD - 1) / 2), int(ZONE_H * (CANVAS_PAD - 1) / 2)))
        self._widget_cache[key] = canvas
        return canvas

    def _photo_for(self, scene):
        """Brand-graded full-frame photo for repo/external assets (cached)."""
        asset = scene["asset"]
        key = ("photo", asset.get("id"))
        if key in self._photo_cache:
            return self._photo_cache[key]
        if asset.get("kind") == "repo":
            path = os.path.join(common.ROOT, asset["path"])
        elif asset.get("kind") == "external":
            path = asset.get("path") or ""
        else:
            return None
        if not path or not os.path.exists(path):
            return None
        try:
            im = Image.open(path).convert("RGB")
            im = im.resize((1296, 2304), Image.LANCZOS)
            graded = Image.fromarray(vp.brand_grade(np.array(im, np.uint8)))
            self._photo_cache[key] = graded
            return graded
        except Exception:
            return None

    def _render_scene_visual(self, fr, scene, ts, sd):
        asset = scene["asset"]
        kind = asset.get("kind")
        if kind in ("repo", "external"):
            photo = self._photo_for(scene)
            if photo is not None:
                self._render_photo(fr, scene, photo, ts, sd)
                return True
            # missing/unreadable photo → deterministic procedural fallback
        self._render_procedural(fr, scene, ts, sd)
        return False

    def _render_photo(self, fr, scene, photo, ts, sd):
        zoom, dx, dy = self._kb_params(scene)
        pr = clamp(ts / max(sd, 0.1))
        s = zoom + 0.05 * pr
        pw, ph = photo.size
        cw, ch = pw / s, ph / s
        cx = pw / 2 + dx * pr * 2
        cy = ph / 2 + dy * pr * 2
        box = (int(cx - cw / 2), int(cy - ch / 2), int(cx + cw / 2), int(cy + ch / 2))
        crop = photo.crop(box).resize((W, H), Image.BILINEAR).convert("RGBA")
        # soft darkening: top for the subtitle band, bottom for the handle
        shade = self._cached("photo_shade", self._photo_shade)
        crop.alpha_composite(shade)
        a = ease(ts / 0.5)
        fr.paste(with_alpha(crop, a), (0, 0))
        # small category chip (part of the composition, never the primary text)
        if ts > 0.5:
            chip = self._chip(scene["visual_category"].replace("-", " ").upper()[:14], 300, 64)
            fr.alpha_composite(with_alpha(chip, ease((ts - 0.5) / 0.4)), (64, ZONE_Y))

    def _photo_shade(self):
        """Warm-black gradient: top keeps the subtitle band readable, the
        bottom keeps the handle zone clear."""
        a = np.zeros((H, W), np.uint8)
        a[:520] = (np.linspace(150, 40, 520)[:, None]).astype(np.uint8)
        a[-360:] = (np.linspace(30, 170, 360)[:, None]).astype(np.uint8)
        rgb = np.zeros((H, W, 3), np.uint8)
        rgb[...] = (10, 8, 6)
        return Image.fromarray(np.dstack([rgb, a]), "RGBA")

    def _render_procedural(self, fr, scene, ts, sd):
        snap = self._scene_snapshot(scene)
        zoom, dx, dy = self._kb_params(scene)
        pr = clamp(ts / max(sd, 0.1))
        s = zoom + 0.05 * pr
        cw, ch = int(snap.width / s), int(snap.height / s)
        cx = snap.width / 2 + dx * pr * 2
        cy = snap.height / 2 + dy * pr * 2
        box = (int(cx - cw / 2), int(cy - ch / 2), int(cx + cw / 2), int(cy + ch / 2))
        crop = snap.crop(box).resize((ZONE_W, ZONE_H), Image.BILINEAR)
        # masked reveal: fade + rise over the first 0.6 s of the scene
        a = ease(ts / 0.6)
        rise = int((1 - a) * 18)
        fr.alpha_composite(with_alpha(crop, a), (0, ZONE_Y + rise))
        # justified code scene: blinking caret at a fixed position (never a
        # moving cursor; only when the narration demonstrates code entry)
        if scene.get("cursor_justified"):
            d = ImageDraw.Draw(fr)
            if (ts * 2) % 1 < 0.55:  # 2 Hz blink
                d.rectangle([296, ZONE_Y + 452, 300, ZONE_Y + 486], fill=GOLD_HI)

    def _render_scene(self, fr, scene, ts, sd):
        if scene["visual_category"] == "brand-mark":
            self.w_hook(fr, ts)
            return
        if scene["visual_category"] == "brand-close":
            self.w_ending(fr, ts)
            return
        self._render_scene_visual(fr, scene, ts, sd)

    # -----------------------------------------------------------------------
    # Procedural compositions (drawn in full state into the scene canvas;
    # motion comes from Ken Burns + masked reveal + crossfade, so per-frame
    # cost stays low and the file size stays small).
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Issue #32 §4-6: every procedural composition now communicates ONE idea
    # in under three seconds. Each comp DECLARES its rendered items (bbox,
    # z-layer, kind) for the deterministic layout gate: a small number of
    # meaningful labels, no random decorative words, no label behind another
    # element, no line through text, no microtext, measurable density.
    # -----------------------------------------------------------------------

    BG_DECL = (16, 13, 10)  # declared zone background for contrast checks

    def _labels_for(self, scene):
        spec = vp.C.get(scene["visual_category"], {})
        sets = spec.get("label_sets") or [["IDEA"]]
        idx = len(scene.get("topic_keywords") or []) % len(sets)
        return sets[idx]

    @staticmethod
    def _item(iid, kind, bbox, z=layout_gate.LAYER_DIAGRAM, **kw):
        it = {"id": iid, "kind": kind,
              "bbox": [int(round(v)) for v in bbox], "z": z}
        it.update(kw)
        return it

    def _measure(self, text, fnt):
        bb = fnt.getbbox(text)
        return bb[2] - bb[0], bb[3] - bb[1]

    def _fit_font(self, text, weight, size, max_w):
        """Largest font size (stepping down by 2) at which `text` fits max_w —
        never below the readable floor."""
        while size > layout_gate.MIN_LABEL_FONT_PX:
            f = font("en", weight, size)
            if self._measure(text, f)[0] <= max_w:
                return f, size
            size -= 2
        return font("en", weight, size), size

    def _topic_word(self, scene, i):
        kws = scene.get("topic_keywords") or []
        return kws[i].upper() if i < len(kws) else ""

    def _draw_centered_label(self, canvas, items, iid, text, fnt, size, color,
                             bg, cx, y, parent=None, group=None):
        """Draw one gold-gradient/flat label centered at cx and declare it."""
        if color == "gold":
            im = gold_text(text, fnt, spacing=1)
        else:
            im = text_img(text, fnt, color)
        x = int(cx - im.width / 2)
        canvas.alpha_composite(im, (x, y))
        items.append(self._item(iid, "text",
                                (x + 10, y + 10, x + im.width - 10, y + im.height - 10),
                                layout_gate.LAYER_LABEL, text=text, font_px=size,
                                color=GOLD_HI if color == "gold" else color, bg=bg,
                                **({"parent": parent} if parent else {}),
                                **({"group": group} if group else {})))

    def _comp_dual(self, canvas, d, scene):
        """One contrast: two cards, one label each, a VS badge — nothing else."""
        labs = self._labels_for(scene)
        lab_a, lab_b = labs[0], labs[1] if len(labs) > 1 else "ACTUAL"
        items = []
        card_w, card_h = 400, 470
        pos = ((60, 160), (620, 160))
        fills = (CARD + (242,), (34, 26, 16, 245))
        outlines = ((233, 180, 74, 150), (255, 228, 158, 220))
        for i, (lab, (cx, cy)) in enumerate(zip((lab_a, lab_b), pos)):
            card = rounded_card(card_w, card_h, 26, fills[i], outlines[i], 2)
            canvas.alpha_composite(card, (cx, cy))
            items.append(self._item(f"card{i}", "card",
                                    (cx, cy, cx + card_w, cy + card_h), group="dual"))
            fnt, size = self._fit_font(lab, 800, 34, card_w - 56)
            self._draw_centered_label(canvas, items, f"label{i}", lab, fnt, size,
                                      "gold" if i else GOLD_HI, CARD,
                                      cx + card_w / 2, cy + 150, parent=f"card{i}")
        vs = gold_text("VS", font("en", 800, 40), spacing=3)
        disc = glow_disc(220, (255, 200, 110, 150), 70, 40)
        canvas.alpha_composite(disc, (540 - 110, 395 - 110))
        canvas.alpha_composite(vs, (540 - vs.width // 2, 395 - vs.height // 2))
        items.append(self._item("vs", "text",
                                (540 - vs.width // 2 + 10, 395 - vs.height // 2 + 10,
                                 540 + vs.width // 2 - 10, 395 + vs.height // 2 - 10),
                                layout_gate.LAYER_LABEL, text="VS", font_px=40,
                                color=GOLD_HI, bg=self.BG_DECL))
        return items

    def _comp_notes(self, canvas, d, scene):
        """One note card: title + ruled lines + the flagged highlight."""
        labs = self._labels_for(scene)
        items = []
        cx0, cy0, cw, ch = 110, 130, 860, 560
        card = rounded_card(cw, ch, 24, CARD + (242,), (233, 180, 74, 160), 2)
        canvas.alpha_composite(card, (cx0, cy0))
        items.append(self._item("card", "card", (cx0, cy0, cx0 + cw, cy0 + ch),
                                group="notes"))
        fnt, size = self._fit_font(labs[0], 800, 36, cw - 88)
        ti = gold_text(labs[0], fnt, spacing=1)
        canvas.alpha_composite(ti, (cx0 + 44, cy0 + 40))
        items.append(self._item("title", "text",
                                (cx0 + 44, cy0 + 40, cx0 + 44 + ti.width, cy0 + 40 + ti.height),
                                layout_gate.LAYER_LABEL, text=labs[0], font_px=size,
                                color=GOLD_HI, bg=CARD, parent="card"))
        for k, lw in enumerate((600, 680, 500)):
            y = cy0 + 210 + k * 62
            d.line([cx0 + 44, y, cx0 + 44 + lw, y], fill=(150, 130, 95, 42), width=6)
            items.append(self._item(f"rule{k}", "decoration",
                                    (cx0 + 44, y - 4, cx0 + 44 + lw, y + 4),
                                    layout_gate.LAYER_DECORATION, opacity=0.16,
                                    parent="card"))
        hl_label = labs[1] if len(labs) > 1 else "ASSUMPTION"
        hlf, hls = self._fit_font(hl_label, 700, 30, 480)
        hw, hh = self._measure(hl_label, hlf)
        hw += 64
        chip = rounded_card(hw, 92, 20, GOLD_HI + (238,), (255, 228, 158, 255), 2)
        canvas.alpha_composite(chip, (cx0 + 44, cy0 + ch - 150))
        items.append(self._item("hl", "chip",
                                (cx0 + 44, cy0 + ch - 150,
                                 cx0 + 44 + hw, cy0 + ch - 58),
                                group="notes", parent="card"))
        d.text((cx0 + 44 + (hw - self._measure(hl_label, hlf)[0]) // 2,
                cy0 + ch - 150 + (92 - hls) // 2 - 4),
               hl_label, font=hlf, fill=INK)
        items.append(self._item("hl_text", "text",
                                (cx0 + 54, cy0 + ch - 140, cx0 + 34 + hw, cy0 + ch - 68),
                                layout_gate.LAYER_LABEL, text=hl_label, font_px=hls,
                                color=INK, bg=GOLD_HI, parent="hl"))
        return items

    def _comp_ladder(self, canvas, d, scene):
        """One vertical chain: 3-4 step chips joined by arrows."""
        labs = self._labels_for(scene)
        n = min(len(labs), 4)
        if n < 3:
            labs = list(labs) + ["NEXT", "CHECK", "DECIDE"]
            n = min(len(labs), 3)
        labs = labs[:n]
        items = []
        chip_w, chip_h, gap = 380, 76, 116
        total = n * chip_h + (n - 1) * gap
        y0 = (ZONE_H - total) // 2
        x0 = 540 - chip_w // 2
        for i, lab in enumerate(labs):
            y = y0 + i * (chip_h + gap)
            chip = self._chip(lab, chip_w, chip_h)
            canvas.alpha_composite(chip, (x0, y))
            items.append(self._item(f"chip{i}", "chip",
                                    (x0, y, x0 + chip.width, y + chip_h), group="chain"))
            t1 = gold_text(lab, font("en", 800, 30), spacing=2)
            items.append(self._item(f"chip{i}_text", "text",
                                    (x0 + (chip.width - t1.width) // 2,
                                     y + (chip_h - t1.height) // 2,
                                     x0 + (chip.width + t1.width) // 2,
                                     y + (chip_h + t1.height) // 2),
                                    layout_gate.LAYER_LABEL, text=lab, font_px=30,
                                    color=GOLD_HI, bg=(26, 20, 13), parent=f"chip{i}",
                                    group="chain"))
            if i < n - 1:
                ay0 = y + chip_h + 10
                ay1 = y + chip_h + gap - 12
                d.line([540, ay0, 540, ay1], fill=(233, 180, 74, 210), width=4)
                d.polygon([(540, ay1 + 12), (540 - 13, ay1 - 6), (540 + 13, ay1 - 6)],
                          fill=GOLD)
                items.append(self._item(f"arrow{i}", "line",
                                        (527, ay0, 553, ay1 + 12),
                                        layout_gate.LAYER_DIAGRAM, group="chain",
                                        endpoints=[(540, ay0), (540, ay1 + 12)]))
        return items

    def _comp_funnel(self, canvas, d, scene):
        """One funnel: three narrowing bars, one label each."""
        labs = list(self._labels_for(scene))[:3]
        while len(labs) < 3:
            labs.append("DECISION")
        items = []
        widths = (860, 640, 420)
        ys = (140, 372, 604)
        for i, (wbar, y) in enumerate(zip(widths, ys)):
            hot = (i == 2)
            col = (233, 180, 74, 240) if hot else (150, 106, 44, 215)
            x0 = 540 - wbar // 2
            d.rounded_rectangle([x0, y, x0 + wbar, y + 84], 16, fill=col)
            items.append(self._item(f"bar{i}", "bar",
                                    (x0, y, x0 + wbar, y + 84), group="funnel"))
            fnt, size = self._fit_font(labs[i], 800, 30, wbar - 60)
            tw, th = self._measure(labs[i], fnt)
            d.text((540 - tw // 2, y + (84 - th) // 2 - 6), labs[i],
                   font=fnt, fill=INK if hot else WARM)
            items.append(self._item(f"bar{i}_text", "text",
                                    (540 - tw // 2, y + (84 - th) // 2 - 6,
                                     540 + tw // 2, y + (84 + th) // 2 - 6),
                                    layout_gate.LAYER_LABEL, text=labs[i], font_px=size,
                                    color=INK if hot else WARM,
                                    bg=(233, 180, 74) if hot else (150, 106, 44),
                                    parent=f"bar{i}"))
        return items

    def _comp_matrix(self, canvas, d, scene):
        """One 2x2 quadrant map: four chips, two faint axes — no scatter noise."""
        labs = list(self._labels_for(scene))[:4]
        while len(labs) < 4:
            labs += ["COST", "VALUE", "RISK", "NOW?"]
        labs = labs[:4]
        items = []
        d.line([540, 130, 540, 710], fill=(233, 180, 74, 46), width=2)
        items.append(self._item("axis_v", "decoration", (538, 130, 542, 710),
                                layout_gate.LAYER_DECORATION, opacity=0.18, group="matrix"))
        d.line([150, 420, 930, 420], fill=(233, 180, 74, 46), width=2)
        items.append(self._item("axis_h", "decoration", (150, 418, 930, 422),
                                layout_gate.LAYER_DECORATION, opacity=0.18, group="matrix"))
        centers = ((320, 250), (760, 250), (320, 590), (760, 590))
        for i, ((cx, cy), lab) in enumerate(zip(centers, labs)):
            chip = self._chip(lab, 300, 68)
            canvas.alpha_composite(chip, (cx - chip.width // 2, cy - 34))
            items.append(self._item(f"quad{i}", "chip",
                                    (cx - chip.width // 2, cy - 34,
                                     cx + chip.width // 2, cy + 34), group="matrix"))
            t1 = gold_text(lab, font("en", 800, 26), spacing=2)
            items.append(self._item(f"quad{i}_text", "text",
                                    (cx - t1.width // 2, cy - t1.height // 2,
                                     cx + t1.width // 2, cy + t1.height // 2),
                                    layout_gate.LAYER_LABEL, text=lab, font_px=26,
                                    color=GOLD_HI, bg=(26, 20, 13),
                                    parent=f"quad{i}", group="matrix"))
        return items

    def _comp_gauges(self, canvas, d, scene):
        """Focus before/after: two dials + labels, one THE GAP verdict."""
        labs = self._labels_for(scene)
        lab_a, lab_b = labs[0], labs[1] if len(labs) > 1 else "DISTRACTED"
        items = []
        self._gauge(d, 300, 320, 170, 0.9, True)
        self._gauge(d, 780, 320, 170, 0.32, False)
        items.append(self._item("gauge_a", "image", (130, 150, 470, 490),
                                layout_gate.LAYER_PRIMARY, group="gauges"))
        items.append(self._item("gauge_b", "image", (610, 150, 950, 490),
                                layout_gate.LAYER_PRIMARY, group="gauges"))
        for iid, lab, cx in (("label_a", lab_a, 300), ("label_b", lab_b, 780)):
            fnt, size = self._fit_font(lab, 800, 30, 320)
            self._draw_centered_label(canvas, items, iid, lab, fnt, size,
                                      "gold" if cx == 300 else DIM, self.BG_DECL,
                                      cx, 545)
        gapf = font("en", 800, 36)
        self._draw_centered_label(canvas, items, "gap", "THE GAP", gapf, 36,
                                  "gold", self.BG_DECL, 540, 690)
        return items

    def _gauge(self, d, cx, cy, r, val, hot):
        a0, sweep = 135, 270
        d.arc([cx - r, cy - r, cx + r, cy + r], a0, a0 + sweep, fill=(70, 60, 45), width=14)
        if val > 0.01:
            d.arc([cx - r, cy - r, cx + r, cy + r], a0, a0 + sweep * clamp(val), fill=GOLD if hot else (150, 132, 100), width=14)
        a = math.radians(a0 + sweep * clamp(val))
        d.line([cx, cy, cx + math.cos(a) * (r - 34), cy + math.sin(a) * (r - 34)], fill=GOLD_HI if hot else (200, 180, 140), width=5)
        d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=GOLD_HI if hot else (200, 180, 140))

    def _comp_bars(self, canvas, d, scene):
        """Task switch / attention residue: three labeled horizontal bars.
        The label always sits ABOVE its bar — never behind or inside another
        element — so the hierarchy is obvious at a glance."""
        labs = list(self._labels_for(scene))[:3]
        while len(labs) < 3:
            labs.append("TASK")
        items = []
        widths = (760, 430, 610)
        fills = ((233, 180, 74, 240), (150, 106, 44, 220), (96, 78, 52, 225))
        for i, (y_top, wbar) in enumerate(zip((150, 390, 630), widths)):
            fnt, size = self._fit_font(labs[i], 700, 28, 820)
            tw, th = self._measure(labs[i], fnt)
            d.text((120, y_top - 6), labs[i], font=fnt, fill=WARM)
            items.append(self._item(f"row{i}_label", "text",
                                    (120, y_top - 6, 120 + tw, y_top - 6 + th),
                                    layout_gate.LAYER_LABEL, text=labs[i],
                                    font_px=size, color=WARM, bg=self.BG_DECL))
            yb = y_top + 56
            d.rounded_rectangle([120, yb, 120 + wbar, yb + 64], 12, fill=fills[i])
            items.append(self._item(f"row{i}_bar", "bar",
                                    (120, yb, 120 + wbar, yb + 64), group="bars"))
        return items

    def _comp_graph(self, canvas, d, scene):
        """Prediction vs actual: one card, two lines, two legend chips."""
        labs = self._labels_for(scene)
        lab_a = labs[0] if labs else "PREDICTION"
        lab_b = labs[1] if len(labs) > 1 else "ACTUAL"
        items = []
        cx0, cy0, cw, ch = 90, 150, 900, 560
        card = rounded_card(cw, ch, 24, CARD + (238,), (233, 180, 74, 120), 2)
        canvas.alpha_composite(card, (cx0, cy0))
        items.append(self._item("card", "card", (cx0, cy0, cx0 + cw, cy0 + ch),
                                group="graph"))
        ox, oy = cx0 + 80, cy0 + 480
        d.line([ox, oy, ox + 740, oy], fill=(140, 120, 90, 46), width=3)
        d.line([ox, oy, ox, cy0 + 100], fill=(140, 120, 90, 46), width=3)
        items.append(self._item("axes", "decoration",
                                (ox, cy0 + 100, ox + 740, oy + 3),
                                layout_gate.LAYER_DECORATION, opacity=0.18,
                                parent="card", group="graph"))
        pred = [(ox + 40 + i * 92, oy - 30 - i * 40 - 20 * math.sin(i * 0.8)) for i in range(8)]
        actual = [(ox + 40 + i * 92, oy - 20 - i * 26 - 12 * math.cos(i * 0.6)) for i in range(8)]
        d.line(pred, fill=(180, 160, 130, 220), width=3)
        d.line(actual, fill=GOLD, width=5)
        items.append(self._item("line_pred", "line",
                                (min(x for x, _ in pred), min(y for _, y in pred),
                                 max(x for x, _ in pred), max(y for _, y in pred)),
                                layout_gate.LAYER_DIAGRAM, group="graph",
                                endpoints=[pred[0], pred[-1]]))
        items.append(self._item("line_actual", "line",
                                (min(x for x, _ in actual), min(y for _, y in actual),
                                 max(x for x, _ in actual), max(y for _, y in actual)),
                                layout_gate.LAYER_DIAGRAM, group="graph",
                                endpoints=[actual[0], actual[-1]]))
        for i, (lab, lx) in enumerate(((lab_a, 500), (lab_b, 730))):
            chip = self._chip(lab, 200, 54)
            canvas.alpha_composite(chip, (lx, cy0 + 40))
            items.append(self._item(f"legend{i}", "chip",
                                    (lx, cy0 + 40, lx + chip.width, cy0 + 94),
                                    parent="card", group="graph"))
            t1 = gold_text(lab, font("en", 800, 24), spacing=1)
            items.append(self._item(f"legend{i}_text", "text",
                                    (lx + (chip.width - t1.width) // 2,
                                     cy0 + 40 + (54 - t1.height) // 2,
                                     lx + (chip.width + t1.width) // 2,
                                     cy0 + 40 + (54 + t1.height) // 2),
                                    layout_gate.LAYER_LABEL, text=lab, font_px=24,
                                    color=GOLD_HI, bg=(26, 20, 13),
                                    parent=f"legend{i}", group="graph"))
        return items

    def _comp_loop(self, canvas, d, scene):
        """The loop: 3-4 step chips on a ring with direction arrows, and the
        eye kept ONLY as a low-opacity accent in the empty center."""
        labs = list(self._labels_for(scene))[:4]
        while len(labs) < 3:
            labs.append("REPEAT")
        n = len(labs)
        items = []
        cx, cy, rx, ry = 540, 430, 350, 285
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(233, 180, 74, 46), width=3)
        items.append(self._item("ring", "decoration",
                                (cx - rx, cy - ry, cx + rx, cy + ry),
                                layout_gate.LAYER_DECORATION, opacity=0.18, group="loop"))
        pts = []
        for i in range(n):
            a = -math.pi / 2 + 2 * math.pi * i / n
            pts.append((cx + math.cos(a) * rx, cy + math.sin(a) * ry))
        for i in range(n):
            # direction arrowhead on the ring between this chip and the next
            a_mid = -math.pi / 2 + 2 * math.pi * (i + 0.5) / n
            mx, my = cx + math.cos(a_mid) * rx, cy + math.sin(a_mid) * ry
            tang = a_mid + math.pi / 2
            tip = (mx + math.cos(tang) * 14, my + math.sin(tang) * 14)
            b1 = (mx + math.cos(tang + 2.6) * 12, my + math.sin(tang + 2.6) * 12)
            b2 = (mx + math.cos(tang - 2.6) * 12, my + math.sin(tang - 2.6) * 12)
            d.polygon([tip, b1, b2], fill=(233, 180, 74, 200))
            items.append(self._item(f"dir{i}", "decoration",
                                    (mx - 16, my - 16, mx + 16, my + 16),
                                    layout_gate.LAYER_DECORATION, opacity=0.78,
                                    group="loop"))
        for i, ((x, y), lab) in enumerate(zip(pts, labs)):
            chip = self._chip(lab, 260, 68)
            canvas.alpha_composite(chip, (int(x - chip.width / 2), int(y - 34)))
            items.append(self._item(f"step{i}", "chip",
                                    (int(x - chip.width / 2), int(y - 34),
                                     int(x + chip.width / 2), int(y + 34)), group="loop"))
            t1 = gold_text(lab, font("en", 800, 28), spacing=2)
            items.append(self._item(f"step{i}_text", "text",
                                    (int(x - t1.width / 2), int(y - t1.height / 2),
                                     int(x + t1.width / 2), int(y + t1.height / 2)),
                                    layout_gate.LAYER_LABEL, text=lab, font_px=28,
                                    color=GOLD_HI, bg=(26, 20, 13),
                                    parent=f"step{i}", group="loop"))
        eye = self.eye.resize((110, 110), Image.LANCZOS)
        canvas.alpha_composite(with_alpha(eye, 0.30), (cx - 55, cy - 55))
        items.append(self._item("eye_accent", "emblem",
                                (cx - 55, cy - 55, cx + 55, cy + 55),
                                layout_gate.LAYER_DECORATION + 5, opacity=0.30,
                                group="loop"))
        return items

    def _comp_web(self, canvas, d, scene):
        """Concept link: three chips in a clear left-to-right chain — no dense
        network, no crossing lines."""
        labs = list(self._labels_for(scene))[:3]
        while len(labs) < 3:
            labs.append("LINK")
        items = []
        xs = (90, 410, 730)
        cy = 420
        for i, (x, lab) in enumerate(zip(xs, labs)):
            chip = self._chip(lab, 240, 64)
            canvas.alpha_composite(chip, (x, cy - 32))
            items.append(self._item(f"node{i}", "chip",
                                    (x, cy - 32, x + chip.width, cy + 32), group="chain"))
            t1 = gold_text(lab, font("en", 800, 26), spacing=2)
            items.append(self._item(f"node{i}_text", "text",
                                    (x + (chip.width - t1.width) // 2, cy - t1.height // 2,
                                     x + (chip.width + t1.width) // 2, cy + t1.height // 2),
                                    layout_gate.LAYER_LABEL, text=lab, font_px=26,
                                    color=GOLD_HI, bg=(26, 20, 13),
                                    parent=f"node{i}", group="chain"))
            if i < 2:
                x0 = x + 240 + 10
                x1 = xs[i + 1] - 10
                d.line([x0, cy, x1 - 14, cy], fill=(233, 180, 74, 210), width=4)
                d.polygon([(x1, cy), (x1 - 18, cy - 10), (x1 - 18, cy + 10)], fill=GOLD)
                items.append(self._item(f"link{i}", "line", (x0, cy - 10, x1, cy + 10),
                                        layout_gate.LAYER_DIAGRAM, group="chain",
                                        endpoints=[(x0, cy), (x1, cy)]))
        return items

    def _comp_notifications(self, canvas, d, scene):
        """Notification → attention shift: a phone with two pings, one arrow,
        one dropping focus bar."""
        labs = list(self._labels_for(scene))[:3]
        while len(labs) < 3:
            labs.append("FOCUS LOST")
        items = []
        ph = rounded_card(330, 640, 40, (18, 15, 11, 245), (233, 180, 74, 140), 3)
        canvas.alpha_composite(ph, (110, 100))
        items.append(self._item("phone", "card", (110, 100, 440, 740), group="notif"))
        for i, y in enumerate((210, 360)):
            card = rounded_card(270, 100, 16, (40, 32, 22, 245), (255, 228, 158, 180), 2)
            canvas.alpha_composite(card, (140, y))
            items.append(self._item(f"ping{i}", "chip", (140, y, 410, y + 100),
                                    parent="phone", group="notif"))
            fnt, size = self._fit_font(labs[i], 800, 26, 230)
            tw, th = self._measure(labs[i], fnt)
            d.text((140 + (270 - tw) // 2, y + (100 - th) // 2 - 4),
                   labs[i], font=fnt, fill=GOLD_HI)
            items.append(self._item(f"ping{i}_text", "text",
                                    (140 + (270 - tw) // 2, y + (100 - th) // 2 - 4,
                                     140 + (270 + tw) // 2, y + (100 + th) // 2 - 4),
                                    layout_gate.LAYER_LABEL, text=labs[i], font_px=size,
                                    color=GOLD_HI, bg=(40, 32, 22), parent=f"ping{i}",
                                    group="notif"))
        d.line([460, 420, 580, 420], fill=(255, 228, 158, 220), width=4)
        d.polygon([(596, 420), (576, 408), (576, 432)], fill=GOLD_HI)
        items.append(self._item("shift", "line", (460, 408, 596, 432),
                                layout_gate.LAYER_DIAGRAM, group="notif",
                                endpoints=[(460, 420), (596, 420)]))
        d.line([620, 420, 1040, 420], fill=(60, 50, 36, 255), width=14)
        d.line([620, 420, 760, 420], fill=GOLD, width=14)
        items.append(self._item("focus_bar", "bar", (612, 410, 1048, 430), group="notif"))
        fnt, size = self._fit_font(labs[2], 800, 28, 420)
        tw, th = self._measure(labs[2], fnt)
        d.text((620, 480), labs[2], font=fnt, fill=GOLD_HI)
        items.append(self._item("focus_label", "text",
                                (620, 480, 620 + tw, 480 + th),
                                layout_gate.LAYER_LABEL, text=labs[2], font_px=size,
                                color=GOLD_HI, bg=self.BG_DECL))
        return items

    def _comp_code(self, canvas, d, scene):
        """Justified code scene: curated, syntax-coherent snippet only.
        Warm palette (gold/ivory/bronze) — no cold syntax colors. The snippet
        is ONE semantic visual (declared as a single image item)."""
        lines = scene.get("code_lines") or vp.CODE_SNIPPETS["debug"]
        items = []
        w, h = 920, 620
        card = rounded_card(w, h, 20, CARD + (242,), (233, 180, 74, 140), 2)
        d2 = ImageDraw.Draw(card)
        for i, col in enumerate(((233, 180, 74, 220), (150, 106, 44, 220), (90, 74, 50, 220))):
            d2.ellipse([30 + i * 34, 26, 50 + i * 34, 46], fill=col)
        f = font("en", 500, 26)
        y = 90
        for ln in lines[:8]:
            if ln.startswith("#"):
                col = (168, 148, 116)
            elif ln.strip().startswith(("def ", "class ", "return")):
                col = GOLD_HI
            elif ":" in ln:
                col = AMBER
            else:
                col = WARM
            d2.text((44, y), ln, font=f, fill=col)
            y += 56
        canvas.alpha_composite(card, (80, 110))
        items.append(self._item("code_card", "card", (80, 110, 80 + w, 110 + h),
                                group="code"))
        items.append(self._item("snippet", "image", (80 + 44, 110 + 84, 80 + w - 40, 110 + y),
                                layout_gate.LAYER_PRIMARY, parent="code_card", group="code"))
        purpose = text_norm.normalize_text(scene.get("visual_purpose", "").upper())[:28]
        pf = font("en", 700, 22)
        pw, phh = self._measure(purpose, pf)
        d2.text((44, h - 52), purpose, font=pf, fill=WARM_DIM)
        items.append(self._item("purpose", "text",
                                (80 + 44, 110 + h - 52, 80 + 44 + pw, 110 + h - 52 + phh),
                                layout_gate.LAYER_LABEL, text=purpose, font_px=22,
                                color=WARM_DIM, bg=CARD, parent="code_card"))
        return items

    # -----------------------------------------------------------------------
    # Legacy beat widgets — ONLY for pre-plan test fixtures (scripts without
    # chunks). They never draw a code card and never draw a moving cursor.
    # -----------------------------------------------------------------------

    def widget(self, fr, t, beat, ts, sd):
        fn = {"hook": self._legacy_hook, "problem": self._legacy_problem, "explain": self._legacy_explain,
              "example": self._legacy_example, "technique": self._legacy_technique,
              "ending": self._legacy_ending}.get(beat)
        if fn:
            fn(fr, t, ts, sd)

    def w_hook(self, fr, ts):
        self._legacy_hook(fr, 0, ts, 1)

    def w_ending(self, fr, ts):
        self._legacy_ending(fr, 0, ts, 1)

    def _legacy_hook(self, fr, t, ts, sd):
        g = self._cached("hookglow", lambda: glow_disc(1000, (255, 186, 90, 190), 380, 140))
        pulse = 0.72 + 0.28 * math.sin(ts * 2.2)
        fr.alpha_composite(with_alpha(g, pulse), (540 - 500, 930 - 500))
        s = ease_back(ts / 0.7)
        size = int(640 * s)
        if size > 4:
            em = self.emblem.resize((size, size), Image.LANCZOS)
            fr.alpha_composite(em, (540 - size // 2, 930 - size // 2))

        # Partial-alpha decorations: floating particles drawn on transparent overlay
        p_overlay = Image.new("RGBA", fr.size, (0, 0, 0, 0))
        d_p = ImageDraw.Draw(p_overlay)
        for i in range(22):
            a = ts * (0.5 + 0.11 * (i % 5)) + i * 2.399
            rx, ry = 360 + 26 * math.sin(i), 330 + 22 * math.cos(i * 2)
            x, y = 540 + math.cos(a) * rx, 930 + math.sin(a) * ry
            dep = 0.55 + 0.45 * math.sin(a)
            r = 2 + 3 * dep
            d_p.ellipse([x - r, y - r, x + r, y + r], fill=(255, 214, 130, int(190 * dep)))
        fr.alpha_composite(p_overlay)

        # Partial-alpha decorations: expanding gold rings drawn on transparent overlay
        # and composited source-over onto the destination frame
        for k in range(2):
            pt = (ts * 0.55 + k * 0.5) % 1.0
            rr = 300 + pt * 260
            alpha = int(120 * (1 - pt))
            if alpha > 0:
                ring_overlay = Image.new("RGBA", fr.size, (0, 0, 0, 0))
                d_ring = ImageDraw.Draw(ring_overlay)
                d_ring.ellipse([540 - rr, 930 - rr * 0.94, 540 + rr, 930 + rr * 0.94],
                               outline=(233, 180, 74, alpha), width=3)
                fr.alpha_composite(ring_overlay)

    def _legacy_problem(self, fr, t, ts, sd):
        d = ImageDraw.Draw(fr)
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

    def _legacy_explain(self, fr, t, ts, sd):
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

    def _legacy_example(self, fr, t, ts, sd):
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

    def _legacy_technique(self, fr, t, ts, sd):
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

    def _legacy_ending(self, fr, t, ts, sd):
        cx, cy, rx, ry = 540, 1120, 330, 105
        # Orbit line with partial alpha composited source-over
        orbit_overlay = Image.new("RGBA", fr.size, (0, 0, 0, 0))
        d_orbit = ImageDraw.Draw(orbit_overlay)
        for i in range(90):
            a0, a1 = 2 * math.pi * i / 90, 2 * math.pi * (i + 1) / 90
            dep = (math.sin(a0) + math.sin(a1)) / 2
            d_orbit.line([cx + math.cos(a0) * rx, cy + math.sin(a0) * ry, cx + math.cos(a1) * rx, cy + math.sin(a1) * ry],
                         fill=(233, 180, 74, int(70 + 130 * (dep * 0.5 + 0.5))), width=int(4 + 6 * (dep * 0.5 + 0.5)))
        fr.alpha_composite(orbit_overlay)

        d = ImageDraw.Draw(fr)
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

        # Orbiting particles with partial alpha composited source-over
        ang = -math.pi / 2 + ts * 1.05
        tail_overlay = Image.new("RGBA", fr.size, (0, 0, 0, 0))
        d_tail = ImageDraw.Draw(tail_overlay)
        for k in range(20):
            aa = ang - k * 0.055
            x, y = cx + math.cos(aa) * rx, cy + math.sin(aa) * ry
            r = 8 * (1 - k / 24)
            d_tail.ellipse([x - r, y - r, x + r, y + r], fill=(255, 226, 150, int(230 * (1 - k / 20))))
        fr.alpha_composite(tail_overlay)

        eye = self._cached("eye_mid", lambda: self.eye.resize((150, 150), Image.LANCZOS))
        fr.alpha_composite(eye, (540 - 75, cy - 75))

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
        fr = self.bg_crop("base", t, t0, sd, punch).convert("RGBA")
        scene, sts, s_d = self._scene_at(t) if self.plan else (None, ts, sd)
        is_photo = False
        if scene is not None:
            is_photo = scene["asset"].get("kind") in ("repo", "external") \
                and self._photo_for(scene) is not None
        if not is_photo:
            if self.plan is None:
                # pre-plan test fixtures keep the legacy background treatment
                self.draw_lattice(fr, t, 0.35 if beat == "hook" else 1.0)
                if beat != "hook":
                    self.draw_web(fr, t, beat)
            else:
                # Issue #32 §4: plan-driven scenes carry one dominant semantic
                # visual — no dense generic network behind them, just a subtle
                # low-opacity brand accent kept clear of every text zone.
                self.draw_brand_accent(fr, t)
        if scene is not None:
            self._render_scene(fr, scene, sts, s_d)
            # short crossfade from the previous frame at scene starts
            if self._last_frame is not None and sts < 0.4 and not self.safe:
                fr = Image.blend(self._last_frame.convert("RGBA"), fr, ease(sts / 0.4))
        else:
            self.widget(fr, t, beat, ts, sd)
        if is_photo and self.plan is None:
            self.draw_lattice(fr, t, 0.3)
        self.draw_captions(fr, t)
        self.draw_chrome(fr, t, beat, t0)
        if flash > 0:
            fr.alpha_composite(with_alpha(self.flash, 0.3 * flash))
        out = fr.convert("RGB")
        self._last_frame = out
        return out

    def background_only(self, t):
        si = self.beat_at(t)
        beat, t0 = self.cuts[si][1], self.cut_t[si]
        sd = (self.cut_t[si + 1] if si + 1 < len(self.cut_t) else self.total) - t0
        fr = self.bg_crop("base", t, t0, sd).convert("RGBA")
        scene, sts, s_d = self._scene_at(t) if self.plan else (None, t - t0, sd)
        if scene is not None and scene["asset"].get("kind") in ("repo", "external") \
                and self._photo_for(scene) is not None:
            self._render_photo(fr, scene, self._photo_for(scene), max(sts, 0.5), max(s_d, 1.0))
        else:
            if self.plan is None:
                self.draw_lattice(fr, t)
                if beat != "hook":
                    self.draw_web(fr, t, beat)
            else:
                self.draw_brand_accent(fr, t)
            if scene is not None:
                self._render_scene(fr, scene, max(sts, 0.6), max(s_d, 1.0))
            else:
                self.widget(fr, t, beat, t - t0, sd)
        return fr.convert("RGB")
