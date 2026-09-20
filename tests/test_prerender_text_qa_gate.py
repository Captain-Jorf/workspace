"""Pre-render TEXT QA gate — deterministic fix for issue #22 (run 35054292820).

The exact history: the calendar evidence packet carried NO Tier A/B source for
the reel-2026-09-18 topic, the GROQ Producer's spoken narration used the claim
word "researchers", the LLM Reviewer approved it, PR #21's word-count/duration
gate legitimately passed it (the script was well-formed), so the pipeline spent
a full TTS + timing + subtitle + FFmpeg render cycle before the final QA
supervisor blocked it with the ONLY error:

    [source_quality] claim words ['researchers'] without tier A/B

The evaluated field is the SPOKEN SCRIPT: qa_supervisor.check_sources builds
its claim text EXCLUSIVELY from script.json chunks[].en[].t (verified below —
the same word in on-screen text or the caption alone never triggers it). The
stored manifest entry {"label": "Mark, Gudith & Klocke research on interrupted
work", "url": "", "tier": "A"} evaluates to tier "?" because QA derives tiers
from URL/dated labels and NEVER trusts a claimed "tier" field.

Guarantees this module pins down:

  1. a single-sourced PRE-RENDER text gate runs EVERY deterministic final-QA
     blocker that can be evaluated before media exists — through the EXACT
     qa_supervisor functions (verified by spy + by end-to-end parity against
     qa.evaluate with a filter for the text-check set, never a copy);
  2. evidence grounding: without Tier A/B evidence in the sanitized packet, the
     policy's own claim-word patterns (source_policy.require_evidence_for_claim_words
     — no second list, and the non-QA "experts say" phrasing is deliberately
     NOT added) are disallowed before TTS; with dated packet evidence the same
     attribution is grounded and ships; nothing (citation, URL, author,
     statistic, tier) is ever invented;
  3. bounded repair: failing pre-render text QA consumes the ONE allowed
     Revision with safe STRUCTURED blocker codes; the Revision must remove the
     attribution — an invented citation is rejected; still blocked → validated
     Static English Fallback; fallback blocked → skip before TTS/render;
  4. zero media-stage calls for an unrepaired claim-word blocker (no TTS, no
     timing, no caption, no FFmpeg render, no posters, no QA of media, and the
     pipeline never imports/touches Buffer publishing paths);
  5. media-only checks (audio/video properties, rendered subtitle geometry,
     frames, posters, buffer readiness, duplicates/quarantine) stay in final
     QA, which remains mandatory and authoritative;
  6. QA is not weakened: reviewer ≥85, words 150-260, 60-120 s, caption/hashtag
     limits and the whole source_quality blocker are untouched; warnings stay
     warnings.
"""
import argparse
import copy
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
import caption as caption_mod  # noqa: E402
import content_producer as cp  # noqa: E402
import llm_provider  # noqa: E402
import pipeline as pl  # noqa: E402
import qa_supervisor as qa  # noqa: E402

POL = common.policy()
FAKE_KEY = "FAKE_GROQ_KEY_FOR_TESTS_ONLY_" + "Y" * 48
PROD_CANDIDATE = "openai/gpt-oss-20b"
CLAIM_PATTERNS = POL["source_policy"]["require_evidence_for_claim_words"]

# The exact script-side facts of run 35054292820 (issue #22). The manifest
# recorded this single source entry — undated label, empty URL, a CLAIMED
# tier "A" that QA must ignore (its matcher derives "?" instead):
MANIFEST_0918_SOURCES = [{"label": "Mark, Gudith & Klocke research on interrupted work",
                          "url": "", "tier": "A"}]
# The claim-word line that triggered the blocker (narration field), phrased to
# land inside the 150-260 word band so the PR #21 fast gate passes it — exactly
# like the real run ("Script retries: 0, Render retries: 0").
CLAIM_LINE = "Researchers say each switch steals minutes from your plan."


def _stage_repo_assets(root):
    img_src = os.path.join(ROOT, "assets", "img")
    img_dst = os.path.join(root, "assets", "img")
    os.makedirs(img_dst, exist_ok=True)
    for f in os.listdir(img_src):
        if f.endswith(".png"):
            shutil.copy(os.path.join(img_src, f), os.path.join(img_dst, f))


def calendar_topic(cal_id=12, date="2077-02-02", with_evidence=True):
    cal = copy.deepcopy(next(c for c in common.calendar()["episodes"] if c["id"] == cal_id))
    if not with_evidence:
        cal["sources"] = []
    return {"content_date": date, "content_id": f"reel-{date}", "title": cal["title"],
            "normalized_topic": common.normalize_title(cal["title"]),
            "pillar": cal.get("pillar", "ATTENTION"),
            "technology_angle": cal.get("technology_angle", ""),
            "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
            "evidence_mode": "calendar", "calendar": cal}


def issue22_script(topic=None, claim=True):
    """Curated context-switching narration (clean, in-range, grounded build)
    mutated to the artifact shape: the claim word inside a SPOKEN narration
    line, sources reduced to the manifest's undated entry (no Tier A/B)."""
    topic = topic or calendar_topic(12, with_evidence=False)
    script = cp.build_script_from_playbook(topic, POL, cp.PLAYBOOKS["context-switching"],
                                           "context-switching", generation_mode="groq")
    if claim:
        for ch in script["chunks"]:
            if ch["beat"] == "explain":
                ch["en"][0]["t"] = CLAIM_LINE
                break
    if not topic.get("calendar", {}).get("sources"):
        script["sources"] = copy.deepcopy(MANIFEST_0918_SOURCES)
    return script


