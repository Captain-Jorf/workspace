"""Pre-render script gate — deterministic fix for run 35050738918 (reel-2026-09-17).

The real failure reconstructed from issue #20 + the committed manifest (the QA
artifact zip lives on the Actions blob host, unreachable from the sandbox — same
limitation documented in tests/test_replay_2026_09_16.py):

  * the GROQ PRODUCER's first-pass output was a 106-word script (playbook
    "llm-generated", generation_mode "groq" — NOT the Revision and NOT the
    Static Fallback: a fallback would show its playbook key and mode, and the
    one allowed Revision is only ever consumed by a rejection, which the
    reviewer never issued);
  * the LLM Reviewer APPROVED it (reviewer_check passed because it only mirrors
    the reviewer's structured fields and never looked at the script — and the
    reviewer prompt had no word-count rule at all);
  * QA then blocked AFTER a full TTS+render cycle: [script_quality] words 106
    outside 150-260; [video_quality] duration 45.3s outside 60-120.
    Script retries: 1 — the pipeline variant-1 re-run spent a second TTS+render.

Guarantees this module pins down:

  1. a deterministic PRE-RENDER gate counts actual spoken words with the QA
     single-sourced logic (common.spoken_word_count) and requires 150-260;
  2. a duration preflight estimates seconds from the CONFIGURED narration rate
     (reproduces ~45.3 s for the exact 106-word script) and must be safe for
     the final 60-120 s QA requirement;
  3. an out-of-range Producer output goes through the ONE allowed Revision with
     explicit instructions (one main idea, one actionable technique,
     conversational English, 175-210 spoken words); if the Revision was used
     and it is still out of range → validated Static English Fallback; if the
     fallback also fails → skip before rendering;
  4. a model-reported word count is never trusted and a structured reviewer
     approval can never override the deterministic checks;
  5. a known-short script never reaches TTS, subtitle or render commands;
  6. nothing is padded: no filler, no repeated CTA, no silence, no slow speech.

QA thresholds are untouched: 150-260 words and 60-120 s remain the final
authority (see tests/test_qa_vs_buffer_status.py and the QA-supervisor checks).
"""
import argparse
import json
import os
import shutil
import sys
import copy
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import content_producer as cp  # noqa: E402
import llm_provider  # noqa: E402
import pipeline as pl  # noqa: E402
import qa_supervisor as qa  # noqa: E402

POL = common.policy()
FAKE_KEY = "FAKE_GROQ_KEY_FOR_TESTS_ONLY_" + "Y" * 48
PROD_CANDIDATE = "openai/gpt-oss-20b"

# The exact artifact of run 35050738918: 106 spoken words, 45.3 s rendered.
# The lines below are a deterministic reconstruction that lands on EXACTLY 106
# spoken words under the single-sourced counter (asserted in setUp below).
REPLAY_106_LINES = {
    "hook": ["Autocomplete makes you faster today, and slower next year. Why?"],
    "problem": ["You used to know the syntax by heart.",
                "You wait for the suggestion and accept it.",
                "The suggestion is faster, so you stop trying."],
    "explain": ["Skill is kept by retrieval, pulling it from memory.",
                "Autocomplete replaces retrieval with recognition.",
                "Recognition is easier, but it doesn't keep the skill alive."],
    "example": ["You wrote regex from memory.",
                "Next month you can't write it at all."],
    "technique": ["Try this: one hour a week, code with autocomplete off.",
                  "Write the hard parts from memory, then check.",
                  "That hour keeps the skill alive."],
    "ending": ["What skill are you losing because AI does it for you?"],
}


def make_llm_output(narration=None):
    narration = copy.deepcopy(narration) if narration is not None else copy.deepcopy(REPLAY_106_LINES)
    return {
        "title": "Autocomplete deskilling reel",
        "technology_angle": "deskilling through AI autocomplete",
        "metacognition_concept": "skill decay and monitoring",
        "hook": " ".join(narration["hook"]),
        "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
        "narration": narration,
        "on_screen_text": ["FAST", "SLOW", "RECALL", "RECOGNIZE", "OFF", "KEEP"],
        "visual_direction": "code visual + memory meter",
        "actionable_technique": "one hour a week autocomplete off",
        "ending": " ".join(narration["ending"]),
        "caption": {"hook": " ".join(narration["hook"]), "intro": "Autocomplete deskilling.",
                    "sections": [], "hashtags": ["#metacognition", "#AI"]},
        "claims": [],
        "sources": [{"label": "Bainbridge (1983) – Ironies of automation", "url": "", "tier": "A"}],
    }


