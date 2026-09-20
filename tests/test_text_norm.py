"""Single-sourced text normalization + production-font glyph coverage.

Issue #32 (run 35488263806, reel-2026-09-24): burned-in subtitles showed tofu
boxes for U+2011 (non-breaking hyphen) because the production font (Sora
latin subset) has no glyph for it, and normalization happened nowhere. These
tests pin the ONE normalization layer that now feeds TTS, word timing,
subtitle wrapping, burned-in rendering, labels, posters and captions — and
the glyph gate that verifies coverage against the ACTUAL font cmap.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import text_norm as tn  # noqa: E402


class NormalizationMap(unittest.TestCase):
    def test_dash_family_maps_to_ascii_hyphen_word_internally(self):
        # U+2010 HYPHEN, U+2011 NON-BREAKING HYPHEN (the reel-2026-09-24 tofu),
        # U+2012 FIGURE DASH, U+2212 MINUS SIGN — all become '-'
        self.assertEqual(tn.normalize_text("stand\u2011up"), "stand-up")
        self.assertEqual(tn.normalize_text("chat\u2011AI"), "chat-AI")
        self.assertEqual(tn.normalize_text("AI\u2011generated"), "AI-generated")
        self.assertEqual(tn.normalize_text("pause\u2011and\u2011reflect"), "pause-and-reflect")
        self.assertEqual(tn.normalize_text("a\u2010b"), "a-b")
        self.assertEqual(tn.normalize_text("a\u2012b"), "a-b")
        self.assertEqual(tn.normalize_text("5\u22123"), "5-3")

    def test_en_em_dash_word_internal_becomes_hyphen(self):
        # Issue #32 line 6: "gap—realizing" and "hearing—creates"
        self.assertEqual(tn.normalize_text("gap\u2014realizing"), "gap-realizing")
        self.assertEqual(tn.normalize_text("hearing\u2014creates"), "hearing-creates")
        self.assertEqual(tn.normalize_text("a\u2013b"), "a-b")

    def test_en_em_dash_as_punctuation_survives_when_supported(self):
        # spaced dashes are sentence punctuation: never deleted; kept when the
        # production font covers them (Sora does: U+2013/U+2014 are in the cmap)
        cov = tn.supported_codepoints()
        self.assertIn(0x2014, cov)
        self.assertEqual(tn.normalize_text("wait \u2014 then go"), "wait \u2014 then go")

    def test_curly_quotes_map_to_supported_ascii_forms(self):
        self.assertEqual(tn.normalize_text("brain\u2019s"), "brain's")
        self.assertEqual(tn.normalize_text("you\u2019re"), "you're")
        self.assertEqual(tn.normalize_text("you\u2019ve"), "you've")
        self.assertEqual(tn.normalize_text("\u2018x\u2019"), "'x'")
        self.assertEqual(tn.normalize_text("\u201chold on\u201d"), '"hold on"')
        self.assertEqual(tn.normalize_text("\u00abici\u00bb"), '"ici"')

    def test_zero_width_chars_removed_and_spaces_normalized(self):
        self.assertEqual(tn.normalize_text("a\u200bb"), "ab")
        self.assertEqual(tn.normalize_text("a\u200cb\u200dc\ufeff"), "abc")
        self.assertEqual(tn.normalize_text("a\u00adb"), "ab")
        self.assertEqual(tn.normalize_text("a\u00a0b"), "a b")     # NBSP → space
        self.assertEqual(tn.normalize_text("a\u202fb"), "a b")     # NNBSP → space
        self.assertEqual(tn.normalize_text("a\u2009b"), "a b")     # thin space
        self.assertEqual(tn.normalize_text("a \u200b b"), "a b")   # no doubled space

    def test_word_count_never_changes(self):
        samples = [
            "You\u2019re in a stand\u2011up, eyes on the screen, but your thoughts wander.",
            "Recognizing that gap\u2014realizing you\u2019re drafting a reply instead of "
            "hearing\u2014creates a metacognitive checkpoint.",
            "Repeat this pause\u2011and\u2011reflect loop for each meeting.",
            "plain ascii line with  seven   words-ish",
        ]
        for s in samples:
            self.assertEqual(len(s.split()), len(tn.normalize_text(s).split()), s)

    def test_idempotent(self):
        samples = [
            "stand\u2011up \u2014 chat\u2011AI \u2019x\u2018 \u201chold on\u201d\u00a0y\u200b",
            "already-normalized stand-up line",
            "gap\u2014realizing you're drafting",
        ]
        for s in samples:
            once = tn.normalize_text(s)
            self.assertEqual(tn.normalize_text(once), once, s)

    def test_replacement_char_is_never_rewritten(self):
        s = "stand\ufffdup"
        self.assertTrue(tn.contains_replacement_char(s))
        self.assertIn("\ufffd", tn.normalize_text(s),
                      "U+FFFD must never be silently rewritten")
        self.assertTrue(any("U+FFFD" in b for b in tn.has_blockers(s)))

    def test_meaning_changing_punctuation_never_deleted(self):
        # the hyphen in compounds and the dashes in punctuation always leave a
        # visible, supported trace — nothing meaningful vanishes
        self.assertEqual(tn.normalize_text("re\u2011entry"), "re-entry")
        out = tn.normalize_text("word \u2014 word")
        self.assertIn("\u2014", out)
        out2 = tn.normalize_text("word\u2011word")
        self.assertIn("-", out2)


class GlyphCoverage(unittest.TestCase):
    def test_coverage_is_the_actual_font_cmap_not_a_guess(self):
        cov = tn.supported_codepoints()
        self.assertGreater(len(cov), 150)
        # verified from the font files themselves (fontTools): the tofu chars
        for cp in (0x2010, 0x2011, 0x2012, 0x202F, 0x200B, 0x200C, 0x200D,
                   0xFEFF, 0xFFFD, 0x2192, 0x2715):
            self.assertNotIn(cp, cov, f"U+{cp:04X} unexpectedly has a glyph")
        # and these DO exist
        for cp in (0x0027, 0x002D, 0x2013, 0x2014, 0x2018, 0x2019, 0x201C,
                   0x201D, 0x00B7, 0x00A0):
            self.assertIn(cp, cov, f"U+{cp:04X} should be covered")

    def test_coverage_is_intersected_across_all_production_weights(self):
        self.assertEqual(tn.PRODUCTION_FONT_WEIGHTS, (400, 500, 600, 700, 800))
        for w in tn.PRODUCTION_FONT_WEIGHTS:
            self.assertTrue(os.path.exists(tn.font_path(w)),
                            f"production font en-{w}.ttf missing — the gate "
                            "must fail closed, and this test needs the font")
        single = tn.supported_codepoints(weights=(600,))
        multi = tn.supported_codepoints()
        self.assertTrue(multi <= single)

    def test_normalized_issue32_lines_have_full_coverage(self):
        lines = [
            "You\u2019re in a stand\u2011up, eyes on the screen, but your thoughts wander.",
            "especially with chat\u2011AI suggestions popping up.",
            "AI\u2011generated agenda suggestion",
            "Recognizing that gap\u2014realizing you\u2019re drafting a reply instead of hearing",
            "Repeat this pause\u2011and\u2011reflect loop",
        ]
        # lines 1, 2, 3, 5 carry U+2011 — raw, they have NO glyph in the
        # production font; line 4 carries U+2014/U+2019 which Sora covers
        # (normalization still maps the curly apostrophe for parity)
        raw_unsupported = [tn.unsupported_characters(ln) for ln in lines]
        self.assertTrue(raw_unsupported[0] and raw_unsupported[1]
                        and raw_unsupported[2] and raw_unsupported[4])
        for ln in lines:
            norm = tn.normalize_text(ln)
            self.assertEqual(tn.has_blockers(norm), [], f"normalized: {norm!r}")
            self.assertEqual(tn.unsupported_characters(norm), [])

    def test_glyph_gate_blocks_unsupported_after_normalization(self):
        script = {"chunks": [{"id": "c1", "en": [{"t": "arrow \u2192 here"}]}],
                  "meta": {"handle": "@ok"}}
        issues = tn.glyph_gate_issues(script)
        self.assertTrue(any("U+2192" in i for i in issues), issues)

    def test_glyph_gate_passes_clean_script(self):
        script = {"chunks": [{"id": "c1",
                              "en": [{"t": "stand-up chat-AI you're fine."}]}],
                  "meta": {"handle": "@metacognition.hq"},
                  "scene_tags": {"hook": "THE DRIFT"}}
        self.assertEqual(tn.glyph_gate_issues(script), [])


class Parity(unittest.TestCase):
    def _timing_for(self, words, cid="c1"):
        return {"chunks": [{"id": cid, "lines": [
            {"words": [{"w": w, "start": i * 0.4, "end": i * 0.4 + 0.3}
                       for i, w in enumerate(words)]}]}]}

    def test_parity_passes_when_timing_matches_normalized_display(self):
        script = {"chunks": [{"id": "c1", "en": [
            {"t": "stand-up chat-AI"}, {"t": "you're fine"}]}]}
        timing = self._timing_for(["stand-up", "chat-AI", "you're", "fine"])
        self.assertEqual(tn.parity_issues(script, timing), [])

    def test_parity_flags_a_stage_that_normalized_on_its_own(self):
        # display normalized but timing words still carry U+2011 and the
        # tokenizer split differently → token sequences diverge
        script = {"chunks": [{"id": "c1", "en": [{"t": "stand-up only"}]}]}
        timing = self._timing_for(["stand", "up", "only"])
        issues = tn.parity_issues(script, timing)
        self.assertTrue(issues and "c1" in issues[0])

    def test_normalize_script_rebuilds_tts_text_from_display(self):
        script = {"chunks": [{"id": "c1", "tts_text": "stand\u2011up chat\u2011AI",
                              "en": [{"t": "stand\u2011up"}, {"t": "chat\u2011AI"}]}],
                  "meta": {"handle": "@x"}}
        changed = tn.normalize_script(script)
        self.assertTrue(changed)
        self.assertEqual(script["chunks"][0]["en"][0]["t"], "stand-up")
        self.assertEqual(script["chunks"][0]["en"][1]["t"], "chat-AI")
        self.assertEqual(script["chunks"][0]["tts_text"], "stand-up chat-AI",
                         "TTS must derive from the same normalized representation")
        # idempotent
        self.assertEqual(tn.normalize_script(script), [])


if __name__ == "__main__":
    unittest.main()