def make_llm_output(narration=None, sources=None, hook=None):
    pb = cp.PLAYBOOKS["context-switching"]
    narration = narration if narration is not None else {
        "hook": [hook or pb["hook"]],
        "problem": list(pb["problem"]),
        "explain": list(pb["explain"]),
        "example": list(pb["example"]),
        "technique": list(pb["technique"]),
        "ending": [pb["ending"]],
    }
    return {
        "title": "Context switching reel",
        "technology_angle": pb["technology_angle"],
        "metacognition_concept": pb["metacognition_concept"],
        "hook": " ".join(narration["hook"]),
        "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
        "narration": narration,
        "on_screen_text": list(pb["web"]),
        "visual_direction": "attention meter + notification visual",
        "actionable_technique": "batch notifications into two windows a day",
        "ending": " ".join(narration["ending"]),
        "caption": {"hook": " ".join(narration["hook"]),
                    "intro": " ".join(narration["problem"]),
                    "sections": [{"title": "TRY THIS", "lines": narration["technique"]}],
                    "hashtags": ["#metacognition", "#metacognitionhq", "#focus"]},
        "claims": [],
        "sources": copy.deepcopy(sources) if sources is not None else [],
    }


def output_with_claim_word():
    """Producer first-pass shaped like the issue #22 artifact: in-range length,
    claim word inside the SPOKEN narration, undated no-URL source list."""
    pb = cp.PLAYBOOKS["context-switching"]
    out = make_llm_output()
    out["narration"]["explain"] = [CLAIM_LINE] + list(pb["explain"][1:])
    out["sources"] = copy.deepcopy(MANIFEST_0918_SOURCES)
    return out


def output_without_attribution():
    """The valid repair: the SAME content with the unsupported attribution
    removed — a direct, qualified observation, no citation added."""
    pb = cp.PLAYBOOKS["context-switching"]
    out = make_llm_output()
    out["narration"]["explain"] = (["Each switch quietly steals minutes from your plan."]
                                   + list(pb["explain"][1:]))
    return out


def approved_review():
    return {"approved": True, "score": 95, "technology_relevance": True,
            "metacognition_relevance": True, "source_grounding": True,
            "unsupported_claims": [], "hook_quality": "good",
            "spoken_english_quality": "good", "novelty": "high",
            "practical_value": "high", "safety": "safe",
            "required_changes": [], "blocking_errors": []}


def clean_env(**overrides):
    e = {k: v for k, v in os.environ.items()
         if k not in ("GROQ_API_KEY", "MOCK_GROQ", "GROQ_BASE_URL",
                      "PRODUCER_MODEL", "REVIEWER_MODEL", "GITHUB_ACTIONS")}
    e["CONTENT_LANGUAGE"] = "en"
    e.update(overrides)
    return e


# ---------------------------------------------------------------------------
# 1. The exact field and the exact reproduction of issue #22
# ---------------------------------------------------------------------------
class Issue22ExactReplay(unittest.TestCase):
    def test_blocker_fires_on_the_spoken_script_field(self):
        script = issue22_script()
        fast = common.pre_render_issues(script, POL)
        self.assertEqual(fast, [], "the PR #21 word/duration gate legitimately "
                         "passed this script (run said: Script retries 0)")
        gate = qa.pre_render_text_gate(script, calendar_topic(12, with_evidence=False), POL)
        self.assertEqual(gate["blocking"],
                         ["[source_quality] claim words ['researchers'] without tier A/B"])

    def test_evaluated_field_is_narration_not_on_screen_or_caption(self):
        """'researchers' only in web labels / caption does not trigger the
        source_quality matcher (it scans spoken narration) — the gate covers
        exactly the fields final QA evaluates, no more and no less."""
        topic = calendar_topic(12, with_evidence=False)
        bare = issue22_script(topic=topic, claim=False)
        self.assertEqual(qa.pre_render_text_gate(bare, topic, POL)["blocking"], [])
        on_screen = copy.deepcopy(bare)
        on_screen["web"] = ["RESEARCHERS", "CUE", "TAP", "NOTE IT", "LATER", "CHOOSE"]
        cap = copy.deepcopy(bare)
        cap["caption"]["intro"] = "Researchers say each switch steals minutes."
        # On-screen text is not a QA-evaluated field; the caption is evaluated
        # by the caption checks (length/hashtags/banned phrases), never for
        # claim words — both keep this script text-clean for source_quality.
        self.assertEqual(qa.pre_render_text_gate(on_screen, topic, POL)["blocking"], [])
        self.assertEqual(qa.pre_render_text_gate(cap, topic, POL)["blocking"], [])
        narration = copy.deepcopy(bare)
        narration["chunks"][1]["en"][0]["t"] = CLAIM_LINE
        self.assertTrue(any("source_quality" in b for b
                            in qa.pre_render_text_gate(narration, topic, POL)["blocking"]))

    def test_stored_tier_field_is_never_trusted(self):
        """The manifest recorded "tier": "A" — QA derives the tier from the
        URL/dated label via ONE matcher; an undated label is "?" and the
        blocker stands. The pre-render gate reuses that exact derivation."""
        self.assertEqual(qa.source_tier("", "Mark, Gudith & Klocke research on interrupted work", POL), "?")
        # single source: qa_supervisor.source_tier delegates to common.source_tier
        import inspect as _inspect
        self.assertIn("common.source_tier(", _inspect.getsource(qa.source_tier))
        for url, label in (("", ""), ("https://doi.org/10.1/x", "study"),
                           ("", "Mark et al. (2008) interrupted work"),
                           ("https://hbr.org/2020/x", "article"),
                           ("https://news.ycombinator.com/item?id=1", "thread")):
            self.assertEqual(qa.source_tier(url, label, POL), common.source_tier(url, label, POL))
        script = issue22_script()
        self.assertEqual(script["sources"][0]["tier"], "A")   # what the manifest recorded
        rep = qa.Report(POL)
        qa.check_sources(rep, script, {}, POL, skip_network=True)
        self.assertIn("[source_quality] claim words ['researchers'] without tier A/B", rep.blocking)

    def test_tier_ab_evidence_present_grounds_the_same_attribution(self):
        """Calendar id 12's OWN curated evidence is dated ("(2008)") → tier A
        through QA's matcher → identical claim wording is grounded and must
        pass pre-render (this is real grounding, not a weakened matcher)."""
        topic = calendar_topic(12, with_evidence=True)
        script = issue22_script(topic=topic, claim=True)
        self.assertIn("Mark, Gudith & Klocke (2008), Cost of interrupted work",
                      json.dumps(script["sources"]))
        gate = qa.pre_render_text_gate(script, topic, POL)
        self.assertEqual(gate["blocking"], [])


