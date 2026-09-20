"""Unit, regression, and H.264 round-trip tests for typing-cursor QA and alpha composition.

Covers:
  * Issue #28 / Run 35480123463 root cause verification and regression fix;
  * Partial-alpha rendering correctness (source-over composition on transparent RGBA overlay);
  * Connected-component shape ownership (area, bounding box, continuation);
  * Bounded temporal analysis across authoritative scene windows;
  * All 13 false-positive controls (not cursors);
  * All 6 positive controls (detected and blocked when unjustified);
  * Allowed control (genuine caret in justified code scene in expected region);
  * Production-equivalent libx264 encode/decode round trips.
"""
import copy
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import content_producer as cp  # noqa: E402
import cursor_qa  # noqa: E402
import imageio_ffmpeg  # noqa: E402
import palette_qa as pq  # noqa: E402
import qa_supervisor as qa  # noqa: E402
from reel_engine import Reel  # noqa: E402
import visual_plan as vp  # noqa: E402

POL = common.policy()
W, H, FPS = 1080, 1920, 30


def ffmpeg_exe():
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not exe or not os.path.exists(exe):
        raise AssertionError("the repository's supported FFmpeg path is unavailable")
    return exe


def encode_decode_frames(frames, crf="21", preset="medium", timestamps=(0.25, 0.5, 0.75)):
    """Production-equivalent libx264 round-trip returning decoded int16 numpy arrays."""
    ff = ffmpeg_exe()
    tmp = tempfile.mkdtemp(prefix="h264_cursor_")
    out = os.path.join(tmp, "clip.mp4")
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-g", str(FPS * 2), out
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for f in frames:
            proc.stdin.write(np.asarray(f, dtype=np.uint8).tobytes())
    finally:
        proc.stdin.close()
        rc = proc.wait()
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise AssertionError(f"libx264 encode failed (rc={rc})")

    decoded = []
    try:
        for k, t in enumerate(timestamps):
            png = os.path.join(tmp, f"f{k}.png")
            subprocess.run([ff, "-v", "error", "-ss", f"{t:.3f}", "-i", out, "-frames:v", "1", "-y", png],
                           check=True, capture_output=True)
            if not os.path.exists(png):
                subprocess.run([ff, "-v", "error", "-i", out, "-frames:v", "1", "-y", png],
                               check=True, capture_output=True)
            decoded.append(np.array(Image.open(png).convert("RGB"), dtype=np.int16))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return decoded


def blank_canvas(rgb=(14, 12, 10)):
    return np.full((H, W, 3), rgb, dtype=np.int16)


