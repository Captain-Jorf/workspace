"""Palette-QA unit tests (issue #26 §2, §6): policy, regions, single source.

These are the fast, deterministic, media-free unit tests for
``build/palette_qa.py``. The decoded H.264 round-trip cases live in
``tests/test_palette_qa_h264.py``.

Two structural guarantees are pinned here:

  * the cold-color family exists in EXACTLY ONE module — no production file
    outside ``build/palette_qa.py`` may contain the raw 8-bit cold inequality
    or the legacy hard-coded 0.002 fraction threshold;
  * every threshold comes from ONE documented policy block and there is no
    environment-variable override that could silently weaken it.
"""
import json
import os
import re
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import palette_qa as pq  # noqa: E402
import visual_plan as vp  # noqa: E402


class PolicyBlock(unittest.TestCase):
    def test_shipped_policy_block_is_used_without_clamping(self):
        cfg = pq.load_policy(common.policy())
        self.assertEqual(cfg["policy_notes"], [],
                         "the shipped palette_qa block must already be inside the hard envelopes")
        self.assertEqual(cfg["method"], pq.METHOD)
        self.assertEqual(cfg["single_frame_core_block_px"], 1024)

    def test_policy_block_carries_every_semantic_rule_with_documentation(self):
        with open(os.path.join(ROOT, "content", "editorial_policy.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        block = raw.get("palette_qa")
        self.assertIsInstance(block, dict, "rendered-palette rules must live in ONE policy location")
        for key in ("min_luma_visible", "visible_min_chroma", "dark_min_chroma",
                    "blue_min_excess", "cyan_min_excess", "cyan_hue_min", "cyan_hue_max",
                    "purple_min_red_over_green", "purple_min_blue_over_green",
                    "purple_hue_min", "purple_hue_max",
                    "min_region_core_px", "single_frame_core_block_px",
                    "max_frame_area_fraction", "frames_per_scene", "persist_min_frames",
                    "region_iou_min", "region_centroid_tol_px", "max_total_frames", "band"):
            self.assertIn(key, block, f"policy key {key} missing")
        comment = (block.get("_comment") or "").lower()
        for concept in ("luminance", "chroma", "region", "persist", "no environment-variable override",
                        "matte black", "gold", "ivory",
                        "independently", "balanced", "(0,255,255)", "(128,0,128)"):
            self.assertIn(concept, comment, f"policy documentation must explain {concept!r}")
        for forbidden in ("blue", "navy", "cyan", "purple"):
            self.assertIn(forbidden, comment)          # named as PROHIBITED families
        # the cyan and purple families are documented with their own guards
        self.assertIn("hue band", comment)
        self.assertIn("rose", comment)

    def test_policy_values_are_clamped_into_the_hard_safety_envelope(self):
        cfg = pq.load_policy({"palette_qa": {
            "min_luma_visible": 999, "visible_min_chroma": 0, "dark_min_chroma": 1,
            "blue_min_excess": 0, "cyan_min_excess": 0,
            "cyan_hue_min": 10, "cyan_hue_max": 999,
            "purple_min_red_over_green": 0, "purple_min_blue_over_green": 0,
            "purple_hue_min": 10, "purple_hue_max": 999,
            "min_region_core_px": 4, "single_frame_core_block_px": 8,
            "max_frame_area_fraction": 0.9, "frames_per_scene": 99,
            "persist_min_frames": 99, "max_total_frames": 999999}})
        self.assertEqual(cfg["min_luma_visible"], 128)
        self.assertEqual(cfg["visible_min_chroma"], 10)
        self.assertEqual(cfg["dark_min_chroma"], 8)
        self.assertEqual(cfg["blue_min_excess"], 6)
        self.assertEqual(cfg["cyan_min_excess"], 6)
        self.assertEqual((cfg["cyan_hue_min"], cfg["cyan_hue_max"]), (120, 260))
        self.assertEqual(cfg["purple_min_red_over_green"], 6)
        self.assertEqual(cfg["purple_min_blue_over_green"], 6)
        self.assertEqual((cfg["purple_hue_min"], cfg["purple_hue_max"]), (200, 360))
        # an EMPTY hue band would silently disable a family → fail closed to defaults
        deg = pq.load_policy({"palette_qa": {"cyan_hue_min": 250, "cyan_hue_max": 190,
                                            "purple_hue_min": 340, "purple_hue_max": 300}})
        self.assertEqual((deg["cyan_hue_min"], deg["cyan_hue_max"]),
                         (pq.DEFAULT_POLICY["cyan_hue_min"], pq.DEFAULT_POLICY["cyan_hue_max"]))
        self.assertEqual((deg["purple_hue_min"], deg["purple_hue_max"]),
                         (pq.DEFAULT_POLICY["purple_hue_min"], pq.DEFAULT_POLICY["purple_hue_max"]))
        self.assertTrue(any("empty hue band" in n for n in deg["policy_notes"]))
        self.assertEqual(cfg["min_region_core_px"], 64)
        # its own hard floor is 256: a single-frame block threshold can never sit
        # below the meaningful-region floor
        self.assertEqual(cfg["single_frame_core_block_px"], 256)
        self.assertEqual(cfg["max_frame_area_fraction"], 0.02)
        self.assertEqual(cfg["frames_per_scene"], 6)
        self.assertEqual(cfg["persist_min_frames"], 6)
        self.assertEqual(cfg["max_total_frames"], 120)
        self.assertTrue(cfg["policy_notes"])

    def test_malformed_policy_falls_back_to_calibrated_defaults(self):
        # (raw block, notes are expected?) — an EMPTY block means "no overrides",
        # so it is silent; a missing/invalid block is reported in the diagnostics.
        for raw, notes_expected in ((None, True), ("not-a-dict", True), ({}, False),
                                    ({"min_luma_visible": "high", "band": [1, 2]}, True)):
            cfg = pq.load_policy({"palette_qa": raw} if raw is not None else {})
            for key, default in pq.DEFAULT_POLICY.items():
                if key in ("band", "method"):
                    continue
                self.assertEqual(cfg[key], default, f"{key} with raw={raw!r}")
            self.assertEqual(bool(cfg["policy_notes"]), notes_expected, f"notes with raw={raw!r}")
        cfg = pq.load_policy({"palette_qa": {"band": {"top_fraction": 0.9, "bottom_fraction": 0.1}}})
        self.assertEqual(cfg["band"], pq.DEFAULT_POLICY["band"])

    def test_no_environment_variable_can_weaken_the_rules(self):
        with open(os.path.join(ROOT, "build", "palette_qa.py"), encoding="utf-8") as fh:
            code = fh.read().split('"""', 2)[2]      # skip the module docstring
        # the real risk is environment ACCESS, not the word appearing in prose
        for token in ("os.environ", "getenv", "environ["):
            self.assertNotIn(token, code,
                             f"palette_qa must not read the environment ({token})")
        before = pq.load_policy(common.policy())
        for name in ("PALETTE_QA", "PALETTE_QA_MIN_LUMA_VISIBLE", "PALETTE_MIN_REGION_CORE_PX",
                     "COLD_PIXEL_FRACTION", "VISUAL_SEMANTICS", "QA_PALETTE_DISABLE"):
            os.environ[name] = "1"
        try:
            self.assertEqual(pq.load_policy(common.policy()), before)
        finally:
            for name in ("PALETTE_QA", "PALETTE_QA_MIN_LUMA_VISIBLE", "PALETTE_MIN_REGION_CORE_PX",
                         "COLD_PIXEL_FRACTION", "VISUAL_SEMANTICS", "QA_PALETTE_DISABLE"):
                os.environ.pop(name, None)


class SingleSourceOfTheColdFormula(unittest.TestCase):
    """The raw inequality may exist only in palette_qa (issue #26 §2)."""

    def _build_sources(self):
        d = os.path.join(ROOT, "build")
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                with open(os.path.join(d, name), encoding="utf-8") as fh:
                    yield name, fh.read()

    def test_only_palette_qa_contains_the_raw_cold_inequality(self):
        for name, src in self._build_sources():
            if name == "palette_qa.py":
                continue
            self.assertNotIn("b > r", src, f"{name} duplicates the raw cold inequality")
            self.assertNotIn("b >= g", src, f"{name} duplicates the raw cold inequality")

    def test_only_palette_qa_carries_the_legacy_fraction_threshold(self):
        for name, src in self._build_sources():
            if name == "palette_qa.py":
                continue
            self.assertIsNone(re.search(r"0\.002\b", src),
                              f"{name} still hard-codes the legacy cold-fraction threshold")

    def test_visual_plan_cold_helpers_delegate(self):
        with open(os.path.join(ROOT, "build", "visual_plan.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("palette_qa.raw_cold_pixel_fraction", src)
        self.assertIn("palette_qa.declared_color_is_cold", src)
        # the deprecated fraction helper must stay a wrapper for test consumers
        self.assertEqual(vp.cold_pixel_fraction(np.full((8, 8, 3), 20, np.uint8)), 0.0)

    def test_qa_supervisor_has_no_second_cold_metric(self):
        with open(os.path.join(ROOT, "build", "qa_supervisor.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("cold_frac", src)
        self.assertIn("palette_qa.analyze_scene", src)
        self.assertIn("palette_qa.scene_verdict", src)


class RegionLabelling(unittest.TestCase):
    def test_labels_are_dense_one_based_with_zero_background(self):
        mask = np.zeros((8, 8), bool)
        mask[1:4, 1:4] = True
        mask[5:7, 5:7] = True
        labels, count = pq.label_components(mask)
        self.assertEqual(count, 2)
        self.assertEqual(int(labels[0, 0]), 0)
        self.assertEqual(sorted(set(np.unique(labels)) - {0}), [1, 2])
        self.assertEqual(int((labels == 1).sum()), 9)
        self.assertEqual(int((labels == 2).sum()), 4)

    def test_connectivity_is_four_connected_and_deterministic(self):
        mask = np.zeros((4, 4), bool)
        mask[0, 0] = True
        mask[1, 1] = True                      # diagonal ≠ connected
        _l, count = pq.label_components(mask)
        self.assertEqual(count, 2)
        again, _ = pq.label_components(mask)
        first, _ = pq.label_components(mask)
        self.assertTrue(np.array_equal(first, again))

    def test_empty_mask_yields_no_labels(self):
        labels, count = pq.label_components(np.zeros((5, 5), bool))
        self.assertEqual(count, 0)
        self.assertFalse(labels.any())

    def test_erosion_removes_speckle_but_keeps_solid_interiors(self):
        speck = np.zeros((10, 10), bool)
        speck[4, 4] = True
        self.assertFalse(pq._erode(speck).any())
        solid = np.zeros((10, 10), bool)
        solid[3:7, 3:7] = True
        self.assertEqual(int(pq._erode(solid).sum()), 4)

    def test_region_records_drop_noise_and_keep_meaningful_regions(self):
        cfg = pq.load_policy(common.policy())
        small = np.zeros((60, 60), bool)
        small[10:18, 10:18] = True                     # 8x8 core → 64 px < 256
        big = np.zeros((60, 60), bool)
        big[20:40, 20:40] = True                       # 20x20 core → 400 px
        core = small | big
        visible = big & (np.arange(60)[None, :] < 30)  # majority of the big region is "visible"
        lum = np.full((60, 60), 30.0)
        chroma = np.full((60, 60), 20.0)
        records = pq.region_records(core, visible, lum, chroma, cfg)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["core_px"], 400)
        self.assertEqual(records[0]["path"], "visible")
        self.assertEqual(records[0]["bbox"], [20, 20, 39, 39])

    def test_band_slice_excludes_frame_edges(self):
        arr = np.zeros((2304, 1296, 3), np.int16)
        rows, cols = pq.band_slice(arr, pq.load_policy(common.policy()))
        self.assertEqual(rows.start, int(2304 * 0.25))
        self.assertEqual(rows.stop, int(2304 * 0.85))
        self.assertEqual((cols.start, cols.stop), (40, 1256))


class PerPixelEvidence(unittest.TestCase):
    def test_luma_chroma_and_blue_excess_agree_with_the_measured_envelope(self):
        noise = np.array([[[5, 6, 13], [8, 6, 15]]], np.int16)      # real codec noise
        navy = np.array([[[10, 26, 46]]], np.int16)                 # #0A1A2E
        self.assertLess(float(pq.luma(noise).max()), 30.0)
        self.assertLessEqual(float(pq.chroma(noise).max()), 10.0)
        self.assertGreater(float(pq.chroma(navy).max()), 30.0)
        self.assertTrue(bool((pq.blue_excess(navy) >= 8).all()))

    def test_legacy_inequality_is_kept_only_as_a_diagnostic(self):
        noise = np.array([[[5, 6, 13], [8, 6, 15]]], np.int16)
        self.assertEqual(int(pq.cold_dominance_mask(noise).sum()), 2)
        self.assertAlmostEqual(pq.raw_cold_pixel_fraction(noise), 1.0)
        warm = np.array([[[233, 180, 74]]], np.int16)
        self.assertEqual(pq.raw_cold_pixel_fraction(warm), 0.0)

    def test_every_prohibited_family_is_classified_independently(self):
        """The gap this closes: canonical cyan and canonical purple are BALANCED
        (b - max(r, g) == 0) and evade a blue-dominance-only rule."""
        self.assertEqual(pq.blue_excess(np.array([[[0, 255, 255]]], np.int16))[0, 0], 0)
        self.assertEqual(pq.blue_excess(np.array([[[128, 0, 128]]], np.int16))[0, 0], 0)
        self.assertEqual(pq.cold_families_of((0, 0, 255)), ["blue"])
        self.assertEqual(pq.cold_families_of((10, 26, 46))[0], "blue")       # dark navy
        self.assertEqual(pq.cold_families_of((0, 255, 255)), ["cyan"])       # canonical cyan
        self.assertIn("cyan", pq.cold_families_of((0, 100, 120)))            # darker cyan
        self.assertIn("cyan", pq.cold_families_of((0, 140, 160)))            # teal
        self.assertEqual(pq.cold_families_of((128, 0, 128)), ["purple"])     # canonical purple
        self.assertIn("purple", pq.cold_families_of((48, 0, 64)))            # dark purple
        self.assertIn("purple", pq.cold_families_of((160, 32, 200)))         # magenta-purple
        for warm in ((0, 200, 0), (200, 0, 0), (230, 140, 40), (196, 74, 88),
                     (233, 180, 74), (246, 240, 228), (139, 69, 19), (60, 40, 25),
                     (128, 128, 128), (245, 243, 238), (30, 30, 32)):
            self.assertEqual(pq.cold_families_of(warm), [], f"{warm} must not be cold")

    def test_family_masks_keep_ordinary_green_and_warm_rose_out(self):
        """The cyan guard excludes green-lead colors; the purple floors exclude
        warm rose (blue only marginally above green)."""
        np_ = np
        frame = np_.zeros((1, 4, 3), np.int16)
        frame[0, 0] = (0, 200, 0)        # pure green (hue 120)
        frame[0, 1] = (0, 255, 128)      # spring green (hue 150 — below the band)
        frame[0, 2] = (196, 74, 88)      # warm rose (hue 347, b - g == 14 < floor)
        frame[0, 3] = (230, 80, 110)     # hot pink-red (hue 348, b - g == 30 < 32)
        masks = pq.family_masks(frame, pq.load_policy(common.policy()))
        for name, mask in masks.items():
            self.assertFalse(bool(mask.any()), f"{name} family false-positived on a warm color")

    def test_declared_color_check_is_categorical(self):
        """A DECLARED swatch uses the SAME three family rules as rendered pixels
        (no luminance floor, no chroma floor, no region size) — including the
        balanced canonical cyan and purple that a blue-only rule missed."""
        for cold in ((40, 70, 190), (0, 0, 10), (6, 6, 20), (0, 255, 255), (128, 0, 128),
                     (0, 128, 128), (143, 0, 255)):
            self.assertTrue(vp._is_cold(cold), f"declared swatch {cold} must be cold")
        for warm in ((233, 180, 74), (246, 240, 228), (246, 228, 190), (122, 88, 32),
                     (16, 13, 10), (168, 148, 116), (30, 30, 32), (196, 74, 88)):
            self.assertFalse(vp._is_cold(warm), f"declared swatch {warm} must stay allowed")
        # near-black blue-bias below the family floor stays allowed as a DECLARATION
        self.assertFalse(vp._is_cold((18, 20, 24)))      # blue is only 6 above red
        # that same near-black card IS caught by the DETECTOR's delta-2 dominance
        # test (detect_code_card), which is a separate, legacy-signature check
        self.assertTrue(bool(pq.cold_dominance_mask(np.array([[[18, 20, 24]]]), 2)[0, 0]))


class SceneAnalysisContract(unittest.TestCase):
    CFG = None

    @classmethod
    def setUpClass(cls):
        cls.CFG = pq.load_policy(common.policy())

    def _frame(self, color=None, size=0):
        a = np.zeros((2304, 1296, 3), np.int16)
        a[..., 0], a[..., 1], a[..., 2] = 10, 8, 6
        if color and size:
            a[700:700 + size, 400:400 + size] = color
        return a

    def test_speckle_only_frame_reports_noise_not_a_region(self):
        """Isolated pixels that DO pass the dark-candidate floor (chroma 14,
        blue_excess 14) must still be reported as noise: there is no coherent
        3x3 core, so no meaningful region and no block."""
        frame = self._frame()
        for xy in ((700, 400), (701, 402), (720, 430)):
            frame[xy[0], xy[1]] = (6, 6, 20)
        res = pq.analyze_frame(frame, self.CFG)
        self.assertFalse(res["blocked"])
        self.assertEqual(res["regions"], [])
        self.assertGreater(res["noise_only_px"], 0)
        self.assertEqual(res["meaningful_area_px"], 0)

    def test_warm_frame_has_zero_evidence(self):
        frame = self._frame(color=(233, 180, 74), size=200)
        res = pq.analyze_frame(frame, self.CFG)
        self.assertEqual(res["candidate_px"], 0)
        self.assertEqual(res["raw_cold_px"], 0)
        self.assertFalse(res["blocked"])

    def test_scene_verdict_shape_and_diagnostics_are_safe(self):
        res = pq.analyze_scene([self._frame() for _ in range(3)], self.CFG)
        ok, reason = pq.scene_verdict(res)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        for key in ("frames_sampled", "max_meaningful_core_px", "max_meaningful_area_fraction",
                    "persistent_cold_regions", "noise_only_px_total", "raw_cold_px_total",
                    "per_frame", "blocked", "reason"):
            self.assertIn(key, res)
        blob = json.dumps(res).lower()
        for forbidden in ("http", ".jpg", ".png", "creator", "asset_url", "query"):
            self.assertNotIn(forbidden, blob)


class ScalarVectorEquivalence(unittest.TestCase):
    """The scalar rule (declared swatches, numpy-free import path) and the
    vectorized masks (rendered frames) must classify identically — this is what
    keeps ONE cold-color definition from drifting into two."""

    def test_scalar_and_vectorized_families_agree_on_a_full_color_grid(self):
        cfg = pq.load_policy(common.policy())
        vals = list(range(0, 256, 17))
        colors = [(r, g, b) for r in vals for g in vals for b in vals]
        grid = np.array([[list(c)] for c in colors], np.int16)
        masks = pq.family_masks(grid, cfg)
        for i, color in enumerate(colors):
            scalar = pq.cold_families_of(color, cfg)
            vector = [name for name in pq.COLD_FAMILIES if bool(masks[name][i, 0])]
            self.assertEqual(scalar, vector, f"scalar/vector disagree on {color}")
        self.assertGreater(len(colors), 4000)

    def test_hue_scalar_matches_the_vectorized_hue(self):
        vals = list(range(0, 256, 23))
        colors = [(r, g, b) for r in vals for g in vals for b in vals]
        arr = np.array([[list(c)] for c in colors], np.int16)
        hues = pq.hue(arr)
        for i, color in enumerate(colors):
            self.assertAlmostEqual(pq.hue_scalar(color), float(hues[i, 0]), places=4,
                                   msg=f"hue disagrees on {color}")

    def test_declared_palette_check_needs_no_numpy(self):
        """visual_plan validates its brand palette at import time, which must not
        require the scientific stack (the light verify/note/record subcommands
        run in a minimal interpreter)."""
        with open(os.path.join(ROOT, "build", "palette_qa.py"), encoding="utf-8") as fh:
            code = fh.read()
        body = code[code.index("def hue_scalar"):code.index("def _family_index")]
        self.assertNotIn("_np()", body, "the scalar path must stay numpy-free")
        self.assertNotIn("np.", body)


class ColdFamiliesBlock(unittest.TestCase):
    """Every prohibited family blocks on a coherent region — including the two
    BALANCED canonical colors that a blue-dominance-only rule cannot see."""

    @classmethod
    def setUpClass(cls):
        cls.CFG = pq.load_policy(common.policy())

    def _frame(self, color, size=120, y=700, x=400):
        a = np.zeros((2304, 1296, 3), np.int16)
        a[..., 0], a[..., 1], a[..., 2] = 10, 8, 6
        a[y:y + size, x:x + size] = color
        return a

    def _family(self, color):
        res = pq.analyze_frame(self._frame(color), self.CFG)
        self.assertTrue(res["blocked"], f"{color} must block as a coherent region")
        self.assertGreaterEqual(len(res["regions"]), 1)
        return res["regions"][0]["family"]

    def test_blue_family_blocks(self):
        self.assertEqual(self._family((0, 0, 255)), "blue")        # pure blue
        self.assertEqual(self._family((10, 26, 46)), "blue")       # dark navy

    def test_canonical_cyan_blocks(self):
        """RGB(0,255,255): b - max(r,g) == 0, invisible to the old blue rule."""
        self.assertEqual(self._family((0, 255, 255)), "cyan")

    def test_cyan_and_teal_variants_block(self):
        self.assertEqual(self._family((0, 128, 128)), "cyan")      # teal
        self.assertEqual(self._family((0, 100, 120)), "cyan")      # darker cyan
        self.assertEqual(self._family((0, 140, 160)), "cyan")      # teal leaning cyan
        self.assertEqual(self._family((0, 60, 70)), "cyan")        # dark cyan/teal

    def test_canonical_purple_blocks(self):
        """RGB(128,0,128): b - max(r,g) == 0, invisible to the old blue rule."""
        self.assertEqual(self._family((128, 0, 128)), "purple")

    def test_violet_and_magenta_variants_block(self):
        self.assertEqual(self._family((143, 0, 255)), "purple")    # violet
        self.assertEqual(self._family((48, 0, 64)), "purple")      # dark purple
        self.assertEqual(self._family((160, 32, 200)), "purple")   # magenta-purple
        self.assertEqual(self._family((255, 0, 255)), "purple")    # magenta

    def test_family_px_counts_are_reported_per_family(self):
        res = pq.analyze_frame(self._frame((0, 255, 255)), self.CFG)
        self.assertGreater(res["family_px"]["cyan"], 0)
        self.assertEqual(res["family_px"]["blue"], 0)
        self.assertEqual(res["family_px"]["purple"], 0)


class WarmBoundariesStayAllowed(unittest.TestCase):
    """Overblocking guard: the declared brand palette and warm/neutral
    neighbours must never be classified as cold."""

    @classmethod
    def setUpClass(cls):
        cls.CFG = pq.load_policy(common.policy())

    def _frame(self, color, size=120, y=700, x=400):
        a = np.zeros((2304, 1296, 3), np.int16)
        a[..., 0], a[..., 1], a[..., 2] = 10, 8, 6
        a[y:y + size, x:x + size] = color
        return a

    def _assert_allowed(self, color, label):
        res = pq.analyze_frame(self._frame(color), self.CFG)
        self.assertFalse(res["blocked"], f"{label} {color} must stay allowed")
        self.assertEqual(res["candidate_px"], 0, f"{label} {color} must not be a candidate")
        self.assertEqual(pq.cold_families_of(color, self.CFG), [], label)

    def test_every_declared_brand_color_is_allowed(self):
        for name, rgb in sorted(vp.BRAND_PALETTE.items()):
            self._assert_allowed(tuple(rgb), f"brand {name}")

    def test_warm_and_neutral_neighbours_are_allowed(self):
        for color, label in (((139, 69, 19), "warm brown"),
                             ((60, 40, 25), "dark warm brown"),
                             ((128, 128, 128), "neutral gray"),
                             ((245, 243, 238), "off-white"),
                             ((30, 30, 32), "desaturated shadow"),
                             ((0, 200, 0), "green without cyan"),
                             ((200, 0, 0), "red"),
                             ((230, 140, 40), "warm orange"),
                             ((196, 74, 88), "rose / warm red"),
                             ((230, 80, 110), "hot pink-red")):
            self._assert_allowed(color, label)

    def test_antialiased_warm_edges_are_allowed(self):
        """Linear ramps between the brand colors and the matte-black base model
        the antialiased edges H.264 actually produces — none may be cold."""
        base = (16, 13, 10)
        for warm in (vp.GOLD, vp.GOLD_HI, vp.IVORY, vp.BRONZE, vp.AMBER):
            for k in range(0, 101, 5):
                mix = tuple(int(round(base[i] + (warm[i] - base[i]) * k / 100.0)) for i in range(3))
                self.assertEqual(pq.cold_families_of(mix, self.CFG), [],
                                 f"antialiased edge {mix} (from {warm}) must stay allowed")

    def test_isolated_cold_bias_speckles_are_diagnostic_only(self):
        """A single sub-threshold or isolated cold pixel is codec noise, not an
        object: it must be reported, never blocked (spatial rules decide)."""
        frame = self._frame((233, 180, 74), size=200)
        speckles = [(700, 400, (5, 6, 13)),        # measured near-black noise
                    (701, 402, (8, 6, 15)),        # measured envelope, higher chroma
                    (720, 430, (6, 12, 20)),       # cyan-biased candidate speckle
                    (740, 460, (10, 6, 15))]       # purple-biased speckle
        for y, x, value in speckles:
            frame[y, x] = value
        res = pq.analyze_frame(frame, self.CFG)
        self.assertFalse(res["blocked"])
        self.assertEqual(res["regions"], [])
        self.assertEqual(res["coherent_core_px"], 0)
        self.assertGreater(res["noise_only_px"] + res["sub_threshold_px"], 0)


class OldGateRegression(unittest.TestCase):
    """Exact regression for the content-blind gate (issue #26).

    Constructs a DECODED-FRAME-STYLE fixture (no encoder needed, so the numbers
    are exact and reproducible) in which MORE than the real incident's 0.23% of
    the QA band carries isolated near-black blue-biased pixels matching the old
    raw inequality. No coherent region exists after erosion/component analysis,
    so the old content-blind fraction rule (> 0.002) blocks while the new
    perceptual+spatial rule passes and reports the pixels as noise.
    """

    @classmethod
    def setUpClass(cls):
        cls.CFG = pq.load_policy(common.policy())

    @staticmethod
    def build_fixture(h=2304, w=1296):
        rows, cols = pq.band_slice(np.zeros((h, w, 3), np.int16), pq.load_policy(common.policy()))
        band_h, band_w = rows.stop - rows.start, cols.stop - cols.start
        frame = np.zeros((h, w, 3), np.int16)
        frame[..., 0], frame[..., 1], frame[..., 2] = 10, 8, 6
        cells = [(dy, dx) for dy in range(0, band_h - 2, 3) for dx in range(0, band_w - 2, 3)]
        placed = 0
        for k, (dy, dx) in enumerate(cells[::37]):     # stride 3 => >=3 px apart
            y, x = rows.start + dy, cols.start + dx
            if k % 2 == 0:
                frame[y, x] = (6, 6, 20)               # candidate, blue family, chroma 14
            else:
                frame[y, x] = (5 + (y % 4), 6, 13 + (x % 3))   # measured noise envelope
            placed += 1
        return frame, placed

    def test_fixture_exceeds_the_real_incident_fraction_under_the_old_rule(self):
        frame, placed = self.build_fixture()
        rows, cols = pq.band_slice(frame, self.CFG)
        band = frame[rows, cols]
        legacy_fraction = float(pq.cold_dominance_mask(band).mean())
        self.assertGreater(legacy_fraction, 0.0023,
                           "fixture must exceed the real incident's 0.23%")
        self.assertGreater(legacy_fraction, 0.002,
                           "the old content-blind gate would have blocked this fixture")
        flagged = band[pq.cold_dominance_mask(band)]
        lum = pq.luma(flagged.reshape(1, -1, 3))
        self.assertLess(float(lum.max()), 30.0, "every flagged pixel must be near-black")
        self.assertGreater(placed, 0)

    def test_new_detector_passes_the_same_fixture_and_reports_the_noise(self):
        frame, _ = self.build_fixture()
        res = pq.analyze_frame(frame, self.CFG)
        self.assertFalse(res["blocked"], res["block_reason"])
        self.assertEqual(res["regions"], [], "no coherent region may survive erosion")
        self.assertEqual(res["coherent_core_px"], 0)
        self.assertEqual(res["max_region_core_px"], 0)
        self.assertGreater(res["noise_only_px"], 0,
                           "candidate-but-disconnected pixels must be reported as noise")
        self.assertGreater(res["sub_threshold_px"], 0,
                           "measured-envelope pixels below the floors must be reported")
        self.assertLess(res["candidate_area_fraction"], self.CFG["max_frame_area_fraction"])

    def test_fixture_is_deterministic(self):
        first, n1 = self.build_fixture()
        second, n2 = self.build_fixture()
        self.assertEqual(n1, n2)
        self.assertTrue(np.array_equal(first, second))


if __name__ == "__main__":
    unittest.main(verbosity=2)