# ---------------------------------------------------------------------------
# 2. Every pattern the existing source_quality matcher already knows
# ---------------------------------------------------------------------------
class ClaimWordMatcherCoverage(unittest.TestCase):
    def _script_with(self, line, sources):
        topic = calendar_topic(12, with_evidence=False)
        script = issue22_script(topic=topic, claim=False)
        for ch in script["chunks"]:
            if ch["beat"] == "explain":
                ch["en"][0]["t"] = line
                break
        script["sources"] = copy.deepcopy(sources)
        return script, topic

    def test_every_policy_claim_pattern_blocks_pre_render(self):
        for pattern in CLAIM_PATTERNS:
            with self.subTest(pattern=pattern):
                script, topic = self._script_with(
                    f"Watch what happens next because {pattern} explain this.",
                    copy.deepcopy(MANIFEST_0918_SOURCES))
                blocking = qa.pre_render_text_gate(script, topic, POL)["blocking"]
                self.assertIn(f"[source_quality] claim words ['{pattern}'] without tier A/B", blocking)

    def test_case_variants_and_word_boundaries(self):
        for line, should_block in (
                ("Researchers say each switch costs time.", True),
                ("RESEARCHERS SAY each switch costs time.", True),
                ("the Researchers of the lab measured it", True),
                ("studies show it plainly", True),
                ("Researchership is not what I mean", False),
                ("The provenance of the idea is unclear", False),
                ("Journalism has this problem too", False)):
            with self.subTest(line=line):
                script, topic = self._script_with(line, copy.deepcopy(MANIFEST_0918_SOURCES))
                blocking = qa.pre_render_text_gate(script, topic, POL)["blocking"]
                sq = [b for b in blocking if b.startswith("[source_quality] claim words")]
                if should_block:
                    self.assertTrue(sq, f"expected a claim-word block for {line!r}, got {blocking}")
                else:
                    self.assertEqual(sq, [], f"{line!r} must not match the QA word-boundary matcher")

    def test_tier_urls_ground_and_discovery_does_not(self):
        for url, blocks in (("https://doi.org/10.1/xyz", False),          # tier A domain
                            ("https://arxiv.org/abs/2001.00001", False),  # tier A domain
                            ("https://hbr.org/2020/the-cost", False),      # tier B domain
                            ("https://news.ycombinator.com/item?id=1", True),  # C: discovery only
                            ("", True)):                                   # undated label only
            with self.subTest(url=url):
                srcs = [{"label": "Some interrupted-work study", "url": url}]
                script, topic = self._script_with("Researchers say each switch costs time.", srcs)
                blocking = qa.pre_render_text_gate(script, topic, POL)["blocking"]
                self.assertEqual(bool([b for b in blocking if "claim words" in b]), blocks)

    def test_limited_claims_mode_blocks_any_attribution(self):
        topic = calendar_topic(12, with_evidence=False)
        script = issue22_script(topic=topic, claim=True)
        script["meta"]["evidence_mode"] = "limited-claims"
        blocking = qa.pre_render_text_gate(script, topic, POL)["blocking"]
        self.assertIn("[source_quality] limited-claims mode must not make research claims", blocking)


