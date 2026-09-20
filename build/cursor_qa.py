"""Deterministic typing-cursor QA and shape-ownership verification.

BACKGROUND AND MECHANISM
------------------------
A genuine typing cursor is a small, standalone vertical rectangle (caret)
rendered at a fixed position inside a code-entry or terminal container:
  * width 3-6 px, height 14-70 px, aspect ratio >= 6.0;
  * blinks at 2 Hz ((ts * 2) % 1 < 0.55);
  * stands on a dark background in the designated code zone;
  * is justified ONLY when code_justified=true and cursor_justified=true.

The previous single-frame detector inspected only one middle frame per scene and
classified any isolated 3-6px wide, 14-70px tall vertical gold segment as a
typing cursor without verifying:
  1. Connected-component ownership (whether the candidate bar is merely the
     tangent of a much larger shape such as an ellipse, ring, border, chart,
     network line, or logo);
  2. Bounded temporal movement and blinking behavior across multiple frames
     within the authoritative scene window;
  3. Context and expected-region justification.

This module implements:
  * Deterministic 4-connected shape ownership using the repository's
    palette_qa.label_components;
  * Rejection of candidate bars that belong to components exceeding the cursor
    area envelope (max 450 px) or bounding box (max 16x80 px);
  * Fill ratio and curvature/continuation constraints;
  * Multi-frame temporal analysis over bounded deterministic frame samples to
    differentiate stationary blinking carets from moving graphic elements
    (e.g. an expanding ellipse ring translating at ~143 px/s) and static non-code
    structures;
  * Contextual validation against the scene's code/cursor justification and
    expected code-entry region.

CONFIGURATION AND SAFETY ENVELOPES
----------------------------------
All parameters are defined in ``content/editorial_policy.json`` (key: ``cursor_qa``).
Hard safety envelopes are enforced in code; no environment variable can weaken
detection.
"""
import copy
import math
import os
import sys

METHOD = "cursor_qa/v1"
POLICY_KEY = "cursor_qa"

# ---------------------------------------------------------------------------
# Calibrated defaults — single source of truth for cursor analysis.
# ---------------------------------------------------------------------------
DEFAULT_POLICY = {
    "method": METHOD,
    "frames_per_scene": 3,
    "min_cursor_width": 3,
    "max_cursor_width": 6,
    "min_cursor_height": 30,
    "max_cursor_height": 70,
    "min_aspect_ratio": 6.0,
    "max_cursor_component_area": 450,
    "max_component_bbox_width": 16,
    "max_component_bbox_height": 80,
    "temporal_stability_tol_px": 8,
    "max_temporal_velocity_px_s": 25.0,
    "expected_region_x_min": 256,
    "expected_region_x_max": 340,
    "expected_region_y_min": 880,
    "expected_region_y_max": 1000,
    "band": {
        "top_fraction": 0.30,
        "bottom_fraction": 0.80,
        "left_fraction": 0.08,
        "right_fraction": 0.92,
    },
}

_HARD_LIMITS = {
    "frames_per_scene": (1.0, 6.0),
    "min_cursor_width": (2.0, 4.0),
    "max_cursor_width": (5.0, 10.0),
    "min_cursor_height": (14.0, 36.0),
    "max_cursor_height": (50.0, 90.0),
    "min_aspect_ratio": (4.0, 8.0),
    "max_cursor_component_area": (200.0, 1000.0),
    "max_component_bbox_width": (8.0, 40.0),
    "max_component_bbox_height": (50.0, 120.0),
    "temporal_stability_tol_px": (4.0, 30.0),
    "max_temporal_velocity_px_s": (10.0, 60.0),
    "expected_region_x_min": (150.0, 350.0),
    "expected_region_x_max": (280.0, 450.0),
    "expected_region_y_min": (700.0, 950.0),
    "expected_region_y_max": (930.0, 1150.0),
}

_INT_KEYS = (
    "frames_per_scene",
    "min_cursor_width",
    "max_cursor_width",
    "min_cursor_height",
    "max_cursor_height",
    "max_cursor_component_area",
    "max_component_bbox_width",
    "max_component_bbox_height",
    "temporal_stability_tol_px",
    "expected_region_x_min",
    "expected_region_x_max",
    "expected_region_y_min",
    "expected_region_y_max",
)


