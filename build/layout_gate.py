"""Deterministic layout collision + visual-density gate for rendered scenes.

Issue #32 (run 35488263806, reel-2026-09-24) was rejected by human review
because procedural scenes were visually cluttered: tiny labels behind
foreground bars, random decorative words (DRAFT, SWITCH, NOTE, COUNT) under
other elements, network lines crossing semantic cards, several unrelated
elements competing in one scene.

Every rendered item now DECLARES a bounding box, a z-layer and a kind:

    {"id": str, "kind": one of KINDS, "bbox": [x0, y0, x1, y1],
     "z": int layer, "group": optional str, "parent": optional item id,
     "text": optional str, "font_px": optional int,
     "color": optional (r,g,b), "bg": optional (r,g,b),
     "opacity": optional float 0..1, "endpoints": optional [(x,y),(x,y)]}

check_items() blocks BEFORE a frame is rendered:
  * intersecting text boxes;
  * text hidden by a higher foreground element;
  * labels intersecting chart bars/cards they do not belong to;
  * diagram lines crossing semantic text (except their own endpoint labels);
  * labels below the minimum readable size;
  * too many foreground components / too many text labels in one scene;
  * foreground occupying too much of the composition area;
  * insufficient whitespace margin around the dominant visual;
  * insufficient contrast between a text item and its background;
  * elements entering the subtitle or handle safe zones (frame space);
  * decorative background may intersect text ONLY below the documented
    opacity limit — and never as semantic foreground.

The gate is a pure function of the declared items: no LLM, no approval flag.
The renderer runs it before drawing (fail closed); the final QA re-runs it on
the declared items recorded in layout.json, and compares against sampled
decoded frames.
"""

# --- z layers (single ordering for the whole frame) -------------------------
LAYER_BACKGROUND = 0      # marble, photo shade, brand accent
LAYER_DECORATION = 10     # low-opacity brand motif (never semantic)
LAYER_DIAGRAM = 20        # bars, cards, diagram lines, chips
LAYER_PRIMARY = 30        # the dominant semantic visual
LAYER_LABEL = 40          # semantic text labels
LAYER_SUBTITLE = 50       # karaoke subtitle scrim + text
LAYER_CHROME = 60         # tag, handle, watermark, progress

# --- item kinds ---------------------------------------------------------------
KINDS = ("text", "card", "bar", "chip", "line", "decoration", "image",
         "emblem", "arrow", "subtitle", "handle", "tag", "progress")
TEXT_KINDS = ("text", "subtitle", "handle", "tag")
FOREGROUND_KINDS = ("card", "bar", "chip", "image", "emblem", "arrow")

# --- measurable scene-density limits (design space 1080x840) ------------------
MAX_FOREGROUND_COMPONENTS = 8     # meaningful foreground objects per scene
MAX_TEXT_LABELS = 4               # semantic text labels per scene
MIN_LABEL_FONT_PX = 22            # below this a label is unreadable at 9:16
MIN_MICROTEXT_FONT_PX = 18        # anything below is never allowed
MAX_FOREGROUND_AREA_FRACTION = 0.62   # union of foreground boxes / zone area
MIN_DOMINANT_MARGIN_PX = 24       # whitespace around the dominant visual
MIN_UNRELATED_SEPARATION_PX = 14  # between unrelated foreground elements
DECOR_MAX_ALPHA = 0.20            # decoration may sit behind text only under this
MIN_TEXT_CONTRAST = 3.0           # WCAG-style contrast for large labels

ZONE_W, ZONE_H = 1080, 840


def _rect(b):
    return (float(b[0]), float(b[1]), float(b[2]), float(b[3]))


def intersects(a, b, pad=0.0):
    ax0, ay0, ax1, ay1 = _rect(a)
    bx0, by0, bx1, by1 = _rect(b)
    return not (ax1 - pad <= bx0 or bx1 - pad <= ax0 or
                ay1 - pad <= by0 or by1 - pad <= ay0)