def script_from_narration(narration, meta_extra=None):
    chunks, idx = [], 1
    for beat in ["hook", "problem", "explain", "example", "technique", "ending"]:
        lines = narration.get(beat, [])
        if not lines:
            continue
        chunks.append({"id": f"c{idx}", "beat": beat,
                       "en": [{"t": l, "scene": beat, "beat": beat} for l in lines],
                       "fa": [], "tts_text": " ".join(lines)})
        idx += 1
    script = {"meta": dict({"title": "t", "topic": "Autocomplete makes you faster today",
                            "content_id": "reel-2077-01-02", "content_date": "2077-01-02",
                            "pillar": "CODING", "technology_angle": "deskilling through AI autocomplete",
                            "metacognition_concept": "skill decay and monitoring",
                            "playbook": "llm-generated", "language": "en", "content_language": "en",
                            "generation_mode": "groq", "handle": "@metacognition.hq",
                            "fps": 30, "w": 1080, "h": 1920, "gap": 0.34, "lead": 0.55, "tail": 1.2},
                           **(meta_extra or {})),
              "chunks": chunks, "caption": {"hook": " ".join(narration.get("hook", [])),
                                            "intro": "", "sections": [], "ctas": [], "hashtags": []}}
    return script


def approved_review():
    return {"approved": True, "score": 95, "technology_relevance": True,
            "metacognition_relevance": True, "source_grounding": True,
            "unsupported_claims": [], "hook_quality": "good",
            "spoken_english_quality": "good", "novelty": "high",
            "practical_value": "high", "safety": "safe",
            "required_changes": [], "blocking_errors": []}


def rejected_review(changes):
    r = approved_review()
    r.update({"approved": False, "score": 70, "required_changes": changes,
              "blocking_errors": changes})
    return r


def _stage_repo_assets(root):
    img_src = os.path.join(ROOT, "assets", "img")
    img_dst = os.path.join(root, "assets", "img")
    os.makedirs(img_dst, exist_ok=True)
    for f in os.listdir(img_src):
        if f.endswith(".png"):
            shutil.copy(os.path.join(img_src, f), os.path.join(img_dst, f))


def calendar_topic(cal_id=11, date="2077-01-02"):
    cal = next(c for c in common.calendar()["episodes"] if c["id"] == cal_id)
    return {"content_date": date, "content_id": f"reel-{date}", "title": cal["title"],
            "normalized_topic": common.normalize_title(cal["title"]),
            "pillar": cal.get("pillar", "CODING"),
            "technology_angle": "autocomplete in code editors",
            "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
            "evidence_mode": "calendar", "calendar": cal}


def clean_env(**overrides):
    e = {k: v for k, v in os.environ.items()
         if k not in ("GROQ_API_KEY", "MOCK_GROQ", "GROQ_BASE_URL",
                      "PRODUCER_MODEL", "REVIEWER_MODEL", "GITHUB_ACTIONS")}
    e["CONTENT_LANGUAGE"] = "en"
    e.update(overrides)
    return e


class ExactReplayOfTheFailure(unittest.TestCase):
    """The 106-word / 45.3-second episode, reconstructed and stopped at the gate."""

    def test_replay_is_exactly_106_words_and_estimates_the_rendered_45_3s(self):
        script = script_from_narration(REPLAY_106_LINES)
        self.assertEqual(common.spoken_word_count(script), 106,
                         "fixture must reproduce the artifact's exact spoken-word count")
        est = common.estimate_spoken_seconds(script, POL)
        self.assertAlmostEqual(est, 45.3, delta=2.5,
                               msg=f"duration model must reproduce the real 45.3 s render (got {est})")

    def test_replay_is_caught_deterministically_before_tts(self):
        script = script_from_narration(REPLAY_106_LINES)
        issues = common.pre_render_issues(script, POL)
        self.assertTrue(issues)
        self.assertIn("spoken words 106 below the required 150-260", "; ".join(issues))
        self.assertIn("estimated spoken duration", "; ".join(issues))
        # QA (unchanged thresholds) counts exactly the same number as the gate.
        rep = qa.Report(POL)
        qa.check_script(rep, script, POL)
        self.assertEqual(rep.details["script"]["words"], common.spoken_word_count(script))
        self.assertIn("[script_quality] words 106 outside 150-260", rep.blocking)

    def test_model_reported_word_count_is_never_trusted(self):
        out = make_llm_output()
        out["word_count"] = 205          # a model claiming a compliant count...
        out["narration"] = dict(REPLAY_106_LINES)
        script = cp.build_script_from_llm(calendar_topic(), POL, out)
        self.assertEqual(common.spoken_word_count(script), 106)
        self.assertTrue(common.pre_render_issues(script, POL),
                        "claimed counts must not satisfy the gate; only the actual lines count")


