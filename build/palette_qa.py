"""Rendered-palette QA — perceptually grounded cold-color detection (issue #26).

WHY THIS MODULE EXISTS
----------------------
The legacy rendered-frame palette check was one raw 8-bit inequality applied to
one decoded frame per scene::

    cold = (b > r + 4) & (b >= g - 6)          # fraction > 0.002 → block

Verified audit evidence (issue #26, run 35071043285, reel-2026-09-21, scene s4 —
a procedural confidence gauge with no photograph, no repository hero image and
no external asset):

  * pre-encode cold-pixel count: 0.00%;
  * lossless PNG round-trip: 0.00%;
  * decoded H.264 frames: 0.23% of the sampled band flags under the legacy
    inequality ONLY because of codec chroma quantization;
  * every flagged pixel was essentially black (RGB like (5-8, 6, 13-15),
    luminance < ~30) in flat near-black regions — no blue object, text or
    overlay.

Locally reproduced with the production encode settings (libx264 CRF 21,
yuv420p, preset medium, on the renderer's own 1296x2304 matte-black/gold gauge
composition): 0.015%-0.075% of the band flags under the legacy inequality, the
LARGEST connected component is 56 px and its 3x3-eroded core is 4 px. The raw
inequality is therefore a poor gate: it cannot distinguish codec noise in
near-black areas from a real navy/blue object.

WHAT REPLACES IT
----------------
Pixel evidence uses three perceptually meaningful quantities instead of one RGB
inequality:

  * ``luma``        — Rec.709 luminance (0..255);
  * ``chroma``      — max(r,g,b) - min(r,g,b): absolute chroma magnitude (a
                      near-gray pixel cannot be "visibly blue");
  * ``blue_excess`` — b - max(r,g): how far blue dominates BOTH other channels
                      (negative for any warm pixel).

THREE PROHIBITED FAMILIES, each classified independently (``family_masks``):

  * ``blue``   — ``(b - r >= blue_min_excess) & (b - g >= blue_min_excess)``
  * ``cyan``   — ``(g - r >= cyan_min_excess) & (b - r >= cyan_min_excess) &
                  (g - b <= cyan_max_green_over_blue)``
  * ``purple`` — ``(r - g >= purple_min_red_over_green) &
                  (b - g >= purple_min_blue_over_green)``

A blue-dominance-only rule is NOT sufficient: canonical cyan RGB(0,255,255) and
canonical purple RGB(128,0,128) are balanced — ``b - max(r, g) == 0`` for both —
even though the brand policy prohibits them. The cyan and purple rules above
catch the balanced/dark variants while the green-lead and warm-rose guards keep
ordinary green and rose/warm red allowed.

Two detection paths on top of the families (thresholds from the policy block):

  A. VISIBLE COLD — ``luma >= min_luma_visible`` and ``chroma >=
     visible_min_chroma``: a genuinely visible cold element.
  B. DARK COLD — ``luma < min_luma_visible`` (a dark element) and ``chroma >=
     dark_min_chroma``.
     Low luminance is NOT ignored: a real dark-navy object still has a large
     absolute chroma (e.g. #0A1A2E = (10,26,46) → chroma 36), while measured
     H.264 noise on a near-black field stays at chroma <= ~10 with a dominant
     channel <= ~32 (calibration: tests/test_palette_qa_h264.py).

Region evidence — a cold PIXEL is not a cold OBJECT:

  * the candidate mask is eroded with a 3x3 structuring element; the surviving
    core pixels are the spatially coherent interior of any solid patch;
  * a region is MEANINGFUL only when its core component is at least
    ``min_region_core_px`` pixels. Measured codec noise: 4 px core (largest
    single noise component 56 px total) → never meaningful;
  * a frame is blocked outright when one meaningful core reaches
    ``single_frame_core_block_px``, or when a meaningful region exists and the
    cold-dominant area covers ``max_frame_area_fraction`` of the band (a big
    obvious cold object, caught even from a single sampled frame).

Scene evidence (bounded, deterministic multi-frame sampling):

  * a MEANINGFUL region that persists across ``persist_min_frames`` sampled
    frames of the scene — matched as the same object by core-bounding-box
    overlap or centroid proximity — blocks the scene: a small but real object
    is caught, one-frame codec noise is not;
  * per-frame evidence is never averaged away across the whole video: every
    sampled frame is judged on its own and the maximum meaningful core / area
    fraction is part of both the verdict and the diagnostics.

The profile palette stays matte black / charcoal / warm metallic gold / amber /
bronze / ivory; blue, navy, cyan and purple remain prohibited as dominant or
meaningful visual elements.

CONFIGURATION
-------------
Every threshold comes from ONE documented policy block
(``content/editorial_policy.json`` → ``palette_qa``). Hard safety envelopes are
enforced in code: a malformed or suspiciously weak configuration is clamped and
the change is recorded in the diagnostics (``policy_notes``). There is
deliberately NO environment-variable override — production cannot silently
weaken the palette rule.

Safety: no image data, no URL, no filename and no remote metadata ever leaves
this module. The returned structures contain only counts, fractions, pixel
counts and geometry.
"""
METHOD = "palette_qa/v1"