def inside(inner, outer, pad=0.0):
    return (inner[0] >= outer[0] - pad and inner[1] >= outer[1] - pad and
            inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad)


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _union_area(boxes):
    """Coarse union area on a 4px raster grid (deterministic, cheap)."""
    if not boxes:
        return 0.0
    cell = 4
    x0 = int(min(b[0] for b in boxes) // cell)
    y0 = int(min(b[1] for b in boxes) // cell)
    cells = set()
    for b in boxes:
        for gx in range(int(b[0] // cell), int((b[2] + cell - 1) // cell) + 1):
            for gy in range(int(b[1] // cell), int((b[3] + cell - 1) // cell) + 1):
                cells.add((gx, gy))
    return len(cells) * cell * cell


def _rel_luminance(rgb):
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(fg, bg):
    l1, l2 = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _segment_hits_box(p0, p1, box, pad=0.0):
    """Conservative segment↔rect test by dense sampling along the segment."""
    x0, y0, x1, y1 = _rect(box)
    x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
    steps = max(8, int(max(abs(p1[0] - p0[0]), abs(p1[1] - p0[1])) / 6))
    for k in range(steps + 1):
        u = k / steps
        x = p0[0] + (p1[0] - p0[0]) * u
        y = p0[1] + (p1[1] - p0[1]) * u
        if x0 <= x <= x1 and y0 <= y <= y1:
            return True
    return False


def check_items(items, zone_w=ZONE_W, zone_h=ZONE_H, safe_boxes=()):
    """Deterministic collision + density gate on declared items.

    `safe_boxes` are frame-space rectangles (subtitle band, handle band) that
    NO non-chrome/non-subtitle item may enter; items in zone space are passed
    as-is (compositions live in the 1080x840 design space, which the renderer
    keeps clear of both bands by construction — the renderer additionally
    checks the frame-space placement).
    Returns a list of violation strings; empty = the scene may render.
    """
    issues = []
    items = [it for it in (items or []) if it]
    by_id = {it.get("id"): it for it in items if it.get("id")}
    texts = [it for it in items if it.get("kind") in TEXT_KINDS]
    foreground = [it for it in items if it.get("kind") in FOREGROUND_KINDS]
    decorations = [it for it in items if it.get("kind") == "decoration"]
    lines = [it for it in items if it.get("kind") in ("line", "arrow")]

    # ---- item sanity ------------------------------------------------------
    for it in items:
        b = it.get("bbox")
        if not (isinstance(b, (list, tuple)) and len(b) == 4 and
                b[2] > b[0] and b[3] > b[1]):
            issues.append(f"item {it.get('id', '?')}: malformed bbox {b!r}")
        if it.get("kind") not in KINDS:
            issues.append(f"item {it.get('id', '?')}: unknown kind {it.get('kind')!r}")

    # ---- text vs text: never intersect -------------------------------------
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            if intersects(a["bbox"], b["bbox"], pad=-2):
                issues.append(f"text overlap: {a.get('id', '?')!r} ({a.get('text', '')[:24]!r}) "
                              f"intersects {b.get('id', '?')!r} ({b.get('text', '')[:24]!r})")

    # ---- text vs foreground: contained by parent or fully clear ------------
    def _ancestors(iid):
        """Full parent chain of an item id (handles text→chip→card nesting)."""
        seen = set()
        cur = iid
        while cur and cur not in seen:
            seen.add(cur)
            cur = by_id.get(cur, {}).get("parent")
        return seen

    for t in texts:
        parent = by_id.get(t.get("parent"))
        anc = _ancestors(t.get("parent"))
        for f in foreground:
            if f.get("id") in anc:
                # the text legitimately sits inside this element (direct
                # parent or any ancestor, e.g. label → chip → phone)
                if parent is not None and f.get("id") == parent.get("id") \
                        and not inside(t["bbox"], f["bbox"], pad=6):
                    issues.append(f"label {t.get('id', '?')!r} ({t.get('text', '')[:24]!r}) "
                                  f"is not fully inside its parent {f.get('id', '?')!r} — "
                                  "text crossing a card/bar boundary")
                continue
            if intersects(t["bbox"], f["bbox"], pad=-2):
                issues.append(f"label {t.get('id', '?')!r} ({t.get('text', '')[:24]!r}) "
                              f"intersects foreground {f.get('kind')} {f.get('id', '?')!r} — "
                              "text hidden behind / crossing another element")

    # ---- lines: never through semantic text; only toward own-group ends ----
    for ln in lines:
        eps = ln.get("endpoints") or [ (ln["bbox"][0], (ln["bbox"][1] + ln["bbox"][3]) / 2),
                                       (ln["bbox"][2], (ln["bbox"][1] + ln["bbox"][3]) / 2) ]
        for t in texts:
            if not _segment_hits_box(eps[0], eps[1], t["bbox"], pad=2):
                continue
            # allowed only when the text is this line's own endpoint label
            if t.get("group") and t.get("group") == ln.get("group"):
                near_end = any(abs(t["bbox"][0] + t["bbox"][2]) / 2 - e[0] < 90 and
                               abs(t["bbox"][1] + t["bbox"][3]) / 2 - e[1] < 90
                               for e in eps)
                if near_end:
                    continue
            issues.append(f"line {ln.get('id', '?')!r} crosses text "
                          f"{t.get('id', '?')!r} ({t.get('text', '')[:24]!r})")
        # lines must not cut through unrelated foreground shapes either
        for f in foreground:
            if f.get("group") and f.get("group") == ln.get("group"):
                continue
            if f.get("kind") in ("card",) and _segment_hits_box(eps[0], eps[1], f["bbox"], pad=-6):
                issues.append(f"line {ln.get('id', '?')!r} crosses card {f.get('id', '?')!r}")

    # ---- decoration: may intersect text ONLY under the documented opacity --
    for dec in decorations:
        op = float(dec.get("opacity", 1.0))
        for t in texts:
            if intersects(dec["bbox"], t["bbox"]) and op > DECOR_MAX_ALPHA:
                issues.append(f"decoration {dec.get('id', '?')!r} (opacity {op:.2f}) "
                              f"crosses text {t.get('id', '?')!r} — decoration may touch "
                              f"text only below the documented opacity {DECOR_MAX_ALPHA} "
                              "and never through glyph interiors")

    # ---- readability: minimum sizes + contrast ------------------------------
    for t in texts:
        px = t.get("font_px")
        if px is None:
            continue
        if px < MIN_MICROTEXT_FONT_PX:
            issues.append(f"label {t.get('id', '?')!r} font {px}px — unreadable microtext "
                          f"(hard floor {MIN_MICROTEXT_FONT_PX}px)")
        elif px < MIN_LABEL_FONT_PX and t.get("kind") == "text":
            issues.append(f"label {t.get('id', '?')!r} font {px}px below the readable "
                          f"minimum {MIN_LABEL_FONT_PX}px")
        if t.get("color") is not None and t.get("bg") is not None:
            c = contrast_ratio(t["color"], t["bg"])
            if c < MIN_TEXT_CONTRAST:
                issues.append(f"label {t.get('id', '?')!r} ({t.get('text', '')[:24]!r}) "
                              f"contrast {c:.1f}:1 below {MIN_TEXT_CONTRAST}:1")

    # ---- density limits ------------------------------------------------------
    semantic_fg = [f for f in foreground if f.get("kind") != "decoration"]
    if len(semantic_fg) > MAX_FOREGROUND_COMPONENTS:
        issues.append(f"scene density: {len(semantic_fg)} foreground components "
                      f"> {MAX_FOREGROUND_COMPONENTS} — one idea per scene")
    semantic_texts = [t for t in texts if t.get("kind") == "text"]
    if len(semantic_texts) > MAX_TEXT_LABELS:
        issues.append(f"scene density: {len(semantic_texts)} text labels "
                      f"> {MAX_TEXT_LABELS} — too many words compete")
    fg_area = _union_area([f["bbox"] for f in semantic_fg])
    frac = fg_area / float(zone_w * zone_h)
    if frac > MAX_FOREGROUND_AREA_FRACTION:
        issues.append(f"scene density: foreground fills {frac:.0%} of the zone "
                      f"> {MAX_FOREGROUND_AREA_FRACTION:.0%} — no whitespace left")

    # whitespace margin around the dominant (largest) foreground visual
    if semantic_fg:
        dom = max(semantic_fg, key=lambda f: area(f["bbox"]))
        db = dom["bbox"]
        others = [f["bbox"] for f in semantic_fg if f is not dom]
        others += [t["bbox"] for t in texts if t.get("parent") != dom.get("id")]
        margin = MIN_DOMINANT_MARGIN_PX
        ring = [db[0] - margin, db[1] - margin, db[2] + margin, db[3] + margin]
        for ob in others:
            if intersects(ob, ring) and not inside(ob, db, pad=6) and not inside(db, ob, pad=6):
                # tolerated only when the item touches the dominant (arrow/label of it)
                if not intersects(ob, db, pad=-2):
                    issues.append(f"dominant visual {dom.get('id', '?')!r} has no "
                                  f"{margin}px whitespace margin near {ob!r}")
                    break

    # separation of unrelated foreground elements
    for i, a in enumerate(semantic_fg):
        for b in semantic_fg[i + 1:]:
            if a.get("group") and a.get("group") == b.get("group"):
                continue
            if inside(a["bbox"], b["bbox"], pad=4) or inside(b["bbox"], a["bbox"], pad=4):
                continue
            if intersects(a["bbox"], b["bbox"], pad=-MIN_UNRELATED_SEPARATION_PX):
                issues.append(f"foreground {a.get('id', '?')!r} and {b.get('id', '?')!r} "
                              f"closer than {MIN_UNRELATED_SEPARATION_PX}px without a declared relation")

    # ---- safe zones (frame space; renderer passes these in) ------------------
    if safe_boxes:
        for it in items:
            if it.get("kind") in ("subtitle", "handle", "tag", "progress"):
                continue
            for name, sb in safe_boxes:
                if intersects(it["bbox"], sb, pad=-4):
                    issues.append(f"item {it.get('id', '?')!r} enters the {name} safe zone")
    return issues


def scene_density_report(items, zone_w=ZONE_W, zone_h=ZONE_H):
    """Measurable density summary (recorded in layout.json for the issue
    report and final QA): component/label counts, label sizes, area share."""
    items = items or []
    texts = [it for it in items if it.get("kind") == "text"]
    fg = [it for it in items if it.get("kind") in FOREGROUND_KINDS]
    return {
        "foreground_components": len(fg),
        "text_labels": len(texts),
        "min_label_px": min([it.get("font_px", 99) for it in texts] or [0]),
        "foreground_area_fraction": round(_union_area([f["bbox"] for f in fg]) /
                                          float(zone_w * zone_h), 3),
        "items": len(items),
    }