# ---------------------------------------------------------------------------
# 3. The gate covers all deterministic text blockers — same implementation
# ---------------------------------------------------------------------------
class GateCoverageAndParity(unittest.TestCase):
    def test_gate_calls_the_exact_final_qa_functions(self):
        """Spy-patching the qa_supervisor module proves the gate dispatches to
        the named final-QA check functions themselves (no copies): while the
        spies are installed the GATE behavior changes through the SAME names
        evaluate() uses."""
        names = ["check_content_language", "check_english_only", "check_technology_relevance",
                 "check_metacognition_relevance", "check_topic", "check_sources", "check_script",
                 "check_english", "check_caption_text", "check_reviewer_output"]
        calls = {n: {"count": 0} for n in names}
        originals = {n: getattr(qa, n) for n in names}
        # function identity: the gate and final QA resolve the same objects
        import inspect
        src = inspect.getsource(qa.pre_render_text_gate)
        for n, fn in originals.items():
            def spy(*a, _n=n, **k):
                calls[_n]["count"] += 1
                return originals[_n](*a, **k)
            spy.__name__ = n
            setattr(qa, n, spy)
        try:
            script = issue22_script()
            gate = qa.pre_render_text_gate(script, calendar_topic(12, with_evidence=False), POL,
                                           reviewer_output=approved_review())
            for n in names:
                self.assertEqual(calls[n]["count"], 1, f"{n} must run exactly once in the pre-render gate")
            self.assertIn("[source_quality] claim words ['researchers'] without tier A/B", gate["blocking"])
        finally:
            for n, fn in originals.items():
                setattr(qa, n, fn)
        for n in names:
            self.assertIn(f"{n}(", src)

    def _write_ep(self, tmp, script, topic, reviewer_output=None):
        ep = os.path.join(tmp, "ep")
        os.makedirs(ep, exist_ok=True)
        common.save_json(os.path.join(ep, "script.json"), script)
        common.save_json(os.path.join(ep, "topic.json"), topic)
        cap, tag = caption_mod.build(script)
        cap = caption_mod.fit(cap, tag)
        cap_path = os.path.join(tmp, "caption.txt")
        with open(cap_path, "w", encoding="utf-8") as fh:
            fh.write(cap + "\n\n" + tag + "\n")
        if reviewer_output is not None:
            common.save_json(os.path.join(ep, "reviewer_report.json"),
                             {"model": "x/y", "raw": {}, "output": reviewer_output})
        return ep, cap_path

    def _final_text_blocking(self, ep, cap_path, script, topic):
        """Full supervisor verdict on the SAME artifacts, filtered to the
        text-gate check set — the authoritative parity reference."""
        a = argparse.Namespace(ep=ep, topic=os.path.join(ep, "topic.json"), memory=None,
                               video=None, caption=cap_path, poster=None, poster45=None,
                               public_url="", skip_network=True, no_frames=True,
                               out_json=os.path.join(ep, "_qa.json"), out_md=os.path.join(ep, "_qa.md"))
        result = qa.evaluate(a, POL)
        prefixes = tuple(f"[{c}]" for c in qa.TEXT_QA_GATE_CHECKS)
        return sorted({b for b in result["blocking_errors"] if b.startswith(prefixes)}), result

    def test_parity_blocking_sets_match_final_qa(self):
        topic = calendar_topic(12, with_evidence=False)
        variants = []
        variants.append(("issue #22 verbatim", issue22_script(topic=topic)))
        s = issue22_script(topic=topic, claim=False)
        for ch in s["chunks"]:
            if ch["beat"] == "explain":
                ch["en"][0]["t"] = "Benchmarks show 92% accuracy and 100% certainty in the lab."
        variants.append(("unsupported statistics + banned 100%", s))
        s2 = issue22_script(topic=topic, claim=False)
        s2["chunks"][0]["en"][0]["t"] = "سلام دوست من — این ویدیو را ببین"
        variants.append(("Persian in spoken narration", s2))
        s3 = issue22_script(topic=topic, claim=False)
        s3["chunks"][0]["en"][0]["t"] = "In today's video we explain attention residue."
        variants.append(("banned opener", s3))
        s4 = issue22_script(topic=topic, claim=False)
        s4["sources"] = [{"label": "x", "url": "https://example.com/study"}]
        variants.append(("fake example.com citation", s4))
        s5 = issue22_script(topic=topic, claim=False)
        for ch in s5["chunks"]:
            if ch["beat"] == "explain":
                ch["en"][0]["t"] = "Researchers say TODO ship it now."
        variants.append(("claim word + placeholder", s5))
        s6 = issue22_script(topic=topic)   # clean script + contradicted review below
        review_bad = {"approved": True, "score": 95, "technology_relevance": True,
                      "metacognition_relevance": True, "source_grounding": True,
                      "unsupported_claims": ["researchers cited without evidence"],
                      "hook_quality": "good", "spoken_english_quality": "good", "novelty": "high",
                      "practical_value": "high", "safety": "safe", "required_changes": [],
                      "blocking_errors": []}
        variants.append(("clean script, reviewer unsupported_claims contradiction", s6))
        with tempfile.TemporaryDirectory(prefix="parity_") as tmp:
            for name, script in variants:
                with self.subTest(name):
                    reviewer = review_bad if "contradiction" in name else None
                    ep, cap_path = self._write_ep(tmp + "/" + name.replace(" ", "_").replace("#", ""),
                                                   script, topic, reviewer)
                    final_text, result = self._final_text_blocking(ep, cap_path, script, topic)
                    gate = qa.pre_render_text_gate(script, topic, POL, ep_dir=ep)
                    # same blocker STRINGS (verbatim); ordering across checks may
                    # differ only where final QA repeats a message per line
                    self.assertEqual(sorted(set(gate["blocking"])), final_text,
                                     "pre-render text gate must reproduce the final-QA text blockers verbatim")
                    if "contradiction" in name:
                        self.assertIn("[reviewer_check] reviewer unsupported_claims", " ".join(gate["blocking"]))

    def test_clean_curated_scripts_are_gate_clean(self):
        """Every playbook and trend lens, built from its own calendar topic and
        evaluated by the FULL text gate (caption included), stays clean — the
        gate adds no phantom blockers beyond final QA."""
        for key, pb in cp.PLAYBOOKS.items():
            cal_id = next((c for c, k in cp.CALENDAR_MAP.items() if k == key), 12)
            topic = calendar_topic(cal_id)
            script = cp.build_script_from_playbook(topic, POL, pb, key)
            with self.subTest(playbook=key):
                self.assertEqual(qa.pre_render_text_gate(script, topic, POL)["blocking"], [])
                self.assertEqual(common.pre_render_issues(script, POL), [])

    def test_media_only_checks_stay_in_final_qa(self):
        """Layout geometry, MP4/audio properties, posters and Buffer readiness
        are NOT evaluated pre-render (they need media) — and the full
        supervisor still evaluates them (mandatory, authoritative)."""
        topic = calendar_topic(12, with_evidence=False)
        script = issue22_script(topic=topic, claim=False)
        with tempfile.TemporaryDirectory(prefix="media_only_") as tmp:
            ep, cap_path = self._write_ep(tmp, script, topic, approved_review())
            gate = qa.pre_render_text_gate(script, topic, POL, ep_dir=ep)
            for b in gate["blocking"]:
                self.assertFalse(b.startswith(("[subtitle_layout]", "[audio_quality]",
                                               "[video_quality]", "[buffer_readiness]")),
                                 f"media-only check leaked into the pre-render gate: {b}")
            final_text, result = self._final_text_blocking(ep, cap_path, script, topic)
            joined = " ".join(result["blocking_errors"])
            self.assertIn("MP4 missing", joined)
            self.assertIn("layout.json missing", joined)
            self.assertIn("poster 9x16 missing", joined)
            self.assertEqual(final_text, [])   # text side: clean — media blockers only

    def test_warnings_are_never_promoted_to_blockers(self):
        """Policy-defined warnings (hook style, moderate relevance, few
        contractions...) stay warnings in the gate — requirement 8. The hook
        below is a plain statement: policy warns "not question nor contrast"
        but never blocks; both gate and final QA must agree."""
        topic = calendar_topic(12, with_evidence=False)
        script = issue22_script(topic=topic, claim=False)
        script["chunks"][0]["en"][0]["t"] = "Ten minutes of deep focus vanish with every incoming ping."
        gate = qa.pre_render_text_gate(script, topic, POL)
        self.assertEqual(gate["blocking"], [])
        self.assertTrue(any("hook not question nor contrast" in w for w in gate["warnings"]),
                        f"expected the policy warning, got {gate['warnings']}")
        for w in gate["warnings"]:
            self.assertNotIn(w, gate["blocking"])
        # and the same warning shows up (not escalated) in the final verdict
        with tempfile.TemporaryDirectory(prefix="warn_") as tmp:
            ep, cap_path = self._write_ep(tmp, script, topic, approved_review())
            final_text, result = self._final_text_blocking(ep, cap_path, script, topic)
            self.assertEqual(final_text, [])
            self.assertTrue(any("hook not question nor contrast" in w for w in result["warnings"]))

    def test_thresholds_and_reviewer_bar_unchanged(self):
        self.assertEqual(POL["length"]["narration_words"], [150, 260])
        self.assertEqual(POL["length"]["hard_seconds"], [60, 120])
        self.assertEqual(POL["qa_thresholds"], {"min_score": 85, "min_score_floor": 80,
                                                "blocking_always_blocks": True})
        self.assertEqual(common.min_qa_score(POL), 85)
        self.assertEqual(POL["caption_policy"]["max_chars"], 2200)
        self.assertEqual(POL["hashtag_policy"]["min"], 3)
        self.assertEqual(POL["hashtag_policy"]["max"], 8)
        p = common.pre_render_params(POL)
        self.assertEqual((p["words_min"], p["words_max"]), (150, 260))
        # The reviewer score bar <85 is a deterministic blocker pre-render too
        # (same function as final QA):
        weak = approved_review()
        weak.update({"approved": False, "score": 84})
        gate = qa.pre_render_text_gate(issue22_script(topic=calendar_topic(12, with_evidence=False),
                                                      claim=False),
                                       calendar_topic(12, with_evidence=False), POL,
                                       reviewer_output=weak)
        self.assertIn("[reviewer_check] reviewer not approved (score 84)", gate["blocking"])
        self.assertIn("[reviewer_check] reviewer score 84 < 85", gate["blocking"])


