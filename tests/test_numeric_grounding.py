"""Numeric-claim grounding (fix for run 35043984004 / reel-2026-09-16).

Calendar/evergreen generation must not introduce percentages, statistics, study
results or precise numeric claims unless the sanitized evidence packet
explicitly supports them. The producer now:
  * hard-rules the producer/reviewer/revision prompts (Reviewer treats an
    unsupported numeric claim as a blocker),
  * runs a DETERMINISTIC pre-gate mirroring the QA source_quality rule,
  * falls back to the static playbooks (which obey the same rule) when the
    numbers survive the single revision.
QA thresholds are untouched: the QA supervisor still blocks the same claims.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import content_producer as cp  # noqa: E402
import llm_provider  # noqa: E402
import qa_supervisor as qa  # noqa: E402

POL = common.policy()
FAKE_KEY = "FAKE_GROQ_KEY_FOR_TESTS_ONLY_" + "X" * 48
PROD_CANDIDATE = "openai/gpt-oss-20b"

# The narration from the real failing run used 100% / 92% / 80% for a calendar
# topic whose evidence packet contains NO statistics at all.
ARTIFACT_NUMBERS = "100%", "92%", "80%"


def clean_env(**overrides):
    e = {k: v for k, v in os.environ.items()
         if k not in ("GROQ_API_KEY", "MOCK_GROQ", "GROQ_BASE_URL",
                      "PRODUCER_MODEL", "REVIEWER_MODEL", "GITHUB_ACTIONS")}
    e["CONTENT_LANGUAGE"] = "en"
    e.update(overrides)
    return e


def calendar_topic(cal_id=10):
    cal = next((c for c in common.calendar()["episodes"] if c["id"] == cal_id),
               common.calendar()["episodes"][0])
    return {"content_date": "2026-09-16", "content_id": "reel-2026-09-16",
            "title": cal["title"], "normalized_topic": common.normalize_title(cal["title"]),
            "pillar": cal.get("pillar", "AI_JUDGMENT"),
            "technology_angle": cal.get("technology_angle", "confidence calibration"),
            "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
            "evidence_mode": "calendar", "calendar": cal}


def producer_json(numbers_line=None, ending="Save this reel for your next debugging session."):
    explain = ["The model is trained to be fluent, not calibrated.",
               "Your brain wants to trust the fluency, because checking feels slower."]
    if numbers_line:
        explain.append(numbers_line)
    return {
        "title": "Why does AI sound so sure?",
        "technology_angle": "confidence calibration in language models",
        "metacognition_concept": "calibration bias (over-confidence)",
        "hook": "Why does AI sound so sure, even when it's wrong?",
        "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
        "narration": {"hook": "Why does AI sound so sure, even when it's wrong?",
                      "problem": explain[:1], "explain": explain[1:],
                      "example": ["Ask the model for a library function that does not exist, and it will invent one."],
                      "technique": ["Open the docs, run the code, and check one source."],
                      "ending": ending},
        "on_screen_text": ["A", "B", "C", "D", "E", "F"],
        "visual_direction": "confidence meter",
        "actionable_technique": "verify before accept",
        "ending": ending,
        "caption": {"hook": "Why does AI sound so sure?", "intro": "x", "sections": [],
                    "hashtags": ["#AI", "#coding"]},
        "claims": [],
        "sources": [{"label": "Xiong et al., 2024 - Can LLMs express their uncertainty?", "url": "", "tier": "A"}],
    }


APPROVED_REVIEW = {"approved": True, "score": 95, "technology_relevance": True,
                   "metacognition_relevance": True, "source_grounding": True,
                   "unsupported_claims": [], "hook_quality": "good",
                   "spoken_english_quality": "good", "novety": "high",
                   "practical_value": "high", "safety": "safe",
                   "required_changes": [], "blocking_errors": []}


class MatcherTests(unittest.TestCase):
    def test_numeric_matcher(self):
        self.assertEqual(common.find_numeric_claims("it is 100% confident"), ["100%"])
        self.assertEqual(common.find_numeric_claims("92 % of the time and 80 percent"), ["92 %", "80 percent"])
        self.assertEqual(common.find_numeric_claims("no numbers here"), [])
        self.assertEqual(common.normalize_numeric_claim("92 %"), "92%")
        self.assertEqual(common.normalize_numeric_claim("80 percent"), "80%")

    def test_artifact_numbers_all_unsupported_for_calendar_packet(self):
        # The exact statistics from the failing artifact, in order of appearance.
        packet = llm_provider.build_evidence_packet(calendar_topic(), POL)
        text = "Models are 100% fluent on easy cases, 92% on the benchmark, and 80% on hard questions."
        bad = common.unsupported_numeric_claims(text, packet)
        self.assertEqual(bad, ["100%", "92%", "80%"])

    def test_every_calendar_packet_carries_no_numeric_evidence(self):
        for ep in common.calendar()["episodes"]:
            topic = calendar_topic(ep["id"])
            packet = llm_provider.build_evidence_packet(topic, POL)
            self.assertEqual(packet["numeric_evidence"], [], f"episode {ep['id']} carries numbers")
            self.assertIn("no statistics", packet["numeric_evidence_note"])

    def test_supported_numbers_are_allowed(self):
        packet = {"trusted_excerpt": "the benchmark reported 92% accuracy",
                  "evidence_source": {"label": "X"}, "discovery_source": {"name": "Y"}}
        self.assertEqual(common.unsupported_numeric_claims("It reached 92% on the task.", packet), [])
        self.assertEqual(common.unsupported_numeric_claims("It reached 93% on the task.", packet), ["93%"])


class PromptRuleTests(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(ROOT, "build", "llm_provider.py"), encoding="utf-8").read()

    def test_producer_hard_rule(self):
        self.assertIn("Numeric grounding", self.src)
        self.assertIn("Do NOT invent or guess URLs", self.src)
        self.assertIn("leave source \"url\" empty unless the packet provides it", self.src)

    def test_reviewer_numeric_blocker_rule(self):
        self.assertIn("NUMERIC CLAIMS (blocker)", self.src)
        self.assertIn("set source_grounding=false and approved=false", self.src)
        self.assertIn("Do NOT approve a number by inventing or adjusting a citation", self.src)

    def test_revision_never_invents_citations(self):
        self.assertIn("Numeric fix rule", self.src)
        self.assertIn("NEVER keep a number by adding, adjusting or inventing a citation/URL", self.src)

    def test_conversational_rules(self):
        self.assertIn("natural contractions", self.src)
        for connector in ("furthermore", "moreover", "thus", "hence", "utilize", "in conclusion"):
            self.assertIn(connector, self.src)
        self.assertIn("max 20 words", self.src.replace("max_words_per_line", "20"))


class GuardEndToEndTests(unittest.TestCase):
    """cp.main() with mocked Groq transport: the deterministic gate must force a
    revision and, if the numbers survive, take the static fallback path."""

    def _topic_file(self, tmp):
        path = os.path.join(tmp, "topic.json")
        common.save_json(path, calendar_topic())
        return path

    def _run_main(self, topic_path, out_dir, produces, reviews):
        with mock.patch.object(sys, "argv", ["content_producer.py", "--topic", topic_path,
                                             "--out", out_dir, "--variant", "0"]):
            with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
                with mock.patch.object(llm_provider, "discover_models",
                                       return_value=([PROD_CANDIDATE], {"http_status": 200})), \
                     mock.patch.object(llm_provider.GroqProducer, "produce", side_effect=produces), \
                     mock.patch.object(llm_provider.GroqReviewer, "review", side_effect=reviews):
                    cp.main()
        return common.load_json(os.path.join(out_dir, "script.json"))

    @staticmethod
    def _narration_text(script):
        return " ".join(l["t"] for ch in script["chunks"] for l in ch["en"])

    def test_numbers_force_revision_then_groq_script_without_numbers(self):
        tmp = tempfile.mkdtemp(prefix="numgate_rev_")
        bad = producer_json(numbers_line="Benchmarks show 92% accuracy, and 100% of the demos look fine.")
        clean = producer_json()
        try:
            script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"),
                                    produces=[(bad, {}), (clean, {})],
                                    reviews=[(dict(APPROVED_REVIEW), {}), (dict(APPROVED_REVIEW), {})])
        finally:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(script["meta"]["generation_mode"], "groq")
        text = self._narration_text(script).lower()
        self.assertEqual(common.find_numeric_claims(text), [], f"numbers leaked into narration: {text}")
        self.assertNotIn("92", text)
        self.assertNotIn("100", text)

    def test_numbers_surviving_revision_take_static_fallback(self):
        tmp = tempfile.mkdtemp(prefix="numgate_fb_")
        bad = producer_json(numbers_line="Benchmarks show 92% accuracy, and 100% of the demos look fine.")
        try:
            script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"),
                                    produces=[(bad, {}), (bad, {})],
                                    reviews=[(dict(APPROVED_REVIEW), {}), (dict(APPROVED_REVIEW), {})])
        finally:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
        text = self._narration_text(script).lower()
        self.assertEqual(common.find_numeric_claims(text), [])

    def test_cta_type_recorded_from_rolling_memory(self):
        tmp = tempfile.mkdtemp(prefix="numgate_cta_")
        clean = producer_json()  # ending contains "Save this reel ..."
        try:
            script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"),
                                    produces=[(clean, {})],
                                    reviews=[(dict(APPROVED_REVIEW), {})])
        finally:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn(script["meta"]["cta_type"], POL["cta_policy"]["allowed_types"])
        # ending says "Save this ..." and the most recent recorded reel used a
        # different type → the classifier's label is kept.
        self.assertEqual(script["meta"]["cta_type"], "save")


class QAUnchangedTests(unittest.TestCase):
    def test_qa_still_blocks_the_artifact_statistics(self):
        script = cp.build_script_from_playbook(calendar_topic(), POL,
                                               cp.PLAYBOOKS["automation-bias"], "automation-bias")
        script["chunks"][2]["en"][0]["t"] = ("Benchmarks show 92% accuracy, and 100% of the demos "
                                             "look fine, with 80% on hard cases.")
        r = qa.Report(POL)
        qa.check_sources(r, script, calendar_topic(), POL, skip_network=True)
        self.assertTrue(any("statistics" in b and "92%" in b for b in r.blocking), r.blocking)
        self.assertTrue(any("cannot be verified automatically" in b for b in r.blocking))
        self.assertNotIn("stats_verified", script["meta"])  # no allowlist escape hatch

    def test_guard_and_qa_use_the_same_matcher(self):
        text = "Models are 100% fluent, 92 % on the benchmark, and 80 percent on hard questions."
        self.assertEqual(common.find_numeric_claims(text), qa.common.find_numeric_claims(text))

    def test_playbooks_contain_no_percentages(self):
        for key, pb in cp.PLAYBOOKS.items():
            blob = json.dumps(pb, ensure_ascii=False)
            self.assertEqual(common.find_numeric_claims(blob), [], f"playbook {key} contains statistics")
            for _, lines in cp.chunk_plan(pb):
                for line in lines:
                    self.assertEqual(common.find_numeric_claims(line), [], f"playbook {key}: {line!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