# ---------------------------------------------------------------------------
# numpy is loaded on first use (see _np) so that importing this module never
# requires the scientific stack — only analyzing frames does.
# ---------------------------------------------------------------------------


def _np():
    """numpy, imported on FIRST USE rather than at module import.

    The pixel/region analysis needs numpy, and the production environment
    installs it from requirements.txt. Importing it at module scope would,
    however, make the whole QA module graph unusable in a minimal interpreter —
    including the light pipeline subcommands (``verify``/``note``/``record``)
    that never touch a frame. Every function below that analyzes pixels calls
    this helper, so a genuinely numpy-less environment still fails loudly at the
    moment a frame is analyzed (fail closed) instead of silently skipping QA.
    """
    import numpy
    return numpy


POLICY_KEY = "palette_qa"

# ---------------------------------------------------------------------------
# Calibrated defaults — same keys/semantics as the policy block.
# ---------------------------------------------------------------------------
DEFAULT_POLICY = {
    "method": METHOD,
    "min_luma_visible": 64,            # a pixel below this is treated as "dark"
    "visible_min_chroma": 16,          # visible cold must be clearly chromatic
    "dark_min_chroma": 12,             # dark cold needs real chroma (noise: <=10)
    "blue_min_excess": 8,              # blue family: b beats r AND g by this much
    "cyan_min_excess": 8,              # cyan family: g and b each beat r by this much
    "cyan_hue_min": 165,               # ... inside the cyan/teal hue band (degrees)
    "cyan_hue_max": 200,               #     (green is 120, navy is 213)
    "purple_min_red_over_green": 24,   # purple family: r beats g by this much
    "purple_min_blue_over_green": 32,  # ... b beats g by this much (excludes warm rose)
    "purple_hue_min": 260,             # ... inside the violet/magenta hue band
    "purple_hue_max": 335,             #     (blue is 240, rose/warm red is ~347)
    "min_region_core_px": 256,         # smallest MEANINGFUL coherent region
    "single_frame_core_block_px": 1024,  # one big obvious cold object
    "max_frame_area_fraction": 0.004,  # cold-dominant area share when a region exists
    "frames_per_scene": 3,             # bounded deterministic samples per scene
    "persist_min_frames": 2,           # same object seen in >= N sampled frames
    "region_iou_min": 0.15,
    "region_centroid_tol_px": 48,
    "max_total_frames": 40,            # hard bound on decoded QA frames
    "band": {"top_fraction": 0.25, "bottom_fraction": 0.85, "side_margin_px": 40},
}

# Hard safety envelope per key (min, max) — applied to the shipped defaults.
_HARD_LIMITS = {
    "min_luma_visible": (24.0, 128.0),
    "visible_min_chroma": (10.0, 48.0),
    "dark_min_chroma": (8.0, 40.0),
    "blue_min_excess": (6.0, 32.0),
    "cyan_min_excess": (6.0, 32.0),
    "cyan_hue_min": (120.0, 200.0),
    "cyan_hue_max": (170.0, 260.0),
    "purple_min_red_over_green": (6.0, 48.0),
    "purple_min_blue_over_green": (6.0, 48.0),
    "purple_hue_min": (200.0, 320.0),
    "purple_hue_max": (300.0, 360.0),
    "min_region_core_px": (64.0, 20000.0),
    "single_frame_core_block_px": (256.0, 400000.0),
    "max_frame_area_fraction": (0.0005, 0.02),
    "frames_per_scene": (1.0, 6.0),
    "persist_min_frames": (1.0, 6.0),
    "region_iou_min": (0.05, 0.9),
    "region_centroid_tol_px": (8.0, 400.0),
    "max_total_frames": (6.0, 120.0),
}

_INT_KEYS = ("min_luma_visible", "visible_min_chroma", "dark_min_chroma",
             "blue_min_excess", "cyan_min_excess", "cyan_hue_min", "cyan_hue_max",
             "purple_min_red_over_green", "purple_min_blue_over_green",
             "purple_hue_min", "purple_hue_max", "min_region_core_px",
             "single_frame_core_block_px", "frames_per_scene", "persist_min_frames",
             "region_centroid_tol_px", "max_total_frames")

