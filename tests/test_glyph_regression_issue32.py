"""EXACT glyph regression for the five issue-#32 defect lines.

reel-2026-09-24 (run 35488263806) burned tofu boxes into the subtitles for
U+2011 non-breaking hyphens ("stand‑up", "chat‑AI", "AI‑generated",
"pause‑and‑reflect") plus an em dash and curly apostrophes: QA scored 93/100
and approved while a human reviewer rejected the reel on sight.

This fixture replays the EXACT subtitle lines from issue #32 through the
single-sourced normalization layer, the ACTUAL production font cmap,
TTS/timing/display parity, subtitle wrapping and the full renderer — and
pins the corrected contraction detector. All artifacts live in a
TemporaryDirectory; nothing is committed.
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import text_norm  # noqa: E402
import visual_plan as vp  # noqa: E402
from reel_engine import Reel  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "issue32_subtitles.json")
POL = common.policy()

# The five exact defect lines from issue #32 (verbatim, raw characters).
# Line 2 appears as the tail of subtitle line 4 in the issue's table — the
# fixture stores the full subtitle line; the fragment is asserted below.
DEFECT_LINES = [
    "You\u2019re in a stand\u2011up, eyes on the screen, but your thoughts wander.",
    "Our minds treat a brief pause as a cue to switch modes, especially with "
    "chat\u2011AI suggestions popping up.",
    "AI\u2011generated agenda suggestion",
    "Recognizing that gap\u2014realizing you\u2019re drafting a reply instead of hearing",
    "Repeat this pause\u2011and\u2011reflect loop",
]
EXPECTED_NORMALIZED = [
    "You're in a stand-up, eyes on the screen, but your thoughts wander.",
    "Our minds treat a brief pause as a cue to switch modes, especially with "
    "chat-AI suggestions popping up.",
    "AI-generated agenda suggestion",
    "Recognizing that gap-realizing you're drafting a reply instead of hearing",
    "Repeat this pause-and-reflect loop",
]


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def make_script(fix):
    """The issue-#32 script shape (producer output BEFORE finalization)."""
    chunks = []
    for i, (beat, line) in enumerate(zip(fix["beats"], fix["lines"])):
        chunks.append({"id": f"c{i + 1}", "beat": beat, "tts_text": line,
                       "en": [{"t": line, "scene": beat, "beat": beat}]})
    return {
        "meta": {"content_id": "issue32-fixture", "content_date": "2026-09-24",
                 "handle": "@metacognition.hq", "topic": fix["topic"],
                 "pillar": "ATTENTION", "language": "en",
                 "technology_angle": "attention during stand-ups with AI tools"},
        "chunks": chunks,
        "scene_tags": {"hook": "THE DRIFT", "problem": "WHY IT HAPPENS",
                       "explain": "THE GAP", "example": "THE MOMENT",
                       "technique": "THE FIX", "ending": "REPLAY IT"},
        "web": ["QUESTION", "DRIFT", "GAP", "DRAFT", "PAUSE", "REFOCUS"],
        "visual_direction": vp.pillar_visual_direction("ATTENTION"),
        "visuals": [],
        "sources": fix["sources"],
    }


def make_timing(script, out_dir, wps=0.42, gap=0.34):
    t = 0.0
    chunks = []
    for ch in script["chunks"]:
        lines = []
        for en in ch["en"]:
            words, lstart = [], t
            for w in en["t"].split():
                words.append({"w": w, "start": round(t, 2), "end": round(t + wps, 2)})
                t += wps
            t += gap
            lines.append({"text": en["t"], "scene": en["scene"], "beat": en["beat"],
                          "start": round(lstart, 2), "end": round(t - gap, 2),
                          "words": words})
        chunks.append({"id": ch["id"], "beat": ch["beat"], "lines": lines, "dur": 1.0})
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


def episode_dir():
    """Episode dir exactly as the pipeline builds it AFTER the producer's
    single-sourced finalization (normalize + glyph gate at source)."""
    fix = load_fixture()
    script = make_script(fix)
    text_norm.normalize_script(script)          # content_producer._finalize_script_text
    assert not text_norm.glyph_gate_issues(script)
    d = tempfile.mkdtemp(prefix="ep_issue32_")
    common.save_json(os.path.join(d, "script.json"), script)
    plan = vp.build_visual_plan(script, POL, allow_external=False)
    common.save_json(os.path.join(d, "visual_plan.json"), plan)
    total = make_timing(script, d)
    silent_wav(d, total)
    return d, script, plan


class ExactDefectLines(unittest.TestCase):
    def test_raw_lines_are_exactly_the_issue32_fixture(self):
        fix = load_fixture()
        self.assertEqual(len(fix["lines"]), 13)
        # the five defect lines appear verbatim in the fixture (the chat-AI
        # defect appears as the issue quoted it — the tail of subtitle line 4)
        for raw in DEFECT_LINES:
            hit = raw in fix["lines"] or any(raw in ln for ln in fix["lines"])
            self.assertTrue(hit, f"fixture lost the raw line {raw!r}")
        self.assertIn("especially with chat\u2011AI suggestions popping up.",
                      [ln[-len("especially with chat\u2011AI suggestions popping up."):]
                       for ln in fix["lines"]])

    def test_normalization_map_matches_expected_forms(self):
        for raw, want in zip(DEFECT_LINES, EXPECTED_NORMALIZED):
            self.assertEqual(text_norm.normalize_text(raw), want)
        # fixture's own expectation table
        for raw, want in load_fixture()["expected_normalization"].items():
            self.assertEqual(text_norm.normalize_text(raw), want)

    def test_raw_lines_lack_glyphs_and_normalized_lines_have_them(self):
        from fontTools.ttLib import TTFont
        cmap = TTFont(text_norm.font_path(600), lazy=True).getBestCmap()
        for raw, norm in zip(DEFECT_LINES, EXPECTED_NORMALIZED):
            raw_missing = [ch for ch in raw if ord(ch) not in cmap and not ch.isspace()]
            norm_missing = [ch for ch in norm if ord(ch) not in cmap and not ch.isspace()]
            self.assertEqual(norm_missing, [], f"normalized line must be fully covered: {norm!r}")
            if "\u2011" in raw:
                self.assertIn("\u2011", raw_missing,
                              f"raw line must demonstrate the missing glyph: {raw!r}")

    def test_no_replacement_char_and_no_tofu_source_after_normalization(self):
        for raw in DEFECT_LINES:
            norm = text_norm.normalize_text(raw)
            self.assertNotIn("\ufffd", norm)
            self.assertEqual(text_norm.has_blockers(norm), [])


class TTSTimingDisplayParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ep, cls.script, cls.plan = episode_dir()
        cls.reel = Reel(cls.ep, POL)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.ep, ignore_errors=True)

    def test_script_and_timing_and_subtitles_share_one_representation(self):
        timing = common.load_json(os.path.join(self.ep, "timing.json"))
        self.assertEqual(text_norm.parity_issues(self.script, timing), [],
                         "timing must derive from the same normalized text as display")
        # every karaoke word IS the normalized token (drift would raise in
        # _build_captions; additionally verify equality here)
        for ln in self.reel.lines:
            for w in ln["words"]:
                self.assertEqual(text_norm.normalize_text(w["w"]), w["w"])
        # subtitle cue text equals the joined normalized words — one source
        cue_words = " ".join(c["text"] for c in self.reel.layout["en"])
        script_words = " ".join(
            en["t"] for ch in self.script["chunks"] for en in ch["en"])
        self.assertEqual(cue_words, script_words)

    def test_wrapping_respects_three_row_cap_and_safe_zone(self):
        L = POL["layout"]
        cues = self.reel.layout["en"]
        self.assertTrue(cues)
        for c in cues:
            self.assertLessEqual(c["rows"], L["en_max_rows"],
                                 f"cue over the 3-row cap: {c['text']!r}")
            x0, top, x1, bottom = c["bbox"]
            self.assertGreaterEqual(x0, (1080 - L["en_max_width"]) // 2 - 2)
            self.assertLessEqual(x1 - x0, L["en_max_width"] + 2)
            self.assertGreaterEqual(top, L["safe_top"])
            self.assertLessEqual(bottom, L["safe_top"] + 3 * L["en_row_height"] + 2)
            self.assertEqual(c["direction"], "ltr")

    def test_glyph_gate_runs_before_frame_zero(self):
        # the renderer already passed its pre-render gates to construct; prove
        # a corrupted script would have been refused BEFORE frame 0
        bad = make_script(load_fixture())
        bad["chunks"][0]["en"][0]["t"] = "stand\ufffdup broken"
        issues = text_norm.glyph_gate_issues(bad)
        self.assertTrue(any("U+FFFD" in i for i in issues))

    def test_upstream_drift_into_subtitles_is_fail_closed(self):
        # if some stage handed the renderer an UNNORMALIZED word (upstream
        # drift), the subtitle builder must refuse — never draw tofu
        bad_dir = tempfile.mkdtemp(prefix="ep_issue32_drift_")
        try:
            script = make_script(load_fixture())
            # skip normalization on purpose — simulate the pre-fix pipeline
            common.save_json(os.path.join(bad_dir, "script.json"), script)
            make_timing(script, bad_dir)
            silent_wav(bad_dir, 90)
            with self.assertRaises(RuntimeError) as ctx:
                Reel(bad_dir, POL)
            msg = str(ctx.exception)
            self.assertTrue("not normalized" in msg or "glyph" in msg, msg)
        finally:
            shutil.rmtree(bad_dir, ignore_errors=True)


class ContractionDetectorSeesBothApostrophes(unittest.TestCase):
    def test_issue32_false_warning_is_fixed(self):
        import qa_supervisor as qa
        fix = load_fixture()
        script = make_script(fix)      # RAW curly apostrophes, as Groq wrote them
        # the ROOT CAUSE: markers use straight apostrophes, the raw script
        # uses curly ones — unnormalized, the old detector saw zero matches
        raw_low = " ".join(common.narration_lines(script)).lower()
        old_style = sum(1 for m in POL["tone"]["informality_markers"] if m in raw_low)
        self.assertEqual(old_style, 0, "reproduces issue #32: raw curly apostrophes "
                                       "match no straight-apostrophe marker")
        # the FIX: QA normalizes with the single-sourced layer first —
        # You're/you're/you've are contractions again
        rep = qa.Report(POL)
        qa.check_english(rep, script, POL)
        self.assertFalse(any("no contractions" in w for w in rep.warnings),
                         f"issue #32 false positive reproduced: {rep.warnings}")
        self.assertGreaterEqual(rep.details["english"]["informality"], 1,
                                "you're must be counted")
        # and still correct on the finalized (straight-apostrophe) script
        text_norm.normalize_script(script)
        rep2 = qa.Report(POL)
        qa.check_english(rep2, script, POL)
        self.assertGreaterEqual(rep2.details["english"]["informality"], 1)
        self.assertFalse(any("no contractions" in w for w in rep2.warnings))


if __name__ == "__main__":
    unittest.main()
