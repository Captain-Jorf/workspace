"""Limited, non-spam hashtag set with the brand tag guaranteed present.

Regression for the missing `#metacognitionhq` warning in run 35043984004:
caption generation now normalizes every hashtag set (groq AND static paths)
against hashtag_policy — brand tags (always) first, banned/spam tags dropped,
case-insensitive de-duplication, cap at hashtag_policy.max (non-spam limit).
The QA supervisor's caption rules are untouched.
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import caption  # noqa: E402
import common  # noqa: E402
import content_producer as cp  # noqa: E402
import qa_supervisor as qa  # noqa: E402

POL = common.policy()
HP = POL["hashtag_policy"]


def calendar_topic(cal_id=2, date="2026-09-16"):
    cal = next((c for c in common.calendar()["episodes"] if c["id"] == cal_id),
               common.calendar()["episodes"][0])
    return {"content_date": date, "content_id": f"reel-{date}", "title": cal["title"],
            "normalized_topic": common.normalize_title(cal["title"]),
            "pillar": cal.get("pillar", "AI_JUDGMENT"),
            "technology_angle": cal.get("technology_angle", "automation bias"),
            "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
            "evidence_mode": "calendar", "calendar": cal}


class NormalizeHashtagTests(unittest.TestCase):
    def test_brand_tags_first_and_guaranteed(self):
        out = common.normalize_hashtags(["#AI", "#coding"], POL)
        self.assertEqual(out[:2], HP["always"])
        self.assertIn("#metacognitionhq", out)
        self.assertEqual(out, ["#metacognition", "#metacognitionhq", "#AI", "#coding"])

    def test_case_insensitive_dedupe(self):
        out = common.normalize_hashtags(["#MetaCognition", "#ai", "#AI", "#MetacognitionHQ"], POL)
        self.assertEqual(sorted(t.lower() for t in out),
                         sorted(t.lower() for t in ["#metacognition", "#metacognitionhq", "#ai"]))
        self.assertEqual(len(out), len({t.lower() for t in out}))

    def test_banned_spam_tags_dropped(self):
        for banned in HP["banned"]:
            out = common.normalize_hashtags([banned, "#AI"], POL)
            self.assertNotIn(banned, out)

    def test_cap_respected_and_brand_survives(self):
        many = [f"#tag{i}" for i in range(12)]
        out = common.normalize_hashtags(many, POL)
        self.assertLessEqual(len(out), HP["max"])
        self.assertEqual(out[:2], HP["always"])

    def test_plain_words_get_hash(self):
        self.assertEqual(common.normalize_hashtags(["AI"], POL)[2], "#AI")


class CaptionBuilderTests(unittest.TestCase):
    def test_playbook_caption_includes_brand_tags_within_limit(self):
        s = cp.build_script_from_playbook(calendar_topic(), POL,
                                          cp.PLAYBOOKS["automation-bias"], "automation-bias")
        _, tag = caption.build(s)
        tags = tag.split()
        for a in HP["always"]:
            self.assertIn(a, tags)
        self.assertLessEqual(len(tags), HP["max"])
        self.assertGreaterEqual(len(tags), HP["min"])
        self.assertFalse(set(t.lower() for t in tags) & set(b.lower() for b in HP["banned"]))

    def test_caption_builder_normalizes_garbage_input(self):
        s = cp.build_script_from_playbook(calendar_topic(), POL,
                                          cp.PLAYBOOKS["automation-bias"], "automation-bias")
        s["caption"]["hashtags"] = ["#fyp", "#viral", "#viral", "#coding"]
        _, tag = caption.build(s)
        tags = tag.split()
        self.assertEqual(tags[:2], HP["always"])
        self.assertNotIn("#fyp", tags)
        self.assertNotIn("#viral", tags)
        self.assertIn("#coding", tags)

    def test_llm_path_default_hashtags_include_brand(self):
        llm_out = {
            "title": "t", "technology_angle": "AI judgment", "metacognition_concept": "calibration",
            "hook": "Why does AI sound sure?", "scenes": [],
            "narration": {"hook": "h", "problem": ["p"], "explain": ["e"],
                          "example": ["x"], "technique": ["try it"], "ending": "save this"},
            "on_screen_text": ["a"], "visual_direction": "gauge",
            "actionable_technique": "check one source", "ending": "save this",
            "claims": [], "sources": [{"label": "s", "url": "", "tier": "B"}],
        }
        s = cp.build_script_from_llm(calendar_topic(), POL, llm_out, generation_mode="groq")
        for a in HP["always"]:
            self.assertIn(a, s["caption"]["hashtags"])
        self.assertLessEqual(len(s["caption"]["hashtags"]), HP["max"])

    def test_llm_path_drops_banned_hashtags(self):
        llm_out = {
            "title": "t", "technology_angle": "AI judgment", "metacognition_concept": "calibration",
            "hook": "Why does AI sound sure?", "scenes": [],
            "narration": {"hook": "h", "problem": ["p"], "explain": ["e"],
                          "example": ["x"], "technique": ["try it"], "ending": "save this"},
            "on_screen_text": ["a"], "visual_direction": "gauge",
            "actionable_technique": "check one source", "ending": "save this",
            "caption": {"hook": "h", "intro": "i", "sections": [],
                        "hashtags": ["#metacognition", "#fyp", "#foryou", "#AI"]},
            "claims": [], "sources": [{"label": "s", "url": "", "tier": "B"}],
        }
        s = cp.build_script_from_llm(calendar_topic(), POL, llm_out, generation_mode="groq")
        self.assertNotIn("#fyp", s["caption"]["hashtags"])
        self.assertNotIn("#foryou", s["caption"]["hashtags"])
        self.assertIn("#metacognitionhq", s["caption"]["hashtags"])


class QACaptionRulesUnchanged(unittest.TestCase):
    """The QA supervisor's caption checks must keep working exactly as before."""

    def _qa(self, tags, body_len=400):
        tmp = tempfile.mkdtemp(prefix="cap_qa_")
        cap = os.path.join(tmp, "caption.txt")
        with open(cap, "w", encoding="utf-8") as f:
            f.write("Hook line here.\n" + "x" * body_len + "\n\n" + " ".join(tags) + "\n")
        s = cp.build_script_from_playbook(calendar_topic(), POL,
                                          cp.PLAYBOOKS["automation-bias"], "automation-bias")
        s["caption"]["hook"] = "Hook line here."
        r = qa.Report(POL)
        qa.check_caption(r, cap, s, POL)
        return r

    def test_spam_hashtags_still_blocked(self):
        r = self._qa(["#metacognition", "#metacognitionhq", "#AI", "#fyp"])
        self.assertTrue(any("spam hashtags" in b for b in r.blocking), r.blocking)

    def test_missing_brand_tag_still_warns(self):
        r = self._qa(["#metacognition", "#AI", "#coding"])
        self.assertTrue(any("missing brand #metacognitionhq" in w for w in r.warnings), r.warnings)

    def test_normalized_caption_has_no_warnings(self):
        tags = common.normalize_hashtags(["#AI", "#coding"], POL)
        r = self._qa(tags)
        self.assertEqual(r.blocking, [])
        self.assertEqual(r.warnings, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