# A core mask larger than this is obviously a huge cold object: block it without
# running component labelling (keeps the check O(pixels) in the worst case).
_HUGE_CORE_PX = 400000


def load_policy(pol=None):
    """The ONE palette-QA configuration shared by production and tests.

    Reads ``pol["palette_qa"]`` when present, clamps every value into its hard
    safety envelope and records what changed. Missing or invalid entries fall
    back to the calibrated default for that key (fail closed: the shipped
    defaults are the reviewed, calibrated configuration).
    """
    if pol is None:
        try:
            import common
            pol = common.policy()
        except Exception:                                        # noqa: BLE001
            pol = {}
    raw = (pol or {}).get(POLICY_KEY)
    notes = []
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_POLICY.items()}
    if raw is None:
        notes.append("policy block missing — calibrated defaults used")
        raw = {}
    elif not isinstance(raw, dict):
        notes.append("policy block is not an object — calibrated defaults used")
        raw = {}
    for key, default in DEFAULT_POLICY.items():
        if key in ("band", "method") or key not in raw:
            continue
        try:
            val = float(raw[key])
        except (TypeError, ValueError):
            notes.append(f"{key}: not numeric — default {default} used")
            continue
        lo, hi = _HARD_LIMITS[key]
        if not (lo <= val <= hi):
            notes.append(f"{key}={val} outside hard limits [{lo}, {hi}] — clamped")
            val = min(max(val, lo), hi)
        cfg[key] = int(round(val)) if key in _INT_KEYS else val
    if cfg["single_frame_core_block_px"] < cfg["min_region_core_px"]:
        notes.append("single_frame_core_block_px < min_region_core_px — raised to fit")
        cfg["single_frame_core_block_px"] = cfg["min_region_core_px"]
    for lo_key, hi_key in (("cyan_hue_min", "cyan_hue_max"), ("purple_hue_min", "purple_hue_max")):
        if cfg[lo_key] >= cfg[hi_key]:
            notes.append(f"{lo_key} >= {hi_key} — defaults used (empty hue band would disable the family)")
            cfg[lo_key], cfg[hi_key] = DEFAULT_POLICY[lo_key], DEFAULT_POLICY[hi_key]
    if cfg["persist_min_frames"] > cfg["frames_per_scene"]:
        notes.append("persist_min_frames > frames_per_scene — reduced to fit")
        cfg["persist_min_frames"] = cfg["frames_per_scene"]
    band_raw = raw.get("band")
    if band_raw is not None and not isinstance(band_raw, dict):
        notes.append("band is not an object — defaults used")
        band_raw = None
    if isinstance(band_raw, dict):
        band = dict(DEFAULT_POLICY["band"])
        for key in band:
            if key not in band_raw:
                continue
            try:
                band[key] = float(band_raw[key])
            except (TypeError, ValueError):
                notes.append(f"band.{key}: not numeric — default used")
        if not (0.0 <= band["top_fraction"] < band["bottom_fraction"] <= 1.0):
            notes.append("band fractions invalid — defaults used")
            band = dict(DEFAULT_POLICY["band"])
        band["side_margin_px"] = int(max(0.0, band["side_margin_px"]))
        cfg["band"] = band
    cfg["method"] = METHOD
    cfg["policy_notes"] = notes
    return cfg


# ---------------------------------------------------------------------------
# Per-pixel evidence
# ---------------------------------------------------------------------------
def _channels(arr):
    np = _np()
    a = np.asarray(arr)
    if a.ndim != 3 or a.shape[2] < 3 or a.size == 0:
        raise ValueError("palette_qa expects a non-empty (h, w, 3) RGB frame")
    return a[..., 0].astype(np.int16), a[..., 1].astype(np.int16), a[..., 2].astype(np.int16)


def luma(arr):
    r, g, b = _channels(arr)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def chroma(arr):
    np = _np()
    r, g, b = _channels(arr)
    return np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)


def blue_excess(arr):
    """b - max(r, g) in 8-bit levels (positive = blue-dominant)."""
    np = _np()
    r, g, b = _channels(arr)
    return b - np.maximum(r, g)


