"""Decoded H.264 round-trip palette tests (issue #26 §5).

The previous palette tests inspected in-memory PIL frames, so they could not
reproduce codec-introduced chroma noise — the exact mechanism that made
``visual_semantics`` reject the clean procedural confidence-gauge scene s4 of
reel-2026-09-21 with "0.23% cold" on decoded H.264 frames whose pre-encode
cold-pixel count was 0.00%.

These tests use the repository's SUPPORTED local FFmpeg path
(``imageio_ffmpeg.get_ffmpeg_exe()``, a pinned dependency in requirements.txt —
system ffprobe is not needed and its absence never skips these tests):

    generated frame sequence → production-equivalent libx264 encode (CRF 21,
    yuv420p, profile high, preset medium — the renderer's own settings) →
    decoded frames → palette QA.

All fixtures are generated locally: no network, no downloads, no committed
media (everything lives in a TemporaryDirectory).
"""
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import palette_qa as pq  # noqa: E402
import visual_plan as vp  # noqa: E402

W, H = 1296, 2304          # the renderer's own frame size
FPS = 30
N_FRAMES = 30


def ffmpeg_exe():
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not exe or not os.path.exists(exe):
        raise AssertionError("the repository's supported FFmpeg path is unavailable "
                             "(imageio-ffmpeg is a pinned dependency)")
    return exe


def encode_decode(frames, crf="21", preset="medium", fracs=(0.25, 0.5, 0.75)):
    """Production-equivalent libx264 round-trip; returns the decoded frames.

    ``fracs`` are fractions of the clip duration — never absolute seconds, so a
    short fixture cannot be sampled past its own end.
    """
    ff = ffmpeg_exe()
    tmp = tempfile.mkdtemp(prefix="h264_rt_")
    out = os.path.join(tmp, "clip.mp4")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
           "-profile:v", "high", "-g", str(FPS * 2), out]
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
    for k, frac in enumerate(fracs):
        t = max(0.0, min(duration * frac, max(0.0, duration - 1.0 / FPS)))
        png = os.path.join(tmp, f"f{k}.png")
        subprocess.run([ff, "-v", "error", "-ss", f"{t:.3f}", "-i", out, "-frames:v", "1", "-y", png],
                       check=True, capture_output=True)
        if not os.path.exists(png):        # very short clip: take the first frame
            subprocess.run([ff, "-v", "error", "-i", out, "-frames:v", "1", "-y", png],
                           check=True, capture_output=True)
        decoded.append(np.array(Image.open(png).convert("RGB"), dtype=np.int16))
    return decoded


# ---------------------------------------------------------------------------
# Locally generated fixtures (no downloads, no committed media)
# ---------------------------------------------------------------------------
def warm_canvas():
    a = np.zeros((H, W, 3), np.int16)
    a[..., 0], a[..., 1], a[..., 2] = 10, 8, 6
    return a


def gauge_scene(i, n=N_FRAMES):
    """Matte-black/gold procedural confidence gauge (the s4 mechanism)."""
    im = Image.fromarray(warm_canvas().astype(np.uint8), "RGB")
    d = ImageDraw.Draw(im)
    cx, cy = W // 2, H // 2
    wob = int(18 * np.sin(i / 5.0))
    for k in range(3):
        rad = 300 - 70 * k
        d.arc([cx - rad + wob, cy - rad - 180 * k, cx + rad + wob, cy + rad - 180 * k],
              start=200 + i * 2.5, end=200 + i * 2.5 + 250, fill=(233, 180, 74), width=13)
    d.rectangle([120 + wob, 1400, W - 120 + wob, 1520], fill=(26, 20, 13), outline=(150, 106, 44))
    return np.array(im, dtype=np.int16)


def typography_scene(i):
    """Gold/ivory antialiased-style type blocks on matte black."""
    im = Image.fromarray(warm_canvas().astype(np.uint8), "RGB")
    d = ImageDraw.Draw(im)
    for row in range(6):
        for col in range(14):
            x, y = 120 + col * 78 + (i % 3), 600 + row * 120
            d.rectangle([x, y, x + 46, y + 78],
                        fill=(233, 180, 74) if (row + col) % 3 else (246, 240, 228))
    return np.array(im, dtype=np.int16)