# ---------------------------------------------------------------------------
# 4. Evidence grounding & the producer ladder (bounded correction)
# ---------------------------------------------------------------------------
class ProducerLadder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="text_gate_ladder_")
        self._orig_mem = common.MEMORY_PATH
        common.MEMORY_PATH = os.path.join(self.tmp, "editorial_memory.json")
        self.topic = calendar_topic(12, with_evidence=False)
        topic_path = os.path.join(self.tmp, "topic.json")
        common.save_json(topic_path, self.topic)
        self.topic_path = topic_path
        self.ep = os.path.join(self.tmp, "ep")

    def tearDown(self):
        common.MEMORY_PATH = self._orig_mem
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
        script = common.load_json(os.path.join(self.ep, "script.json")) if exit_code == 0 else None
        return script, calls, exit_code

    def test_claim_word_first_output_consumes_the_one_revision_and_repairs(self):
        script, calls, code = self._run_main(
            produces=[(output_with_claim_word(), {}), (output_without_attribution(), {})],
            reviews=[(approved_review(), {}), (approved_review(), {})])
        self.assertEqual(code, 0)
        self.assertEqual(calls["produce"], 2, "exactly the ONE allowed revision")
        self.assertEqual(calls["review"], 2, "Reviewer + final Reviewer — the capped chain")
        self.assertEqual(script["meta"]["generation_mode"], "groq")
        text = " ".join(common.narration_lines(script)).lower()
        self.assertNotIn("researchers", text)
        self.assertEqual(qa.pre_render_text_gate(script, self.topic, POL)["blocking"], [])
        # the revision request carries SAFE STRUCTURED codes — not raw web text
        req = " ".join(calls["packets"][1].get("revision_request") or [])
        self.assertIn("evidence blocker (deterministic pre-render text QA", req)
        self.assertIn("REMOVE the attribution", req)
        self.assertIn("appropriately qualified observation", req)
        self.assertIn("citation", req)
        self.assertNotIn(CLAIM_LINE, req, "the narration line itself must not be echoed to the revision")

    def test_grounded_attribution_from_the_packet_needs_no_revision(self):
        """Tier A evidence IN the packet (dated calendar label) grounds the same
        wording — merged verbatim into the script's sources — and ships on the
        first pass without any revision."""
        self.topic = calendar_topic(12, with_evidence=True)
        common.save_json(self.topic_path, self.topic)
        out = output_with_claim_word()
        out["sources"] = []   # the model adds nothing; grounding comes from the packet
        script, calls, code = self._run_main(produces=[(out, {})], reviews=[(approved_review(), {})])
        self.assertEqual(code, 0)
        self.assertEqual(calls["produce"], 1, "grounded claim wording must not burn the revision")
        self.assertEqual(script["meta"]["generation_mode"], "groq")
        self.assertIn("Mark, Gudith & Klocke (2008), Cost of interrupted work",
                      json.dumps(script["sources"]))
        self.assertTrue(qa.pre_render_text_gate(script, self.topic, POL)["warnings"] is not None)
        self.assertEqual(qa.pre_render_text_gate(script, self.topic, POL)["blocking"], [])

    def test_llm_script_sources_are_derived_never_claimed(self):
        out = output_without_attribution()
        out["sources"] = [{"label": "Very serious study", "url": "", "tier": "A"}]  # a claimed tier
        script = cp.build_script_from_llm(self.topic, POL, out)
        self.assertEqual(script["sources"][-1]["tier"], "?",
                         "stored tiers are re-derived with QA's matcher, never trusted")

    def test_revision_that_invents_a_citation_is_rejected(self):
        bad_revision = output_without_attribution()
        bad_revision["sources"] = [{"label": "Mark et al.", "url": "https://arxiv.org/abs/2401.00001",
                                    "tier": "A"}]
        script, calls, code = self._run_main(
            produces=[(output_with_claim_word(), {}), (bad_revision, {})],
            reviews=[(approved_review(), {}), (approved_review(), {})])
        self.assertEqual(code, 0)
        self.assertEqual(script["meta"]["generation_mode"], "static-fallback",
                         "invented citations cannot be the repair — the validated fallback decides")
        self.assertEqual(calls["produce"], 2)
        blob = json.dumps(script)
        self.assertNotIn("2401.00001", blob)
        self.assertNotIn("arxiv", blob.lower())
        rep = common.load_json(os.path.join(self.ep, "producer_report.json"), {})
        # the report must honestly say the groq result was REJECTED and the
        # fallback was built ("groq-rejected" after the honesty gate, or
        # "static-fallback" if it never claimed groq for this artifact)
        self.assertIn(rep["mode"], ("groq-rejected", "static-fallback"))
        self.assertEqual(script["meta"]["generation_mode"], "static-fallback")

    def test_first_output_invented_citation_is_flagged_to_the_revision(self):
        out = output_without_attribution()
        out["sources"] = [{"label": "Made-up paper", "url": "https://doi.org/10.5555/fake.2026"}]
        script, calls, code = self._run_main(produces=[(out, {}), (output_without_attribution(), {})],
                                             reviews=[(approved_review(), {}), (approved_review(), {})])
        self.assertEqual(code, 0)
        req = " ".join(calls["packets"][1].get("revision_request") or [])
        self.assertIn("remove the source URL", req)
        self.assertIn("never invent or guess URLs", req)

    def test_revision_still_blocked_falls_back_to_the_validated_static_script(self):
        stubborn = output_with_claim_word()
        script, calls, code = self._run_main(
            produces=[(stubborn, {}), (stubborn, {})],
            reviews=[(approved_review(), {}), (approved_review(), {})])
        self.assertEqual(code, 0)
        self.assertEqual(calls["produce"], 2, "no second revision exists")
        self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
        text = " ".join(common.narration_lines(script)).lower()
        self.assertNotIn("researchers", text)
        self.assertEqual(qa.pre_render_text_gate(script, self.topic, POL)["blocking"], [])
        self.assertEqual(script["meta"]["playbook"], "context-switching")

    def test_blocked_fallback_skips_before_tts(self):
        blocked_fallback = issue22_script(topic=self.topic)
        orig = cp.build_script_from_playbook
        try:
            with mock.patch.object(cp, "build_script_from_playbook",
                                   return_value=blocked_fallback):
                script, calls, code = self._run_main(
                    produces=[(output_with_claim_word(), {}), (output_with_claim_word(), {})],
                    reviews=[(approved_review(), {}), (approved_review(), {})])
        finally:
            cp.build_script_from_playbook = orig
        self.assertEqual(code, 3, "a text-blocked fallback must skip before any media stage")
        self.assertIsNone(script)
        self.assertFalse(os.path.exists(os.path.join(self.ep, "script.json")),
                         "no script.json may exist for a gate-skipped producer run")
        rep = common.load_json(os.path.join(self.ep, "producer_report.json"), {})
        self.assertIn("[source_quality] claim words ['researchers'] without tier A/B",
                      rep["gate"]["text_blocking"])

    def test_reviewer_structured_contradiction_blocks_adoption(self):
        """approved:true + a non-empty unsupported_claims list is a structured
        contradiction: the revision is NOT adopted (final QA would block the
        same list — burning a render for it is exactly what must not happen)."""
        contradicting = approved_review()
        contradicting["unsupported_claims"] = ["researchers cited without evidence"]
        script, calls, code = self._run_main(
            produces=[(output_with_claim_word(), {}), (output_without_attribution(), {})],
            reviews=[(approved_review(), {}), (contradicting, {})])
        self.assertEqual(code, 0)
        self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
        self.assertEqual(calls["produce"], 2)

    def test_first_pass_structured_contradiction_triggers_revision(self):
        contradicting = approved_review()
        contradicting["blocking_errors"] = ["ungrounded attribution"]
        script, calls, code = self._run_main(
            produces=[(output_without_attribution(), {}), (output_without_attribution(), {})],
            reviews=[(contradicting, {}), (approved_review(), {})])
        self.assertEqual(code, 0)
        self.assertEqual(calls["produce"], 2, "the reviewer's own blocking list must trigger the one revision")
        self.assertEqual(script["meta"]["generation_mode"], "groq")