def hue(arr):
    """HSV hue in degrees (0-360) — the perceptual coordinate used to separate
    cyan/teal from blue/navy and violet/magenta from warm rose. Pixels with zero
    chroma report 0.0 (they can never match a family: every family rule also
    requires a channel separation)."""
    np = _np()
    r, g, b = _channels(arr)
    mx = np.maximum(np.maximum(r, g), b).astype(np.float32)
    mn = np.minimum(np.minimum(r, g), b).astype(np.float32)
    delta = np.where((mx - mn) > 0, mx - mn, 1.0)
    h = np.zeros(mx.shape, np.float32)
    h = np.where(mx == r, 60.0 * (((g - b) / delta) % 6.0), h)
    h = np.where(mx == g, 60.0 * (((b - r) / delta) + 2.0), h)
    h = np.where(mx == b, 60.0 * (((r - g) / delta) + 4.0), h)
    return np.where((mx - mn) > 0, h % 360.0, 0.0)


def cold_dominance_mask(arr, delta=4):
    """The LEGACY raw cold inequality, kept in exactly one place:

        ``(b > r + delta) & (b >= g - 6)``

    A comparative diagnostic only — never a gate on its own (see docstring).
    """
    r, g, b = _channels(arr)
    return (b > r + delta) & (b >= g - 6)


def raw_cold_pixel_fraction(arr, delta=4):
    """Fraction of pixels flagged by the legacy raw inequality (diagnostic)."""
    m = cold_dominance_mask(arr, delta)
    return float(m.mean()) if m.size else 0.0


def declared_color_is_cold(rgb, cfg=None):
    """Categorical cold check for a DECLARED brand color / palette swatch.

    A named color is cold when it belongs to any prohibited family under the
    SAME three family rules used for rendered pixels — including their guards —
    so canonical cyan RGB(0,255,255) and canonical purple RGB(128,0,128) are
    rejected too, not only blue-dominant colors, while ordinary green and warm
    rose stay allowed. Only the luminance/chroma floors and the spatial rules
    are skipped here: a declared swatch is a solid brand color, carries no codec
    noise and has no region geometry. Rendered DECODED pixels are judged by
    ``analyze_frame`` instead. Defined here so the cold-color family exists in
    exactly ONE module.
    """
    cfg = cfg or DEFAULT_POLICY
    return bool(cold_families_of(rgb, cfg))


COLD_FAMILIES = ("blue", "cyan", "purple")

# Label precedence, NARROWEST family first. A cyan or purple color is often also
# blue-dominant (e.g. teal (0,140,160) or violet (143,0,255)); the narrow
# hue-banded families describe it better, so they win the region label on a tie.
# The wide blue family has no hue band and therefore comes last.
FAMILY_PRECEDENCE = ("cyan", "purple", "blue")


def family_masks(band, cfg=None):
    """The three prohibited cold-color FAMILIES, each classified independently.

    A single blue-dominance inequality is not enough: the canonical, balanced
    members of two prohibited families have equal blue and green (cyan
    RGB(0,255,255)) or equal blue and red (purple RGB(128,0,128)), so
    ``b - max(r, g)`` is ZERO for both — they would evade a blue-only detector.
    Each family therefore has its own documented rule (all in 8-bit levels):

      * ``blue``   — blue materially above BOTH other channels::
            (b - r >= blue_min_excess) & (b - g >= blue_min_excess)
        covers pure blue, navy, indigo, steel blue and blue-leaning cyan.

      * ``cyan``   — green AND blue materially above red, INSIDE the cyan/teal
        hue band (the band is what separates cyan from navy, whose hue is
        blue; the channel floors separate it from ordinary green)::
            (g - r >= cyan_min_excess) & (b - r >= cyan_min_excess)
            & (cyan_hue_min <= hue < cyan_hue_max)
        covers canonical cyan RGB(0,255,255) (hue 180), teal (hue 180), darker
        cyan (hue 190) and cyan-leaning teal (hue 187), while pure green
        RGB(0,200,0) (hue 120), spring green (hue 150) and navy (hue 213)
        stay out.

      * ``purple`` — red AND blue materially above green, INSIDE the
        violet/magenta hue band (the band is what separates violet from blue;
        the blue-over-green floor is what separates it from warm rose, which
        also has blue slightly above green)::
            (r - g >= purple_min_red_over_green)
            & (b - g >= purple_min_blue_over_green)
            & (purple_hue_min <= hue < purple_hue_max)
        covers canonical purple RGB(128,0,128) (hue 300), violet (hue 274),
        dark purple (hue 285) and magenta-purple (hue 286), while rose/warm
        red (hue 347) and pure blue (hue 240) stay out.

    The blue rule is a pure channel rule because blue/navy IS the wide
    blue-dominance family; cyan and purple are the balanced families that need
    the perceptual coordinate to be separated from it.

    Achromatic pixels are excluded by construction (every rule needs a real
    channel separation) and the luminance/chroma floors plus the coherent-region
    and persistence rules below are applied on top, so the measured near-black
    H.264 chroma noise (chroma <= ~10, isolated) is never treated as an object.
    """
    cfg = cfg or DEFAULT_POLICY
    r, g, b = _channels(band)
    h = hue(band)
    blue = (b - r >= cfg["blue_min_excess"]) & (b - g >= cfg["blue_min_excess"])
    cyan = ((g - r >= cfg["cyan_min_excess"]) & (b - r >= cfg["cyan_min_excess"])
            & (h >= cfg["cyan_hue_min"]) & (h < cfg["cyan_hue_max"]))
    purple = ((r - g >= cfg["purple_min_red_over_green"])
              & (b - g >= cfg["purple_min_blue_over_green"])
              & (h >= cfg["purple_hue_min"]) & (h < cfg["purple_hue_max"]))
    return {"blue": blue, "cyan": cyan, "purple": purple}