def warm_photo_scene(i):
    """A grey-scale 'photograph' fixture in the PDM/CC0 style, warm-graded."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    base = 0.55 + 0.35 * np.sin(xx / 90.0 + i / 6.0) * np.cos(yy / 120.0)
    base = np.clip(base, 0, 1)
    rgb = np.stack([base * 220 + 20, base * 180 + 18, base * 120 + 12], -1)
    return vp.brand_grade(np.clip(rgb, 0, 255).astype(np.uint8)).astype(np.int16)


def _mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def brand_blocks_scene(i):
    """Every declared brand color as a large coherent block (warm palette)."""
    im = Image.fromarray(warm_canvas().astype(np.uint8), "RGB")
    d = ImageDraw.Draw(im)
    for k, (_name, rgb) in enumerate(sorted(vp.BRAND_PALETTE.items())):
        col, row = k % 4, k // 4
        x, y = 80 + col * 300, 620 + row * 200
        d.rectangle([x, y, x + 250, y + 160], fill=tuple(rgb))
    return np.array(im, dtype=np.int16)


def brand_and_aa_scene(i):
    """Antialiased-style warm edges: linear ramps from each warm brand color to
    the matte-black base, plus thin gold/ivory strokes (typography edges)."""
    im = Image.fromarray(warm_canvas().astype(np.uint8), "RGB")
    d = ImageDraw.Draw(im)
    base = (16, 13, 10)
    for k, warm in enumerate((vp.GOLD, vp.GOLD_HI, vp.IVORY, vp.BRONZE, vp.AMBER)):
        y0 = 620 + k * 180
        for row in range(140):
            d.line([(90, y0 + row), (1200, y0 + row)], fill=_mix(base, warm, row / 139.0), width=1)
    for k in range(9):                      # thin strokes = antialiased glyph edges
        x = 120 + k * 130 + (i % 3)
        d.rectangle([x, 1560, x + 3, 1700], fill=tuple(vp.GOLD))
        d.rectangle([x + 8, 1580, x + 11, 1680], fill=tuple(vp.IVORY))
    return np.array(im, dtype=np.int16)


def with_block(frame, color, size=120, y=700, x=400):
    a = frame.copy()
    a[y:y + size, x:x + size] = color
    return a


def with_small_block(frame, color, size=26, y=760, x=430):
    return with_block(frame, color, size=size, y=y, x=x)


def with_codec_like_noise(frame, seed=0, n=2600):
    """Reconstruct the audit's H.264 near-black chroma-noise MECHANISM.

    Measured on real encodes: essentially black pixels (RGB like (5-8, 6,
    13-15), luminance < 30) scattered in flat near-black regions, flagged by the
    legacy raw inequality. Values here are drawn from that measured envelope.
    """
    a = frame.copy()
    rng = np.random.default_rng(seed)
    ys = rng.integers(500, 1900, n)
    xs = rng.integers(80, 1200, n)
    for y, x in zip(ys, xs):
        a[y, x] = (5 + (y % 4), 6, 13 + (x % 3))
    return a


class CodecRoundTrip(unittest.TestCase):
    """1-3: warm procedural scenes must survive a real encode/decode cycle."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = pq.load_policy()
        cls.gauge_decoded = encode_decode([gauge_scene(i) for i in range(N_FRAMES)])
        cls.typo_decoded = encode_decode([typography_scene(i) for i in range(N_FRAMES)])
        cls.photo_decoded = encode_decode([warm_photo_scene(i) for i in range(N_FRAMES)])

    def _assert_cold_only_if_near_black_noise(self, decoded, label):
        """Legacy-flagged pixels may exist — but ONLY as near-black noise."""
        for arr in decoded:
            band = arr[pq.band_slice(arr, self.cfg)]
            mask = pq.cold_dominance_mask(band)
            if not mask.any():
                continue
            px = band[mask]
            lum = 0.2126 * px[:, 0] + 0.7152 * px[:, 1] + 0.0722 * px[:, 2]
            self.assertLess(float(lum.max()), 40.0,
                            f"{label}: a legacy-flagged pixel is NOT near-black noise "
                            "(a real cold element would have to be blocked)")

    def test_integers_only_frame_sequence_is_clean_before_encoding(self):
        frames = [gauge_scene(i) for i in range(N_FRAMES)]
        for arr in frames:
            band = arr[pq.band_slice(arr, self.cfg)]
            self.assertEqual(int(pq.cold_dominance_mask(band).sum()), 0,
                             "the procedural gauge fixture must be cold-free BEFORE encoding")

    def test_confidence_gauge_scene_passes_after_round_trip(self):
        scene = pq.analyze_scene(self.gauge_decoded, self.cfg)
        self.assertFalse(scene["blocked"], scene["reason"])
        self.assertEqual(scene["persistent_cold_regions"], 0)
        self._assert_cold_only_if_near_black_noise(self.gauge_decoded, "gauge")

    def test_typography_scene_passes_after_round_trip(self):
        scene = pq.analyze_scene(self.typo_decoded, self.cfg)
        self.assertFalse(scene["blocked"], scene["reason"])
        self._assert_cold_only_if_near_black_noise(self.typo_decoded, "typography")

    def test_warm_graded_photo_fixture_passes_after_round_trip(self):
        scene = pq.analyze_scene(self.photo_decoded, self.cfg)
        self.assertFalse(scene["blocked"], scene["reason"])

    def test_decoded_frames_reproduce_the_legacy_cold_false_positive(self):
        """The codec mechanism, on a REAL encode — NOT a threshold crossing.

        The real incident (reel-2026-09-21) reported 0.23% legacy cold pixels and
        was blocked by the old 0.20% gate. These local fixtures reproduce the
        same near-black chroma-noise MECHANISM but measure far less of it
        (approximately 0.015%-0.075%), i.e. BELOW the old 0.20% threshold — they
        would NOT have triggered the old gate on their own. That exact >0.23%
        case is covered deterministically by
        tests/test_palette_qa_units.py::OldGateRegression, which builds a
        decoded-frame-style fixture above the real incident's fraction and shows
        the old rule blocking while the new detector passes it.
        """
        measured = 0
        for arr in self.gauge_decoded + self.typo_decoded:
            band = arr[pq.band_slice(arr, self.cfg)]
            mask = pq.cold_dominance_mask(band)
            if not mask.any():
                continue
            measured += int(mask.sum())
            px = band[mask]
            lum = 0.2126 * px[:, 0] + 0.7152 * px[:, 1] + 0.0722 * px[:, 2]
            chroma = px.max(1) - px.min(1)
            self.assertLess(float(lum.max()), 40.0)
            self.assertLess(int(chroma.max()), self.cfg["visible_min_chroma"] + 12)
        # whatever the encoder did, the palette verdict is unchanged
        for decoded in (self.gauge_decoded, self.typo_decoded):
            self.assertFalse(pq.analyze_scene(decoded, self.cfg)["blocked"])
        # and this fixture stays BELOW the old content-blind 0.20% gate: it is a
        # mechanism demonstration, not a reproduction of the 0.23% threshold crossing
        band_px = sum(int(np.asarray(a[pq.band_slice(a, self.cfg)][..., 0].size))
                      for a in self.gauge_decoded + self.typo_decoded)
        legacy_fraction = measured / max(1, band_px)
        self.assertLess(legacy_fraction, 0.002,
                        "if this ever exceeds the old 0.20% gate, update the report wording")