# ---------------------------------------------------------------------------
# 5. Prompts & packets: evidence-grounded instructions, single source
# ---------------------------------------------------------------------------
class PromptGrounding(unittest.TestCase):
    def _capture(self, packet, who):
        captured = {}

        def fake_chat(prompt, model, max_tokens=1200, temperature=0.7, timeout=None):
            captured["prompt"] = prompt
            payload = (llm_provider.StaticEnglishFallback().produce({"topic": "x", "technology_angle": "y"})
                       if who == "producer" else approved_review())
            return json.dumps(payload), {"model": model}

        with mock.patch.dict(os.environ, {"GROQ_API_KEY": FAKE_KEY}, clear=False):
            with mock.patch.object(llm_provider, "call_groq_chat", fake_chat):
                if who == "producer":
                    llm_provider.GroqProducer().produce(packet, _discovered=[PROD_CANDIDATE])
                else:
                    llm_provider.GroqReviewer().review({"technology_angle": "x",
                                                        "metacognition_concept": "y"},
                                                       packet, _discovered=[PROD_CANDIDATE])
        return captured["prompt"]

    def test_producer_prompt_forbids_claim_words_without_evidence(self):
        packet = llm_provider.build_evidence_packet(calendar_topic(12, with_evidence=False), POL)
        self.assertFalse(packet["has_tier_ab_evidence"])
        self.assertEqual(packet["evidence_tier"], "?")
        prompt = self._capture(packet, "producer")
        self.assertIn("NO Tier A/B evidence", prompt)
        for pattern in CLAIM_PATTERNS:
            self.assertIn(f"\u201c{pattern}\u201d", prompt,
                          f"the injected list must be the policy's own patterns ({pattern!r})")
        self.assertIn("direct, appropriately qualified observation", prompt)
        self.assertIn("Do NOT invent or guess URLs", prompt)
        self.assertIn('leave source "url" empty unless the packet provides it', prompt)

    def test_producer_prompt_grounds_attribution_when_evidence_exists(self):
        packet = llm_provider.build_evidence_packet(calendar_topic(12, with_evidence=True), POL)
        self.assertTrue(packet["has_tier_ab_evidence"])
        self.assertEqual(packet["evidence_tier"], "A")
        prompt = self._capture(packet, "producer")
        self.assertIn("DERIVABLY carries Tier A evidence", prompt)
        self.assertIn("Mark, Gudith & Klocke (2008), Cost of interrupted work", prompt)

    def test_reviewer_prompt_refuses_to_approve_ungrounded_attribution(self):
        packet = llm_provider.build_evidence_packet(calendar_topic(12, with_evidence=False), POL)
        prompt = self._capture(packet, "reviewer")
        self.assertIn("CLAIM ATTRIBUTIONS (blocker", prompt)
        self.assertIn("MUST NOT approve", prompt)
        self.assertIn("NO Tier A/B evidence", prompt)
        self.assertIn("\u201cresearchers\u201d", prompt)

    def test_no_second_keyword_list_and_no_invented_patterns(self):
        src = open(os.path.join(ROOT, "build", "llm_provider.py"), encoding="utf-8").read()
        self.assertNotIn("researchers studying LLM uncertainty", src,
                         "the old example line instructed the exact blocked pattern")
        self.assertNotIn("experts say", src,
                         "'experts say' is not a source_quality pattern — it must not become one")
        prod_src = open(os.path.join(ROOT, "build", "content_producer.py"), encoding="utf-8").read()
        self.assertNotIn("experts say", prod_src)
        # both prompts read the policy list; nothing hard-codes its own claim list
        self.assertIn("require_evidence_for_claim_words", src)
        self.assertIn("claim_word_patterns", src)

    def test_revision_prompt_carries_the_attribution_fix_rule(self):
        packet = llm_provider.build_evidence_packet(calendar_topic(12, with_evidence=False), POL)
        packet["revision_request"] = ["evidence blocker (deterministic pre-render text QA): claim words ..."]
        prompt = self._capture(packet, "producer")
        self.assertIn("Attribution fix rule", prompt)
        self.assertIn("REMOVE the attribution", prompt)
        self.assertIn("NEVER keep a number by adding, adjusting or inventing a citation/URL", prompt)
        self.assertIn("the packet provides NO source urls", prompt)


