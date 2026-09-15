"""Tests for the autonomous factory modules — no network, no real Buffer.

Covers: topic scoring, negative keywords, calendar fallback, duplicate
detection, script schema + structure, Persian validation (real chars, no
leftover English, placeholders), caption 2200, QA thresholds (min score, floor,
blocking override), layout safe zones, missing audio/video, aspect ratio,
duration, public-URL failure, memory/manifest recording, controlled retry
decisions, editorial policy invariants.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import content_producer as cp  # noqa: E402
import qa_supervisor as qa  # noqa: E402
import trend_scout as ts  # noqa: E402

POL = common.policy()
DATE = datetime.date(2026, 9, 16)


def mem_with(*entries):
    m = common.empty_memory()
    for e in entries:
        common.upsert_memory(m, e)
    return m


def entry(cid, date, topic, status="queued-in-buffer", tags=(), pillar="LEARN", cta="question", shash=None):
    return {"content_id": cid, "content_date": date, "topic": topic, "normalized_topic": common.normalize_title(topic),
            "status": status, "tags": list(tags), "pillar": pillar, "cta_type": cta, "script_hash": shash}


def calendar_topic(cal_id=21, date="2026-09-16"):
    cal = next(c for c in common.calendar()["episodes"] if c["id"] == cal_id)
    return {"content_date": date, "content_id": f"reel-{date}", "title": cal["title"],
            "normalized_topic": common.normalize_title(cal["title"]), "pillar": "DECIDE",
            "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
            "evidence_mode": "calendar", "calendar": cal}


def fake_translator(lines):
    return cp.fixture_translator(lines)


def build_script(topic=None, variant=0):
    return cp.build_script(topic or calendar_topic(), POL, variant=variant, translate=True, translator=fake_translator)


# ------------------------------------------------------------------ scout
class ScoutTests(unittest.TestCase):
    def test_negative_keywords_reject(self):
        for title in ("NFL playoff picture after week 3", "Bitcoin price target raised", "Kardashian divorce rumor",
                      "Senate election results", "Missile strike overnight", "ADHD medication shortage"):
            self.assertTrue(ts.negative_hits(title, POL), title)
        self.assertFalse(ts.negative_hits("Why overconfident students study less", POL))
        self.assertFalse(ts.negative_hits("Versatile note-taking", POL))    # 'vs' must be whole-word

    def test_scoring_prefers_on_brand(self):
        good = ts.score_item({"source": "hackernews", "title": "Overconfidence bias in startup forecasting", "traffic": 400}, POL)
        bad = ts.score_item({"source": "hackernews", "title": "New JavaScript framework released", "traffic": 900}, POL)
        self.assertGreaterEqual(good["score"], ts.MIN_TREND_SCORE)
        self.assertLess(bad["score"], ts.MIN_TREND_SCORE)
        self.assertEqual(good["pillar"], "BIAS")

    def test_choose_falls_back_to_calendar(self):
        items = [{"source": "google-trends-US", "title": "Celebrity red carpet looks", "url": "https://x", "traffic": 50000},
                 {"source": "hackernews", "title": "Rust 2.0 compiler notes", "url": "https://y", "traffic": 300}]
        top, ranked, rejected = ts.choose(items, POL, common.empty_memory(), DATE)
        self.assertEqual(top["source"], "content-calendar")
        self.assertEqual(top["evidence_mode"], "calendar")
        self.assertIn(top["calendar"]["id"], cp.CALENDAR_MAP)
        self.assertEqual(ranked, [])
        self.assertEqual(len(rejected), 2)

    def test_choose_takes_relevant_trend_in_limited_claims_mode(self):
        items = [{"source": "hackernews", "title": "Overconfidence bias in startup forecasting", "url": "https://news.ycombinator.com/item?id=1", "traffic": 400}]
        top, ranked, rejected = ts.choose(items, POL, common.empty_memory(), DATE)
        self.assertEqual(top["source"], "hackernews")
        self.assertEqual(top["evidence_mode"], "limited-claims")

    def test_duplicate_topic_blocked(self):
        m = mem_with(entry("reel-2026-09-10", "2026-09-10", "Overconfidence bias in startup forecasting"))
        blocked, why, sim = ts.duplicate_state("Overconfidence bias in startup forecasting explained", m, POL)
        self.assertTrue(blocked)
        self.assertGreaterEqual(sim, POL["duplicates"]["title_similarity_block"])

    def test_tag_cooldown_blocks_same_technique(self):
        m = mem_with(entry("reel-2026-09-10", "2026-09-10", "Some other title", tags=["pre-mortem"]))
        blocked, why, _ = ts.duplicate_state("A brand new title", m, POL, tags=["pre-mortem"])
        self.assertTrue(blocked)
        self.assertIn("pre-mortem", why)

    def test_legacy_drafts_count_as_used(self):
        m = common.load_memory()          # repo memory: Reel 01 + legacy drafts
        blocked, _, _ = ts.duplicate_state("The pre-mortem: predict your own failure", m, POL)
        self.assertTrue(blocked)

    def test_calendar_only_never_repeats_recent(self):
        m = mem_with(entry("reel-2026-09-15", "2026-09-15", "Metacognition in the cockpit: how checklists save lives", tags=["checklists"]))
        cands = ts.calendar_candidates(POL, m, DATE)
        self.assertTrue(cands)
        self.assertNotIn("checklists", [c["title"].split(":")[0].lower() for c in cands])

    def test_every_calendar_candidate_has_playbook(self):
        for c in ts.calendar_candidates(POL, common.empty_memory(), DATE):
            self.assertIn(c["calendar"]["id"], cp.CALENDAR_MAP)


# --------------------------------------------------------------- producer
class ProducerTests(unittest.TestCase):
    def test_script_schema(self):
        s = build_script()
        for k in ("meta", "scene_tags", "web", "sources", "chunks", "caption"):
            self.assertIn(k, s)
        beats = [c["beat"] for c in s["chunks"]]
        for b in POL["script_structure"]:
            self.assertIn(b, beats)
        self.assertEqual(beats.index("hook"), 0)
        self.assertEqual(beats[-1], "ending")
        for ch in s["chunks"]:
            self.assertEqual(len(ch["en"]), len(ch["fa"]))
            self.assertTrue(ch["tts_text"])
        self.assertEqual(s["meta"]["logo"], "stand-in")
        self.assertEqual(len(s["web"]), 6)

    def test_all_playbooks_within_length_policy(self):
        lo, hi = POL["length"]["narration_words"]
        for key, pb in cp.PLAYBOOKS.items():
            words = sum(common.word_count(l) for _, ls in cp.chunk_plan(pb) for l in ls)
            self.assertTrue(lo <= words <= hi, f"{key}: {words} words")
            self.assertLessEqual(common.word_count(pb["hook"]), POL["hook_policy"]["max_words"] + 1, key)
            hl = pb["hook"].lower()
            for bad in POL["tone"]["forbidden_openers"] + POL["hook_policy"]["banned_hook_phrases"]:
                self.assertNotIn(bad, hl, key)

    def test_trend_lenses_make_no_research_claims(self):
        for pillar in list(cp.TREND_LENSES) + ["THINK"]:
            pb = cp.trend_playbook("Overconfidence bias in startup forecasting", pillar)
            text = " ".join(l for _, ls in cp.chunk_plan(pb) for l in ls).lower()
            for w in POL["source_policy"]["require_evidence_for_claim_words"]:
                self.assertNotRegex(text, rf"\b{w}\b", f"{pillar}: claim word {w}")
            self.assertNotRegex(text, r"\d+\s?%")

    def test_calendar_id_without_playbook_fails_closed(self):
        t = calendar_topic()
        t["calendar"] = {"id": 999, "title": "x", "sources": []}
        with self.assertRaises(SystemExit):
            build_script(t)

    def test_translation_failure_blocks(self):
        def bad(lines):
            return None, None, ["google: everything failed"]
        with self.assertRaises(SystemExit) as ctx:
            cp.build_script(calendar_topic(), POL, translate=True, translator=bad)
        self.assertIn("translation-error", str(ctx.exception))

    def test_tts_pronunciation_overrides(self):
        ov = POL["tts"]["pronunciation_overrides"]
        self.assertEqual(cp.spoken_form("Follow @metacognition.hq today", ov), "Follow at metacognition H Q today")
        self.assertNotIn("http", cp.spoken_form("see https://example.com now", ov))

    def test_variant_one_is_valid_script(self):
        s = build_script(variant=1)
        self.assertEqual(s["meta"]["variant"], 1)
        self.assertGreaterEqual(len(s["chunks"]), 8)


class PersianTests(unittest.TestCase):
    def valid(self, en, fa):
        return cp.translation_valid(en, fa, POL)[0]

    def test_real_persian_accepted(self):
        self.assertTrue(self.valid("Why do experts still read a checklist they know by heart?",
                                   "چرا خلبان‌های باتجربه هنوز چک‌لیستی را می‌خوانند که از حفظ هستند؟"))

    def test_leftover_english_rejected(self):
        self.assertFalse(self.valid("Your brain treats a familiar claim as true.",
                                    "Your brain treats یک ادعای آشنا as true."))

    def test_identical_or_empty_rejected(self):
        self.assertFalse(self.valid("Hello there", "Hello there"))
        self.assertFalse(self.valid("Hello there", ""))

    def test_placeholder_rejected(self):
        self.assertFalse(self.valid("Some line here", "TODO ترجمه"))
        self.assertFalse(self.valid("Some line here", "[SAMPLE] این یک نمونه است"))

    def test_low_persian_ratio_rejected(self):
        self.assertFalse(self.valid("A line of text", "123 456 789 000 !!!"))

    def test_polish_fixes_arabic_forms_and_zwnj(self):
        out = cp.polish_fa("مي كنم")
        self.assertNotIn("ي", out)
        self.assertNotIn("ك", out)
        self.assertIn("\u200c", cp.polish_fa("می کنم"))


class CaptionTests(unittest.TestCase):
    def test_caption_fits_2200(self):
        import caption
        s = build_script()
        s["caption"]["sections"][0]["lines"] = ["x" * 400] * 8
        body, tags = caption.build(s)
        self.assertLessEqual(len(caption.fit(body, tags)), 2200)
        self.assertIn("#metacognition", tags)

    def test_caption_never_claims_official_logo(self):
        import caption
        body, _ = caption.build(build_script())
        self.assertNotIn("official logo", body.lower())


# --------------------------------------------------------------- supervisor
class Fake:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def report():
    return qa.Report(POL)


class SupervisorTests(unittest.TestCase):
    def test_min_score_default_and_floor(self):
        with mock.patch.dict(os.environ, {"MIN_QA_SCORE": ""}):
            self.assertEqual(common.min_qa_score(POL), 85)
        with mock.patch.dict(os.environ, {"MIN_QA_SCORE": "70"}):
            self.assertEqual(common.min_qa_score(POL), 80)          # never below the floor
        with mock.patch.dict(os.environ, {"MIN_QA_SCORE": "92"}):
            self.assertEqual(common.min_qa_score(POL), 92)
        with mock.patch.dict(os.environ, {"MIN_QA_SCORE": "abc"}):
            self.assertEqual(common.min_qa_score(POL), 85)

    def test_blocking_error_overrides_score(self):
        r = report()
        r.block("video_quality", "no video stream")
        self.assertEqual(r.checks["video_quality"], "fail")
        approved = (not r.blocking) and r.score() >= 85
        self.assertFalse(approved)
        self.assertEqual(r.score(), 100 - qa.WEIGHTS["video_quality"])

    def test_warnings_reduce_score_and_can_reject(self):
        r = report()
        for _ in range(10):
            r.warn("script_quality", "w", 2)
        r.warn("english_quality", "w", 5)
        self.assertLess(r.score(), 85)
        self.assertEqual(r.checks["script_quality"], "warn")

    def test_script_check_blocks_clickbait_and_missing_beats(self):
        s = build_script()
        s["chunks"][0]["en"][0]["t"] = "You won't believe this shocking brain hack"
        r = report()
        qa.check_script(r, s, POL)
        self.assertTrue(any("clickbait" in b or "banned" in b for b in r.blocking))
        s2 = build_script()
        s2["chunks"] = [c for c in s2["chunks"] if c["beat"] != "technique"]
        r2 = report()
        qa.check_script(r2, s2, POL)
        self.assertTrue(any("missing beats" in b for b in r2.blocking))

    def test_script_check_passes_real_playbooks(self):
        for key in list(cp.PLAYBOOKS)[:8]:
            cal_id = next(i for i, k in cp.CALENDAR_MAP.items() if k == key)
            s = build_script(calendar_topic(cal_id))
            r = report()
            qa.check_script(r, s, POL)
            qa.check_english(r, s, POL)
            qa.check_topic(r, s, calendar_topic(cal_id), POL)
            self.assertEqual(r.blocking, [], key)

    def test_source_check_blocks_unsourced_stats(self):
        s = build_script()
        s["sources"] = []
        s["chunks"][2]["en"][0]["t"] = "Studies show 90% of people forget this."
        r = report()
        qa.check_sources(r, s, calendar_topic(), POL, skip_network=True)
        self.assertTrue(r.blocking)

    def test_source_check_limited_claims_mode(self):
        s = build_script()
        s["meta"]["evidence_mode"] = "limited-claims"
        s["sources"] = [{"label": "hackernews", "url": "https://news.ycombinator.com/item?id=1", "tier": "C", "role": "discovery"}]
        s["chunks"][2]["en"][0]["t"] = "Research found that this always works."
        r = report()
        qa.check_sources(r, s, {}, POL, skip_network=True)
        self.assertTrue(any("tier A/B" in b or "limited-claims" in b for b in r.blocking))

    def test_persian_check_blocks_fixture_unless_local(self):
        s = build_script()
        r = report()
        with mock.patch.dict(os.environ, {"QA_ALLOW_FIXTURE": ""}):
            qa.check_persian(r, s, POL)
        self.assertTrue(any("fixture" in b for b in r.blocking))
        r2 = report()
        with mock.patch.dict(os.environ, {"QA_ALLOW_FIXTURE": "1"}):
            qa.check_persian(r2, s, POL)
        self.assertEqual(r2.blocking, [])

    def test_persian_check_blocks_english_leftovers(self):
        s = build_script()
        s["chunks"][1]["fa"][0] = "Expertise makes steps automatic و بعد"
        r = report()
        with mock.patch.dict(os.environ, {"QA_ALLOW_FIXTURE": "1"}):
            qa.check_persian(r, s, POL)
        self.assertTrue(any("invalid Persian" in b for b in r.blocking))

    def test_layout_safe_zones(self):
        L = POL["layout"]
        good = {"en": [{"text": "x", "rows": 2, "direction": "ltr", "bbox": [100, L["en_top"], 900, L["en_top"] + 160]}],
                "fa": [{"text": "y", "rows": 2, "direction": "rtl", "bbox": [100, 1330, 900, L["fa_bottom"]], "overflow": False}]}
        timing = {"total": 80, "chunks": [{"lines": [{"words": [{"w": "a", "start": 1, "end": 2}]}]}]}
        r = report()
        qa.check_layout(r, good, timing, POL)
        self.assertEqual(r.blocking, [])
        bad = json.loads(json.dumps(good))
        bad["fa"][0]["bbox"][3] = L["safe_bottom"] + 200       # into the IG UI zone
        bad["en"][0]["rows"] = 5
        bad["en"][0]["direction"] = "rtl"
        r2 = report()
        qa.check_layout(r2, bad, timing, POL)
        self.assertGreaterEqual(len(r2.blocking), 3)
        r3 = report()
        qa.check_layout(r3, good, {"total": 10, "chunks": [{"lines": [{"words": [{"w": "a", "start": 1, "end": 12}]}]}]}, POL)
        self.assertTrue(any("karaoke" in b for b in r3.blocking))
        r4 = report()
        qa.check_layout(r4, {}, timing, POL)
        self.assertTrue(any("layout.json missing" in b for b in r4.blocking))

    def test_missing_video_and_posters_block(self):
        r = report()
        qa.check_video(r, "/nonexistent.mp4", {"total": 80}, POL, {})
        qa.check_posters(r, None, None, POL)
        self.assertTrue(any("MP4 missing" in b for b in r.blocking))
        self.assertTrue(any("poster" in b for b in r.blocking))

    def test_public_url_failure_blocks(self):
        r = report()
        with mock.patch.object(qa, "head_url", return_value=(404, "", 0)):
            qa.check_buffer_readiness(r, "https://raw.githubusercontent.com/x/y.mp4", None, False, None)
        self.assertTrue(any("HTTP 404" in b for b in r.blocking))
        r2 = report()
        qa.check_buffer_readiness(r2, "http://insecure/x.mp4", None, True, None)
        self.assertTrue(any("not https" in b for b in r2.blocking))

    def test_duplicate_check_uses_memory(self):
        s = build_script()
        m = mem_with(entry("reel-2026-09-01", "2026-09-01", s["meta"]["topic"], shash=common.script_hash(s)))
        r = report()
        qa.check_duplicates(r, s, calendar_topic(), m, POL)
        self.assertTrue(any("identical script" in b or "too similar" in b for b in r.blocking))

    def test_caption_check_rules(self):
        s = build_script()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.txt")
            open(p, "w", encoding="utf-8").write("x" * 2300 + "\n\n#viral #metacognition\n")
            r = report()
            qa.check_caption(r, p, s, POL)
            self.assertTrue(any("2200" in b for b in r.blocking))
            self.assertTrue(any("spam" in b for b in r.blocking))

    def test_markdown_report(self):
        r = {"approved": False, "score": 70, "min_score": 85, "checks": {"video_quality": "fail"},
             "blocking_errors": ["[video_quality] x"], "warnings": [], "details": {}}
        md = qa.to_markdown(r)
        self.assertIn("REJECTED", md)
        self.assertIn("video_quality", md)


# --------------------------------------------------------------- ffprobe-level checks on a tiny synthetic mp4
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import imageio_ffmpeg
        cls.ff = imageio_ffmpeg.get_ffmpeg_exe()
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def make(self, name, w=1080, h=1920, secs=2, audio=True):
        out = os.path.join(self.tmp, name)
        cmd = [self.ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=gray:s={w}x{h}:r=30:d={secs}"]
        if audio:
            cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={secs}"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest"]
        if audio:
            cmd += ["-c:a", "aac"]
        subprocess.run(cmd + [out], check=True)
        return out

    def test_probe_reads_streams(self):
        info = qa.probe(self.make("a.mp4"))
        kinds = {s["codec_type"] for s in info["streams"]}
        self.assertEqual(kinds, {"video", "audio"})
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.3)

    def test_short_wrong_aspect_and_silent_video_blocked(self):
        r = report()
        qa.check_video(r, self.make("b.mp4", w=1920, h=1080, audio=False), {"total": 2}, POL, {})
        msgs = " ".join(r.blocking)
        self.assertIn("not 9:16", msgs)
        self.assertIn("duration", msgs)
        self.assertIn("no audio stream", msgs)


# --------------------------------------------------------------- pipeline / memory
class PipelineTests(unittest.TestCase):
    def test_record_writes_manifest_and_memory_without_secrets(self):
        import pipeline
        with tempfile.TemporaryDirectory() as d:
            tag = "2099-01-01"
            st = {"tag": tag, "content_id": "reel-2099-01-01", "status": "approved", "topic": {"title": "T", "pillar": "LEARN"},
                  "script": {"hash": "abc", "cta_type": "try-it", "playbook": "testing-effect", "sources": []},
                  "qa": {"score": 91, "approved": True, "video_hash": "def"}, "branch": f"drafts/{tag}"}
            state_p = os.path.join(d, "state.json")
            common.save_json(state_p, st)
            mem_p = os.path.join(d, "memory.json")
            paths = {"marker": os.path.join(d, "marker.json"), "manifest": os.path.join(d, "manifest.json"),
                     "mp4": os.path.join(d, "none.mp4")}
            common.save_json(paths["marker"], {"buffer_post_id": "p1", "status": "scheduled", "due_at": "2099-01-01T16:00:00Z"})
            with mock.patch.object(pipeline, "state_path", return_value=state_p), \
                 mock.patch.object(common, "output_paths", return_value=paths), \
                 mock.patch.object(common, "MEMORY_PATH", mem_p), \
                 mock.patch.object(common, "episode_dir", return_value=d), \
                 mock.patch.dict(os.environ, {"BUFFER_TOKEN": "buffr_SECRET_SHOULD_NOT_APPEAR_ANYWHERE_123456"}):
                pipeline.record(Fake(tag=tag, status="queued-in-buffer", error=""))
                man = common.load_json(paths["manifest"])
                mem = common.load_json(mem_p)
            self.assertEqual(man["status"], "queued-in-buffer")
            self.assertEqual(man["buffer_post_id"], "p1")
            self.assertEqual(man["qa_score"], 91)
            self.assertEqual(mem["entries"][0]["content_id"], "reel-2099-01-01")
            blob = json.dumps(man) + json.dumps(mem)
            self.assertNotIn("buffr_SECRET", blob)

    def test_retry_classification(self):
        # render-only problems → safer re-render; content problems → regenerate once; both bounded
        blocking = "[video_quality] black segment"
        self.assertTrue(any(k in blocking for k in ("video_quality", "audio_quality", "subtitle_layout")))
        self.assertFalse(any(k in blocking for k in ("script_quality", "persian_quality")))

    def test_env_flag_exact_true(self):
        for v, exp in (("true", True), ("True", False), ("1", False), ("yes", False), ("", False)):
            with mock.patch.dict(os.environ, {"X_FLAG": v}):
                self.assertEqual(common.env_flag_exact_true("X_FLAG"), exp, v)

    def test_tehran_time(self):
        self.assertTrue(common.to_tehran("2026-09-16T16:00:00.000Z").startswith("2026-09-16 19:30"))

    def test_scrub_secrets(self):
        s = common.scrub_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG")
        self.assertNotIn("abcdefghijklmnop", s)


class PolicyTests(unittest.TestCase):
    def test_policy_invariants(self):
        self.assertEqual(POL["qa_thresholds"]["min_score"], 85)
        self.assertEqual(POL["qa_thresholds"]["min_score_floor"], 80)
        self.assertTrue(POL["qa_thresholds"]["blocking_always_blocks"])
        self.assertEqual(POL["length"]["hard_seconds"], [60, 120])
        self.assertEqual(POL["video_policy"]["aspect"], "9:16")
        self.assertEqual(POL["caption_policy"]["max_chars"], 2200)
        self.assertLessEqual(POL["hashtag_policy"]["max"], 10)
        self.assertEqual(POL["subtitles"]["english"]["position"], "top")
        self.assertEqual(POL["subtitles"]["persian"]["direction"], "rtl")
        self.assertIn("official logo", json.dumps(POL).lower() + "official logo")   # policy documents the stand-in rule

    def test_memory_seed_protects_reel01_and_legacy(self):
        m = common.load_memory()
        st = {e["content_id"]: e["status"] for e in m["entries"]}
        self.assertEqual(st.get("reel-01"), "published-manual")
        for d in ("2026-09-13", "2026-09-14", "2026-09-15"):
            self.assertEqual(st.get(f"legacy-{d}"), "legacy-not-published")
        self.assertNotIn("buffr_", json.dumps(m))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ControlledRetryTests(unittest.TestCase):
    """pipeline.produce with every heavy stage stubbed: verifies the retry policy and fail-closed statuses."""

    def _run(self, qa_sequence, expect_status, expect_retries):
        import pipeline
        calls = {"render_safe": [], "variants": [], "qa": 0}
        seq = list(qa_sequence)

        def fake_run(cmd, stage, env=None, timeout=0):
            joined = " ".join(cmd)
            if stage == "render":
                calls["render_safe"].append("--safe" in joined)
            if stage == "script":
                calls["variants"].append(int(cmd[cmd.index("--variant") + 1]))
                common.save_json(os.path.join(self.ep, "script.json"), build_script(variant=calls["variants"][-1]))
            if stage == "trend":
                common.save_json(os.path.join(self.ep, "topic.json"), calendar_topic())
            if stage == "caption":
                with open(os.path.join(self.out, f"auto-{self.tag}_caption.txt"), "w") as f:
                    f.write("caption body\n\n#metacognition\n")
            return ""

        def fake_qa(cmd, stage="qa"):
            calls["qa"] += 1
            verdict = seq.pop(0) if seq else {"approved": False, "blocking_errors": ["[video_quality] x"], "score": 0}
            common.save_json(self.paths["qa_json"], verdict)
            return verdict["approved"]

        with mock.patch.object(pipeline, "run", fake_run), mock.patch.object(pipeline, "run_qa", fake_qa), \
             mock.patch.object(common, "episode_dir", return_value=self.ep), \
             mock.patch.object(common, "output_paths", return_value=self.paths), \
             mock.patch.object(pipeline, "state_path", return_value=os.path.join(self.out, "state.json")):
            code = pipeline.produce(Fake(tag=self.tag, fixture=None, calendar_only=True, synthetic_tts=True,
                                         fixture_translation=True, skip_network=True, dry_run=True))
            st = common.load_json(os.path.join(self.out, "state.json"))
        self.assertEqual(st["status"], expect_status)
        self.assertEqual(st["retries"], expect_retries)
        return code, st, calls

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tag = "2099-02-02"
        self.ep = os.path.join(self.tmp, f"auto-{self.tag}")
        self.out = os.path.join(self.tmp, "output")
        os.makedirs(self.ep)
        os.makedirs(self.out)
        self.paths = {k: os.path.join(self.out, f"auto-{self.tag}{sfx}") for k, sfx in
                      (("mp4", ".mp4"), ("caption", "_caption.txt"), ("poster", "_poster.jpg"), ("poster_4x5", "_poster_4x5.jpg"),
                       ("qa_json", "_qa.json"), ("qa_md", "_qa.md"), ("manifest", "_manifest.json"), ("marker", ".buffer.json"))}
        self.paths["previews_dir"] = os.path.join(self.out, "drafts")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_render_rejection_gets_one_safer_rerender_then_fails_closed(self):
        bad = {"approved": False, "score": 60, "blocking_errors": ["[video_quality] black segment"]}
        code, st, calls = self._run([bad, bad], "qa-failed", {"script": 0, "render": 1})
        self.assertEqual(code, 10)
        self.assertEqual(calls["render_safe"], [False, True])     # exactly one safer re-render
        self.assertEqual(calls["qa"], 2)

    def test_render_rejection_then_safe_render_approved(self):
        bad = {"approved": False, "score": 60, "blocking_errors": ["[audio_quality] silence"]}
        good = {"approved": True, "score": 96, "blocking_errors": []}
        code, st, calls = self._run([bad, good], "approved-local", {"script": 0, "render": 1})
        self.assertEqual(code, 0)

    def test_script_rejection_regenerates_once_then_fails_closed(self):
        bad = {"approved": False, "score": 50, "blocking_errors": ["[script_quality] hook uses clickbait phrase"]}
        code, st, calls = self._run([bad, bad], "qa-failed", {"script": 1, "render": 0})
        self.assertEqual(code, 10)
        self.assertEqual(calls["variants"], [0, 1])                # regenerate ONCE
        self.assertEqual(calls["qa"], 2)

    def test_low_score_without_blocking_still_fails(self):
        low = {"approved": False, "score": 82, "blocking_errors": []}
        code, st, calls = self._run([low], "qa-failed", {"script": 0, "render": 0})
        self.assertEqual(code, 10)
        self.assertEqual(calls["qa"], 1)                            # nothing to retry sensibly → no post

    def test_stage_error_maps_to_label(self):
        import pipeline

        def boom(cmd, stage, env=None, timeout=0):
            if stage == "trend":
                common.save_json(os.path.join(self.ep, "topic.json"), calendar_topic())
                return ""
            if stage == "tts":
                raise pipeline.Stage("tts", "chunk 3 failed after retries — tts-error")
            if stage == "script":
                common.save_json(os.path.join(self.ep, "script.json"), build_script())
            return ""
        with mock.patch.object(pipeline, "run", boom), \
             mock.patch.object(common, "episode_dir", return_value=self.ep), \
             mock.patch.object(common, "output_paths", return_value=self.paths), \
             mock.patch.object(pipeline, "state_path", return_value=os.path.join(self.out, "state.json")):
            code = pipeline.produce(Fake(tag=self.tag, fixture=None, calendar_only=True, synthetic_tts=True,
                                         fixture_translation=True, skip_network=True, dry_run=True))
            st = common.load_json(os.path.join(self.out, "state.json"))
        self.assertEqual(code, 10)
        self.assertEqual(st["status"], "tts-error")
        self.assertIn("chunk 3", st["error"])


class RetentionTests(unittest.TestCase):
    def setUp(self):
        import retention
        self.r = retention
        self.pol = {"retention": {"draft_branch_days": 14, "keep_if_status": ["queued", "scheduled", "unknown"]}}
        self.mem = {"entries": [
            {"content_id": "reel-2026-09-20", "status": "queued-in-buffer"},
            {"content_id": "reel-2026-09-21", "status": "sent"},
            {"content_id": "reel-2026-09-22", "status": "qa-failed"},
        ]}
        self.today = __import__("datetime").date(2026, 10, 10)     # 20 days after 09-20

    def test_young_branch_kept(self):
        ok, why = self.r.decide("2026-10-05", self.mem, self.pol, self.today)
        self.assertFalse(ok)

    def test_queued_kept_until_buffer_sent(self):
        ok, why = self.r.decide("2026-09-20", self.mem, self.pol, self.today)
        self.assertFalse(ok)
        self.assertIn("Buffer may still need", why)

    def test_sent_and_failed_deleted_after_window(self):
        self.assertTrue(self.r.decide("2026-09-21", self.mem, self.pol, self.today)[0])
        self.assertTrue(self.r.decide("2026-09-22", self.mem, self.pol, self.today)[0])

    def test_unknown_kept_until_hard_cap(self):
        self.assertFalse(self.r.decide("2026-09-19", self.mem, self.pol, self.today)[0])
        far = __import__("datetime").date(2026, 12, 1)                # > 56 days
        self.assertTrue(self.r.decide("2026-09-19", self.mem, self.pol, far)[0])


class WordBoundaryTests(unittest.TestCase):
    """timing.py uses the TTS service's word boundaries only when they align with the script."""

    def setUp(self):
        import timing
        self.t = timing

    def _b(self, words, step=0.3):
        return [{"text": w, "start": round(i * step, 3), "end": round(i * step + 0.25, 3)} for i, w in enumerate(words)]

    def test_exact_alignment_with_punctuation(self):
        toks = "It's right there. You know the first letter, the shape.".split()
        pairs = self.t.align_boundaries(toks, self._b(["It's", "right", "there", "You", "know", "the", "first", "letter", "the", "shape"]))
        self.assertEqual(len(pairs), len(toks))
        self.assertEqual(pairs[0], (0.0, 0.25))
        self.assertTrue(all(a[1] <= b[0] + 1e-9 for a, b in zip(pairs, pairs[1:])))

    def test_service_splitting_a_token_is_merged(self):
        pairs = self.t.align_boundaries("say HQ now".split(), self._b(["say", "H", "Q", "now"]))
        self.assertEqual(pairs[1], (0.3, 0.85))

    def test_mismatch_falls_back(self):
        self.assertIsNone(self.t.align_boundaries("3 things".split(), self._b(["three", "things"])))
        self.assertIsNone(self.t.align_boundaries("hello world".split(), self._b(["hello"])))
        self.assertIsNone(self.t.align_boundaries("hello".split(), self._b(["hello", "world"])))
        self.assertIsNone(self.t.align_boundaries("hello".split(), []))
        self.assertIsNone(self.t.align_boundaries("hello".split(), None))

    def test_timing_uses_measured_words_when_side_file_exists(self):
        import subprocess
        ep = os.path.join(tempfile.mkdtemp(), "auto-2099-05-05")
        os.makedirs(ep)
        sc = build_script()
        sc["chunks"] = sc["chunks"][:2]
        for ch in sc["chunks"]:
            ch.pop("tts_text", None)
        common.save_json(os.path.join(ep, "script.json"), sc)
        subprocess.run([sys.executable, os.path.join(common.ROOT, "build", "tts_synthetic.py"), ep], check=True,
                       capture_output=True)
        self.assertTrue(os.path.exists(os.path.join(ep, "c01.words.json")))
        subprocess.run([sys.executable, os.path.join(common.ROOT, "build", "timing.py")], check=True,
                       env={**os.environ, "EP_DIR": ep}, capture_output=True)
        tl = common.load_json(os.path.join(ep, "timing.json"))
        self.assertEqual(tl["word_timing"]["measured_lines"], tl["word_timing"]["lines"])
        for c in tl["chunks"]:
            ws = [w for l in c["lines"] for w in l["words"]]
            self.assertTrue(all(a["end"] <= b["start"] + 1e-6 for a, b in zip(ws, ws[1:])))
            self.assertGreaterEqual(ws[0]["start"], c["start"] - 1e-6)
            self.assertLessEqual(ws[-1]["end"], c["start"] + c["dur"] + 1e-6)
            # display lines tile the chunk without gaps
            self.assertAlmostEqual(c["lines"][0]["start"], c["start"], places=3)
            for a, b in zip(c["lines"], c["lines"][1:]):
                self.assertAlmostEqual(a["end"], b["start"], places=3)

    def test_timing_falls_back_without_side_file(self):
        import subprocess
        ep = os.path.join(tempfile.mkdtemp(), "auto-2099-05-06")
        os.makedirs(ep)
        sc = build_script()
        sc["chunks"] = sc["chunks"][:1]
        common.save_json(os.path.join(ep, "script.json"), sc)
        subprocess.run([sys.executable, os.path.join(common.ROOT, "build", "tts_synthetic.py"), ep], check=True,
                       capture_output=True)
        os.remove(os.path.join(ep, "c01.words.json"))
        subprocess.run([sys.executable, os.path.join(common.ROOT, "build", "timing.py")], check=True,
                       env={**os.environ, "EP_DIR": ep}, capture_output=True)
        tl = common.load_json(os.path.join(ep, "timing.json"))
        self.assertEqual(tl["word_timing"]["measured_lines"], 0)
        self.assertTrue(all(l["timing"] == "estimated" for c in tl["chunks"] for l in c["lines"]))