def _np():
    import numpy
    return numpy


def load_policy(pol=None):
    """Load and clamp cursor QA policy against hard safety envelopes."""
    raw = (pol or {}).get(POLICY_KEY) if pol else None
    cfg = copy.deepcopy(DEFAULT_POLICY)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in cfg and not isinstance(cfg[k], dict):
                cfg[k] = v
        if isinstance(raw.get("band"), dict):
            cfg["band"].update(raw["band"])

    notes = []
    for k, (lo, hi) in _HARD_LIMITS.items():
        val = cfg.get(k)
        if val is None:
            continue
        try:
            fval = float(val)
        except (TypeError, ValueError):
            cfg[k] = DEFAULT_POLICY[k]
            notes.append(f"{k}={val!r} unparseable; reset to {cfg[k]}")
            continue
        clamped = max(lo, min(hi, fval))
        if clamped != fval:
            notes.append(f"{k}={fval} clamped to hard safety limit [{lo},{hi}] → {clamped}")
        cfg[k] = int(clamped) if k in _INT_KEYS else float(clamped)

    cfg["policy_notes"] = notes
    return cfg


def find_cursor_candidates(arr, cfg=None):
    """Detect candidate standalone caret components on a single frame.

    Applies connected-component ownership to reject tangent slices of large
    shapes (ellipses, rings, cards, diagrams, logos, network lines).
    Returns list of candidate dicts with bounding boxes and centroid coordinates.
    """
    if arr is None or arr.shape[0] < 1000 or arr.shape[1] < 600:
        return []
    np = _np()
    import palette_qa as pq

    cfg = cfg or DEFAULT_POLICY
    band_cfg = cfg.get("band") or DEFAULT_POLICY["band"]
    h, w = arr.shape[0], arr.shape[1]
    y0_band = int(h * band_cfg.get("top_fraction", 0.30))
    y1_band = int(h * band_cfg.get("bottom_fraction", 0.80))
    x0_band = int(w * band_cfg.get("left_fraction", 0.08))
    x1_band = int(w * band_cfg.get("right_fraction", 0.92))

    band = arr[y0_band:y1_band, x0_band:x1_band]
    r, g, b = band[..., 0], band[..., 1], band[..., 2]
    gold = (r > 170) & (g > 110) & (g < 235) & (b < 160) & (r > b + 80)
    if not gold.any():
        return []

    labels, n_comp = pq.label_components(gold)
    if n_comp == 0:
        return []
    comp_areas = np.bincount(labels.ravel())

    min_w = int(cfg["min_cursor_width"])
    max_w = int(cfg["max_cursor_width"])
    min_h = int(cfg["min_cursor_height"])
    max_h = int(cfg["max_cursor_height"])
    min_ar = float(cfg["min_aspect_ratio"])
    max_area = int(cfg["max_cursor_component_area"])
    max_bbox_w = int(cfg["max_component_bbox_width"])
    max_bbox_h = int(cfg["max_component_bbox_height"])

    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    ncol = band.shape[1]
    candidates_by_owner = {}

    for x in range(ncol - min_w):
        col = gold[:, x]
        if not col.any():
            continue
        best_len, run, start, run_start = 0, 0, 0, 0
        for y in range(col.shape[0]):
            if col[y]:
                if run == 0:
                    run_start = y
                run += 1
                if run > best_len:
                    best_len, start = run, run_start
            else:
                run = 0
        if not (min_h <= best_len <= max_h):
            continue

        y0, y1 = start, start + best_len
        seg = gold[y0:y1, x:x + max_w]
        solid = 0
        for wdx in range(seg.shape[1]):
            if float(seg[:, wdx].mean()) >= 0.85:
                solid += 1
            else:
                break
        if not (min_w <= solid <= max_w):
            continue
        if (best_len / float(solid)) < min_ar:
            continue

        # Abutting solid fill check
        edge_r = gold[y0:y1, min(x + solid, ncol - 1):min(x + solid + 14, ncol)]
        edge_l = gold[y0:y1, max(x - 14, 0):x]
        if (float(edge_r.mean()) > 0.5 if edge_r.size else False) or \
           (float(edge_l.mean()) > 0.5 if edge_l.size else False):
            continue

        # Rectangularity: flat top and bottom
        if float(gold[y0, x:x + solid].mean()) < 0.6 or \
           float(gold[y1 - 1, x:x + solid].mean()) < 0.6:
            continue

        # Width at mid-height
        ym = y0 + best_len // 2
        row = gold[ym, :]
        lo, hi = x, min(x + solid - 1, ncol - 1)
        while lo > 0 and row[lo - 1]:
            lo -= 1
        while hi < ncol - 1 and row[hi + 1]:
            hi += 1
        if (hi - lo + 1) > 2 * solid + 2:
            continue

        # Straightness: vertical alignment
        def _xc(y):
            xs = np.nonzero(gold[max(0, y):y + 1, max(0, x - 5):min(ncol, x + solid + 5)])[0]
            return float(xs.mean()) if xs.size else None
        c0, cm, c2 = _xc(y0 + 1), _xc(y0 + best_len // 2), _xc(y1 - 2)
        if None in (c0, cm, c2) or (max(c0, cm, c2) - min(c0, cm, c2)) > 2.5:
            continue

        # Dark surround
        sx0, sx1 = max(0, x - 40), min(ncol, x + solid + 40)
        sy0, sy1 = max(0, y0 - 40), min(band.shape[0], y1 + 40)
        surround = np.concatenate([lum[sy0:y0, sx0:sx1].ravel(),
                                   lum[y1:sy1, sx0:sx1].ravel()])
        if surround.size and float(np.median(surround)) >= 60:
            continue

        # Adjacency: isolated from neighboring gold
        jy0, jy1 = max(0, y0 - 10), min(band.shape[0], y1 + 10)
        left = gold[jy0:jy1, max(0, x - 30):max(0, x - 2)]
        right = gold[jy0:jy1, min(ncol - 1, x + solid + 2):min(ncol, x + solid + 30)]
        if (float(left.mean()) > 0.005 if left.size else False) or \
           (float(right.mean()) > 0.005 if right.size else False):
            continue

        # -------------------------------------------------------------------
        # CONNECTED-COMPONENT SHAPE OWNERSHIP
        # -------------------------------------------------------------------
        sub_labels = labels[y0:y1, x:x + solid]
        pos = sub_labels[sub_labels > 0]
        if pos.size == 0:
            continue
        owner_id = int(np.bincount(pos).argmax())
        owner_area = int(comp_areas[owner_id])

        # Rule 1: Component Area Rule (rejects ellipses, cards, charts, logos)
        if owner_area > max_area:
            continue

        # Rule 2: Component Bounding Box Rule
        comp_ys, comp_xs = np.nonzero(labels == owner_id)
        comp_w = int(comp_xs.max() - comp_xs.min() + 1)
        comp_h = int(comp_ys.max() - comp_ys.min() + 1)
        if comp_w > max_bbox_w or comp_h > max_bbox_h:
            continue

        # Rule 3: Candidate-to-Component Relationship (fill ratio)
        bar_area = solid * best_len
        if owner_area > int(1.35 * bar_area + 20):
            continue

        # Rule 4: Curvature / Continuation Rule
        if comp_h > best_len + 8:
            continue

        # Candidate verified in full-frame coordinates
        fx = x0_band + x
        fy = y0_band + y0
        cand = {
            "x": fx, "y": fy, "w": solid, "h": best_len,
            "cx": fx + solid / 2.0, "cy": fy + best_len / 2.0,
            "area": owner_area, "bbox": (fx, fy, solid, best_len),
            "owner_bbox": (x0_band + int(comp_xs.min()), y0_band + int(comp_ys.min()), comp_w, comp_h),
            "owner_id": owner_id,
        }
        if owner_id not in candidates_by_owner or solid > candidates_by_owner[owner_id]["w"]:
            candidates_by_owner[owner_id] = cand

    return list(candidates_by_owner.values())


def detect_cursor(arr, pol=None):
    """Backwards-compatible single-frame cursor detector with shape ownership.

    Returns True if a qualified standalone typing caret candidate exists in
    the frame, False otherwise (including for ellipse tangents, card borders,
    spines, chart axes, network lines, and logos).
    """
    cfg = load_policy(pol) if pol else DEFAULT_POLICY
    cands = find_cursor_candidates(arr, cfg)
    return bool(cands)


def analyze_scene_cursors(arrs, times, sc=None, layout=None, pol=None):
    """Multi-frame scene typing cursor detector and justification validator.

    Inspects a bounded deterministic set of decoded frames across the authoritative
    scene window. Considers:
      * Connected-component shape ownership (area, bounding box, continuation);
      * Temporal movement and stability (speed > 25 px/s or dx > 8 px rejects
        expanding rings and moving diagram elements);
      * Contextual justification (allowed only when code_justified=true and
        cursor_justified=true, within the expected code-entry region).
    """
    cfg = load_policy(pol) if pol else DEFAULT_POLICY
    sc = sc or {}
    sid = sc.get("scene_id", "unknown")
    is_code = bool(sc.get("code_justified"))
    is_cursor = bool(sc.get("cursor_justified"))

    # Expected code-entry region
    exp_x0 = int(cfg["expected_region_x_min"])
    exp_x1 = int(cfg["expected_region_x_max"])
    exp_y0 = int(cfg["expected_region_y_min"])
    exp_y1 = int(cfg["expected_region_y_max"])

    # If layout contains explicit render-event metadata for this scene, use it
    if layout and isinstance(layout.get("visual_plan"), dict):
        events = layout["visual_plan"].get("cursor_events") or []
        for ev in events:
            if ev.get("scene_id") == sid and "expected_region" in ev:
                rx0, ry0, rx1, ry1 = ev["expected_region"]
                margin = 40
                exp_x0, exp_x1 = rx0 - margin, rx1 + margin
                exp_y0, exp_y1 = ry0 - margin, ry1 + margin
                break

    # Extract candidates from each sampled frame
    per_frame_cands = []
    for k, arr in enumerate(arrs):
        cands = find_cursor_candidates(arr, cfg)
        t = times[k] if k < len(times) else float(k)
        per_frame_cands.append((t, cands))

    all_cands = [c for _, cands in per_frame_cands for c in cands]
    if not all_cands:
        return {"ok": True, "reason": None, "candidates": [], "detected": False,
                "justified": is_cursor}

    # Temporal analysis: detect translation across frames
    stationary_cands = []
    moving_cands = []
    tol_dx = float(cfg["temporal_stability_tol_px"])
    max_vel = float(cfg["max_temporal_velocity_px_s"])

    # Group candidate detections across frames
    if len(per_frame_cands) >= 2:
        for i in range(len(per_frame_cands)):
            t_i, cands_i = per_frame_cands[i]
            for c_i in cands_i:
                is_moving = False
                for j in range(len(per_frame_cands)):
                    if i == j:
                        continue
                    t_j, cands_j = per_frame_cands[j]
                    dt = abs(t_j - t_i)
                    for c_j in cands_j:
                        dx = abs(c_j["cx"] - c_i["cx"])
                        dy = abs(c_j["cy"] - c_i["cy"])
                        if dt > 0.01:
                            vel = dx / dt
                            if (dx > tol_dx or vel > max_vel) and dy < 50:
                                is_moving = True
                                break
                    if is_moving:
                        break
                if is_moving:
                    moving_cands.append(c_i)
                else:
                    stationary_cands.append(c_i)
    else:
        stationary_cands = all_cands

    # Candidates that move across frames are classified as animated graphic elements
    if not stationary_cands:
        return {"ok": True, "reason": None, "candidates": moving_cands,
                "detected": False, "note": "moving graphic elements rejected",
                "justified": is_cursor}

    # Evaluate stationary caret candidates against justification rules
    for c in stationary_cands:
        cx, cy = c["cx"], c["cy"]
        in_expected_region = (exp_x0 <= cx <= exp_x1) and (exp_y0 <= cy <= exp_y1)

        # A. Genuine cursor in justified coding scene
        if is_code and is_cursor:
            if not in_expected_region:
                return {
                    "ok": False,
                    "reason": f"rendered frame of scene {sid} contains a cursor outside expected code region ({c['bbox']})",
                    "candidates": stationary_cands,
                    "detected": True,
                }
            # Inside expected region: justified and verified
            continue

        # B & C. Cursor rendered in non-code scene or when cursor_justified=false
        return {
            "ok": False,
            "reason": f"rendered frame of scene {sid} contains an unjustified typing cursor",
            "candidates": stationary_cands,
            "detected": True,
        }

    return {"ok": True, "reason": None, "candidates": stationary_cands,
            "detected": True, "justified": is_cursor}