def hue_scalar(rgb):
    """Pure-Python HSV hue in degrees for ONE RGB triple — mirrors ``hue``
    exactly (same branch order, same tie behaviour), so declared-color checks
    and rendered-pixel masks can never disagree. Deliberately numpy-free: the
    declared-palette check runs while visual_plan is being imported, which must
    stay possible in a minimal interpreter."""
    r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0.0
    h = 0.0
    if mx == r:
        h = 60.0 * (((g - b) / d) % 6)
    if mx == g:
        h = 60.0 * (((b - r) / d) + 2)
    if mx == b:
        h = 60.0 * (((r - g) / d) + 4)
    return h % 360.0


def cold_families_of(rgb, cfg=None):
    """Family names matching a single RGB triple (used by the declared-swatch
    check and by tests). Scalar twin of ``family_masks`` — the two are kept in
    lockstep by tests/test_palette_qa_units.py::ScalarVectorEquivalence."""
    cfg = cfg or DEFAULT_POLICY
    r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
    h = hue_scalar((r, g, b))
    out = []
    if (b - r >= cfg["blue_min_excess"]) and (b - g >= cfg["blue_min_excess"]):
        out.append("blue")
    if ((g - r >= cfg["cyan_min_excess"]) and (b - r >= cfg["cyan_min_excess"])
            and cfg["cyan_hue_min"] <= h < cfg["cyan_hue_max"]):
        out.append("cyan")
    if ((r - g >= cfg["purple_min_red_over_green"])
            and (b - g >= cfg["purple_min_blue_over_green"])
            and cfg["purple_hue_min"] <= h < cfg["purple_hue_max"]):
        out.append("purple")
    return out


def _family_index(families, shape):
    """0 = none, 1..3 = cold family index in FAMILY_PRECEDENCE order — the
    narrowest matching family wins (cyan, then purple, then blue)."""
    np = _np()
    idx = np.zeros(shape, np.int8)
    for i, name in enumerate(FAMILY_PRECEDENCE, start=1):
        idx = np.where((idx == 0) & families[name], np.int8(i), idx)
    return idx


def pixel_masks(band, cfg=None, families=None):
    """The two detection-path pixel masks for one frame BAND."""
    cfg = cfg or DEFAULT_POLICY
    if families is None:
        families = family_masks(band, cfg)
    cold = families["blue"] | families["cyan"] | families["purple"]
    lum, ch = luma(band), chroma(band)
    visible = cold & (lum >= cfg["min_luma_visible"]) & (ch >= cfg["visible_min_chroma"])
    dark = cold & (lum < cfg["min_luma_visible"]) & (ch >= cfg["dark_min_chroma"])
    return visible, dark


def band_slice(arr, cfg=None):
    """The QA band of a frame (policy-controlled, edges excluded)."""
    cfg = cfg or DEFAULT_POLICY
    b = cfg["band"]
    h, w = arr.shape[0], arr.shape[1]
    top = int(h * b["top_fraction"])
    bottom = int(h * b["bottom_fraction"])
    side = int(b["side_margin_px"])
    return (slice(top, bottom), slice(side, max(side, w - side)))


def _erode(mask):
    """3x3 erosion — the spatially coherent core of a mask (vectorized)."""
    np = _np()
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return np.zeros_like(mask)
    return (mask[1:-1, 1:-1] & mask[:-2, 1:-1] & mask[2:, 1:-1]
            & mask[1:-1, :-2] & mask[1:-1, 2:])