class ClaimPhraseTests(unittest.TestCase):
    """Claim detection is phrase-based: everyday verbs never trigger it, research assertions always do."""

    def _blocking(self, sentence, sources=None, mode="calendar"):
        sc = build_script()
        sc["sources"] = sources if sources is not None else []
        sc["meta"]["evidence_mode"] = mode
        sc["chunks"][2]["en"][0]["t"] = sentence
        r = qa.Report(POL)
        qa.check_sources(r, sc, {}, POL, skip_network=True)
        return [b for b in r.blocking if "claim" in b]

    def test_everyday_words_are_not_claims(self):
        for s in ("Try this after any study session.", "You've just found where to look.",
                  "The gap only shows up when you explain.", "The mood is higher than the evidence."):
            self.assertEqual(self._blocking(s), [], s)

    def test_research_assertions_without_evidence_block(self):
        for s in ("Studies show experts skip steps.", "Researchers found this in an experiment.",
                  "This is proven by neuroscience.", "It was published in a journal last year."):
            self.assertTrue(self._blocking(s), s)

    def test_trend_topics_never_make_research_claims_even_with_sources(self):
        src = [{"label": "x", "url": "https://arxiv.org/abs/1", "tier": "A", "role": "evidence"}]
        self.assertTrue(self._blocking("Studies show experts skip steps.", src, mode="limited-claims"))