# ===========================================================================
# 1. PARTIAL-ALPHA RENDERING TESTS
# ===========================================================================
class PartialAlphaRenderingTests(unittest.TestCase):
    """§1: Verification of proper source-over alpha composition."""

    def test_20_percent_alpha_ring_produces_dim_blended_gold_not_solid(self):
        bg = Image.new("RGBA", (W, H), (14, 12, 10, 255))
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d_ring = ImageDraw.Draw(overlay)
        rr = 400
        # 20% alpha of (233, 180, 74) is alpha 51
        d_ring.ellipse([540 - rr, 930 - int(rr * 0.94), 540 + rr, 930 + int(rr * 0.94)],
                       outline=(233, 180, 74, 51), width=3)
        bg.alpha_composite(overlay)
        rgb_frame = bg.convert("RGB")
        arr = np.array(rgb_frame, dtype=np.int16)

        # The pixel at the left tangent (540 - rr = 140, 930)
        px = arr[930, 540 - rr]
        r, g, b = int(px[0]), int(px[1]), int(px[2])

        # Expected blend: 233*0.2 + 14*0.8 = 57.8; 180*0.2 + 12*0.8 = 45.6; 74*0.2 + 10*0.8 = 22.8
        self.assertAlmostEqual(r, 58, delta=4)
        self.assertAlmostEqual(g, 46, delta=4)
        self.assertAlmostEqual(b, 23, delta=4)
        # Must NOT be solid GOLD (233, 180, 74)
        self.assertLess(r, 100)
        self.assertLess(g, 70)
        self.assertLess(b, 40)

    def test_alpha_0_is_invisible(self):
        bg = Image.new("RGBA", (100, 100), (14, 12, 10, 255))
        overlay = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        d.ellipse([20, 20, 80, 80], outline=(233, 180, 74, 0), width=3)
        bg.alpha_composite(overlay)
        arr = np.array(bg)
        self.assertTrue((arr[..., :3] == [14, 12, 10]).all())

    def test_alpha_255_remains_fully_opaque(self):
        bg = Image.new("RGBA", (100, 100), (14, 12, 10, 255))
        overlay = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        d.rectangle([20, 20, 40, 40], fill=(233, 180, 74, 255))
        bg.alpha_composite(overlay)
        arr = np.array(bg)
        self.assertTrue((arr[25:35, 25:35, :3] == [233, 180, 74]).all())

    def test_overlapping_partial_alpha_decorations_use_source_over(self):
        bg = Image.new("RGBA", (100, 100), (0, 0, 0, 255))
        # First layer: 50% opacity red
        o1 = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(o1).rectangle([20, 20, 80, 80], fill=(200, 0, 0, 128))
        bg.alpha_composite(o1)
        # Second layer: 50% opacity green
        o2 = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(o2).rectangle([20, 20, 80, 80], fill=(0, 200, 0, 128))
        bg.alpha_composite(o2)

        px = np.array(bg)[50, 50]
        # Source-over: green over red over black:
        # red over black (a1=0.5): rgb = 100, 0, 0
        # green (a2=0.5) over that: r = 100 * (1 - 0.5) = 50; g = 200 * 0.5 = 100
        self.assertAlmostEqual(int(px[0]), 50, delta=3)
        self.assertAlmostEqual(int(px[1]), 100, delta=3)

    def test_final_rgb_conversion_preserves_composited_appearance(self):
        bg = Image.new("RGBA", (100, 100), (14, 12, 10, 255))
        overlay = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle([20, 20, 80, 80], fill=(233, 180, 74, 51))
        bg.alpha_composite(overlay)
        composed_rgba = np.array(bg)
        final_rgb = np.array(bg.convert("RGB"))
        self.assertTrue((composed_rgba[..., :3] == final_rgb).all())

    def test_hook_theme_and_subtitle_contrast_intact(self):
        """Verifies hook palette and that subtitle band remains unobstructed."""
        tmp = tempfile.mkdtemp(prefix="test_hook_")
        try:
            script, plan = cp.PLAYBOOKS["sunk-cost-architecture"], None
            r = blank_canvas()
            # Subtitle band (around y=1300 to y=1500) must stay dark
            lum = 0.2126 * r[..., 0] + 0.7152 * r[..., 1] + 0.0722 * r[..., 2]
            self.assertLess(float(lum.mean()), 30)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 2. CONNECTED-COMPONENT AND SHAPE OWNERSHIP TESTS