# ---------------------------------------------------------------------------
# 6. Pipeline: zero media-stage calls for an unrepaired claim-word blocker
# ---------------------------------------------------------------------------
class PipelineZeroMedia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pipeline_textgate_")
        self._orig_root, self._orig_content = common.ROOT, common.CONTENT
        self._orig_mem = common.MEMORY_PATH
        common.ROOT = self.tmp
        common.CONTENT = os.path.join(self.tmp, "content")
        common.MEMORY_PATH = os.path.join(self.tmp, "content", "editorial_memory.json")
        os.makedirs(common.CONTENT, exist_ok=True)
        # a real checkout carries the repo brand assets — the pre-render
        # visual plan's provenance check requires them to exist
        _stage_repo_assets(self.tmp)
        self.tag = "2077-02-02"
        self.ep = common.episode_dir(self.tag)
        os.makedirs(self.ep, exist_ok=True)
        self.topic = calendar_topic(12, with_evidence=False, date=self.tag)
        self.commands = []
        self.qa_calls = []

    def tearDown(self):
        common.ROOT, common.CONTENT = self._orig_root, self._orig_content
        common.MEMORY_PATH = self._orig_mem
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_run_factory(self, script_by_pass):
        def fake_run(cmd, stage, env=None, timeout=1800):
            self.commands.append((stage, " ".join(str(c) for c in cmd)))
            if stage == "trend":
                common.save_json(os.path.join(self.ep, "topic.json"), self.topic)
            elif stage == "script":
                i = min(sum(1 for s, _ in self.commands if s == "script") - 1, len(script_by_pass) - 1)
                common.save_json(os.path.join(self.ep, "script.json"), script_by_pass[i])
            return ""
        return fake_run

    def _produce(self, scripts, reviewer_out=None):
        if reviewer_out is not None:
            common.save_json(os.path.join(self.ep, "reviewer_report.json"),
                             {"model": "x/y", "raw": {}, "output": reviewer_out})

        def fake_qa(cmd, stage="qa"):
            self.qa_calls.append(stage)
            return False

        a = argparse.Namespace(tag=self.tag, fixture=None, calendar_only=True,
                               synthetic_tts=False, skip_network=True, dry_run=True)
        with mock.patch.object(pl, "run", self._fake_run_factory(scripts)), \
             mock.patch.object(pl, "run_qa", fake_qa):
            rc = pl.produce(a)
        return rc

    def test_unrepaired_claim_word_skips_before_every_media_stage(self):
        blocked = issue22_script(topic=self.topic)
        rc = self._produce([blocked, blocked], reviewer_out=approved_review())
        self.assertEqual(rc, 10)
        stages = [s for s, _ in self.commands]
        self.assertEqual(stages, ["trend", "script", "script"],
                         f"only the producer stage + its ONE retry may run, got: {self.commands}")
        joined = " ".join(c for _, c in self.commands)
        for media in ("tts_edge.py", "tts_synthetic.py", "timing.py", "caption.py",
                      "render_auto.py", "poster_auto.py", "ffmpeg", "ffprobe"):
            self.assertNotIn(media, joined, f"{media} must never run for a text-blocked script")
        self.assertEqual(self.qa_calls, [], "final QA must not evaluate a pre-render-skipped script")
        paths = common.output_paths(self.tag)
        for key in ("mp4", "caption", "poster", "poster_4x5", "qa_json", "marker"):
            self.assertFalse(os.path.exists(paths[key]), f"{key} must not exist after a pre-render skip")
        st = pl.load_state(self.tag)
        self.assertEqual(st["status"], "qa-failed")
        self.assertEqual(st["stage"], "qa")
        self.assertEqual(st["retries"]["script"], 1, "bounded: the existing single script retry")
        self.assertIn("[source_quality] claim words ['researchers'] without tier A/B", st["error"])
        self.assertIn("skipped before TTS/render (no media, no Buffer)", st["error"])
        # and the record cascade keeps it qa-failed (never a Buffer relabel)
        pl.record(argparse.Namespace(tag=self.tag, status="approved-dry-run", error=None))
        manifest = common.load_json(paths["manifest"], {})
        self.assertEqual(manifest["status"], "qa-failed")

    def test_repaired_on_the_retry_proceeds_to_media_once(self):
        blocked = issue22_script(topic=self.topic)
        fixed = issue22_script(topic=self.topic, claim=False)
        run_orig = self._fake_run_factory([blocked, fixed])

        def run2(cmd, stage, env=None, timeout=1800):
            run_orig(cmd, stage, env=env, timeout=timeout)
            if stage == "tts":
                raise pl.Stage("tts", "test stop after the text gate let the repaired script through")

        a = argparse.Namespace(tag=self.tag, fixture=None, calendar_only=True,
                               synthetic_tts=False, skip_network=True, dry_run=True)
        with mock.patch.object(pl, "run", run2), \
             mock.patch.object(pl, "run_qa", lambda cmd, stage="qa": False):
            rc = pl.produce(a)
        self.assertEqual(rc, 10)
        stages = [s for s, _ in self.commands]
        self.assertEqual(stages, ["trend", "script", "script", "tts"],
                         "one producer retry, then the repaired script proceeds to media")
        st = pl.load_state(self.tag)
        self.assertNotIn("pre-render", st.get("error", ""))
        self.assertEqual(st["gate"]["ok"], True)
        self.assertEqual(st["gate"]["text_blocking"], [])

    def test_no_buffer_publish_import_or_media_mutation_from_the_gate_path(self):
        src = open(os.path.join(ROOT, "build", "pipeline.py"), encoding="utf-8").read()
        prod = open(os.path.join(ROOT, "build", "content_producer.py"), encoding="utf-8").read()
        for needle in ("buffer_publish", "insta_publish", "AUTO_PUBLISH_ENABLED"):
            self.assertNotIn(needle, prod, "the producer must never touch publishing paths")
        # the pipeline only records Buffer OUTCOME states relayed by the
        # workflow; it never invokes the Buffer mutation path itself
        self.assertNotIn("buffer_publish", src)
        self.assertNotIn("insta_publish", src)