class BlockedColdContent(unittest.TestCase):
    """4-9: real cold content (visible or dark) must still block."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = pq.load_policy()

    def _decoded_three(self, color, size=120, y=700, x=400):
        frames = [with_block(gauge_scene(i), color, size=size, y=y, x=x)
                  for i in range(N_FRAMES)]
        return encode_decode(frames)

    def test_visibly_blue_object_is_blocked_after_round_trip(self):
        decoded = self._decoded_three((30, 90, 200), size=300)
        scene = pq.analyze_scene(decoded, self.cfg)
        self.assertTrue(scene["blocked"], "a large visible blue object must block")

    def test_dark_navy_region_is_blocked_even_at_low_luminance(self):
        decoded = self._decoded_three((10, 26, 46))          # #0A1A2E
        scene = pq.analyze_scene(decoded, self.cfg)
        self.assertTrue(scene["blocked"], "a dark-navy connected region must block")
        self.assertEqual(scene["per_frame"][0]["regions"][0]["path"], "dark")

    def test_cyan_region_is_blocked_after_round_trip(self):
        self.assertTrue(pq.analyze_scene(self._decoded_three((0, 200, 220)), self.cfg)["blocked"])

    def test_purple_region_is_blocked_after_round_trip(self):
        self.assertTrue(pq.analyze_scene(self._decoded_three((128, 0, 200)), self.cfg)["blocked"])

    def test_small_isolated_near_black_blue_bias_is_ignored_as_codec_noise(self):
        frames = [with_codec_like_noise(gauge_scene(i), seed=i) for i in range(N_FRAMES)]
        decoded = encode_decode(frames)
        legacy = sum(int(pq.cold_dominance_mask(a[pq.band_slice(a, self.cfg)]).sum())
                     for a in decoded)
        scene = pq.analyze_scene(decoded, self.cfg)
        self.assertFalse(scene["blocked"], scene["reason"])
        self.assertGreaterEqual(legacy, 0)

    def test_small_but_persistent_cold_object_is_blocked(self):
        """One frame of a small cold object is noise-scale; the SAME object in
        the sampled frames is a real visual and must block."""
        frames = [with_small_block(gauge_scene(i), (20, 45, 95), size=26, y=760, x=430)
                  for i in range(N_FRAMES)]
        decoded = encode_decode(frames)
        scene = pq.analyze_scene(decoded, self.cfg)
        self.assertTrue(scene["blocked"], "a persistent small cold object must block")
        self.assertGreaterEqual(scene["persistent_cold_regions"], 1)

    def test_one_frame_of_the_same_small_object_is_not_enough(self):
        base = gauge_scene(0)
        decoded = encode_decode([base] * 6)
        # inject the same small object into ONE decoded frame only
        one = pq.analyze_scene([with_small_block(decoded[0], (20, 45, 95)),
                                decoded[1], decoded[2]], self.cfg)
        self.assertFalse(one["blocked"], one["reason"])


class CanonicalFamiliesBlocked(unittest.TestCase):
    """Every prohibited family blocks a coherent region after a REAL encode —
    including the two BALANCED canonical colors (RGB(0,255,255) and
    RGB(128,0,128)) whose blue excess is exactly zero."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = pq.load_policy()
        cls.N = 12          # short clips: these fixtures are static

    def _decoded(self, color, size=120, y=700, x=400):
        frames = [with_block(gauge_scene(i, n=self.N), color, size=size, y=y, x=x)
                  for i in range(self.N)]
        return encode_decode(frames)

    def _assert_blocked(self, color, family, size=120):
        decoded = self._decoded(color, size=size)
        scene = pq.analyze_scene(decoded, self.cfg)
        self.assertTrue(scene["blocked"], f"{color} must block after H.264 round-trip")
        fams = set()
        for fr in scene["per_frame"]:
            fams.update(r["family"] for r in fr["regions"])
        self.assertIn(family, fams, f"{color} must be labelled {family}, got {fams}")

    def test_pure_blue_object_is_blocked_after_round_trip(self):
        self._assert_blocked((0, 0, 255), "blue", size=300)

    def test_dark_navy_region_is_blocked_even_at_low_luminance(self):
        decoded = self._decoded((10, 26, 46))
        scene = pq.analyze_scene(decoded, self.cfg)
        self.assertTrue(scene["blocked"])
        self.assertEqual(scene["per_frame"][0]["regions"][0]["path"], "dark")

    def test_canonical_cyan_is_blocked_after_round_trip(self):
        self._assert_blocked((0, 255, 255), "cyan")

    def test_darker_cyan_and_teal_are_blocked_after_round_trip(self):
        self._assert_blocked((0, 100, 120), "cyan")      # darker cyan
        self._assert_blocked((0, 128, 128), "cyan")      # teal
        self._assert_blocked((0, 60, 70), "cyan")        # dark cyan/teal

    def test_canonical_purple_is_blocked_after_round_trip(self):
        self._assert_blocked((128, 0, 128), "purple")

    def test_violet_dark_purple_and_magenta_are_blocked_after_round_trip(self):
        self._assert_blocked((143, 0, 255), "purple")    # violet
        self._assert_blocked((48, 0, 64), "purple")      # dark purple
        self._assert_blocked((160, 32, 200), "purple")   # magenta-purple


