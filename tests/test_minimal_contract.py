"""Issue #33: the MINIMAL reel style — the default scheduled production format.

Fixtures (test media only in TemporaryDirectory, never committed):
  A  long hook wraps into 2-3 centered lines inside the safe bounds, above
     the documented readable floor (never shrunk below it);
  B  long compound words fit, or are split at a safe hyphen break;
  C  unicode-normalized punctuation parity — normalized strings reach the
     renderer, no tofu/U+FFFD, TTS/timing/display use the same words;
  D  final CTA: explicit Follow + @metacognition.hq, centered, readable,
     required duration;
  E  minimal visual complexity — one dominant visual per scene, <= 3
     meaningful labels, no label collisions, no lattice/web;
  F  production-equivalent H.264 round-trip — banner / middle / CTA frames
     decode cleanly from the real MP4 and pass the minimal contract checks.

Also pinned here:
  * zero Openverse / photo calls in minimal mode (the fetcher is mocked to
    RAISE if ever called — the pipeline, plan builder and renderer all run
    without touching it);
  * the six-scene stable structure (one idea per scene);
  * style selection: cron-forced minimal, explicit-flag rich, never LLM text.
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import asset_fetch as af  # noqa: E402
import content_producer as cp  # noqa: E402
import pipeline as pl  # noqa: E402
import qa_supervisor as qa  # noqa: E402
import style_config as sc  # noqa: E402
import visual_plan as vp  # noqa: E402

POL = common.policy()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def topic_stub():
    return {"content_date": "2026-09-20", "content_id": "reel-minimal-test",
            "title": "Automation bias in AI assistants",
            "normalized_topic": "automation bias in ai assistants",
            "pillar": "AI_JUDGMENT", "technology_angle": "AI assistants",
            "discovery_source": {"name": "x", "url": "", "tier": "cal"},
            "evidence_mode": "calendar", "calendar": {}}


def minimal_script():
    script = cp.build_script_from_playbook(topic_stub(), POL,
                                           cp.PLAYBOOKS["automation-bias"],
                                           "automation-bias")
    script, changed = pl.minimal_prepare_cta(script)
    assert changed
    return script


def make_timing(script, out_dir, wps=0.4):
    t = 0.0
    chunks = []
    for ch in script["chunks"]:
        lines = []
        for en in ch["en"]:
            words, lstart = [], t
            for w in en["t"].split():
                words.append({"w": w, "start": round(t, 2),
                              "end": round(t + wps, 2)})
                t += wps
            t += 0.34
            lines.append({"text": en["t"], "scene": en.get("scene"),
                          "beat": ch["beat"], "start": round(lstart, 2),
                          "end": round(t - 0.34, 2), "words": words})
        chunks.append({"id": ch["id"], "beat": ch["beat"], "lines": lines,
                       "dur": 1.0})
    total = round(t + 0.5, 2)
    common.save_json(os.path.join(out_dir, "timing.json"),
                     {"total": total, "chunks": chunks})
    return total


def silent_wav(out_dir, seconds):
    with wave.open(os.path.join(out_dir, "full.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        n = int(seconds * 44100)
        w.writeframes(struct.pack("<%dh" % n, *([0] * n)))


class Episode:
    """Temporary episode dir with a minimal script + plan (+optional render)."""

    def __init__(self, script=None, style="minimal"):
        self.dir = tempfile.TemporaryDirectory(prefix="mn_ep_")
        self.d = self.dir.name
        self.script = script or minimal_script()
        common.save_json(os.path.join(self.d, "script.json"), self.script)
        self.plan = vp.build_visual_plan(self.script, POL, style=style)
        common.save_json(os.path.join(self.d, "visual_plan.json"), self.plan)
        self.total = make_timing(self.script, self.d)
        silent_wav(self.d, self.total)

    def reel(self, style="minimal"):
        from reel_engine import Reel
        return Reel(self.d, POL, style=style)

    def cleanup(self):
        self.dir.cleanup()


# ---------------------------------------------------------------------------
# zero Openverse in minimal (issue #33 §11)
# ---------------------------------------------------------------------------

class ZeroOpenverseInMinimal(unittest.TestCase):
    def test_plan_builder_never_calls_the_fetcher(self):
        """build_visual_plan(style='minimal') must not touch asset_fetch —
        even when ASSET_FETCH=1 (the workflow's default) is set."""
        script = minimal_script()
        with mock.patch.dict(os.environ, {"ASSET_FETCH": "1"}):
            with mock.patch.object(af, "fetch_image",
                                   side_effect=AssertionError(
                                       "Openverse was called in minimal mode")) \
                    as fetch, \
                 mock.patch.object(af, "_get",
                                   side_effect=AssertionError(
                                       "Openverse HTTP was called in minimal mode")) as get:
                plan = vp.build_visual_plan(script, POL, allow_external=True,
                                            style="minimal")
                fetch.assert_not_called()
                get.assert_not_called()
        self.assertEqual(plan["photo_policy"]["designated"], [])
        self.assertEqual(plan["photo_policy"]["photos_retrieved"], 0)
        self.assertEqual(plan["photo_policy"]["degraded"], False)
        for sc in plan["scenes"]:
            self.assertIn(sc["asset"]["kind"], ("repo", "procedural"))
            self.assertFalse(sc["photo_designated"])
            self.assertFalse(sc["code_justified"])
            self.assertFalse(sc["cursor_justified"])

    def test_reel_render_never_calls_the_fetcher(self):
        ep = Episode()
        try:
            with mock.patch.dict(os.environ, {"ASSET_FETCH": "1"}):
                with mock.patch.object(af, "fetch_image",
                                       side_effect=AssertionError(
                                           "Openverse called during render")) as fetch:
                    reel = ep.reel()
                    for t in (0.1, 5.0, reel.total * 0.5, reel.total - 1.0):
                        reel.frame(t)
                fetch.assert_not_called()
        finally:
            ep.cleanup()

    def test_pipeline_forces_asset_fetch_off_in_minimal(self):
        """produce() in minimal style must force ASSET_FETCH=0 for every
        child process (structural guard: the workflow sets ASSET_FETCH=1
        for the rich path, so the pipeline itself must neutralize it)."""
        with open(os.path.join(ROOT, "build", "pipeline.py"),
                  encoding="utf-8") as f:
            src = f.read()
        # the minimal branch exists and forces the fetch env off
        self.assertIn("style_config.MINIMAL", src)
        self.assertIn('os.environ["ASSET_FETCH"] = "0"', src)
        # and the plan build always passes the resolved style
        self.assertIn("vp.build_visual_plan(script, pol, style=STYLE)", src)
        # behavior: resolving with no flags (manual/cron) is minimal, and
        # the pipeline's handoff to the renderer is explicit
        style, _ = sc.style_for_pipeline(environ={})
        self.assertEqual(style, sc.MINIMAL)

    def test_pipeline_minimal_plan_is_rebuilt_not_reused_as_rich(self):
        """A cached RICH plan must never be reused for a minimal render."""
        ep = Episode(style="experimental-rich")
        try:
            rich_plan = common.load_json(
                os.path.join(ep.d, "visual_plan.json"))
            self.assertNotEqual(rich_plan.get("style"), "minimal")
            self.assertTrue(rich_plan.get("scenes"))
            # pipeline's plan-reuse predicate (mirrored exactly)
            STYLE = sc.MINIMAL
            plan = rich_plan
            plan_style_ok = bool(plan) and plan.get("style") == STYLE
            self.assertFalse(plan_style_ok,
                             "a rich plan must be rebuilt, never reused, "
                             "for a minimal render")
        finally:
            ep.cleanup()


# ---------------------------------------------------------------------------
# the six-scene stable structure (issue #33 §3)
# ---------------------------------------------------------------------------

class SixSceneContract(unittest.TestCase):
    BEATS = ("hook", "problem", "explain", "example", "technique", "ending")
    CATS = {"hook": "mn-banner", "problem": "mn-two-state",
            "explain": "mn-dial", "example": "mn-card",
            "technique": "mn-three-step", "ending": "mn-cta"}

    def test_six_scenes_one_idea_per_beat(self):
        ep = Episode()
        try:
            plan = ep.plan
            self.assertEqual(plan.get("style"), "minimal")
            self.assertEqual(len(plan["scenes"]), 6)
            for i, sc in enumerate(plan["scenes"]):
                self.assertEqual(sc["beat"], self.BEATS[i])
                self.assertEqual(sc["visual_category"],
                                 self.CATS[self.BEATS[i]])
                self.assertEqual(sc["beat_split"], 0,
                                 "one scene per beat — no splits")
                self.assertTrue(sc["narration"].strip())
            issues = vp.visual_semantic_issues(plan, ep.script, POL)
            self.assertEqual(issues, [], issues)
        finally:
            ep.cleanup()

    def test_reel_cuts_follow_the_six_beats(self):
        ep = Episode()
        try:
            reel = ep.reel()
            beats = [b for _, b in reel.cuts]
            self.assertEqual(beats, list(self.BEATS))
        finally:
            ep.cleanup()

    def test_missing_beat_fails_closed(self):
        script = minimal_script()
        # drop the technique beat
        script = dict(script)
        script["chunks"] = [c for c in script["chunks"]
                            if c["beat"] != "technique"]
        with self.assertRaises(ValueError):
            vp.build_visual_plan(script, POL, style="minimal")


# ---------------------------------------------------------------------------
# fixture A — long hook wraps 2-3 centered lines above the floor
# ---------------------------------------------------------------------------

class FixtureA_HookWrap(unittest.TestCase):
    def test_long_hook_wraps_inside_safe_bounds_above_floor(self):
        import text_norm
        from reel_engine import Reel
        # a 14-word hook (policy max is 15) — must wrap to <= 3 centered
        # lines, never below the documented floor
        long_hook = ("When does your AI assistant make you stop asking "
                     "whether the answer is even right?")
        self.assertLessEqual(len(long_hook.split()), 15)
        floor = POL["minimal"]["floor_main"]
        res = Reel.mn_safe_wrap(long_hook, 800, 64, floor,
                                Reel.MN_MAX_TEXT_W - 60, 3)
        self.assertIsNotNone(res, "a policy-length hook must fit at the floor")
        lines, size = res
        self.assertGreaterEqual(size, floor, "never shrink below the floor")
        self.assertLessEqual(len(lines), 3)
        # each line measured with the REAL font fits the width
        from reel_engine import font
        f = font("en", 800, size)
        for ln in lines:
            self.assertLessEqual(f.getlength(ln), Reel.MN_MAX_TEXT_W - 60)
        # and the full banner renders with the hook in the first frame
        script = minimal_script()
        first_line = [l for c in script["chunks"] if c["beat"] == "hook"
                      for l in c["en"]][0]["t"]
        ep = Episode(script=script)
        try:
            reel = ep.reel()
            fr = reel.frame(0.08)
            # the hook text must be visible (bright) in its declared zone
            import numpy as np
            hook_items = [it for it in reel.layout["minimal"]
                          if it["scene"] == "s1" and it["kind"] == "hook"]
            self.assertTrue(hook_items)
            a = np.array(fr)
            for it in hook_items:
                x0, y0, x1, y1 = it["bbox"]
                box = a[y0:y1, x0:x1]
                lum = (0.2126 * box[..., 0] + 0.7152 * box[..., 1]
                       + 0.0722 * box[..., 2])
                self.assertGreater(float((lum > 100).mean()), 0.05,
                                   f"hook line {it['text']!r} not visible")
            # brand line + handle present too
            kinds = {it["kind"] for it in reel.layout["minimal"]
                     if it["scene"] == "s1"}
            self.assertIn("brand", kinds)
            self.assertIn("handle", kinds)
            self.assertEqual(
                [it for it in reel.layout["minimal"]
                 if it["scene"] == "s1" and it["kind"] == "brand"][0]["text"],
                "METACOGNITION FOR THE AI AGE")
        finally:
            ep.cleanup()

    def test_hook_that_cannot_fit_never_shrinks_below_floor(self):
        from reel_engine import Reel
        # a single un-splittable word longer than the safe width at the
        # floor → the wrap must FAIL (None), and the renderer must split
        # at a safe hyphen or fail closed — never emit sub-floor text
        floor = POL["minimal"]["floor_main"]
        word = "x" * 90  # no hyphen, no space — pathological
        self.assertIsNone(
            Reel.mn_safe_wrap(word, 800, 64, floor, 200, 3),
            "an un-splittable word cannot fit — must not silently shrink")

    def test_hook_becomes_a_banner_block_in_the_plan(self):
        ep = Episode()
        try:
            banner = ep.plan["scenes"][0]["banner"]
            self.assertEqual(banner["brand_line"], "METACOGNITION FOR THE AI AGE")
            self.assertTrue(banner["hook_text"].strip())
            self.assertEqual(banner["handle"], "@metacognition.hq")
            self.assertTrue(banner["gold_keyword"])
            # the gold keyword is a word OF the hook line (deterministic)
            self.assertIn(banner["gold_keyword"].lower(),
                          banner["hook_text"].lower().split())
        finally:
            ep.cleanup()


# ---------------------------------------------------------------------------
# fixture B — long compound words fit or split safely
# ---------------------------------------------------------------------------

class FixtureB_CompoundWords(unittest.TestCase):
    def test_compound_word_fits_when_possible(self):
        from reel_engine import Reel
        res = Reel.mn_safe_wrap("The counter-intuitive drift is measurable",
                                800, 64, 44, 900, 3)
        self.assertIsNotNone(res)
        joined = " ".join(res[0])
        self.assertIn("counter-intuitive", joined)

    def test_wide_compound_splits_at_safe_hyphen(self):
        from reel_engine import Reel
        ep = Episode()
        try:
            # 393px at the floor — wider than the 380px safe width, so the
            # wrap must split at the hyphen (a safe, visible break) instead
            # of shrinking below the floor or clipping
            res = ep.reel()._mn_wrap_or_split("counter-intuitive thinking",
                                              800, 64, 44, 380, 3)
            self.assertIsNotNone(res, "the safe hyphen split must recover")
            lines, size = res
            self.assertGreaterEqual(size, 44)
            self.assertEqual(" ".join(lines), "counter intuitive thinking")
            # the raw un-split compound word must not appear on any line
            for ln in lines:
                self.assertNotIn("counter-intuitive", ln)
            # and when the width ALLOWS it, the word stays intact (no split)
            res2 = Reel.mn_safe_wrap("counter-intuitive thinking",
                                     800, 64, 44, 700, 3)
            self.assertIsNotNone(res2)
            self.assertIn("counter-intuitive", " ".join(res2[0]))
        finally:
            ep.cleanup()


# ---------------------------------------------------------------------------
# fixture C — unicode normalization parity (no tofu, one representation)
# ---------------------------------------------------------------------------

class FixtureC_NormalizationParity(unittest.TestCase):
    def test_banner_and_cta_text_are_normalized(self):
        import text_norm
        ep = Episode()
        try:
            banner = ep.plan["scenes"][0]["banner"]
            cta = ep.plan["scenes"][-1]["cta"]
            self.assertEqual(text_norm.normalize_text(banner["hook_text"]),
                             banner["hook_text"])
            self.assertEqual(text_norm.normalize_text(cta["follow_line"]),
                             cta["follow_line"])
            self.assertFalse(text_norm.contains_replacement_char(
                banner["hook_text"] + cta["follow_line"]))
        finally:
            ep.cleanup()

    def test_punctuation_variant_reaches_render_as_normalized(self):
        """A curly-quote / em-dash hook is normalized ONCE (plan + subtitles
        + TTS words share the representation) — no tofu glyphs, no U+FFFD.

        The curly apostrophe MUST normalize to a straight one; the spaced
        em-dash stays only because the PRODUCTION font's cmap covers it
        (otherwise text_norm rewrites it to '-'). Either way the rendered
        string is idempotent and fully covered by the font.
        """
        import text_norm
        script = minimal_script()
        hook_lines = [l for c in script["chunks"] if c["beat"] == "hook"
                      for l in c["en"]]
        raw = "So\u2019s the catch: the tool is fluent \u2014 and you feel sure."
        # the producer normalizes script lines ONCE before writing
        # script.json — TTS/timing/display then share that representation
        hook_lines[0]["t"] = text_norm.normalize_text(raw)
        ep = Episode(script=script)
        try:
            banner = ep.plan["scenes"][0]["banner"]
            hook = banner["hook_text"]
            self.assertNotIn("\u2019", hook, "curly quote must normalize")
            self.assertIn("'", hook)
            # the em-dash is either kept (font covers it) or hyphenated —
            # never a variant the production font cannot draw
            if "\u2014" in hook:
                self.assertIn(0x2014, text_norm.supported_codepoints())
            self.assertEqual(hook, text_norm.normalize_text(hook),
                             "the plan must carry the idempotent "
                             "normalized form")
            self.assertFalse(text_norm.contains_replacement_char(hook))
            # every declared text item renders with the production font
            reel = ep.reel()  # glyph gate runs in __init__ — raises on tofu
            for it in reel.layout["minimal"]:
                self.assertFalse(
                    text_norm.contains_replacement_char(it["text"]))
                self.assertEqual(it["text"],
                                 text_norm.normalize_text(it["text"]))
        finally:
            ep.cleanup()

    def test_subtitle_words_are_the_normalized_tts_words(self):
        ep = Episode()
        try:
            reel = ep.reel()
            import text_norm
            n_words = 0
            for ln in self._captions(reel):
                for row in ln["rows"]:
                    for wd, _imgs, _off in row:
                        n_words += 1
                        self.assertEqual(text_norm.normalize_text(wd["w"]),
                                         wd["w"])
                        self.assertFalse(text_norm.contains_replacement_char(
                            wd["w"]))
            self.assertGreater(n_words, 50)
        finally:
            ep.cleanup()

    @staticmethod
    def _captions(reel):
        return reel.cap_lines


# ---------------------------------------------------------------------------
# fixture D — final CTA contract
# ---------------------------------------------------------------------------

class FixtureD_FinalCTA(unittest.TestCase):
    def test_cta_scene_exists_with_follow_and_handle(self):
        ep = Episode()
        try:
            cta = ep.plan["scenes"][-1]
            self.assertEqual(cta["visual_category"], "mn-cta")
            self.assertEqual(cta["beat"], "ending")
            cc = cta["cta"]
            self.assertIn("follow", cc["follow_line"].lower())
            self.assertIn("@metacognition.hq", cc["follow_line"])
            self.assertEqual(cc["handle"], "@metacognition.hq")
            self.assertEqual(cc["gold_phrase"], "@metacognition.hq")
        finally:
            ep.cleanup()

    def test_cta_duration_in_required_window(self):
        ep = Episode()
        try:
            cd = ep.plan["scenes"][-1]["expected_duration"]
            lo, hi = ep.plan["cta_contract"]["duration_target_s"]
            self.assertGreaterEqual(cd, lo,
                                    f"CTA {cd:.2f}s below {lo}s target")
            self.assertLessEqual(cd, hi,
                                 f"CTA {cd:.2f}s above {hi}s target")
        finally:
            ep.cleanup()

    def test_cta_is_spoken_and_displayed_as_one_line(self):
        """The canonical follow line replaces the ending beat BEFORE TTS —
        audio and display are the same line (one kinetic layer)."""
        script = minimal_script()
        ending = [l["t"] for c in script["chunks"] if c["beat"] == "ending"
                  for l in c["en"]]
        self.assertEqual(ending, [vp.MINIMAL_CTA_LINE])
        # idempotent
        script2, changed = pl.minimal_prepare_cta(script)
        self.assertFalse(changed)

    def test_cta_visible_in_rendered_frame(self):
        import numpy as np
        ep = Episode()
        try:
            reel = ep.reel()
            t_cta = reel.total - 1.5
            a = np.array(reel.frame(t_cta))
            cta_items = [it for it in reel.layout["minimal"]
                         if it["kind"] == "cta"]
            self.assertTrue(cta_items)
            for it in cta_items:
                x0, y0, x1, y1 = it["bbox"]
                lum = (0.2126 * a[y0:y1, x0:x1, 0]
                       + 0.7152 * a[y0:y1, x0:x1, 1]
                       + 0.0722 * a[y0:y1, x0:x1, 2])
                self.assertGreater(float((lum > 100).mean()), 0.04)
        finally:
            ep.cleanup()

    def test_non_follow_ending_is_replaced(self):
        """A playbook ending (recap + question, ~10s, no follow ask) is
        normalized to the canonical CTA — the question chunk is dropped."""
        script = cp.build_script_from_playbook(
            topic_stub(), POL, cp.PLAYBOOKS["automation-bias"],
            "automation-bias")
        before = [l["t"] for c in script["chunks"] if c["beat"] == "ending"
                  for l in c["en"]]
        self.assertNotIn("follow", " ".join(before).lower())
        new_script, changed = pl.minimal_prepare_cta(script)
        self.assertTrue(changed)
        after = [l["t"] for c in new_script["chunks"] if c["beat"] == "ending"
                 for l in c["en"]]
        self.assertEqual(after, [vp.MINIMAL_CTA_LINE])


# ---------------------------------------------------------------------------
# fixture E — visual complexity: one dominant visual, <= 3 labels, no
# lattice/web, no overlapping semantic text
# ---------------------------------------------------------------------------

class FixtureE_VisualComplexity(unittest.TestCase):
    def test_at_most_three_meaningful_labels_per_scene(self):
        ep = Episode()
        try:
            for sc in ep.plan["scenes"]:
                self.assertLessEqual(len(sc.get("labels", [])), 3)
                for lab in sc.get("labels", []):
                    self.assertLessEqual(len(lab), 12)
        finally:
            ep.cleanup()

    def test_no_lattice_or_web_in_minimal(self):
        ep = Episode()
        try:
            reel = ep.reel()
            self.assertEqual(reel.lat, [],
                             "no background-word lattice in minimal")
            self.assertEqual(reel.nodes, [], "no node web in minimal")
            self.assertEqual(reel.web_edges, [])
        finally:
            ep.cleanup()

    def test_no_declared_text_overlaps(self):
        ep = Episode()
        try:
            items = [it for it in ep.reel().layout["minimal"]
                     if it["kind"] in ("hook", "cta", "brand", "handle",
                                       "label")]
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    a, b = items[i], items[j]
                    if a["scene"] != b["scene"]:
                        continue
                    ax0, ay0, ax1, ay1 = a["bbox"]
                    bx0, by0, bx1, by1 = b["bbox"]
                    ox = min(ax1, bx1) - max(ax0, bx0)
                    oy = min(ay1, by1) - max(ay0, by0)
                    self.assertFalse(ox > 6 and oy > 6,
                                     f"overlap {a['text']!r} / "
                                     f"{b['text']!r}")
        finally:
            ep.cleanup()

    def test_all_declared_text_inside_safe_bounds(self):
        ep = Episode()
        try:
            m = POL["minimal"]
            top, bottom, side = m["safe"]["top"], m["safe"]["bottom"], m["safe"]["side"]
            items = ep.reel().layout["minimal"]
            self.assertTrue(items)
            for it in items:
                x0, y0, x1, y1 = it["bbox"]
                if it["kind"] in ("hook", "cta", "brand", "handle", "label"):
                    self.assertGreaterEqual(x0, side, it)
                    self.assertGreaterEqual(y0, top, it)
                    self.assertLessEqual(x1, 1080 - side, it)
                    self.assertLessEqual(y1, bottom, it)
        finally:
            ep.cleanup()

    def test_font_floors_respected(self):
        ep = Episode()
        try:
            m = POL["minimal"]
            for it in ep.reel().layout["minimal"]:
                if it["kind"] == "label":
                    self.assertGreaterEqual(it["font"], m["floor_label"], it)
                elif it["kind"] in ("hook", "cta"):
                    self.assertGreaterEqual(it["font"], m["floor_main"], it)
                elif it["kind"] in ("brand", "handle"):
                    self.assertGreaterEqual(it["font"], m["floor_brand"], it)
        finally:
            ep.cleanup()


# ---------------------------------------------------------------------------
# style selection (issue #33 §2)
# ---------------------------------------------------------------------------

class StyleSelection(unittest.TestCase):
    def test_cron_forces_minimal_even_with_rich_flags(self):
        e = {"GITHUB_EVENT_NAME": "schedule",
             "REEL_STYLE": "experimental-rich",
             "ALLOW_EXPERIMENTAL_STYLE": "1"}
        self.assertEqual(sc.resolve_style(e), "minimal")

    def test_rich_needs_both_flags(self):
        self.assertEqual(sc.resolve_style(
            {"REEL_STYLE": "experimental-rich",
             "ALLOW_EXPERIMENTAL_STYLE": "1"}), "experimental-rich")
        # ungated rich request: fail closed (no silent renderer switch)
        with self.assertRaises(ValueError):
            sc.resolve_style({"REEL_STYLE": "experimental-rich"})
        self.assertEqual(sc.resolve_style({}), "minimal")

    def test_unknown_style_fails_closed(self):
        for bad in ("rich", "minimal rich", "auto"):
            with self.assertRaises(ValueError):
                sc.resolve_style({"REEL_STYLE": bad,
                                  "ALLOW_EXPERIMENTAL_STYLE": "1"})

    def test_llm_text_cannot_select_a_style(self):
        with self.assertRaises(ValueError):
            sc.normalize_style("make it fancy with photos please")

    def test_build_visual_plan_rejects_unknown_style(self):
        script = minimal_script()
        with self.assertRaises(ValueError):
            vp.build_visual_plan(script, POL, style="fancy")
        # None keeps legacy rich (back-compat)
        legacy = vp.build_visual_plan(script, POL, allow_external=False)
        self.assertNotEqual(legacy.get("style"), "minimal")


# ---------------------------------------------------------------------------
# fixture F — production-equivalent H.264 round-trip
# ---------------------------------------------------------------------------

class FixtureF_H264RoundTrip(unittest.TestCase):
    def _render_mp4(self, ep):
        from render_auto import main as render_main
        import subprocess
        out = os.path.join(ep.d, "auto-test.mp4")
        cmd = [sys.executable, os.path.join(ROOT, "build", "render_auto.py"),
               "--ep", ep.d, "--out", out]
        r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True,
                           timeout=2400)
        self.assertEqual(r.returncode, 0,
                         f"render failed: {r.stdout[-800:]} {r.stderr[-800:]}")
        return out

    def test_h264_roundtrip_banner_middle_cta_clean(self):
        ep = Episode()
        video = None
        try:
            video = self._render_mp4(ep)
            self.assertTrue(os.path.exists(video))
            ok, err = qa.decode_ok(video)
            self.assertTrue(ok, f"decoded MP4 failed: {err}")
            # sample decoded frames at banner / a body scene / CTA — only
            # the ACTIVE scene's declared text may be ink-checked at each t
            import numpy as np
            import bisect
            layout = common.load_json(os.path.join(ep.d, "layout.json"), {})
            cuts = [(b["start"], b["beat"]) for b in layout.get("beats", [])]
            cut_t = [t for t, _ in cuts]
            scene_of_beat = {sc["beat"]: sc["scene_id"] for sc in ep.plan["scenes"]}

            def active_scene_at(t):
                i = bisect.bisect_right(cut_t, t) - 1
                return scene_of_beat[cuts[max(0, i)][1]]

            frame_tmp = tempfile.mkdtemp(prefix="mn_rt_")
            try:
                for t, kinds_wanted in (
                        (0.08, ("hook", "brand", "handle")),
                        (ep.total * 0.45, ("label",)),
                        (ep.total - 1.5, ("cta",))):
                    arr = qa._decode_frame(video, t, frame_tmp)
                    self.assertIsNotNone(arr,
                                         f"could not decode frame at {t}s")
                    a = arr.astype(np.int16)
                    self.assertGreater(float(np.std(a)), 2.0,
                                       f"frame at {t}s is blank")
                    sid = active_scene_at(t)
                    items = [it for it in layout["minimal"]
                             if it["scene"] == sid
                             and it["kind"] in kinds_wanted]
                    self.assertTrue(items,
                                    f"no {kinds_wanted} item for scene {sid}")
                    for it in items:
                        ink = qa._mn_ink_fraction(
                            a, [it["bbox"][0] - 6, it["bbox"][1] - 6,
                                it["bbox"][2] + 6, it["bbox"][3] + 6])
                        self.assertGreater(ink, 0.04,
                                           f"{it['kind']} {it['text']!r} "
                                           f"not visible in decoded frame "
                                           f"at {t}s")
            finally:
                shutil.rmtree(frame_tmp, ignore_errors=True)
            # the supervisor's minimal contract runs on the REAL mp4
            rep = qa.Report(POL)
            layout = common.load_json(os.path.join(ep.d, "layout.json"), {})
            plan = common.load_json(os.path.join(ep.d, "visual_plan.json"))
            script = common.load_json(os.path.join(ep.d, "script.json"))
            qa.check_minimal_contract(rep, ep.d, script, video, POL,
                                      no_frames=False)
            blockers = [b for b in rep.blocking if b.startswith("minimal")]
            self.assertEqual(blockers, [], blockers)
            self.assertEqual(rep.details["minimal_contract"]["frames"],
                             len(ep.plan["scenes"]),
                             "every scene's decoded frames must be checked")
        finally:
            if video and os.path.exists(video):
                os.remove(video)
            ep.cleanup()


