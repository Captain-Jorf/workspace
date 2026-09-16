"""Subtitle layout: at most 3 rendered EN rows per cue (QA hard threshold, unchanged).

Regression for the `subtitle_layout` blocker in run 35043984004 ("EN 4 rows > 3"
x3): the old engine shrank the font down to 38px and kept the 4-row cue. The new
engine SPLITS the cue at a natural phrase/sentence boundary into consecutive
sub-cues, each keeping its own word-level TTS timings — no shrinking, LTR, high
contrast, Instagram safe zone preserved.

Uses the real Sora font (assets/fonts/en-600.ttf) and the actual failing cue
patterns: real 4- and 5-row narration lines at 54px within the 980px column.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import qa_supervisor as qa  # noqa: E402
from reel_engine import Reel  # noqa: E402

POL = common.policy()
LAYOUT = POL["layout"]

# Actual failing cue patterns: these narration lines wrap to 4/5 rows at the full
# 54px subtitle font inside the 980px wide column (verified against en-600.ttf).
LINE_4ROW = ("Language models are trained to be fluent, not calibrated, "
             "and your brain can't tell the difference.")
LINE_5ROW = ("Most people accept the answer, because checking feels slower, "
             "and the pause you skip is where learning lives.")
# A line with a sentence boundary mid-way: the split MUST land on it.
LINE_SENTENCE = ("Language models sound fluent and certain. "
                 "Your brain can't check that confidence without doing extra work.")
LINE_2ROW = "Check one source before you trust the answer."
LINE_1ROW = "That's the habit."

WORDS = 0.4  # seconds per word (synthetic but word-level, like edge-tts timing)


def words_of(text, t0):
    words, t = [], t0
    for w in text.split():
        words.append({"w": w, "start": round(t, 2), "end": round(t + WORDS, 2)})
        t += WORDS
    return words, t


def make_episode(tmp, lines):
    """Write a minimal episode dir (script.json + timing.json) for Reel()."""
    ep = os.path.join(tmp, "ep")
    os.makedirs(ep, exist_ok=True)
    common.save_json(os.path.join(ep, "script.json"), {
        "meta": {"content_id": "reel-test", "content_date": "2077-01-01",
                 "language": "en", "content_language": "en", "handle": "@metacognition.hq",
                 "technology_angle": "confidence calibration in language models"},
        "scene_tags": {}, "web": ["QUESTION", "PROBLEM", "IDEA", "EXAMPLE", "TRY", "YOU"],
        "visuals": {}, "caption": {"hook": "Why?"},
    })
    t = 0.0
    chunks = []
    for i, (text, beat) in enumerate(lines):
        words, t = words_of(text, t)
        chunks.append({"beat": beat, "dur": round(WORDS * len(words) + 0.3, 2),
                       "lines": [{"text": text, "start": round(t - WORDS * len(words), 2),
                                  "end": round(t, 2), "words": words, "beat": beat}]})
    common.save_json(os.path.join(ep, "timing.json"),
                     {"total": round(t + 0.5, 2), "chunks": chunks})
    return ep


class SubtitleSplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="subtitle_split_")
        lines = [(LINE_4ROW, "explain"), (LINE_5ROW, "explain"),
                 (LINE_SENTENCE, "example"), (LINE_2ROW, "technique"), (LINE_1ROW, "ending")]
        cls.ep = make_episode(cls.tmp, lines)
        cls.lines = lines
        cls.reel = Reel(cls.ep, POL)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_cue_has_at_most_three_rows_at_full_size(self):
        self.assertGreater(len(self.reel.layout["en"]), len(self.lines),
                           "cues must be split into more sub-cues than raw lines")
        for e in self.reel.layout["en"]:
            self.assertLessEqual(e["rows"], LAYOUT["en_max_rows"], f"{e['text'][:40]!r} has {e['rows']} rows")
            # No shrinking: the hard limit is enforced by splitting, not font size.
            self.assertEqual(e["font"], LAYOUT["en_font_size"])
            self.assertEqual(e["direction"], "ltr")

    def test_cues_stay_inside_the_instagram_safe_zone(self):
        for e in self.reel.layout["en"]:
            x0, y0, x1, y1 = e["bbox"]
            self.assertGreaterEqual(y0, LAYOUT["safe_top"] - 10)
            self.assertLessEqual(y1, LAYOUT["safe_bottom"])
            self.assertGreaterEqual(x0, 0)
            self.assertLessEqual(x1, LAYOUT["width"])

    def test_split_preserves_text_and_word_order(self):
        original = " ".join(text for text, _ in self.lines)
        rendered = " ".join(e["text"] for e in self.reel.layout["en"])
        self.assertEqual(rendered, original)
        # every sub-cue is a contiguous slice of its source line
        line_texts = [text for text, _ in self.lines]
        for e in self.reel.layout["en"]:
            self.assertTrue(any(e["text"] in lt for lt in line_texts), e["text"])

    def test_split_preserves_word_level_timing(self):
        # word timings per raw line
        words_by_line = []
        t = 0.0
        for text, _ in self.lines:
            words, t = words_of(text, t)
            words_by_line.append(words)
        # map each cue back to its raw line + word span
        idx = 0  # word cursor inside the current line
        line_i = 0
        prev_end = None
        for e in self.reel.layout["en"]:
            n_words = len(e["text"].split())
            span = words_by_line[line_i][idx:idx + n_words]
            idx += n_words
            if idx >= len(words_by_line[line_i]):
                line_i += 1
                idx = 0
            self.assertEqual([w["w"] for w in span], e["text"].split())
            self.assertEqual(e["start"], span[0]["start"])
            self.assertEqual(e["end"], span[-1]["end"])
            if prev_end is not None:
                # sub-cues of consecutive lines never overlap
                self.assertGreaterEqual(e["start"], prev_end - 0.001)
            prev_end = e["end"]

    def _split_boundaries(self, line_text):
        """Last word of each sub-cue that is followed by another sub-cue of the same
        raw line (sub-cues are contiguous and in order, so walk the word list)."""
        cue_texts = [e["text"] for e in self.reel.layout["en"] if e["text"] in line_text]
        self.assertGreater(len(cue_texts), 1, f"expected a split for {line_text[:40]!r}")
        words = line_text.split()
        boundaries, pos = [], 0
        for c in cue_texts:
            end_pos = pos + len(c.split())
            if end_pos < len(words):
                boundaries.append(words[end_pos - 1])
            pos = end_pos
        self.assertEqual(pos, len(words), "sub-cues must cover the raw line exactly")
        return boundaries

    def test_split_lands_on_natural_boundaries(self):
        # 4-row line: split after a phrase/clause boundary (rank >= 1)
        for b in self._split_boundaries(LINE_4ROW):
            self.assertGreaterEqual(Reel._boundary_rank(b), 1,
                                    f"split after {b!r} is not a natural boundary")
        # sentence line: split MUST be after the sentence period
        self.assertEqual(self._split_boundaries(LINE_SENTENCE), ["certain."])
        # 5-row line: at least two cues, all boundaries natural
        for b in self._split_boundaries(LINE_5ROW):
            self.assertGreaterEqual(Reel._boundary_rank(b), 1, b)

    def test_raw_timing_lines_and_cuts_untouched(self):
        self.assertEqual(len(self.reel.lines), len(self.lines),
                         "self.lines must stay on the raw timing lines (karaoke sync, cuts)")
        self.assertEqual([ln["text"] for ln in self.reel.lines], [t for t, _ in self.lines])


class QAThresholdRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="subtitle_qa_")
        lines = [(LINE_4ROW, "explain"), (LINE_SENTENCE, "example"), (LINE_1ROW, "ending")]
        self.ep = make_episode(self.tmp, lines)
        self.reel = Reel(self.ep, POL)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_old_four_row_layout_is_still_blocked_by_qa(self):
        # The old engine emitted one layout entry per raw line, shrunken to 38px,
        # keeping the 4 rows — QA must still block exactly that.
        old = {"en": [], "fa": []}
        t = 0.0
        for text, _ in [(LINE_4ROW, "explain"), (LINE_5ROW, "explain"), (LINE_2ROW, "technique")]:
            old["en"].append({"text": text, "start": t, "end": t + 4, "rows": 4, "font": 38,
                              "direction": "ltr", "bbox": [50, 200, 1030, 200 + 4 * 78]})
            t += 5
        timing = common.load_json(os.path.join(self.ep, "timing.json"))
        r = qa.Report(POL)
        qa.check_layout(r, old, timing, POL)
        self.assertEqual(r.blocking, ["[subtitle_layout] EN 4 rows > 3"] * 3)

    def test_new_split_layout_passes_layout_qa(self):
        timing = common.load_json(os.path.join(self.ep, "timing.json"))
        r = qa.Report(POL)
        qa.check_layout(r, self.reel.layout, timing, POL)
        self.assertEqual(r.blocking, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