# ---------------------------------------------------------------------------
# Region evidence — deterministic connected components of the coherent core
# ---------------------------------------------------------------------------
def _row_runs(row):
    """[(start, end), ...] of True runs in one boolean row (end exclusive)."""
    np = _np()
    padded = np.concatenate(([0], row.view(np.int8), [0]))
    idx = np.flatnonzero(np.diff(padded))
    return [(int(idx[i]), int(idx[i + 1])) for i in range(0, len(idx), 2)]


def label_components(mask):
    """Deterministic 4-connected labeling of a boolean mask → (int32 label
    image with 0 = background, component count).

    Row-run based union-find: one union per touching run pair instead of one per
    pixel, so a solid region costs O(rows) and the whole frame stays cheap.
    """
    np = _np()
    h, w = mask.shape
    labels = np.zeros((h, w), np.int32)
    parent = []
    next_id = 0
    prev_runs = []

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for y in range(h):
        row = mask[y]
        runs = _row_runs(row) if row.any() else []
        cur_runs = []
        for (x0, x1) in runs:
            rid = next_id
            next_id += 1
            parent.append(rid)
            labels[y, x0:x1] = rid + 1          # 0 stays reserved for background
            for (px0, px1, pid) in prev_runs:
                if px1 > x0 and x1 > px0:
                    ra, rb = find(rid), find(pid)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
            cur_runs.append((x0, x1, rid))
        prev_runs = cur_runs
    n = next_id
    if n == 0:
        return labels, 0
    remap = np.zeros(n + 1, dtype=np.int32)     # remap[0] = 0 (background)
    for rid in range(n):
        remap[rid + 1] = find(rid) + 1
    labels = remap[labels]
    roots = np.unique(labels)
    roots = roots[roots > 0]
    if roots.size == 0:
        return np.zeros_like(labels), 0
    # renumber 1..k in ascending original id order (deterministic)
    lut = np.zeros(int(roots.max()) + 1, np.int32)
    lut[roots] = np.arange(1, len(roots) + 1, dtype=np.int32)
    return lut[labels], int(len(roots))


def region_records(core_mask, visible_mask, lum, chroma_img, cfg, family_idx=None):
    """Meaningful regions of an eroded core mask (deterministic order).

    Each record: cold family, path visible|dark, coherent-core size, bounding
    box, centroid and mean luma/chroma of the coherent core. Regions smaller
    than ``min_region_core_px`` are codec-noise scale and are dropped here.
    """
    np = _np()
    labels, count = label_components(core_mask)
    if count == 0:
        return []
    counts = np.bincount(labels.ravel(), minlength=count + 1)
    out = []
    for lab in range(1, count + 1):
        if counts[lab] < cfg["min_region_core_px"]:
            continue
        sel = labels == lab
        ys, xs = np.nonzero(sel)
        visible_hits = int(visible_mask[ys, xs].sum())
        values = lum[ys, xs]
        chroma_values = chroma_img[ys, xs]
        family = "blue"
        if family_idx is not None:
            hits = [(name, int((family_idx[ys, xs] == i).sum()))
                    for i, name in enumerate(FAMILY_PRECEDENCE, start=1)]
            family = max(hits, key=lambda kv: kv[1])[0]     # ties → narrowest family
        out.append({
            "family": family,
            "path": "visible" if visible_hits * 2 >= int(counts[lab]) else "dark",
            "core_px": int(counts[lab]),
            "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
            "centroid": [float(xs.mean()), float(ys.mean())],
            "mean_luma": float(values.mean()),
            "mean_chroma": float(chroma_values.mean()),
        })
    out.sort(key=lambda c: (-c["core_px"], c["bbox"][1], c["bbox"][0]))
    return out


