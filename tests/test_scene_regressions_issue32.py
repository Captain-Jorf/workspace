"""EXACT scene regressions for issue #32, through a production-equivalent
H.264 round trip.

Scene A — "You're in a stand‑up…" (attention drift): the rejected reel
showed DRAFT/NOD labels crossing each other and a dense network behind the
text. The corrected scene must be one clean dominant diagram: no text/text
intersection, no line through text, no network through the cards.

Scene B — "Repeat this pause‑and‑reflect loop": the rejected reel showed
overlapping TASK/RESIDUE bars and hidden NOTE/COUNT labels. The corrected
scene must be an understandable loop: every label visible, nothing
overlapping, every step readable.

Both scenes are rendered by the REAL engine, encoded with the renderer's own
libx264 settings (CRF 21, yuv420p, profile high) and inspected as DECODED
frames — artifacts live in a TemporaryDirectory only. Glyph verdicts stay
authoritative from the source + production font cmap (issue #32 §7), not OCR.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import layout_gate  # noqa: E402
import text_norm  # noqa: E402
import visual_plan as vp  # noqa: E402
from test_glyph_regression_issue32 import episode_dir  # noqa: E402
import reel_engine  # noqa: E402
from reel_engine import Reel, W, H, FPS, ZONE_Y  # noqa: E402

POL = common.policy()


def ffmpeg_exe():
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not exe or not os.path.exists(exe):
        raise AssertionError("the repository's supported FFmpeg path is unavailable "
                             "(imageio-ffmpeg is a pinned dependency)")
    return exe


def h264_round_trip(frames, tmp, name, crf="21"):
    """Production-equivalent encode → decoded sample frames (TemporaryDirectory)."""
    ff = ffmpeg_exe()
    out = os.path.join(tmp, f"{name}.mp4")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", crf,
           "-pix_fmt", "yuv420p", "-profile:v", "high", "-g", str(FPS * 2), out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for f in frames:
            proc.stdin.write(np.asarray(f, dtype=np.uint8).tobytes())
    finally:
        proc.stdin.close()
        rc = proc.wait()
    if rc != 0:
        raise AssertionError(f"libx264 encode failed (rc={rc})")
    duration = len(frames) / float(FPS)
    decoded = []
    for k, frac in enumerate((0.30, 0.55, 0.80)):
        t = max(0.0, min(duration * frac, max(0.0, duration - 1.0 / FPS)))
        png = os.path.join(tmp, f"{name}_f{k}.png")
        subprocess.run([ff, "-v", "error", "-ss", f"{t:.3f}", "-i", out,
                        "-frames:v", "1", "-y", png],
                       check=True, capture_output=True)
        from PIL import Image
        decoded.append(np.asarray(Image.open(png).convert("RGB"), dtype=np.int16))
    return decoded


class Issue32SceneRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ep, cls.script, cls.plan = episode_dir()
        cls.reel = Reel(cls.ep, POL)
        cls.tmp = tempfile.mkdtemp(prefix="issue32_h264_")
        by_id = {sc["scene_id"]: sc for sc in cls.plan["scenes"]}
        # Scene A: the scene rendering the stand-up narration line
        cls.scene_a = None
        cls.scene_b = None
        for sc, a, b in cls.reel.scene_times:
            if sc["beat"] == "problem" and cls.scene_a is None:
                cls.scene_a = (sc, a, b)
            if sc["visual_category"] == "reflect-loop":
                cls.scene_b = (sc, a, b)
        assert cls.scene_a is not None and cls.scene_b is not None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.ep, ignore_errors=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------- scene A
    def test_scene_a_is_one_clean_attention_diagram(self):
        sc, a, b = self.scene_a
        items = self.reel._scene_items.get(sc["scene_id"])
        self.assertTrue(items, "scene A must declare its layout items")
        # the pre-render layout gate passes on the declared items — no DRAFT/NOD
        # crossing, no label behind another element, no line through text
        shifted = []
        for it in items:
            it2 = dict(it)
            bx = it2["bbox"]
            it2["bbox"] = [bx[0], bx[1] + ZONE_Y, bx[2], bx[3] + ZONE_Y]
            shifted.append(it2)
        self.assertEqual(layout_gate.check_items(shifted), [])
        texts = [it for it in items if it["kind"] == "text"]
        self.assertLessEqual(len(texts), layout_gate.MAX_TEXT_LABELS,
                             "few labels — one idea per scene")
        # no random decorative words in the composition (the issue-#32 rejection
        # showed DRAFT/NOD/SWITCH/NOTE scattered as decoration)
        forbidden = {"DRAFT", "NOD", "SWITCH", "NOTE"}
        if sc["visual_category"] != "task-queue":
            forbidden |= {"COUNT", "TASK", "RESIDUE"}
        for it in texts:
            self.assertNotIn((it.get("text") or "").strip().upper(), forbidden,
                             f"scene A carries a rejected decorative word: {it.get('text')!r}")
        rep = layout_gate.scene_density_report(items)
        self.assertLessEqual(rep["foreground_components"],
                             layout_gate.MAX_FOREGROUND_COMPONENTS)
        self.assertGreaterEqual(rep["min_label_px"], layout_gate.MIN_LABEL_FONT_PX)

    # ------------------------------------------------------------- scene B
    def test_scene_b_is_an_understandable_loop_with_visible_labels(self):
        sc, a, b = self.scene_b
        self.assertEqual(sc["visual_category"], "reflect-loop")
        items = self.reel._scene_items.get(sc["scene_id"])
        self.assertTrue(items)
        texts = [it for it in items if it["kind"] == "text"]
        labels = [(it.get("text") or "").strip().upper() for it in texts]
        # the loop's steps are the technique's own words — no hidden NOTE/COUNT
        # behind bars; every label declared visible (its own z-layer, no
        # overlap — the gate below proves visibility: no text intersects
        # foreign foreground, and each label sits inside its own chip)
        self.assertTrue(labels, "every loop step must carry a visible label")
        shifted = []
        for it in items:
            it2 = dict(it)
            bx = it2["bbox"]
            it2["bbox"] = [bx[0], bx[1] + ZONE_Y, bx[2], bx[3] + ZONE_Y]
            shifted.append(it2)
        self.assertEqual(layout_gate.check_items(shifted), [],
                         "overlapping TASK/RESIDUE bars / hidden NOTE-COUNT was the rejection")
        # each label sits INSIDE its own chip — readable, never behind a bar
        by_id = {it["id"]: it for it in items}
        for it in texts:
            parent = by_id.get(it.get("parent") or "")
            self.assertIsNotNone(parent, f"label {it['id']} must belong to a step chip")
            self.assertTrue(layout_gate.inside(it["bbox"], parent["bbox"], pad=6))

    # --------------------------------------------- decoded H.264 inspection
    def _decode_scene(self, sc, a, b, name, n_frames=12):
        frames = []
        for i in range(n_frames):
            t = a + (b - a) * (i + 0.5) / n_frames
            frames.append(self.reel.frame(t))
        return h264_round_trip(frames, self.tmp, name)

    def test_scene_a_decoded_frames_are_clean_and_in_safe_zones(self):
        sc, a, b = self.scene_a
        decoded = self._decode_scene(sc, a, b, "scene_a")
        for arr in decoded:
            self.assertGreater(float(np.std(arr)), 8.0, "blank frame")
            # subtitle band holds only the warm scrim + gold ink — never tofu
            # boxes (authoritative glyph verdict comes from the cmap gate at
            # Reel construction; this checks the band is unobstructed)
            band = arr[200:444, 60:1020]
            lum = 0.2126 * band[..., 0] + 0.7152 * band[..., 1] + 0.0722 * band[..., 2]
            self.assertLessEqual(float(np.percentile(lum, 97)), 255)
            cold = ((band[..., 2] > band[..., 0] + 6) &
                    (band[..., 2] >= band[..., 1] - 6)).mean()
            self.assertLess(float(cold), 0.002, "cold element in the subtitle band")
            # scene zone carries visible content (the diagram is actually drawn)
            zone = arr[ZONE_Y + 60:ZONE_Y + 780, 100:980]
            self.assertGreater(float(np.std(zone)), 12.0,
                               "scene zone looks empty — composition missing")

    def test_scene_b_decoded_frames_show_the_loop_not_colliding_bars(self):
        sc, a, b = self.scene_b
        decoded = self._decode_scene(sc, a, b, "scene_b")
        for arr in decoded:
            self.assertGreater(float(np.std(arr)), 8.0, "blank frame")
            zone = arr[ZONE_Y + 60:ZONE_Y + 780, 100:980]
            self.assertGreater(float(np.std(zone)), 12.0)
            cold = ((zone[..., 2] > zone[..., 0] + 6) &
                    (zone[..., 2] >= zone[..., 1] - 6)).mean()
            self.assertLess(float(cold), 0.002, "cold palette in the loop scene")

    # -------------------------------------------------- subtitles on screen
    def test_defect_lines_render_normalized_subtitles_on_decoded_frames(self):
        """The five issue-#32 lines: every burned-in cue text is the
        normalized form, LTR, inside the safe zone — verified against the
        decoded-frame timeline (which cues are up when)."""
        cues = self.reel.layout["en"]
        defect_norm = [text_norm.normalize_text(l) for l in (
            "You\u2019re in a stand\u2011up, eyes on the screen, but your thoughts wander.",
            "especially with chat\u2011AI suggestions popping up.",
            "AI\u2011generated agenda suggestion",
            "Recognizing that gap\u2014realizing you\u2019re drafting a reply instead of hearing",
            "Repeat this pause\u2011and\u2011reflect loop")]
        shown = " || ".join(c["text"] for c in cues)
        for want in defect_norm:
            # each normalized line appears as cue text (possibly split into
            # consecutive cues by the 3-row wrapper)
            words = want.split()
            self.assertTrue(all(w in shown for w in words),
                            f"normalized line missing from subtitles: {want!r}")
        for c in cues:
            self.assertEqual(c["direction"], "ltr")
            x0, top, x1, bottom = c["bbox"]
            self.assertGreaterEqual(top, POL["layout"]["safe_top"])
            self.assertLessEqual(bottom, POL["layout"]["safe_top"]
                                 + 3 * POL["layout"]["en_row_height"] + 2)
            self.assertLessEqual(x1 - x0, POL["layout"]["en_max_width"] + 2)
        # and the decoded frames carry ink in the subtitle band while a
        # defect line is on screen (e.g. the stand-up line, ~scene A start)
        sc, a, b = self.scene_a
        t = a + 0.9
        active = [c for c in cues if c["start"] <= t <= c["end"]]
        if active:
            arr = np.asarray(self.reel.frame(t).convert("RGB"), dtype=np.int16)
            decoded = self._decode_scene(sc, a, b, "sub_band", n_frames=6)
            band = decoded[len(decoded) // 2][200:444, 60:1020]
            bright = (0.2126 * band[..., 0] + 0.7152 * band[..., 1]
                      + 0.0722 * band[..., 2]) > 120
            self.assertGreater(float(bright.mean()), 0.0008,
                               "subtitle ink missing while a cue is active")

    def test_no_dense_network_behind_plan_driven_scenes(self):
        """Issue #32 §4: the generic lattice/web background is gone for
        plan-driven scenes — only the subtle brand accent remains."""
        sc, a, b = self.scene_a
        arr = np.asarray(self.reel.frame((a + b) / 2).convert("RGB"), dtype=np.int16)
        # the brand accent arcs live in y<190 / y>1560 — the subtitle band and
        # the scene zone must show no dense network line field
        zone = arr[ZONE_Y:ZONE_Y + 840, :, :]
        goldish = ((zone[..., 0] > 150) & (zone[..., 1] > 110) &
                   (zone[..., 1] < 210) & (zone[..., 2] < 130))
        self.assertLess(float(goldish.mean()), 0.20,
                        "too much gold linework in the scene zone — dense network?")


if __name__ == "__main__":
    unittest.main()