class GateProducerBehavior(unittest.TestCase):
    """cp.main(): bounded repair ladder with mocked Groq transport."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prerender_gate_")
        topic_path = os.path.join(self.tmp, "topic.json")
        common.save_json(topic_path, calendar_topic())
        self.topic_path = topic_path
        self.ep = os.path.join(self.tmp, "ep")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, produces, reviews):
        calls = {"produce": 0, "review": 0, "packets": []}

        def produce(self_, packet, _discovered=None):
            calls["produce"] += 1
            calls["packets"].append(json.loads(json.dumps(packet)))
            i = min(calls["produce"] - 1, len(produces) - 1)
            return produces[i][0], dict(produces[i][1], selection={"reason": "stub"})

        def review(self_, out, packet, _discovered=None, producer_model=None):
            calls["review"] += 1
            i = min(calls["review"] - 1, len(reviews) - 1)
            return dict(reviews[i][0]), dict(reviews[i][1])

        with mock.patch.object(sys, "argv", ["content_producer.py", "--topic", self.topic_path,
                                              "--out", self.ep, "--variant", "0"]):
            with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
                with mock.patch.object(llm_provider, "discover_models",
                                       return_value=([PROD_CANDIDATE], {"http_status": 200})), \
                     mock.patch.object(llm_provider.GroqProducer, "produce", produce), \
                     mock.patch.object(llm_provider.GroqReviewer, "review", review):
                    try:
                        cp.main()
                        exit_code = 0
                    except SystemExit as e:
                        exit_code = e.code if isinstance(e.code, int) else 1
        return common.load_json(os.path.join(self.ep, "script.json")) if exit_code == 0 else None, \
            calls, exit_code

    def test_short_producer_output_is_routed_through_the_one_allowed_revision(self):
        # First Producer output: the exact 106-word artifact. Reviewer APPROVES it
        # (that is the bug this PR fixes) → the deterministic gate must still send
        # it through the one allowed Revision — with explicit instructions.
        short = make_llm_output()
        # a compliant revision output (within 150-260, target band, safe estimate)
        fixed = make_llm_output()
        fixed["narration"] = {
            "hook": ["Autocomplete makes you faster today, and slower next year. Why?"],
            "problem": ["You used to know the syntax by heart.",
                        "Now you wait for the ghost text, then hit Tab and move on.",
                        "The suggestion is faster, so you stop trying to recall.",
                        "Every accepted line makes waiting the habit."],
            "explain": ["Skill is kept by retrieval, pulling it from memory.",
                        "Autocomplete replaces retrieval with recognition.",
                        "Recognition is easy, but it does not keep the skill alive.",
                        "So the decay is silent: you can still read code, you can't write it."],
            "example": ["You used to write regex from memory.",
                         "Now the assistant drafts it, you glance, accept, and it ships.",
                         "Next month the same pattern defeats you and costs a debugging hour.",
                         "You recognize the shape, but you can't produce it."],
            "technique": ["Try this: one hour a week, code with autocomplete off.",
                          "Write the hard parts from memory first, then use the suggestion as your check.",
                          "Notice the stall; stay in it ten seconds before you tab.",
                          "That hour is your skill insurance."],
            "ending": ["What skill are you losing because AI does it for you?"],
        }
        script, calls, code = self._run_main(produces=[(short, {}), (fixed, {})],
                                              reviews=[(approved_review(), {}), (approved_review(), {})])
        self.assertEqual(code, 0)
        self.assertEqual(calls["produce"], 2, "out-of-range first output must trigger exactly one revision")
        self.assertEqual(calls["review"], 2, "Reviewer + final Reviewer only — the policy chain")
        self.assertEqual(script["meta"]["generation_mode"], "groq")
        words = common.spoken_word_count(script)
        self.assertTrue(150 <= words <= 260, words)
        self.assertEqual(common.pre_render_issues(script, POL), [])
        # the revision instruction explicitly carries the length contract
        rev_packet = calls["packets"][1]
        req = " ".join(rev_packet.get("revision_request") or []).lower()
        for needle in ("150", "260", "175", "210", "one main idea", "one actionable technique",
                       "conversational english", "spoken words"):
            self.assertIn(needle, req, f"revision request must state '{needle}'")
        self.assertIn("never pad", req)

    def test_revision_still_short_uses_validated_static_fallback(self):
        short = make_llm_output()
        still_short = make_llm_output()
        still_short["narration"]["problem"] = still_short["narration"]["problem"] + ["And you accept again."]
        script, calls, code = self._run_main(produces=[(short, {}), (still_short, {})],
                                             reviews=[(approved_review(), {}), (approved_review(), {})])
        self.assertEqual(code, 0)
        self.assertEqual(calls["produce"], 2, "no second revision — one is the cap")
        self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
        self.assertEqual(script["meta"]["playbook"], "deskilling-autocomplete",
                         "calendar id 11 (this exact topic) must fall back to its own playbook")
        words = common.spoken_word_count(script)
        self.assertTrue(150 <= words <= 260, words)
        self.assertEqual(common.pre_render_issues(script, POL), [],
                         "the static fallback is VALIDATED by the same gate before it ships")
        p = common.pre_render_params(POL)
        self.assertTrue(p["target_min"] <= words <= p["target_max"],
                        f"fallback must sit in the safe target band, got {words}")

    def test_invalid_fallback_skips_before_rendering(self):
        # If even the static fallback fails the gate, nothing ships: no script.json,
        # nonzero exit, and no media stage can read anything.
        bad_fallback = script_from_narration({"hook": ["Too short to render."],
                                              "problem": ["Still too short."],
                                              "ending": ["No."]})
        orig_builder = cp.build_script_from_playbook
        try:
            with mock.patch.object(cp, "build_script_from_playbook",
                                   return_value=bad_fallback):
                script, calls, code = self._run_main(produces=[(make_llm_output(), {}),
                                                                 (make_llm_output(), {})],
                                                      reviews=[(approved_review(), {}),
                                                               (approved_review(), {})])
        finally:
            cp.build_script_from_playbook = orig_builder
        self.assertEqual(code, 3, "gate skip must exit nonzero BEFORE any media stage")
        self.assertIsNone(script)
        self.assertFalse(os.path.exists(os.path.join(self.ep, "script.json")),
                         "no script.json may exist for a gate-skipped producer run")
        report = common.load_json(os.path.join(self.ep, "producer_report.json"), {})
        self.assertTrue(report["gate"]["issues"])
        self.assertEqual(report["gate"]["words"], common.spoken_word_count(bad_fallback))

    def test_no_padding_or_repeated_cta_is_ever_applied(self):
        # The adopted fallback is byte-identical to a plain playbook build:
        # the gate never pads with extra lines, silence notes or repeated CTAs.
        topic = calendar_topic()
        pb = cp.PLAYBOOKS["deskilling-autocomplete"]
        plain = cp.build_script_from_playbook(topic, POL, pb, "deskilling-autocomplete")
        script, calls, code = self._run_main(produces=[(make_llm_output(), {}), (make_llm_output(), {})],
                                             reviews=[(approved_review(), {}), (approved_review(), {})])
        self.assertEqual(code, 0)
        self.assertEqual(common.narration_lines(script), common.narration_lines(plain),
                         "fallback validation must not alter content to reach the range")
        text = " ".join(common.narration_lines(script)).lower()
        ending = script["chunks"][-1]["en"][0]["t"].lower().rstrip("?.")
        self.assertEqual(text.count(ending), 1,
                         "the CTA must appear exactly once in narration — never repeated to pad length")
        for bad in ("silence", "apad", "filler", "in conclusion", "to be honest"):
            self.assertNotIn(bad, text)
        src = open(os.path.join(ROOT, "build", "content_producer.py"), encoding="utf-8").read()
        for pad in ("apad", "silenceremove", "atempo", "asetrate"):
            self.assertNotIn(pad, src, "the producer must never touch audio padding")

    def test_provider_failure_paths_still_fall_back_bounded(self):
        # 429/timeout/malformed on the first produce → static fallback, no retry storm.
        with mock.patch.object(sys, "argv", ["content_producer.py", "--topic", self.topic_path,
                                             "--out", self.ep, "--variant", "0"]):
            with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
                with mock.patch.object(llm_provider, "discover_models",
                                       return_value=([PROD_CANDIDATE], {"http_status": 200})), \
                     mock.patch.object(llm_provider.GroqProducer, "produce",
                                       side_effect=RuntimeError("Groq quota 429 exhausted")):
                    cp.main()
        script = common.load_json(os.path.join(self.ep, "script.json"))
        self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
        self.assertEqual(common.pre_render_issues(script, POL), [])

    def test_reviewer_rejection_with_short_revision_falls_back(self):
        # Reviewer rejects first → one revision (used) → revision still outside
        # 150-260 → fallback (no more retries allowed).
        short = make_llm_output()
        script, calls, code = self._run_main(produces=[(short, {}), (short, {})],
                                             reviews=[(rejected_review(["make it land"]), {}),
                                                      (approved_review(), {})])
        self.assertEqual(code, 0)
        self.assertEqual(calls["produce"], 2, "exactly the one allowed revision")
        self.assertEqual(script["meta"]["generation_mode"], "static-fallback")


class GateBoundaries(unittest.TestCase):
    """150/260 required; 175/210 targeted. 149/261 rejected."""

    def _script_with_words(self, n, chunks=6):
        # deterministic filler-free fixture: N distinct-word lines grouped into chunks
        words = [f"w{i}" for i in range(1, n + 1)]
        per = max(1, len(words) // chunks + (1 if len(words) % chunks else 0))
        lines = [" ".join(words[i:i + per]) for i in range(0, len(words), per)]
        narration = {"hook": [], "problem": [], "explain": [], "example": [],
                     "technique": [], "ending": []}
        beats = list(narration)
        for i, ln in enumerate(lines):
            narration[beats[min(i, len(beats) - 1)]].append(ln)
        # keep the hook short and legal, push its words into problem
        hook_words = narration["hook"]
        if hook_words:
            extra = hook_words.pop(0)
            narration["problem"].insert(0, extra)
        return script_from_narration(narration)

    def _words(self, n):
        s = self._script_with_words(n)
        self.assertEqual(common.spoken_word_count(s), n)
        return s

    def test_word_range_boundaries(self):
        p = common.pre_render_params(POL)
        self.assertEqual((p["words_min"], p["words_max"]), (150, 260))
        for n, in_range in ((149, False), (150, True), (175, True),
                            (210, True), (260, True), (261, False)):
            script = self._words(n)
            words_issue = [i for i in common.pre_render_issues(script, POL) if "spoken words" in i]
            if in_range:
                self.assertEqual(words_issue, [], f"{n} words must satisfy the required 150-260 range")
            else:
                self.assertTrue(words_issue, f"{n} words must be rejected by the required 150-260 range")
                self.assertIn(f"spoken words {n}", words_issue[0])

    def test_target_band_guides_duration_safety(self):
        p = common.pre_render_params(POL)
        self.assertEqual((p["target_min"], p["target_max"]), (175, 210))
        # 150 words is within the REQUIRED range but too little spoken material
        # for a safe 60-120 s render at the configured rate → duration preflight
        # rejects it (this is what keeps the target band meaningful).
        s150 = self._words(150)
        self.assertTrue(common.spoken_word_count(s150) >= p["words_min"])
        est150 = common.estimate_spoken_seconds(s150, POL)
        self.assertLess(est150, p["dur_min"])
        self.assertIn("estimated spoken duration", "; ".join(common.pre_render_issues(s150, POL)),
                      "the duration preflight must reject a minimum-length script that cannot "
                      "safely reach 60 s — this is why prompts target 175-210 words")
        # 175 and 210 pass both rules comfortably
        for n in (175, 210):
            self.assertEqual(common.pre_render_issues(self._words(n), POL), [])

    def test_estimate_uses_configured_narration_rate(self):
        s = self._words(180)
        base = common.estimate_spoken_seconds(s, POL)
        slower = json.loads(json.dumps(POL))
        slower["tts"]["rate"] = "-20%"
        est = common.estimate_spoken_seconds(s, slower)
        self.assertGreater(est, base, "a slower configured rate must lengthen the estimate")
        faster = json.loads(json.dumps(POL))
        faster["tts"]["rate"] = "+20%"
        self.assertLess(common.estimate_spoken_seconds(s, faster), est)


class ReviewerBoundary(unittest.TestCase):
    """reviewer_check must not rubber-stamp what the script itself violates."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reviewer_boundary_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_report(self, out):
        common.save_json(os.path.join(self.tmp, "reviewer_report.json"),
                         {"model": "x/y", "raw": {}, "output": out})

    def test_approval_of_106_word_script_is_blocked_deterministically(self):
        # The 2026-09-17 situation: a structured "approved:95" report on a 106-word
        # script used to make reviewer_check PASS. Now reviewer_check must FAIL.
        self._write_report(approved_review())
        script = script_from_narration(REPLAY_106_LINES)
        self.assertEqual(common.spoken_word_count(script), 106)
        rep = qa.Report(POL)
        qa.check_reviewer(rep, script, calendar_topic(), self.tmp, POL)
        joined = "; ".join(rep.blocking)
        self.assertIn("reviewer approved a script with 106 spoken words outside policy 150-260", joined)
        self.assertIn("structured approval cannot override", joined)
        self.assertEqual(rep.checks["reviewer_check"], "fail")

    def test_approval_of_in_range_script_still_passes(self):
        self._write_report(approved_review())
        topic = calendar_topic()
        script = cp.build_script_from_playbook(topic, POL, cp.PLAYBOOKS["deskilling-autocomplete"],
                                               "deskilling-autocomplete")
        rep = qa.Report(POL)
        qa.check_reviewer(rep, script, topic, self.tmp, POL)
        self.assertEqual(rep.blocking, [])
        self.assertEqual(rep.checks["reviewer_check"], "pass")

    def test_missing_reviewer_report_is_still_tolerated(self):
        script = script_from_narration(REPLAY_106_LINES)
        rep = qa.Report(POL)
        empty = tempfile.mkdtemp(prefix="no_report_", dir=self.tmp)
        qa.check_reviewer(rep, script, calendar_topic(), empty, POL)
        self.assertEqual(rep.details["reviewer"], {"present": False})