def analyze_frame(arr, cfg=None, band=None):
    """Per-frame palette evidence (counts / fractions / geometry only)."""
    np = _np()
    cfg = cfg or DEFAULT_POLICY
    frame = np.asarray(arr)
    band_arr = frame[band] if band is not None else frame[band_slice(frame, cfg)]
    band_px = int(band_arr[..., 0].size)

    families = family_masks(band_arr, cfg)
    visible_mask, dark_mask = pixel_masks(band_arr, cfg, families)
    candidate = visible_mask | dark_mask
    candidate_px = int(candidate.sum())
    raw_px = int(cold_dominance_mask(band_arr).sum())
    core_mask = _erode(candidate)
    core_px = int(core_mask.sum())

    result = {
        "band_px": band_px,
        "raw_cold_px": raw_px,
        "candidate_px": candidate_px,
        "candidate_area_fraction": round(candidate_px / band_px, 6) if band_px else 0.0,
        "sub_threshold_px": max(0, raw_px - candidate_px),
        "visible_cold_px": int(visible_mask.sum()),
        "dark_cold_px": int(dark_mask.sum()),
        "family_px": {name: int((families[name] & candidate).sum()) for name in COLD_FAMILIES},
        "coherent_core_px": core_px,
        "meaningful_area_px": 0,
        "noise_only_px": candidate_px,
        "max_region_core_px": 0,
        "regions": [],
        "meaningful_area_fraction": 0.0,
        "blocked": False,
        "block_reason": "",
    }
    if core_px == 0:
        return result                       # pure speckle: no cold OBJECT at all

    lum, ch = luma(band_arr), chroma(band_arr)
    if core_px > _HUGE_CORE_PX:
        result["regions"] = [{"family": next(n for n in FAMILY_PRECEDENCE if families[n].any()),
                              "path": "area", "core_px": core_px, "bbox": None,
                              "centroid": None, "mean_luma": None, "mean_chroma": None}]
        result["meaningful_area_px"] = candidate_px
        result["noise_only_px"] = 0
        result["max_region_core_px"] = core_px
        result["meaningful_area_fraction"] = result["candidate_area_fraction"]
        result["blocked"] = True
        result["block_reason"] = ("a blue/navy/cyan/purple area covers "
                                  f"{100.0 * result['candidate_area_fraction']:.2f}% of the "
                                  "frame band")
        return result

    result["regions"] = region_records(core_mask, visible_mask, lum, ch, cfg,
                                       _family_index(families, candidate.shape))
    meaningful_area = sum(r["core_px"] for r in result["regions"])
    result["meaningful_area_px"] = meaningful_area
    result["noise_only_px"] = max(0, candidate_px - meaningful_area)
    result["max_region_core_px"] = max((r["core_px"] for r in result["regions"]), default=0)
    result["meaningful_area_fraction"] = (meaningful_area / band_px) if band_px else 0.0

    if result["regions"]:
        if result["max_region_core_px"] >= cfg["single_frame_core_block_px"]:
            result["blocked"] = True
            top_family = result["regions"][0].get("family", "blue")
            result["block_reason"] = (
                f"a single decoded frame contains a coherent {top_family} cold region "
                f"(blue/navy/cyan/purple family, core {result['max_region_core_px']}px) — the "
                "profile palette is matte black/gold/amber/ivory")
        elif result["candidate_area_fraction"] >= cfg["max_frame_area_fraction"]:
            result["blocked"] = True
            result["block_reason"] = (
                f"a meaningful blue/navy/cyan/purple area covers "
                f"{100.0 * result['candidate_area_fraction']:.2f}% of the frame band")
    return result


def _same_object(a, b, cfg):
    """Are two meaningful regions the same visual object across frames?"""
    if a.get("bbox") is None or b.get("bbox") is None:
        return False        # an over-threshold region blocks on its own
    ax0, ay0, ax1, ay1 = a["bbox"]
    bx0, by0, bx1, by1 = b["bbox"]
    ix = max(0, min(ax1, bx1) - max(ax0, bx0) + 1)
    iy = max(0, min(ay1, by1) - max(ay0, by0) + 1)
    inter = ix * iy
    union = (ax1 - ax0 + 1) * (ay1 - ay0 + 1) + (bx1 - bx0 + 1) * (by1 - by0 + 1) - inter
    if union > 0 and inter / float(union) >= cfg["region_iou_min"]:
        return True
    dx = a["centroid"][0] - b["centroid"][0]
    dy = a["centroid"][1] - b["centroid"][1]
    return (dx * dx + dy * dy) ** 0.5 <= cfg["region_centroid_tol_px"]