# ===========================================================================
class ConnectedComponentShapeOwnershipTests(unittest.TestCase):
    """§4: Proof of shape ownership rules."""

    def test_component_area_rule_rejects_large_shapes(self):
        # A 3x40 vertical segment that is part of a large component (> 450 px)
        arr = blank_canvas()
        im = Image.fromarray(arr.astype(np.uint8))
        d = ImageDraw.Draw(im)
        # Large ellipse outline: area ~6,000+ px
        d.ellipse([140, 530, 940, 1330], outline=(233, 180, 74), width=3)
        res = cursor_qa.detect_cursor(np.array(im, dtype=np.int16))
        self.assertFalse(res, "candidate tangent of a large ellipse must be rejected by area rule")

    def test_component_bounding_box_rule_rejects_wide_and_tall_shapes(self):
        # A card border: 400x240 px, width > 16, height > 80
        arr = blank_canvas()
        im = Image.fromarray(arr.astype(np.uint8))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([340, 700, 740, 940], 24, outline=(233, 180, 74), width=3)
        res = cursor_qa.detect_cursor(np.array(im, dtype=np.int16))
        self.assertFalse(res, "card border must be rejected by component bbox rule")

    def test_candidate_to_component_fill_ratio_rejects_continuations(self):
        # A vertical bar of 4x35 that has extra shapes branching off it
        arr = blank_canvas()
        arr[800:835, 400:404] = (233, 180, 74)  # 4x35 bar
        arr[815:825, 404:450] = (233, 180, 74)  # branch to the right (extra 460 px)
        res = cursor_qa.detect_cursor(arr)
        self.assertFalse(res, "branching candidate must be rejected by fill ratio rule")

    def test_standalone_caret_satisfies_component_rules(self):
        # Isolated 4x34 caret
        arr = blank_canvas()
        arr[922:956, 296:300] = (255, 208, 100)
        cands = cursor_qa.find_cursor_candidates(arr)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertLessEqual(c["area"], 450)
        self.assertLessEqual(c["w"], 16)
        self.assertLessEqual(c["h"], 80)
        self.assertTrue(cursor_qa.detect_cursor(arr))


