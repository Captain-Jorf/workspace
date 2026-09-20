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
        raw = json.load(open(os.path.join(ROOT, "content", "editorial_policy.json"), encoding="utf-8"))
        block = raw.get("palette_qa")
        self.assertIsInstance(block, dict, "rendered-palette rules must live in ONE policy location")
        for key in ("min_luma_visible", "visible_min_chroma", "visible_min_blue_excess",
                    "dark_min_chroma", "dark_min_blue_excess", "min_region_core_px",
                    "single_frame_core_block_px", "max_frame_area_fraction",
                    "frames_per_scene", "persist_min_frames", "region_iou_min",
                    "region_centroid_tol_px", "max_total_frames", "band"):
            self.assertIn(key, block, f"policy key {key} missing")
        comment = (block.get("_comment") or "").lower()
        for concept in ("luminance", "chroma", "region", "persist", "no environment-variable override",
                        "matte black", "gold", "ivory"):
            self.assertIn(concept, comment, f"policy documentation must explain {concept!r}")
        for forbidden in ("blue", "navy", "cyan", "purple"):
            self.assertIn(forbidden, comment)          # named as PROHIBITED families

    def test_policy_values_are_clamped_into_the_hard_safety_envelope(self):
        cfg = pq.load_policy({"palette_qa": {
            "min_luma_visible": 999, "visible_min_chroma": 0, "dark_min_chroma": 1,
            "min_region_core_px": 4, "single_frame_core_block_px": 8,
            "max_frame_area_fraction": 0.9, "frames_per_scene": 99,
            "persist_min_frames": 99, "max_total_frames": 999999}})
        self.assertEqual(cfg["min_luma_visible"], 128)
        self.assertEqual(cfg["visible_min_chroma"], 10)
        self.assertEqual(cfg["dark_min_chroma"], 8)
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

    def test_declared_color_check_is_categorical(self):
        """A DECLARED swatch is judged by dominance alone — no luminance floor,
        no chroma floor, no region size; a near-black blue swatch is still
        rejected to stay conservative."""
        self.assertTrue(pq.declared_color_is_cold((40, 70, 190)))
        self.assertTrue(pq.declared_color_is_cold((0, 0, 10)))
        self.assertTrue(vp._is_cold((6, 6, 20)))
        self.assertFalse(vp._is_cold((233, 180, 74)))
        self.assertFalse(vp._is_cold((246, 240, 228)))
        # delta-6 boundary, exactly as the previous implementation behaved
        self.assertFalse(vp._is_cold((18, 20, 24)))      # 24 is not > 18 + 6
        self.assertTrue(vp._is_cold((18, 20, 25)))
        # the legacy cold code card is caught by the DETECTOR's delta-2 dominance
        # test (detect_code_card), not by the declared-swatch check
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