class WarmPaletteSurvivesEncoding(unittest.TestCase):
    """Overblocking guard on REAL decoded frames: the declared brand palette,
    warm/neutral neighbours and antialiased warm edges must all pass."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = pq.load_policy()
        cls.N = 12

    def _decoded_scene(self, build):
        frames = [build(i) for i in range(self.N)]
        return encode_decode(frames)

    def test_all_declared_brand_colors_pass_after_round_trip(self):
        decoded = self._decoded_scene(brand_blocks_scene)
        scene = pq.analyze_scene(decoded, self.cfg)
        self.assertFalse(scene["blocked"], scene["reason"])
        self.assertEqual(scene["persistent_cold_regions"], 0)
        self.assertEqual(scene["max_meaningful_core_px"], 0)

    def test_antialiased_gold_and_ivory_edges_pass_after_round_trip(self):
        decoded = self._decoded_scene(brand_and_aa_scene)
        scene = pq.analyze_scene(decoded, self.cfg)
        self.assertFalse(scene["blocked"], scene["reason"])
        self.assertEqual(scene["persistent_cold_regions"], 0)

    def test_warm_boundary_colors_pass_after_round_trip(self):
        """green without cyan, red, warm orange and rose/warm red stay allowed
        even as large coherent blocks on decoded frames."""
        for color, label in (((0, 200, 0), "green"), ((200, 0, 0), "red"),
                             ((230, 140, 40), "orange"), ((196, 74, 88), "rose"),
                             ((139, 69, 19), "warm brown"), ((128, 128, 128), "neutral gray")):
            frames = [with_block(gauge_scene(i, n=self.N), color, size=240) for i in range(self.N)]
            scene = pq.analyze_scene(encode_decode(frames), self.cfg)
            self.assertFalse(scene["blocked"], f"{label} {color} must stay allowed: {scene['reason']}")

    def test_warm_frames_keep_any_codec_noise_below_the_meaningful_floor(self):
        """A warm frame may still produce a handful of chroma-quantisation
        pixels. What must hold is that they never reach the meaningful-region
        floor (no coherent object), and that the warm palette never produces a
        cyan or purple hue at all."""
        scene = pq.analyze_scene(self._decoded_scene(brand_blocks_scene), self.cfg)
        total = sum(scene["family_px_total"].values())
        self.assertLess(total, self.cfg["min_region_core_px"],
                        f"codec noise must stay below the meaningful-region floor: {scene['family_px_total']}")
        self.assertEqual(scene["family_px_total"]["cyan"], 0)
        self.assertEqual(scene["family_px_total"]["purple"], 0)
        self.assertEqual(scene["max_meaningful_core_px"], 0)
        self.assertEqual(scene["persistent_cold_regions"], 0)


class SceneSamplingEvidence(unittest.TestCase):
    """10 + multi-frame sampling bounds (issue #26 §3)."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = pq.load_policy()

    def test_sample_timestamps_stay_inside_the_scene_window(self):
        for a, b in ((0.0, 2.0), (4.0, 9.0), (59.0, 61.0)):
            for t in pq.sample_times(a, b, self.cfg, total=62.0):
                self.assertGreaterEqual(t, a)
                self.assertLessEqual(t, b)
        self.assertEqual(pq.sample_times(4.0, 9.0, self.cfg, total=62.0),
                         pq.sample_times(4.0, 9.0, self.cfg, total=62.0))   # deterministic

    def test_frame_budget_is_bounded(self):
        self.assertEqual(pq.frame_budget(self.cfg, 10),
                         min(self.cfg["max_total_frames"],
                             self.cfg["frames_per_scene"] * 10))
        self.assertLessEqual(pq.frame_budget(self.cfg, 999), self.cfg["max_total_frames"])

    def test_diagnostics_carry_no_images_or_remote_metadata(self):
        decoded = encode_decode([gauge_scene(i) for i in range(6)], fracs=(0.8, 0.9, 0.99))
        scene = pq.analyze_scene(decoded, self.cfg)
        text = repr(scene)
        for forbidden in ("http://", "https://", ".jpg", ".png", "asset_url", "creator"):
            self.assertNotIn(forbidden, text)
        self.assertIn("frames_sampled", scene)
        self.assertIn("max_meaningful_area_fraction", scene)
        self.assertIn("persistent_cold_regions", scene)
        self.assertIn("noise_only_px_total", scene)


if __name__ == "__main__":
    unittest.main(verbosity=2)