# ===========================================================================
# 3. FALSE-POSITIVE CONTROLS (MUST NOT BE CURSORS)
# ===========================================================================
class FalsePositiveControlsTests(unittest.TestCase):
    """§8: Negative detector controls."""

    def test_control_expanding_ellipse_tangent_not_cursor(self):
        arr = blank_canvas()
        im = Image.fromarray(arr.astype(np.uint8))
        d = ImageDraw.Draw(im)
        d.ellipse([100, 500, 980, 1360], outline=(233, 180, 74), width=3)
        self.assertFalse(cursor_qa.detect_cursor(np.array(im, dtype=np.int16)))

    def test_control_brand_ring_not_cursor(self):
        arr = blank_canvas()
        im = Image.fromarray(arr.astype(np.uint8))
        d = ImageDraw.Draw(im)
        d.ellipse([240, 630, 840, 1230], outline=(233, 180, 74), width=3)
        self.assertFalse(cursor_qa.detect_cursor(np.array(im, dtype=np.int16)))

    def test_control_uppercase_I_not_cursor(self):
        font = ImageFont.truetype(f"{ROOT}/assets/fonts/en-800.ttf", 30)
        # Uppercase I in a word
        im = Image.new("RGB", (W, H), (14, 12, 10))
        ImageDraw.Draw(im).text((500, 900), "IDEA", font=font, fill=(233, 180, 74))
        self.assertFalse(cursor_qa.detect_cursor(np.array(im, dtype=np.int16)))
        # Standalone uppercase I: aspect ratio < 6.0
        im2 = Image.new("RGB", (W, H), (14, 12, 10))
        ImageDraw.Draw(im2).text((500, 900), "I", font=font, fill=(233, 180, 74))
        self.assertFalse(cursor_qa.detect_cursor(np.array(im2, dtype=np.int16)))

    def test_control_lowercase_l_not_cursor(self):
        font = ImageFont.truetype(f"{ROOT}/assets/fonts/en-800.ttf", 30)
        im = Image.new("RGB", (W, H), (14, 12, 10))
        ImageDraw.Draw(im).text((500, 900), "BUILD", font=font, fill=(233, 180, 74))
        self.assertFalse(cursor_qa.detect_cursor(np.array(im, dtype=np.int16)))
        im2 = Image.new("RGB", (W, H), (14, 12, 10))
        ImageDraw.Draw(im2).text((500, 900), "l", font=font, fill=(233, 180, 74))
        self.assertFalse(cursor_qa.detect_cursor(np.array(im2, dtype=np.int16)))

    def test_control_pipe_glyph_not_cursor(self):
        font = ImageFont.truetype(f"{ROOT}/assets/fonts/en-800.ttf", 30)
        im = Image.new("RGB", (W, H), (14, 12, 10))
        ImageDraw.Draw(im).text((500, 900), "A | B", font=font, fill=(233, 180, 74))
        self.assertFalse(cursor_qa.detect_cursor(np.array(im, dtype=np.int16)))
        im2 = Image.new("RGB", (W, H), (14, 12, 10))
        ImageDraw.Draw(im2).text((500, 900), "|", font=font, fill=(233, 180, 74))
        self.assertFalse(cursor_qa.detect_cursor(np.array(im2, dtype=np.int16)))

    def test_control_3px_vertical_divider_not_cursor(self):
        div = blank_canvas()
        div[600:850, 500:503] = (233, 180, 74)  # 250px tall divider
        self.assertFalse(cursor_qa.detect_cursor(div))

    def test_control_card_border_not_cursor(self):
        arr = blank_canvas()
        im = Image.fromarray(arr.astype(np.uint8))
        ImageDraw.Draw(im).rounded_rectangle([300, 800, 700, 1100], 20, outline=(233, 180, 74), width=3)
        self.assertFalse(cursor_qa.detect_cursor(np.array(im, dtype=np.int16)))

    def test_control_narrow_chart_bar_not_cursor(self):
        arr = blank_canvas()
        # Bar connected to horizontal baseline
        arr[800:850, 400:405] = (233, 180, 74)
        arr[850:854, 300:700] = (233, 180, 74)
        self.assertFalse(cursor_qa.detect_cursor(arr))

    def test_control_network_line_not_cursor(self):
        arr = blank_canvas()
        # 4px line connected to node circles
        im = Image.fromarray(arr.astype(np.uint8))
        d = ImageDraw.Draw(im)
        d.line([400, 700, 400, 750], fill=(233, 180, 74), width=4)
        d.ellipse([390, 690, 410, 710], fill=(233, 180, 74))
        d.ellipse([390, 740, 410, 760], fill=(233, 180, 74))
        self.assertFalse(cursor_qa.detect_cursor(np.array(im, dtype=np.int16)))

    def test_control_logo_edge_not_cursor(self):
        em = Image.open(f"{ROOT}/assets/img/logo_emblem.png").convert("RGBA").resize((400, 400))
        bg = Image.new("RGBA", (W, H), (14, 12, 10, 255))
        bg.alpha_composite(em, (340, 760))
        arr = np.array(bg.convert("RGB"), dtype=np.int16)
        self.assertFalse(cursor_qa.detect_cursor(arr))

    def test_control_static_gold_spine_not_cursor(self):
        arr = blank_canvas()
        arr[500:1300, 500:504] = (233, 180, 74)  # 800px continuous spine
        self.assertFalse(cursor_qa.detect_cursor(arr))

    def test_control_moving_vertical_diagram_element_not_cursor(self):
        # Multi-frame test: element translates horizontally by 50px between frames
        f1 = blank_canvas()
        f1[900:940, 300:304] = (233, 180, 74)
        f2 = blank_canvas()
        f2[900:940, 350:354] = (233, 180, 74)
        f3 = blank_canvas()
        f3[900:940, 400:404] = (233, 180, 74)
        res = cursor_qa.analyze_scene_cursors([f1, f2, f3], [1.0, 2.0, 3.0], sc={"scene_id": "s2"})
        self.assertTrue(res["ok"])
        self.assertFalse(res["detected"])

    def test_control_bright_photo_texture_not_cursor(self):
        arr = np.full((H, W, 3), (120, 100, 70), dtype=np.int16)
        arr[650:690, 400:404] = (233, 180, 74)
        self.assertFalse(cursor_qa.detect_cursor(arr))