# ---------------------------------------------------------------------------
# 7. Safety invariants that must survive every gate change
# ---------------------------------------------------------------------------
class SafetyInvariants(unittest.TestCase):
    def test_quarantine_of_reel_2026_09_15_is_permanent_and_final(self):
        self.assertTrue(common.is_quarantined("reel-2026-09-15", "2026-09-15"))
        q = common.load_json(os.path.join(ROOT, "content", "quarantine.json"), {})
        self.assertIn("reel-2026-09-15", q["quarantined_content_ids"])
        # quarantine stays in the FULL supervisor (evaluate), where memory and
        # publish-time state matter — verified through evaluate itself:
        with tempfile.TemporaryDirectory(prefix="quarantine_") as tmp:
            script = issue22_script(topic=calendar_topic(12, with_evidence=False), claim=False)
            script["meta"]["content_id"] = "reel-2026-09-15"
            script["meta"]["content_date"] = "2026-09-15"
            ep = os.path.join(tmp, "ep")
            os.makedirs(ep)
            common.save_json(os.path.join(ep, "script.json"), script)
            a = argparse.Namespace(ep=ep, topic=None, memory=None, video=None, caption=None,
                                   poster=None, poster45=None, public_url="", skip_network=True,
                                   no_frames=True, out_json=os.path.join(tmp, "q.json"),
                                   out_md=os.path.join(tmp, "q.md"))
            result = qa.evaluate(a, POL)
            self.assertIn("reel-2026-09-15", " ".join(result["blocking_errors"]))
            self.assertFalse(result["approved"])

    def test_rejected_reels_are_not_reused_or_republished(self):
        """The safety requirement WITHOUT the mutable production file.

        This test used to assert the CURRENT status of the generated
        ``output/auto-2026-09-18_manifest.json`` — a file every pipeline run
        rewrites. A later legitimate run changed that record to
        ``approved-dry-run`` and the daily workflow's self-test step failed
        before production (issue #26 §1). The deterministic, sandboxed proof
        that a rejected artifact can never be promoted or published now lives in
        ``tests/test_rejected_manifest_isolation.py`` (own TemporaryDirectory
        fixtures, real ``pipeline.record``, no real file touched). What stays
        here is the memory-side invariant, which is about RECORDED history and
        is read from the curated, tracked editorial memory only.
        """
        mem = common.load_memory()
        by_id = {e.get("content_id"): e for e in mem.get("entries", [])}
        for cid in ("reel-2026-09-16", "reel-2026-09-17", "reel-2026-09-18"):
            self.assertIn(cid, by_id, cid)
            entry = by_id[cid]
            self.assertNotIn(entry.get("buffer_post_id"), ("mockpost",), cid)
            # a RECORDED non-approval can never carry a Buffer outcome status —
            # no matter how many later runs rewrite the entry (the previous
            # version of this test pinned a mutable generated FILE instead and
            # broke the daily self-test when a later approved run rewrote it)
            if entry.get("qa_approved") is False:
                self.assertIn(entry.get("status"), pl.FAILURE_STATES,
                              f"{cid}: a rejected artifact can never be relabelled as published")
        # no generated output artifact is read here any more: the sandboxed
        # proof lives in tests/test_rejected_manifest_isolation.py

    def test_no_mock_or_persian_path_or_key_rotation_restored(self):
        for rel in ("build/llm_provider.py", "build/content_producer.py", "build/pipeline.py"):
            src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            self.assertNotIn("deep-translator", src)
            self.assertNotIn("mymemory", src.lower().replace("translation_engine", ""))
            self.assertNotIn("github_models", src.lower())
        # mock stays test-only and fail-closed in the daily path
        wf = open(os.path.join(ROOT, ".github", "workflows", "daily-trend-draft.yml"),
                  encoding="utf-8").read()
        self.assertIn("forbid mock LLM in production (fail closed)", wf)
        self.assertIn('if [ "${MOCK_GROQ:-}" = "1" ]; then', wf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