def analyze_scene(frames, cfg=None):
    """Multi-frame scene verdict for ONE rendered scene.

    ``frames`` is a bounded list of decoded RGB frames sampled deterministically
    from that scene (already restricted to a custom band when the caller
    prefers one).
    """
    cfg = cfg or DEFAULT_POLICY
    per_frame = [analyze_frame(f, cfg) for f in frames]
    n = len(per_frame)
    persist_min = min(cfg["persist_min_frames"], max(1, n))

    # Cluster regions that represent the SAME visual object across frames.
    nodes = [(fi, ri) for fi, fr in enumerate(per_frame) for ri in range(len(fr["regions"]))]
    parent = {node: node for node in nodes}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for i, (fi, ri) in enumerate(nodes):
        for (fj, rj) in nodes[i + 1:]:
            if fi == fj:
                continue
            if _same_object(per_frame[fi]["regions"][ri], per_frame[fj]["regions"][rj], cfg):
                a, b = find((fi, ri)), find((fj, rj))
                if a != b:
                    parent[max(a, b)] = min(a, b)

    clusters = {}
    for node in nodes:
        cluster = clusters.setdefault(find(node), {"frames": set(), "regions": []})
        cluster["frames"].add(node[0])
        cluster["regions"].append(node)
    persistent = [c for c in clusters.values() if len(c["frames"]) >= persist_min]

    max_core = max((fr["max_region_core_px"] for fr in per_frame), default=0)
    max_frac = max((fr["candidate_area_fraction"] for fr in per_frame), default=0.0)
    single_frame = next((fr for fr in per_frame if fr["blocked"]), None)

    reason = ""
    if persistent:
        top = max((per_frame[fi]["regions"][ri] for c in persistent for (fi, ri) in c["regions"]),
                  key=lambda r: (r["core_px"], -r["centroid"][0]))
        kind = f"{'dark ' if top['path'] == 'dark' else ''}{top.get('family', 'blue')}"
        reason = (f"a {kind} cold object persists across "
                  f"{max(len(c['frames']) for c in persistent)} of {n} sampled frames of the "
                  f"scene (blue/navy/cyan/purple family, coherent core {top['core_px']}px) — "
                  "the profile palette is matte black/gold/amber/ivory")
    elif single_frame is not None:
        reason = single_frame["block_reason"]

    return {
        "frames_sampled": n,
        "method": cfg.get("method", METHOD),
        "max_meaningful_core_px": int(max_core),
        "max_meaningful_area_fraction": round(float(max_frac), 6),
        "frames_with_meaningful_cold": sum(1 for fr in per_frame if fr["regions"]),
        "persistent_cold_regions": len(persistent),
        "coherent_core_px_total": sum(fr["coherent_core_px"] for fr in per_frame),
        "noise_only_px_total": sum(fr["noise_only_px"] for fr in per_frame),
        "raw_cold_px_total": sum(fr["raw_cold_px"] for fr in per_frame),
        "family_px_total": {name: sum(fr["family_px"][name] for fr in per_frame)
                            for name in COLD_FAMILIES},
        "blocked": bool(reason),
        "reason": reason,
        "per_frame": [
            {"raw_cold_px": fr["raw_cold_px"], "candidate_px": fr["candidate_px"],
             "candidate_area_fraction": fr["candidate_area_fraction"],
             "coherent_core_px": fr["coherent_core_px"], "noise_only_px": fr["noise_only_px"],
             "meaningful_area_px": fr["meaningful_area_px"],
             "max_region_core_px": fr["max_region_core_px"],
             "regions": [
                 {"family": r.get("family"), "path": r["path"], "core_px": r["core_px"],
                  "bbox": r["bbox"],
                  "centroid": None if r["centroid"] is None
                  else [round(float(v), 1) for v in r["centroid"]],
                  "mean_luma": None if r["mean_luma"] is None else round(r["mean_luma"], 1),
                  "mean_chroma": None if r["mean_chroma"] is None else round(r["mean_chroma"], 1)}
                 for r in fr["regions"]],
             }
            for fr in per_frame
        ],
    }


def scene_verdict(scene_result):
    """(ok, reason) for one analyzed scene."""
    if scene_result.get("blocked"):
        return False, scene_result.get("reason") or "meaningful cold-color region"
    return True, ""


def sample_times(start, end, cfg=None, total=None, inset=0.15):
    """Bounded deterministic QA sample timestamps INSIDE one scene window.

    ``frames_per_scene`` evenly spaced interior fractions (never the exact cut
    boundary, where a decoder could return the neighbouring frame), clamped into
    the window's inset range and into ``[0, total - 0.3]``.
    """
    cfg = cfg or DEFAULT_POLICY
    n = max(1, int(cfg["frames_per_scene"]))
    start, end = float(start), float(end)
    if end <= start:
        candidates = [start]
    else:
        span = end - start
        candidates = [start + span * ((k + 1) / (n + 1)) for k in range(n)]
        if span > 2 * inset:
            candidates = [min(max(t, start + inset), end - inset) for t in candidates]
    out = []
    for t in candidates:
        t = max(0.0, t)
        if total is not None:
            t = min(t, max(0.0, float(total) - 0.3))
        t = round(t, 3)
        if t not in out:
            out.append(t)
    return out


def frame_budget(cfg, scene_count):
    """Total decoded QA frames for ``scene_count`` scenes (bounded)."""
    cfg = cfg or DEFAULT_POLICY
    return min(int(cfg["max_total_frames"]),
               max(1, int(cfg["frames_per_scene"])) * max(1, int(scene_count)))