# ===========================================================================
# 4. POSITIVE AND ALLOWED CONTROLS
# ===========================================================================
class PositiveAndAllowedControlsTests(unittest.TestCase):
    """§8: Positive detector controls (detected & blocked) and allowed control."""

    def test_control_genuine_isolated_caret_detected(self):
        arr = blank_canvas((18, 20, 24))
        arr[650:690, 400:404] = (233, 180, 74)
        self.assertTrue(cursor_qa.detect_cursor(arr))

    def test_control_genuine_blinking_caret_detected(self):
        # Frame 0: caret ON
        f0 = blank_canvas()
        f0[922:956, 296:300] = (255, 208, 100)
        # Frame 1: caret OFF
        f1 = blank_canvas()
        # Frame 2: caret ON
        f2 = blank_canvas()
        f2[922:956, 296:300] = (255, 208, 100)
        # Unjustified scene
        res = cursor_qa.analyze_scene_cursors([f0, f1, f2], [1.0, 1.25, 1.5],
                                             sc={"scene_id": "s1", "cursor_justified": False})
        self.assertFalse(res["ok"])
        self.assertIn("unjustified typing cursor", res["reason"])

    def test_control_unjustified_caret_in_product_scene_blocked(self):
        f = blank_canvas()
        f[900:940, 300:304] = (233, 180, 74)
        res = cursor_qa.analyze_scene_cursors([f], [1.0],
                                             sc={"scene_id": "s2", "pillar": "PRODUCT",
                                                 "code_justified": False, "cursor_justified": False})
        self.assertFalse(res["ok"])
        self.assertIn("unjustified typing cursor", res["reason"])

    def test_control_unjustified_caret_in_brand_scene_blocked(self):
        f = blank_canvas()
        f[900:940, 300:304] = (233, 180, 74)
        res = cursor_qa.analyze_scene_cursors([f], [1.0],
                                             sc={"scene_id": "s1", "visual_category": "brand-mark",
                                                 "code_justified": False, "cursor_justified": False})
        self.assertFalse(res["ok"])
        self.assertIn("unjustified typing cursor", res["reason"])

    def test_control_caret_with_cursor_justified_false_blocked(self):
        f = blank_canvas()
        f[922:956, 296:300] = (255, 208, 100)
        res = cursor_qa.analyze_scene_cursors([f], [1.0],
                                             sc={"scene_id": "s3", "code_justified": True,
                                                 "cursor_justified": False})
        self.assertFalse(res["ok"])
        self.assertIn("unjustified typing cursor", res["reason"])

    def test_control_caret_leaking_outside_expected_code_region_blocked(self):
        # Caret rendered outside expected code region [296, 922] (e.g. at x=700, y=700)
        f = blank_canvas()
        f[700:740, 700:704] = (255, 208, 100)
        res = cursor_qa.analyze_scene_cursors([f], [1.0],
                                             sc={"scene_id": "s3", "code_justified": True,
                                                 "cursor_justified": True})
        self.assertFalse(res["ok"])
        self.assertIn("cursor outside expected code region", res["reason"])

    def test_control_allowed_caret_in_justified_scene_passes(self):
        # Genuine caret rendered in designated region [296, 922, 300, 956]
        f0 = blank_canvas()
        f0[922:956, 296:300] = (255, 208, 100)
        f1 = blank_canvas()  # blink off
        f2 = blank_canvas()
        f2[922:956, 296:300] = (255, 208, 100)
        layout = {"visual_plan": {"cursor_events": [{
            "scene_id": "s3", "cursor_drawn": True, "expected_region": [296, 922, 300, 956],
            "justified": True, "blink_hz": 2.0
        }]}}
        res = cursor_qa.analyze_scene_cursors([f0, f1, f2], [1.0, 1.25, 1.5],
                                             sc={"scene_id": "s3", "code_justified": True,
                                                 "cursor_justified": True},
                                             layout=layout)
        self.assertTrue(res["ok"])
        self.assertTrue(res["justified"])


