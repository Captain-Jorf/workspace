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

Two detection paths (thresholds come from the documented policy block):

  A. VISIBLE COLD — ``luma >= min_luma_visible``, ``chroma >=
     visible_min_chroma``, ``blue_excess >= visible_min_blue_excess``:
     a genuinely visible blue/navy/cyan/purple element.
  B. DARK COLD — ``luma < min_luma_visible`` (a dark element), ``chroma >=
     dark_min_chroma``, ``blue_excess >= dark_min_blue_excess``.
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
    "visible_min_blue_excess": 8,      # blue must beat max(r,g) by this much
    "dark_min_chroma": 12,             # dark cold needs real chroma (noise: <=10)
    "dark_min_blue_excess": 8,
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
    "visible_min_blue_excess": (6.0, 32.0),
    "dark_min_chroma": (8.0, 40.0),
    "dark_min_blue_excess": (6.0, 32.0),
    "min_region_core_px": (64.0, 20000.0),
    "single_frame_core_block_px": (256.0, 400000.0),
    "max_frame_area_fraction": (0.0005, 0.02),
    "frames_per_scene": (1.0, 6.0),
    "persist_min_frames": (1.0, 6.0),
    "region_iou_min": (0.05, 0.9),
    "region_centroid_tol_px": (8.0, 400.0),
    "max_total_frames": (6.0, 120.0),
}

_INT_KEYS = ("min_luma_visible", "visible_min_chroma", "visible_min_blue_excess",
             "dark_min_chroma", "dark_min_blue_excess", "min_region_core_px",
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


def declared_color_is_cold(rgb, delta=6):
    """Categorical cold check for a DECLARED brand color / palette swatch.

    A named color is cold when blue dominates both other channels. No
    luminance/chroma gating applies: a declared swatch has no codec noise to
    reject. Rendered decoded pixels are judged by ``analyze_frame`` instead.
    Defined here so the cold-color family exists in exactly ONE module.
    """
    r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    return b > r + delta and b >= g - 6


def pixel_masks(band, cfg=None):
    """The two detection-path pixel masks for one frame BAND."""
    cfg = cfg or DEFAULT_POLICY
    lum, ch, be = luma(band), chroma(band), blue_excess(band)
    visible = (lum >= cfg["min_luma_visible"]) & (ch >= cfg["visible_min_chroma"]) \
        & (be >= cfg["visible_min_blue_excess"])
    dark = (lum < cfg["min_luma_visible"]) & (ch >= cfg["dark_min_chroma"]) \
        & (be >= cfg["dark_min_blue_excess"])
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


def region_records(core_mask, visible_mask, lum, chroma_img, cfg):
    """Meaningful regions of an eroded core mask (deterministic order).

    Each record: path visible|dark, coherent-core size, bounding box, centroid
    and mean luma/chroma of the coherent core. Regions smaller than
    ``min_region_core_px`` are codec-noise scale and are dropped here.
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
        out.append({
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

    visible_mask, dark_mask = pixel_masks(band_arr, cfg)
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
        result["regions"] = [{"path": "area", "core_px": core_px, "bbox": None,
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

    result["regions"] = region_records(core_mask, visible_mask, lum, ch, cfg)
    meaningful_area = sum(r["core_px"] for r in result["regions"])
    result["meaningful_area_px"] = meaningful_area
    result["noise_only_px"] = max(0, candidate_px - meaningful_area)
    result["max_region_core_px"] = max((r["core_px"] for r in result["regions"]), default=0)
    result["meaningful_area_fraction"] = (meaningful_area / band_px) if band_px else 0.0

    if result["regions"]:
        if result["max_region_core_px"] >= cfg["single_frame_core_block_px"]:
            result["blocked"] = True
            result["block_reason"] = (
                f"a single decoded frame contains a coherent blue/navy/cyan/purple region "
                f"(core {result['max_region_core_px']}px) — the profile palette is matte "
                "black/gold/amber/ivory")
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
        kind = "dark-navy" if top["path"] == "dark" else "visible"
        reason = (f"a {kind} blue/navy/cyan/purple object persists across "
                  f"{max(len(c['frames']) for c in persistent)} of {n} sampled frames of the "
                  f"scene (coherent core {top['core_px']}px) — the profile palette is matte "
                  "black/gold/amber/ivory")
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
        "blocked": bool(reason),
        "reason": reason,
        "per_frame": [
            {"raw_cold_px": fr["raw_cold_px"], "candidate_px": fr["candidate_px"],
             "candidate_area_fraction": fr["candidate_area_fraction"],
             "coherent_core_px": fr["coherent_core_px"], "noise_only_px": fr["noise_only_px"],
             "meaningful_area_px": fr["meaningful_area_px"],
             "max_region_core_px": fr["max_region_core_px"],
             "regions": [
                 {"path": r["path"], "core_px": r["core_px"], "bbox": r["bbox"],
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