class PipelineStopsBeforeMedia(unittest.TestCase):
    """The 106-word script must never cost a TTS / subtitle / render cycle."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pipeline_gate_")
        self._orig_root, self._orig_content = common.ROOT, common.CONTENT
        common.ROOT = self.tmp
        common.CONTENT = os.path.join(self.tmp, "content")
        os.makedirs(common.CONTENT, exist_ok=True)
        # a real checkout carries the repo brand assets — the pre-render
        # visual plan's provenance check requires them to exist
        _stage_repo_assets(self.tmp)
        self.tag = "2077-11-11"
        self.ep = common.episode_dir(self.tag)
        os.makedirs(self.ep, exist_ok=True)
        self.commands = []
        self.qa_calls = []

    def tearDown(self):
        common.ROOT, common.CONTENT = self._orig_root, self._orig_content
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_run(self, cmd, stage, env=None, timeout=1800):
        self.commands.append((stage, " ".join(str(c) for c in cmd)))
        if stage == "trend":
            common.save_json(os.path.join(self.ep, "topic.json"), calendar_topic(date=self.tag))
        elif stage == "script":
            common.save_json(os.path.join(self.ep, "script.json"), script_from_narration(REPLAY_106_LINES))
        return ""

    def _fake_qa(self, cmd, stage="qa"):
        self.qa_calls.append(stage)
        return False

    def test_short_script_never_reaches_tts_caption_or_render(self):
        a = argparse.Namespace(tag=self.tag, fixture=None, calendar_only=True,
                               synthetic_tts=False, skip_network=True, dry_run=True)
        with mock.patch.object(pl, "run", self._fake_run), \
             mock.patch.object(pl, "run_qa", self._fake_qa):
            rc = pl.produce(a)
        self.assertEqual(rc, 10)
        stages = [s for s, _ in self.commands]
        self.assertEqual(stages, ["trend", "script", "script"],
                         f"exactly the producer stage + its ONE retry, got: {self.commands}")
        joined = " ".join(c for _, c in self.commands)
        for media in ("tts_edge.py", "tts_synthetic.py", "timing.py", "caption.py",
                      "render_auto.py", "poster_auto.py"):
            self.assertNotIn(media, joined, f"{media} must never run for a gate-rejected script")
        self.assertEqual(self.qa_calls, [], "supervisor must not evaluate a skipped script")
        st = pl.load_state(self.tag)
        self.assertEqual(st["status"], "script-error")
        self.assertEqual(st["retries"]["script"], 1, "bounded to the existing single script retry")
        self.assertIn("pre-render script gate", st["error"])
        self.assertIn("spoken words 106", st["error"])

    def test_pipeline_gate_accepts_a_valid_script(self):
        ok_script = cp.build_script_from_playbook(calendar_topic(date=self.tag), POL,
                                                  cp.PLAYBOOKS["deskilling-autocomplete"],
                                                  "deskilling-autocomplete")

        def run2(cmd, stage, env=None, timeout=1800):
            self.commands.append((stage, " ".join(str(c) for c in cmd)))
            if stage == "trend":
                common.save_json(os.path.join(self.ep, "topic.json"), calendar_topic(date=self.tag))
            elif stage == "script":
                common.save_json(os.path.join(self.ep, "script.json"), ok_script)
            elif stage == "tts":
                # reached ONLY because the gate accepted the script — stop here
                raise pl.Stage("tts", "test stop after the gate let the script through")
            return ""

        a = argparse.Namespace(tag=self.tag, fixture=None, calendar_only=True,
                               synthetic_tts=False, skip_network=True, dry_run=True)
        with mock.patch.object(pl, "run", run2):
            rc = pl.produce(a)
        self.assertEqual(rc, 10)  # stage error from the injected stop, NOT from the gate
        st = pl.load_state(self.tag)
        self.assertNotIn("pre-render", st.get("error", ""))
        self.assertEqual(st["gate"]["ok"], True)
        stages = [s for s, _ in self.commands]
        self.assertEqual(stages, ["trend", "script", "tts"],
                         "a gate-clean script proceeds normally to the media stages")


class SingleSourcedCounting(unittest.TestCase):
    def test_gate_and_qa_share_the_word_logic(self):
        for pb_key, pb in cp.PLAYBOOKS.items():
            s = cp.build_script_from_playbook(calendar_topic(), POL, pb, pb_key)
            inline = sum(common.word_count(l["t"]) for ch in s["chunks"] for l in ch["en"])
            self.assertEqual(common.spoken_word_count(s), inline)

    def test_qa_range_unchanged(self):
        self.assertEqual(POL["length"]["narration_words"], [150, 260])
        self.assertEqual(POL["length"]["hard_seconds"], [60, 120])

    def test_static_fallbacks_all_pass_the_gate(self):
        p = common.pre_render_params(POL)
        for key, pb in cp.PLAYBOOKS.items():
            s = cp.build_script_from_playbook(calendar_topic(), POL, pb, key)
            self.assertEqual(common.pre_render_issues(s, POL), [], key)
            words = common.spoken_word_count(s)
            self.assertTrue(p["target_min"] <= words <= p["target_max"], f"{key}: {words}")
            low = " ".join(common.narration_lines(s)).lower()
            markers = sum(1 for m in POL["tone"]["informality_markers"] if m in low)
            self.assertGreaterEqual(markers, 2, f"{key}: conversational contractions required")
            self.assertEqual(common.find_numeric_claims(low), [], f"{key}: no statistics")

    def test_trend_lenses_pass_the_gate(self):
        for title in ("Autocomplete makes you faster today, slower next year. Deskilling",
                      "A new AI coding agent ships", "x"):
            for pillar in ("AI_JUDGMENT", "CODING"):
                pb = cp.trend_playbook(title, pillar)
                topic = {"content_date": "2077-01-02", "content_id": "reel-2077-01-02",
                         "title": title, "normalized_topic": common.normalize_title(title),
                         "pillar": pillar, "technology_angle": "AI tooling",
                         "discovery_source": {"name": "hackernews", "url": "", "tier": "C"},
                         "evidence_mode": "trend"}
                s = cp.build_script_from_playbook(topic, POL, pb, "trend")
                issues = common.pre_render_issues(s, POL)
                word_issues = [i for i in issues if "spoken words" in i]
                self.assertEqual(word_issues, [], f"{pillar} lens for {title!r}: {issues}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