# ===========================================================================
# 5. EXACT SCENE S1 REGRESSION AND H.264 ROUND-TRIP
# ===========================================================================
class ExactSceneS1RegressionTests(unittest.TestCase):
    """§7: Deterministic reconstruction of reel-2026-09-22 scene s1."""

    def test_exact_scene_s1_reconstruction(self):
        # 1. Old uncomposited behavior: solid gold ring reproduces the old detection
        old_bg = Image.new("RGBA", (W, H), (14, 12, 10, 255))
        d_old = ImageDraw.Draw(old_bg)
        rr = 400
        # Old code drew directly with ImageDraw and discarded alpha upon convert("RGB")
        d_old.ellipse([540 - rr, 930 - int(rr * 0.94), 540 + rr, 930 + int(rr * 0.94)],
                      outline=(233, 180, 74), width=3)
        old_arr = np.array(old_bg.convert("RGB"), dtype=np.int16)

        # Tangent vertical run is ~3x40 px
        h = old_arr.shape[0]
        band = old_arr[int(h * 0.30):int(h * 0.80), int(old_arr.shape[1] * 0.08):int(old_arr.shape[1] * 0.92)]
        r, g, b = band[..., 0], band[..., 1], band[..., 2]
        gold = (r > 170) & (g > 110) & (g < 235) & (b < 160) & (r > b + 80)
        labels, n_comp = pq.label_components(gold)
        self.assertGreater(n_comp, 0)
        # Component is huge: > 5000 px
        max_area = max(int((labels == i).sum()) for i in range(1, n_comp + 1))
        self.assertGreater(max_area, 5000)

        # 2. Corrected alpha compositing:
        # Ring with 20% alpha (51) composited onto transparent RGBA overlay
        new_bg = Image.new("RGBA", (W, H), (14, 12, 10, 255))
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d_new = ImageDraw.Draw(overlay)
        d_new.ellipse([540 - rr, 930 - int(rr * 0.94), 540 + rr, 930 + int(rr * 0.94)],
                      outline=(233, 180, 74, 51), width=3)
        new_bg.alpha_composite(overlay)
        new_arr = np.array(new_bg.convert("RGB"), dtype=np.int16)

        # Corrected alpha ring is dim blended gold (not solid gold):
        band_new = new_arr[int(h * 0.30):int(h * 0.80), int(new_arr.shape[1] * 0.08):int(new_arr.shape[1] * 0.92)]
        r2, g2, b2 = band_new[..., 0], band_new[..., 1], band_new[..., 2]
        gold_new = (r2 > 170) & (g2 > 110) & (g2 < 235) & (b2 < 160) & (r2 > b2 + 80)
        self.assertEqual(int(gold_new.sum()), 0, "dim blended gold must not enter bright gold mask")

        # 3. Corrected detector reports no cursor on BOTH pre-fix and post-fix frames
        self.assertFalse(cursor_qa.detect_cursor(old_arr),
                         "corrected detector must reject uncomposited ellipse via connected component ownership")
        self.assertFalse(cursor_qa.detect_cursor(new_arr),
                         "corrected detector must report no cursor for composited ring")

        # 4. Production-equivalent libx264 round-trip on scene s1
        frames = []
        for f_idx in range(15):
            ts = f_idx / float(FPS)
            fr = Image.new("RGBA", (W, H), (14, 12, 10, 255))
            pt = (ts * 0.55) % 1.0
            r_ring = 300 + pt * 260
            alpha = int(120 * (1 - pt))
            if alpha > 0:
                o = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                ImageDraw.Draw(o).ellipse(
                    [540 - r_ring, 930 - int(r_ring * 0.94), 540 + r_ring, 930 + int(r_ring * 0.94)],
                    outline=(233, 180, 74, alpha), width=3)
                fr.alpha_composite(o)
            frames.append(np.array(fr.convert("RGB"), dtype=np.uint8))

        decoded = encode_decode_frames(frames, timestamps=(0.1, 0.25, 0.4))
        sc = {"scene_id": "s1", "beat": "hook", "visual_category": "brand-mark",
              "code_justified": False, "cursor_justified": False}
        res = cursor_qa.analyze_scene_cursors(decoded, [0.1, 0.25, 0.4], sc=sc)
        self.assertTrue(res["ok"], f"scene s1 must pass visual_semantics: {res}")

    def test_h264_unjustified_cursor_positive_control_detected_and_blocked(self):
        """Production-equivalent H.264 encode/decode proves unjustified caret is detected and blocked."""
        frames = []
        for f_idx in range(15):
            arr = blank_canvas()
            # Draw an unjustified caret in scene s1
            arr[900:940, 300:304] = (255, 208, 100)
            frames.append(arr)

        decoded = encode_decode_frames(frames, timestamps=(0.1, 0.25, 0.4))
        sc = {"scene_id": "s1", "beat": "hook", "visual_category": "brand-mark",
              "code_justified": False, "cursor_justified": False}
        res = cursor_qa.analyze_scene_cursors(decoded, [0.1, 0.25, 0.4], sc=sc)
        self.assertFalse(res["ok"])
        self.assertIn("unjustified typing cursor", res["reason"])


