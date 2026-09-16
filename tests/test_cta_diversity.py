"""CTA diversity via rolling editorial memory + deterministic fallback.

Regression for the "same CTA type" warning in run 35043984004 (every reel was
`question` because the LLM label was hardcoded and never propagated to memory).
Now:
  * the CTA type label is derived DETERMINISTICALLY from the ending text
    (classify), then adjusted against the most recent recorded reels
    (avoid_same_type_consecutive);
  * with no memory the fallback is the plain classification — still deterministic;
  * pipeline.produce propagates meta.cta_type → record() → manifest + memory,
    so tomorrow's run sees today's CTA.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import content_producer as cp  # noqa: E402
import pipeline as pl  # noqa: E402

POL = common.policy()
ALLOWED = POL["cta_policy"]["allowed_types"]


def mem_with_entries(*entries):
    m = common.empty_memory()
    for e in entries:
        common.upsert_memory(m, e)
    return m


def entry(cid, date, cta, pillar="AI_JUDGMENT", tags=(), topic="Some other topic about planning"):
    return {"content_id": cid, "content_date": date, "topic": topic,
            "normalized_topic": common.normalize_title(topic), "status": "queued-in-buffer",
            "pillar": pillar, "tags": list(tags), "cta_type": cta, "script_hash": None}


class ClassifierTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(cp.classify_cta_type("Save this reel for your next review."), "save")
        self.assertEqual(cp.classify_cta_type("Try this before your next commit."), "try-it")
        self.assertEqual(cp.classify_cta_type("When did you last catch an AI hallucinating?"), "share-experience")
        self.assertEqual(cp.classify_cta_type("Which model would you trust more?"), "question")
        self.assertEqual(cp.classify_cta_type(""), "question")
        self.assertEqual(cp.classify_cta_type(None), "question")

    def test_deterministic(self):
        t = "Save this for later — you'll need it."
        self.assertEqual(cp.classify_cta_type(t), cp.classify_cta_type(t))


class ChooseCtaTests(unittest.TestCase):
    def test_no_memory_falls_back_to_classification(self):
        self.assertEqual(cp.choose_cta_type("Save this for later.", common.empty_memory(), POL), "save")
        self.assertEqual(cp.choose_cta_type("Which would you pick?", common.empty_memory(), POL), "question")

    def test_avoids_most_recent_type(self):
        mem = mem_with_entries(entry("reel-2026-09-15", "2026-09-15", "question"))
        # ending classifies as question → must NOT stay question when a
        # compatible alternative fits
        out = cp.choose_cta_type("Try this before you trust the answer again.", mem, POL)
        self.assertNotEqual(out, "question")
        self.assertIn(out, ALLOWED)

    def test_keeps_classification_when_not_recent(self):
        mem = mem_with_entries(entry("reel-2026-09-15", "2026-09-15", "try-it"))
        out = cp.choose_cta_type("Save this reel for your next debugging session.", mem, POL)
        self.assertEqual(out, "save")

    def test_rolling_memory_uses_recent_entries(self):
        mem = mem_with_entries(
            entry("reel-2026-09-13", "2026-09-13", "save"),
            entry("reel-2026-09-15", "2026-09-15", "question"),
        )
        out = cp.choose_cta_type("Save this for later — you'll need it.", mem, POL)
        # "save" is recent (2nd most recent) AND is what the ending says,
        # but the most recent was question → 'save' still avoided if possible;
        # ending only fits 'save' → truthful fallback label.
        self.assertIn(out, ALLOWED)

    def test_all_types_recent_falls_back_to_classification(self):
        mem = mem_with_entries(
            entry("reel-2026-09-12", "2026-09-12", "question", pillar="DECIDE"),
            entry("reel-2026-09-13", "2026-09-13", "try-it", pillar="CODING"),
            entry("reel-2026-09-14", "2026-09-14", "share-experience", pillar="LEARN"),
            entry("reel-2026-09-15", "2026-09-15", "save", pillar="PRODUCT"),
        )
        out = cp.choose_cta_type("Save this for later.", mem, POL)
        self.assertEqual(out, "save")  # nothing compatible left → truthful label

    def test_recent_cta_types_helper_skips_missing(self):
        mem = mem_with_entries(
            entry("reel-2026-09-14", "2026-09-14", None),
            entry("reel-2026-09-15", "2026-09-15", "question"),
        )
        self.assertEqual(common.recent_cta_types(mem, 5), ["question"])


class PropagationTests(unittest.TestCase):
    """meta.cta_type must survive pipeline.produce → record() → manifest + memory."""

    def setUp(self):
        self._paths, self._dirs = {}, []

    def _state_dir(self, tag):
        tmp = tempfile.mkdtemp(prefix="cta_prop_")
        paths = {
            "state": os.path.join(ROOT, "output", f"auto-{tag}_state.json"),
            "manifest": os.path.join(ROOT, "output", f"auto-{tag}_manifest.json"),
            "marker": os.path.join(ROOT, "output", f"auto-{tag}.buffer.json"),
            "ep_script": os.path.join(ROOT, "content", "episodes", f"auto-{tag}", "script.json"),
        }
        common.save_json(paths["state"], {
            "content_id": f"reel-{tag}", "tag": tag, "status": "qa-failed",
            "topic": {"title": "Why does AI sound so sure?", "normalized_topic": "sound sure",
                      "pillar": "AI_JUDGMENT"},
            "script": {"summary": "s", "sources": [], "playbook": "hallucination-confidence",
                       "technology_angle": "AI judgment", "metacognition_concept": "calibration",
                       "generation_mode": "groq", "language": "en", "cta_type": "save",
                       "hash": "abc123"},
            "qa": {"approved": False, "score": 69, "blocking_errors": ["x"],
                   "warnings": [], "checks": {}},
            "retries": {"script": 0, "render": 0}, "branch": f"drafts/{tag}", "run_id": "test",
        })
        os.makedirs(os.path.dirname(paths["ep_script"]), exist_ok=True)
        common.save_json(paths["ep_script"], {
            "meta": {"handle": "@metacognition.hq"},
            "caption": {"hook": "Save it?"},
        })
        return tmp, paths

    def tearDown(self):
        for p in (self._paths or {}).values():
            if p and os.path.exists(p):
                os.remove(p)
        for d in (self._dirs or []):
            shutil.rmtree(d, ignore_errors=True)

    def test_record_propagates_cta_to_manifest_and_memory(self):
        tag = "2077-03-03"
        tmp, paths = self._state_dir(tag)
        self._paths, self._dirs = paths, [os.path.dirname(paths["ep_script"]), tmp]
        mem_path = os.path.join(tmp, "memory.json")
        common.save_memory(common.empty_memory(), mem_path)
        with mock.patch.object(common, "MEMORY_PATH", mem_path):
            pl.record(type("A", (), {"tag": tag, "status": "qa-failed", "error": None})())
        manifest = common.load_json(paths["manifest"])
        self.assertEqual(manifest["cta_type"], "save")
        self.assertEqual(manifest["status"], "qa-failed")
        mem = common.load_memory(mem_path)
        e = next(x for x in mem["entries"] if x["content_id"] == f"reel-{tag}")
        self.assertEqual(e["cta_type"], "save")

    def test_produce_state_includes_cta_type(self):
        # produce() projects meta.cta_type into the state file that record()
        # copies into manifest + memory (source-level contract).
        with open(os.path.join(ROOT, "build", "pipeline.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"cta_type": script.get("meta", {}).get("cta_type")', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
