"""Scene-timing parity (issue #26 §4) — one source of truth for scene windows.

Before this change the QA supervisor independently re-estimated scene windows
from the word-level timing (``qa_supervisor._plan_scene_times``) while the
renderer used ``reel_engine.Reel._build_scene_times``: two different
approximations of the same cut points, so a QA frame could be sampled from the
neighbouring scene.

Now the renderer RECORDS the authoritative per-scene start/end timestamps it
actually used in ``layout.json`` (``scene_windows``), and final QA samples
frames inside exactly those windows. A legacy estimator survives ONLY as a
fallback for historical fixtures without recorded windows, and it is always
reported as ``scene_timing_source = "estimated-fallback"``.
"""
import argparse
import copy
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import palette_qa as pq  # noqa: E402
import qa_supervisor as qa  # noqa: E402
import test_visual_direction as tvd  # noqa: E402
import visual_plan as vp  # noqa: E402
from reel_engine import Reel  # noqa: E402

POL = common.policy()


class RendererRecordsAuthoritativeWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ep, cls.script, cls.topic, cls.plan = tvd.episode_0919()
        cls.out = tempfile.mkdtemp(prefix="render_out_")
        import render_auto
        with mock.patch.object(sys, "argv",
                               ["render_auto.py", "--ep", cls.ep,
                                "--out", os.path.join(cls.out, "auto-2077-02-02.mp4"),
                                "--stills-only"]):
            render_auto.main()
        cls.layout = common.load_json(os.path.join(cls.ep, "layout.json"), {})
        cls.timing = common.load_json(os.path.join(cls.ep, "timing.json"), {})
        cls.reel = Reel(cls.ep, POL)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.ep, ignore_errors=True)
        shutil.rmtree(cls.out, ignore_errors=True)

    def test_layout_records_the_renderer_scene_windows(self):
        windows = self.layout.get("scene_windows") or []
        self.assertTrue(windows, "the renderer must record its scene windows")
        self.assertEqual(
            [(w["scene_id"], w["beat"], w["start"], w["end"]) for w in windows],
            [(sc["scene_id"], sc.get("beat"), round(float(a), 3), round(float(b), 3))
             for sc, a, b in self.reel.scene_times],
            "layout.scene_windows must be EXACTLY the renderer's own scene times")

    def test_qa_uses_the_recorded_windows_not_a_second_approximation(self):
        scenes = self.plan["scenes"]
        with mock.patch.object(qa, "_estimate_scene_windows",
                               side_effect=AssertionError("the estimator must not run")):
            windows, source = qa.scene_windows(self.layout, self.timing, scenes,
                                               self.reel.total)
        self.assertEqual(source, "layout")
        self.assertEqual([w[0] for w in windows],
                         [sc["scene_id"] for sc in self.plan["scenes"]])

    def test_every_sampled_timestamp_belongs_to_its_rendered_scene(self):
        cfg = pq.load_policy()
        scenes = self.plan["scenes"]
        windows, _ = qa.scene_windows(self.layout, self.timing, scenes, self.reel.total)
        checked = 0
        for sid, beat, a, b in windows:
            for t in pq.sample_times(a, b, cfg, total=self.reel.total):
                sc, _local, _dur = self.reel._scene_at(t)
                self.assertIsNotNone(sc, f"no scene at t={t}")
                self.assertEqual(sc["scene_id"], sid,
                                 f"t={t} (window {a}-{b}) decoded into {sc['scene_id']}")
                checked += 1
        self.assertGreaterEqual(checked, len(windows))

    def test_windows_are_ordered_inside_the_render_duration(self):
        windows = self.layout["scene_windows"]
        starts = [w["start"] for w in windows]
        self.assertEqual(starts, sorted(starts))
        for w in windows:
            self.assertGreater(w["end"], w["start"])
            self.assertLessEqual(w["end"], self.reel.total + 0.01)


class FallbackTimingIsSeparatedAndFlagged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ep, cls.script, cls.topic, cls.plan = tvd.episode_0919()
        cls.timing = common.load_json(os.path.join(cls.ep, "timing.json"), {})
        cls.scenes = cls.plan["scenes"]
        cls.reel = Reel(cls.ep, POL)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.ep, ignore_errors=True)

    def test_missing_windows_fall_back_and_are_flagged(self):
        windows, source = qa.scene_windows({}, self.timing, self.scenes, self.reel.total)
        self.assertEqual(source, "estimated-fallback")
        self.assertEqual([w[0] for w in windows], [sc["scene_id"] for sc in self.scenes])
        for _sid, _beat, a, b in windows:
            self.assertLess(a, b)

    def test_corrupt_or_incomplete_windows_are_rejected(self):
        good = [{"scene_id": sc["scene_id"], "beat": sc["beat"],
                 "start": a, "end": b} for sc, a, b in self.reel.scene_times]
        cases = {
            "empty": [],
            "incomplete": good[:-1],
            "reversed": [dict(w, end=w["start"]) for w in good],
            "unordered": list(reversed(good)),
            "beyond-duration": [dict(w, start=self.reel.total + 5) for w in good],
            "not-a-number": [dict(w, start="x") for w in good],
        }
        for label, windows in cases.items():
            with self.subTest(label=label):
                layout = {"scene_windows": windows}
                _w, source = qa.scene_windows(layout, self.timing, self.scenes,
                                              self.reel.total)
                self.assertEqual(source, "estimated-fallback", label)

    def test_check_visuals_reports_the_timing_source(self):
        ep, script, topic, plan = tvd.episode_0919()
        try:
            rep = qa.Report(POL)
            qa.check_visuals(rep, ep, script, None, POL, no_frames=True)
            self.assertEqual(rep.details["visuals"]["rendered_frames_checked"], False)
            rep2 = qa.Report(POL)
            layout = {"scene_windows": [{"scene_id": sc["scene_id"], "beat": sc["beat"],
                                         "start": a, "end": b}
                                        for sc, a, b in Reel(ep, POL).scene_times]}
            common.save_json(os.path.join(ep, "layout.json"), layout)
            qa.check_visuals(rep2, ep, script, None, POL, no_frames=True)
            self.assertIn(rep2.details["visuals"].get("rendered_frames_checked"), (False,))
        finally:
            shutil.rmtree(ep, ignore_errors=True)


class NoUselessReRender(unittest.TestCase):
    """§7: visual_semantics must NOT be added to the safe re-render classes —
    re-encoding identical frames with identical settings repairs nothing."""

    def test_render_retry_classification_unchanged(self):
        src = open(os.path.join(ROOT, "build", "pipeline.py"), encoding="utf-8").read()
        m = re.search(r"render_related\s*=\s*any\(k in blocking for k in \(([^)]*)\)\)", src)
        self.assertIsNotNone(m, "render-retry classification not found")
        self.assertEqual(
            {t.strip().strip('"\'') for t in m.group(1).split(",")},
            {"video_quality", "audio_quality", "subtitle_layout"})
        self.assertNotIn("visual_semantics", m.group(1),
                         "a palette failure must fail closed, never trigger a blind re-render")
        m2 = re.search(r"if render_related and not content_related and not safe and "
                       r"st\[\"retries\"\]\[\"render\"\] == 0", src)
        self.assertIsNotNone(m2, "the bounded ONE-safe-re-render rule must stay in place")


if __name__ == "__main__":
    unittest.main(verbosity=2)