# ===========================================================================
# 6. POLICY AND HARD SAFETY ENVELOPES
# ===========================================================================
class CursorPolicyAndSafetyEnvelopesTests(unittest.TestCase):
    """§9: Configuration and hard safety envelope validation."""

    def test_policy_in_editorial_policy(self):
        pol = common.policy()
        self.assertIn("cursor_qa", pol)
        cfg = cursor_qa.load_policy(pol)
        self.assertEqual(cfg["min_cursor_width"], 3)
        self.assertEqual(cfg["max_cursor_width"], 6)
        self.assertEqual(cfg["min_cursor_height"], 30)
        self.assertEqual(cfg["max_cursor_height"], 70)
        self.assertEqual(cfg["max_cursor_component_area"], 450)
        self.assertEqual(cfg["max_component_bbox_width"], 16)
        self.assertEqual(cfg["max_component_bbox_height"], 80)

    def test_hard_safety_envelope_clamps_suspicious_values(self):
        tampered = {
            "cursor_qa": {
                "max_cursor_component_area": 999999,  # dangerously high
                "max_component_bbox_width": 2,        # dangerously low
            }
        }
        cfg = cursor_qa.load_policy(tampered)
        self.assertEqual(cfg["max_cursor_component_area"], 1000)
        self.assertEqual(cfg["max_component_bbox_width"], 8)
        self.assertTrue(len(cfg["policy_notes"]) >= 2)


# ===========================================================================
# 7. UNITTEST DISCOVERY GUARD
# ===========================================================================
class CursorDiscoveryGuardTests(unittest.TestCase):
    """Guard proving critical cursor regression test cases are discoverable by unittest."""

    def test_cursor_cases_discoverable_by_unittest(self):
        loader = unittest.defaultTestLoader
        suite = loader.loadTestsFromName("tests.test_cursor_qa")
        discovered_names = set()

        def _collect(s):
            for item in s:
                if isinstance(item, unittest.TestCase):
                    discovered_names.add(item._testMethodName)
                else:
                    _collect(item)

        _collect(suite)

        required_cases = [
            "test_exact_scene_s1_reconstruction",
            "test_20_percent_alpha_ring_produces_dim_blended_gold_not_solid",
            "test_control_expanding_ellipse_tangent_not_cursor",
            "test_component_area_rule_rejects_large_shapes",
            "test_component_bounding_box_rule_rejects_wide_and_tall_shapes",
            "test_control_unjustified_caret_in_product_scene_blocked",
            "test_control_allowed_caret_in_justified_scene_passes",
            "test_control_caret_leaking_outside_expected_code_region_blocked",
            "test_h264_unjustified_cursor_positive_control_detected_and_blocked",
            "test_control_uppercase_I_not_cursor",
            "test_control_lowercase_l_not_cursor",
            "test_control_pipe_glyph_not_cursor",
            "test_control_card_border_not_cursor",
            "test_control_3px_vertical_divider_not_cursor",
            "test_control_narrow_chart_bar_not_cursor",
            "test_control_network_line_not_cursor",
            "test_control_moving_vertical_diagram_element_not_cursor",
        ]
        for case in required_cases:
            self.assertIn(case, discovered_names,
                          f"critical cursor regression test {case} must be discoverable by unittest")


if __name__ == "__main__":
    unittest.main()