# ---------------------------------------------------------------------------
# QA blocker wiring — the minimal contract must VETO
# ---------------------------------------------------------------------------

class MinimalQAVeto(unittest.TestCase):
    def _qa_on(self, ep, video, no_frames=True):
        rep = qa.Report(POL)
        script = common.load_json(os.path.join(ep.d, "script.json"))
        qa.check_minimal_contract(rep, ep.d, script, video, POL, no_frames)
        return rep

    def test_external_asset_in_plan_blocks(self):
        ep = Episode()
        try:
            sc = ep.plan["scenes"][2]
            sc["asset"] = {"kind": "external", "id": "x",
                           "url": "https://openverse.org/x",
                           "asset_url": "https://openverse.org/x.jpg",
                           "license": "cc0", "sha256": "ab" * 32,
                           "retrieved_utc": "now", "query_sanitized": "q"}
            common.save_json(os.path.join(ep.d, "visual_plan.json"), ep.plan)
            rep = self._qa_on(ep, None)
            self.assertTrue(any("external asset in minimal" in b
                                for b in rep.blocking), rep.blocking)
        finally:
            ep.cleanup()

    def test_missing_cta_blocks(self):
        ep = Episode()
        try:
            ep.plan["scenes"][-1]["cta"] = {"follow_line": "", "handle": ""}
            common.save_json(os.path.join(ep.d, "visual_plan.json"), ep.plan)
            rep = self._qa_on(ep, None)
            self.assertTrue(any("final CTA" in b for b in rep.blocking),
                            rep.blocking)
        finally:
            ep.cleanup()

    def test_text_outside_safe_bounds_blocks(self):
        ep = Episode()
        try:
            # shift a declared label outside the safe zone
            reel = ep.reel()
            for it in reel.layout["minimal"]:
                if it["kind"] == "label":
                    it["bbox"] = [10, it["bbox"][1], it["bbox"][2],
                                  it["bbox"][3]]
                    break
            common.save_json(os.path.join(ep.d, "layout.json"),
                             {"minimal": reel.layout["minimal"]})
            rep = self._qa_on(ep, None)
            self.assertTrue(any("safe bounds" in b for b in rep.blocking),
                            rep.blocking)
        finally:
            ep.cleanup()

    def test_non_minimal_run_is_a_noop(self):
        ep = Episode(style="experimental-rich")
        try:
            rep = self._qa_on(ep, None)
            self.assertFalse(rep.blocking)
            self.assertEqual(rep.details["minimal_contract"]["applied"], False)
        finally:
            ep.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
