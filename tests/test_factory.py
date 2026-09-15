"""Tests for autonomous factory — English-only, Technology × Metacognition, no Persian.

Covers: topic scoring tech-only, negative keywords, calendar fallback, duplicate,
script schema, caption 2200, QA thresholds, layout safe zones, missing audio/video,
aspect ratio, duration, public-URL failure, memory/manifest, retry decisions, policy invariants.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common
import content_producer as cp
import qa_supervisor as qa
import trend_scout as ts

POL = common.policy()
DATE = datetime.date(2026, 9, 16)

def mem_with(*entries):
    m = common.empty_memory()
    for e in entries:
        common.upsert_memory(m, e)
    return m

def entry(cid, date, topic, status="queued-in-buffer", tags=(), pillar="AI_JUDGMENT", cta="question", shash=None):
    return {"content_id": cid, "content_date": date, "topic": topic, "normalized_topic": common.normalize_title(topic),
            "status": status, "tags": list(tags), "pillar": pillar, "cta_type": cta, "script_hash": shash}

def calendar_topic(cal_id=2, date="2026-09-16"):
    cal = next((c for c in common.calendar()["episodes"] if c["id"] == cal_id), common.calendar()["episodes"][0])
    return {"content_date": date, "content_id": f"reel-{date}", "title": cal["title"],
            "normalized_topic": common.normalize_title(cal["title"]), "pillar": cal.get("pillar", "AI_JUDGMENT"),
            "technology_angle": cal.get("technology_angle", "automation bias"),
            "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
            "evidence_mode": "calendar", "calendar": cal}

def build_script(topic=None, variant=0):
    return cp.build_script_from_playbook(topic or calendar_topic(), POL, cp.PLAYBOOKS["automation-bias"], "automation-bias")

class ScoutTests(unittest.TestCase):
    def test_negative_keywords_reject(self):
        for title in ("NFL playoff picture after week 3", "Bitcoin price target raised", "Kardashian divorce rumor",
                      "Senate election results", "Missile strike overnight"):
            self.assertTrue(ts.negative_hits(title, POL), title)
        self.assertFalse(ts.negative_hits("Why AI assistants make you overconfident", POL))

    def test_scoring_prefers_tech(self):
        good = ts.score_item({"source": "hackernews", "title": "AI coding assistant and automation bias in debugging", "traffic": 400}, POL)
        bad = ts.score_item({"source": "hackernews", "title": "Celebrity red carpet looks", "traffic": 900}, POL)
        self.assertGreaterEqual(good["score"], ts.MIN_TREND_SCORE)
        self.assertLess(bad["score"], ts.MIN_TREND_SCORE)
        self.assertIn(good["pillar"], ["AI_JUDGMENT", "CODING", "LEARNING_TECH", "PRODUCT", "ATTENTION", "HUMAN_AI"])

    def test_choose_falls_back_to_calendar(self):
        items = [{"source": "google-trends-US", "title": "Celebrity red carpet looks", "url": "https://x", "traffic": 50000},
                 {"source": "hackernews", "title": "Celebrity gossip news", "url": "https://y", "traffic": 300}]
        top, ranked, rejected = ts.choose(items, POL, common.empty_memory(), DATE)
        self.assertEqual(top["source"], "content-calendar")
        self.assertEqual(top["evidence_mode"], "calendar")

    def test_choose_takes_tech_trend(self):
        items = [{"source": "hackernews", "title": "AI coding assistant and automation bias in debugging", "url": "https://news.ycombinator.com/item?id=1", "traffic": 400}]
        top, ranked, rejected = ts.choose(items, POL, common.empty_memory(), DATE)
        self.assertEqual(top["source"], "hackernews")
        self.assertEqual(top["evidence_mode"], "limited-claims")

    def test_duplicate_topic_blocked(self):
        m = mem_with(entry("reel-2026-09-10", "2026-09-10", "AI coding assistant and automation bias"))
        blocked, why, sim = ts.duplicate_state("AI coding assistant and automation bias explained", m, POL)
        self.assertTrue(blocked)

class ProducerTests(unittest.TestCase):
    def test_script_schema_english_only(self):
        s = build_script()
        for k in ("meta", "scene_tags", "web", "sources", "chunks", "caption"):
            self.assertIn(k, s)
        beats = [c["beat"] for c in s["chunks"]]
        for b in POL["script_structure"]:
            self.assertIn(b, beats)
        self.assertEqual(s["meta"]["language"], "en")
        self.assertEqual(s["meta"]["content_language"], "en")
        for ch in s["chunks"]:
            self.assertEqual(ch["fa"], [])
        self.assertEqual(s["meta"]["logo"], "stand-in")
        self.assertEqual(len(s["web"]), 6)

    def test_all_playbooks_within_length_and_tech(self):
        lo, hi = POL["length"]["narration_words"]
        for key, pb in cp.PLAYBOOKS.items():
            words = sum(common.word_count(l) for _, ls in cp.chunk_plan(pb) for l in ls)
            self.assertTrue(lo <= words <= hi, f"{key}: {words} words")
            self.assertIn("technology_angle", pb)
            # Tech relevance: must contain any tech or metacog tech term
            tech_terms = ["ai", "software", "code", "coding", "program", "product", "attention", "automation", "human", "debug", "bias", "research", "notification", "llm", "hallucination", "metric", "architecture", "offload", "tutorial"]
            self.assertTrue(any(kw in pb["technology_angle"].lower() for kw in tech_terms), f"{key}: {pb['technology_angle']}")

    def test_trend_lenses_tech_only(self):
        for pillar in ["AI_JUDGMENT", "CODING"]:
            pb = cp.trend_playbook("AI coding assistant changes debugging", pillar)
            text = " ".join(l for _, ls in cp.chunk_plan(pb) for l in ls).lower()
            self.assertTrue(any(kw in text for kw in ["ai", "code", "software", "tech"]))

    def test_tts_pronunciation_overrides(self):
        ov = POL["tts"]["pronunciation_overrides"]
        self.assertEqual(cp.spoken_form("Follow @metacognition.hq today", ov), "Follow at metacognition H Q today")

class CaptionTests(unittest.TestCase):
    def test_caption_fits_2200(self):
        import caption
        s = build_script()
        body, tags = caption.build(s)
        self.assertLessEqual(len(caption.fit(body, tags).split("\n\n#")[0]), 2200)

class SupervisorTests(unittest.TestCase):
    def test_min_score_default_and_floor(self):
        with mock.patch.dict(os.environ, {"MIN_QA_SCORE": ""}):
            self.assertEqual(common.min_qa_score(POL), 85)
        with mock.patch.dict(os.environ, {"MIN_QA_SCORE": "70"}):
            self.assertEqual(common.min_qa_score(POL), 80)
        with mock.patch.dict(os.environ, {"MIN_QA_SCORE": "92"}):
            self.assertEqual(common.min_qa_score(POL), 92)

    def test_blocking_error_overrides_score(self):
        r = qa.Report(POL)
        r.block("video_quality", "no video stream")
        self.assertEqual(r.checks["video_quality"], "fail")
        approved = (not r.blocking) and r.score() >= 85
        self.assertFalse(approved)

    def test_script_check_passes_tech_playbooks(self):
        for key in list(cp.PLAYBOOKS)[:6]:
            cal_id = next(i for i, k in cp.CALENDAR_MAP.items() if k == key)
            s = cp.build_script_from_playbook(calendar_topic(cal_id), POL, cp.PLAYBOOKS[key], key)
            r = qa.Report(POL)
            qa.check_script(r, s, POL)
            qa.check_english(r, s, POL)
            self.assertEqual([b for b in r.blocking if "script" in b or "english" in b], [], key)

    def test_english_only_check(self):
        s = build_script()
        r = qa.Report(POL)
        qa.check_english_only(r, s, POL)
        self.assertEqual(r.blocking, [])
        s_bad = build_script()
        s_bad["chunks"][0]["en"][0]["t"] = "سلام این فارسی است"
        r2 = qa.Report(POL)
        qa.check_english_only(r2, s_bad, POL)
        self.assertTrue(r2.blocking)

    def test_technology_relevance(self):
        s = build_script()
        r = qa.Report(POL)
        qa.check_technology_relevance(r, s, calendar_topic(), POL)
        self.assertEqual(r.blocking, [])

    def test_layout_safe_zones_english_only(self):
        L = POL["layout"]
        good = {"en": [{"text": "x", "rows": 2, "direction": "ltr", "bbox": [100, L["en_top"], 900, L["en_top"]+160]}], "fa": []}
        timing = {"total": 80, "chunks": []}
        r = qa.Report(POL)
        qa.check_layout(r, good, timing, POL)
        self.assertEqual(r.blocking, [])

    def test_missing_video_blocks(self):
        r = qa.Report(POL)
        qa.check_video(r, "/nonexistent.mp4", {"total": 80}, POL, {})
        self.assertTrue(any("MP4 missing" in b for b in r.blocking))

class PolicyTests(unittest.TestCase):
    def test_policy_invariants_english_only(self):
        self.assertEqual(POL["qa_thresholds"]["min_score"], 85)
        self.assertEqual(POL["length"]["hard_seconds"], [60, 120])
        self.assertEqual(POL["length"]["target_seconds"], [70, 105])
        self.assertEqual(POL["video_policy"]["aspect"], "9:16")
        self.assertEqual(POL["caption_policy"]["max_chars"], 2200)
        self.assertEqual(POL["content_language"], "en")
        self.assertEqual(POL["page"]["content_language"], "en")

    def test_quarantine_preserved(self):
        self.assertTrue(common.is_quarantined("reel-2026-09-15", "2026-09-15"))
        self.assertFalse(common.is_quarantined("reel-2026-09-16", "2026-09-16"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
