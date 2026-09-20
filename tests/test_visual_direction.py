"""Topic-aware visual direction — deterministic fix for issue #24 (run
35058904480 / reel-2026-09-19, PRODUCT pillar, user interviews / confirmation
bias).

The exact history: the static-fallback Producer set
``visual_direction = technology_angle + " + code visual + ..."``, the old
renderer saw the substring "code" and drew a generic animated
``def solve(): … result = incomplete()`` terminal card with a moving typing
cursor on a PRODUCT reel; the imagery was three repeated stock images; the
final QA scored 100/100 because it only measured subtitle-band contrast on
background-only frames; and the GitHub report carried a legacy
"Subtitles EN ↔ FA" table.

Guarantees this module pins down (issue #24 spec):

  1. the 0919 fixture produces a clean, pillar-correct scene plan: NO code
     scene, 6-10+ distinct compositions, brand moments at hook/ending, and a
     passing pre-render visual-semantic gate;
  2. rendered frames of that plan contain no cold code card, no typing
     cursor, no cold (blue/navy/cyan/purple) hue, and adjacent scenes are not
     perceptual near-duplicates; the renderer re-runs the SAME gate on load
     and refuses a tampered plan;
  3. per-pillar category allowlists: code/terminal categories are allowed
     only for coding pillars; a code scene requires a narration that
     genuinely discusses coding; a cursor requires demonstrated code entry;
  4. provenance: external images need https URLs, an allowed license
     (CC0/PDM — CC-BY is disabled, see item 9), retrieval date, sanitized
     query and a content hash; the $0 Openverse fetcher draws ONLY the
     attribution-free public-domain set (CC0/PDM) and fails soft to None;
  5. variety: a repeated underlying image is not a new scene; one asset may
     not dominate; too few scenes is blocked;
  6. palette: the declared brand palette must stay warm (no blue family);
     the photo grade and the frame QA detect cold hues;
  7. text-heavy slides and subtitle-band obstruction are blocked;
  8. the pipeline consults the visual gate BEFORE TTS/render and fails closed
     with zero media-stage calls (one bounded producer retry, like the text
     gate);
  9. license/attribution safety: CC-BY is disabled end-to-end (fetcher,
     gate, renderer) because an internal manifest credit is not public
     Instagram attribution — a CC-BY asset can never reach publication;
 10. the balanced production mix: a normal 60-120 s reel EXPLICITLY plans
     2-3 photo-designated scenes (distinct topic-relevant CC0/PDM photos
     when the $0 source is safely available, else a DISTINCT procedural
     visual per scene), the remaining content scenes as procedural
     diagrams, and brand assets only at hook/ending — no repository hero
     image is ever a scene background, and the source is bounded (one
     attempt, strict timeout, no retries) and enabled by default in the
     GitHub Actions production path;
 11. brand treatment: retrieved photographs are warm-graded into the
     matte-black/charcoal/gold/amber/bronze/ivory theme while staying
     recognizably different from each other; a blue-dominant source cannot
     leave a blue-dominant final frame;
 12. QA weight invariance: the score stays normalized 0-100, a passing
     visual_semantics check adds zero deduction (an 81 stays 81, never
     becomes >=85), blockers veto regardless of score, and the reviewer
     >=85 bar is unchanged;
 13. the GitHub report is English-only ("English subtitles — synchronized,
     LTR, safe-zone validated"); the bilingual table is gone;
 14. root-cause regression: no "code visual" string survives in the producer
     or LLM provider; an LLM's free-text visual request is recorded for the
     record but can never override the deterministic direction.
"""
import argparse
import copy
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import asset_fetch as af  # noqa: E402
import content_producer as cp  # noqa: E402
import llm_provider  # noqa: E402
import pipeline as pl  # noqa: E402
import qa_supervisor as qa  # noqa: E402
import report_issue  # noqa: E402
import visual_plan as vp  # noqa: E402

POL = common.policy()
W, H = 1080, 1920


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


def topic_0919():
    """The exact reel-2026-09-19 topic (calendar id 14, PRODUCT pillar)."""
    return calendar_topic(14, date="2026-09-19", with_evidence=True)


def script_0919():
    topic = topic_0919()
    pb = cp.PLAYBOOKS["confirmation-bias-research"]
    return cp.build_script_from_playbook(topic, POL, pb, "confirmation-bias-research"), topic


def make_timing(script, out_dir):
    """Deterministic word-level timing (the same shape timing.py emits)."""
    t = 0.0
    chunks = []
    for ch in script["chunks"]:
        lines = []
        for en in ch["en"]:
            words, lstart = [], t
            for w in en["t"].split():
                words.append({"w": w, "start": round(t, 2), "end": round(t + 0.4, 2)})
                t += 0.4
            t += 0.34
            lines.append({"text": en["t"], "scene": en["scene"], "beat": en["beat"],
                          "start": round(lstart, 2), "end": round(t - 0.34, 2), "words": words})
        chunks.append({"id": ch["id"], "beat": ch["beat"], "lines": lines, "dur": 1.0})
    total = round(t + 0.5, 2)
    common.save_json(os.path.join(out_dir, "timing.json"), {"total": total, "chunks": chunks})
    return total


def silent_wav(out_dir, seconds):
    with wave.open(os.path.join(out_dir, "full.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        n = int(seconds * 44100)
        w.writeframes(struct.pack("<%dh" % n, *([0] * n)))


def episode_0919():
    """The full 0919 episode dir: script + plan + timing + audio."""
    d = tempfile.mkdtemp(prefix="ep0919_")
    script, topic = script_0919()
    common.save_json(os.path.join(d, "script.json"), script)
    plan = vp.build_visual_plan(script, POL, allow_external=False)
    common.save_json(os.path.join(d, "visual_plan.json"), plan)
    make_timing(script, d)
    silent_wav(d, 90)
    return d, script, topic, plan


def scene_for_mutation(plan, sid="s2"):
    """Deep-copied plan with the given scene isolated for mutation tests."""
    p = copy.deepcopy(plan)
    return p, next(sc for sc in p["scenes"] if sc["scene_id"] == sid)


# ---------------------------------------------------------------------------
# 1. The exact 0919 regression: plan shape, pillar correctness, clean frames
# ---------------------------------------------------------------------------
class Issue24_0919_Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ep, cls.script, cls.topic, cls.plan = episode_0919()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.ep, ignore_errors=True)

    def test_plan_is_product_correct_and_code_free(self):
        plan = self.plan
        self.assertEqual(plan["pillar"], "PRODUCT",
                         "the 0919 topic is a PRODUCT-pillar user-research reel")
        cats = [sc["visual_category"] for sc in plan["scenes"]]
        for sc in plan["scenes"]:
            self.assertNotIn(sc["visual_category"], vp.CODE_CATEGORIES,
                             f"{sc['scene_id']}: a code scene on a PRODUCT reel "
                             "is exactly the issue-#24 failure")
            self.assertFalse(sc["code_justified"])
            self.assertFalse(sc["cursor_justified"])
        self.assertIn("code-scene", vp.PILLAR_CATEGORIES["CODING"],
                      "coding pillars keep their code categories")
        self.assertNotIn("code-scene", vp.allowed_categories("PRODUCT"))
        # brand moments at the ends, everything else topic-specific
        self.assertEqual(plan["scenes"][0]["visual_category"], "brand-mark")
        self.assertEqual(plan["scenes"][-1]["visual_category"], "brand-close")
        self.assertEqual(plan["scenes"][0]["asset"]["id"], f"repo:{vp.REPO_ASSETS['emblem']}")
        self.assertEqual(plan["scenes"][-1]["asset"]["id"], f"repo:{vp.REPO_ASSETS['eye']}")

    def test_plan_has_enough_distinct_scenes_and_no_reused_image(self):
        scenes = self.plan["scenes"]
        self.assertGreaterEqual(len(scenes), 8, "a 60-120s reel needs a rich storyboard")
        self.assertLessEqual(len(scenes), 12)
        primary = [sc for sc in scenes if sc["visual_category"] not in vp.BRAND_CATEGORIES]
        ids = [sc["asset"]["id"] for sc in primary]
        self.assertEqual(len(ids), len(set(ids)),
                         "no repeated underlying image — the 0919 stock-image failure")
        for a, b in zip(scenes, scenes[1:]):
            self.assertNotEqual(a["asset"]["id"], b["asset"]["id"],
                                "no consecutive repeat of the same image")
        durs = [sc["expected_duration"] for sc in scenes]
        for d in durs:
            self.assertGreater(d, 2.0)
            self.assertLessEqual(d, vp.MAX_SCENE_SECONDS)

    def test_pre_render_visual_gate_passes_0919_plan(self):
        issues = vp.visual_semantic_issues(self.plan, self.script, POL)
        self.assertEqual(issues, [], f"the 0919 plan must pass its own gate: {issues}")
        summary = vp.plan_summary(self.plan)
        self.assertEqual(summary["code_scenes"], [])
        self.assertEqual(summary["external"], 0)

    def test_script_visual_direction_has_no_code_and_is_pillar_specific(self):
        self.assertNotIn("code visual", self.script["visual_direction"].lower(),
                         "the root cause: a 'code visual' string for every pillar")
        self.assertEqual(self.script["visual_direction"],
                         vp.pillar_visual_direction("PRODUCT"))
        visuals = self.script["visuals"]
        self.assertIsInstance(visuals, list)
        for cat in visuals:
            self.assertIn(cat, vp.allowed_categories("PRODUCT"))

    def test_rendered_frames_are_clean(self):
        from reel_engine import Reel
        import numpy as np
        reel = Reel(self.ep, POL)
        self.assertTrue(reel.plan, "a chunked script always renders through the plan")
        self.assertEqual(reel.code_scenes_rendered, [])
        hashes = []
        for sc, a, b in reel.scene_times:
            t = (a + b) / 2
            arr = np.array(reel.frame(t).convert("RGB"), dtype=np.int16)
            self.assertLess(qa.detect_code_card(arr), qa.COLD_CARD_PX,
                            f"{sc['scene_id']}: rendered frame contains a cold code card")
            self.assertFalse(qa.detect_cursor(arr),
                             f"{sc['scene_id']}: rendered frame contains a typing cursor")
            h = arr.shape[0]
            band = arr[int(h * 0.25):int(h * 0.85), 40:arr.shape[1] - 40]
            r, g, bb = band[..., 0], band[..., 1], band[..., 2]
            cold_frac = float(((bb > r + 4) & (bb >= g - 6)).mean())
            self.assertLess(cold_frac, 0.002,
                            f"{sc['scene_id']}: cold (blue/navy/cyan/purple) hue in a brand frame")
            from PIL import Image
            hashes.append(vp.perceptual_hash(Image.fromarray(
                np.clip(arr, 0, 255).astype(np.uint8), "RGB")))
        # scene changes: adjacent scenes are not near-duplicates
        for i in range(len(hashes) - 1):
            d = vp.hamming(hashes[i], hashes[i + 1])
            self.assertGreater(d, 14,
                               f"scenes {i + 1}/{i + 2} are near-duplicates (aHash {d}/256) — "
                               "a zoom-crop of one image is not a new scene")

    def test_renderer_refuses_a_tampered_plan(self):
        import numpy as np
        bad = copy.deepcopy(self.plan)
        sc = bad["scenes"][1]
        sc["visual_category"] = "stack-trace"      # a code category on PRODUCT
        sc["code_justified"] = False
        d2 = tempfile.mkdtemp(prefix="eptamper_")
        try:
            for f in os.listdir(self.ep):
                shutil.copy(os.path.join(self.ep, f), os.path.join(d2, f))
            common.save_json(os.path.join(d2, "visual_plan.json"), bad)
            from reel_engine import Reel
            with self.assertRaises(RuntimeError) as ctx:
                Reel(d2, POL)
            self.assertIn("visual plan blocked before render", str(ctx.exception))
        finally:
            shutil.rmtree(d2, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. Per-pillar allowlists, code and cursor justification
# ---------------------------------------------------------------------------
class PillarAllowlists(unittest.TestCase):
    def test_every_playbook_plan_obeys_its_pillar(self):
        for key, pb in cp.PLAYBOOKS.items():
            topic = {"content_date": "2077-01-01", "content_id": "reel-test",
                     "title": pb["technology_angle"], "normalized_topic": "",
                     "pillar": pb["pillar"], "technology_angle": pb["technology_angle"],
                     "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
                     "evidence_mode": "calendar",
                     "calendar": {"id": 1, "title": pb["technology_angle"], "sources": []}}
            script = cp.build_script_from_playbook(topic, POL, pb, key)
            plan = vp.build_visual_plan(script, POL, allow_external=False)
            for sc in plan["scenes"]:
                self.assertIn(sc["visual_category"], vp.allowed_categories(pb["pillar"]),
                              f"{key}/{sc['scene_id']}: category {sc['visual_category']} "
                              f"outside pillar {pb['pillar']}")
            issues = vp.visual_semantic_issues(plan, script, POL)
            self.assertEqual(issues, [], f"{key}: {issues}")

    def test_code_category_blocked_on_product(self):
        plan, sc = scene_for_mutation(*episode_0919()[:2:2] and (vp.build_visual_plan(
            script_0919()[0], POL, allow_external=False), "s2"))
        script = script_0919()[0]
        sc["visual_category"] = "stack-trace"
        sc["code_justified"] = False
        issues = vp.visual_semantic_issues(plan, script, POL)
        self.assertTrue(any("generic code card is never allowed" in i for i in issues), issues)
        self.assertTrue(any("not allowed for pillar PRODUCT" in i for i in issues), issues)

    def test_code_scene_allowed_on_coding_with_genuine_narration(self):
        script = self._coding_script("Try this: before you run the code, read the "
                                     "stack trace and write the unit test first.")
        plan = vp.build_visual_plan(script, POL, allow_external=False)
        code_scenes = [sc for sc in plan["scenes"] if sc["visual_category"] in vp.CODE_CATEGORIES]
        self.assertTrue(code_scenes, "a coding reel with coding narration gets code scenes")
        for sc in code_scenes:
            self.assertTrue(sc["code_justified"])
            if sc["visual_category"] == "code-scene":
                self.assertTrue(vp.snippet_is_clean(sc["code_lines"]))
        issues = vp.visual_semantic_issues(plan, script, POL)
        self.assertEqual([i for i in issues if "code" in i.lower()], [], issues)

    def test_code_flag_without_coding_narration_is_blocked(self):
        script = self._coding_script("the product launch felt warm")
        plan = vp.build_visual_plan(script, POL, allow_external=False)
        sc = next(s for s in plan["scenes"]
                  if s["visual_category"] in vp.CODE_CATEGORIES)
        # a hand-tampered plan: the segment narration does NOT discuss coding,
        # yet the plan claims the code visual is justified
        sc["narration"] = "the product launch felt warm"
        sc["code_justified"] = True
        issues = vp.visual_semantic_issues(plan, script, POL)
        self.assertTrue(any("narration does not genuinely" in i for i in issues), issues)

    def test_cursor_requires_justified_code_scene(self):
        plan = vp.build_visual_plan(self._coding_script("watch me typing the fix"),
                                    POL, allow_external=False)
        sc = plan["scenes"][1]
        sc["cursor_justified"] = True  # on a non-code category → block
        script = self._coding_script("watch me typing the fix")
        issues = vp.visual_semantic_issues(plan, script, POL)
        self.assertTrue(any("cursor without a justified code scene" in i for i in issues), issues)

    def test_justification_helpers_are_deterministic(self):
        self.assertTrue(vp.code_justified("CODING", "we debug the traceback"))
        self.assertFalse(vp.code_justified("PRODUCT", "we debug the user journey"))
        self.assertTrue(vp.cursor_justified("CODING", "watch me typing the terminal command"))
        self.assertFalse(vp.cursor_justified("CODING", "we read the code"))
        self.assertFalse(vp.cursor_justified("PRODUCT", "typing user notes"))

    def _coding_script(self, technique_line):
        pb = cp.PLAYBOOKS["metacognition-debugging"]
        topic = {"content_date": "2077-01-01", "content_id": "reel-test",
                 "title": pb["technology_angle"], "normalized_topic": "",
                 "pillar": "CODING", "technology_angle": pb["technology_angle"],
                 "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
                 "evidence_mode": "calendar",
                 "calendar": {"id": 1, "title": pb["technology_angle"], "sources": []}}
        script = cp.build_script_from_playbook(topic, POL, pb, "metacognition-debugging")
        for ch in script["chunks"]:
            if ch["beat"] == "technique":
                ch["en"][0]["t"] = technique_line
        return script


# ---------------------------------------------------------------------------
# 3. Provenance, license and URL safety (+ the $0 Openverse fetcher)
# ---------------------------------------------------------------------------
class ProvenanceAndLicense(unittest.TestCase):
    def _ext_asset(self, **over):
        a = {"kind": "external", "id": "ext-test",
             "url": "https://example.org/photographer",
             "asset_url": "https://cdn.example.org/img/1.jpg",
             "creator": "Jane Doe", "license": "cc0",
             "retrieved_utc": "2026-09-16T00:00:00+00:00",
             "query_sanitized": "user interviews",
             "sha256": "a" * 64, "origin": "openverse-api"}
        a.update(over)
        return a

    def test_valid_external_asset_passes(self):
        plan, sc = scene_for_mutation(vp.build_visual_plan(script_0919()[0], POL,
                                                           allow_external=False), "s5")
        sc["asset"] = self._ext_asset()
        issues = [i for i in vp.visual_semantic_issues(plan, script_0919()[0], POL)
                  if "provenance" in i or "license" in i or "URL" in i]
        self.assertEqual(issues, [])

    def test_cc_by_without_creator_is_blocked(self):
        plan, sc = scene_for_mutation(vp.build_visual_plan(script_0919()[0], POL,
                                                           allow_external=False), "s5")
        sc["asset"] = self._ext_asset(license="cc-by", creator="")
        issues = vp.visual_semantic_issues(plan, script_0919()[0], POL)
        self.assertTrue(any("cc-by image without recorded creator" in i for i in issues), issues)

    def test_disallowed_license_is_blocked(self):
        for lic in ("cc-by", "by", "by-nc", "nc", "all-rights", ""):
            plan, sc = scene_for_mutation(vp.build_visual_plan(script_0919()[0], POL,
                                                               allow_external=False), "s5")
            sc["asset"] = self._ext_asset(license=lic)
            issues = vp.visual_semantic_issues(plan, script_0919()[0], POL)
            self.assertTrue(any("license" in i and "not in" in i for i in issues),
                            f"license {lic!r} must be blocked: {issues}")

    def test_unsafe_urls_are_blocked(self):
        bad_urls = ["http://example.org/img.jpg", "https://ex..ample.org/img.jpg",
                    "https://example.org/img (1).jpg", "ftp://example.org/img.jpg"]
        for u in bad_urls:
            plan, sc = scene_for_mutation(vp.build_visual_plan(script_0919()[0], POL,
                                                               allow_external=False), "s5")
            sc["asset"] = self._ext_asset(asset_url=u)
            issues = vp.visual_semantic_issues(plan, script_0919()[0], POL)
            self.assertTrue(any("unsafe" in i for i in issues),
                            f"URL {u!r} must be unsafe: {issues}")

    def test_incomplete_provenance_is_blocked(self):
        for key, val in (("sha256", "short"), ("retrieved_utc", ""), ("query_sanitized", "")):
            plan, sc = scene_for_mutation(vp.build_visual_plan(script_0919()[0], POL,
                                                               allow_external=False), "s5")
            sc["asset"] = self._ext_asset(**{key: val})
            issues = vp.visual_semantic_issues(plan, script_0919()[0], POL)
            self.assertTrue(any("provenance" in i or "hash" in i for i in issues),
                            f"missing {key} must be blocked: {issues}")

    def test_repo_asset_path_traversal_is_blocked(self):
        plan, sc = scene_for_mutation(vp.build_visual_plan(script_0919()[0], POL,
                                                           allow_external=False), "s5")
        sc["asset"] = {"kind": "repo", "id": "repo:evil", "path": "../etc/passwd",
                       "origin": "repository-assets", "license": "internal"}
        issues = vp.visual_semantic_issues(plan, script_0919()[0], POL)
        self.assertTrue(any("unsafe repository asset path" in i for i in issues), issues)

    def test_asset_fetch_query_sanitization(self):
        q = af.sanitize_query("User Interviews, 'confirmation bias' \\; rm -rf / DROP TABLE")
        self.assertEqual(len(q.split()), 8, "query capped at 8 words")
        self.assertNotIn(";", q)
        self.assertNotIn("'", q)
        self.assertEqual(af.sanitize_query("  Héllo wörld   test  "), "h llo w rld test")
        self.assertEqual(af.sanitize_query(""), "")

    def test_license_policy_is_public_domain_only(self):
        # the fetcher and the gate must agree: CC0/PDM only
        self.assertEqual(af.ALLOWED_LICENSES, ("cc0", "pdm"))
        self.assertEqual(vp.ALLOWED_EXTERNAL_LICENSES, ("cc0", "pdm"))
        self.assertEqual(af._OPENVERSE_LICENSE.get("cc0"), "cc0")
        self.assertEqual(af._OPENVERSE_LICENSE.get("pdm"), "pdm")
        self.assertIsNone(af._OPENVERSE_LICENSE.get("by"),
                          "CC-BY must not be fetchable: no public attribution")
        self.assertIsNone(af._OPENVERSE_LICENSE.get("by-nc"))
        self.assertIsNone(af._OPENVERSE_LICENSE.get("by-sa"))
        self.assertIsNone(af._OPENVERSE_LICENSE.get("gpl"))

    def test_asset_fetch_end_to_end_mocked(self):
        """The fetcher returns a full provenance manifest for a CC0 hit,
        SKIPS a CC-BY hit that precedes it (no public attribution), honors
        skip_urls for per-reel diversity, and makes exactly ONE attempt."""
        from PIL import Image
        d = tempfile.mkdtemp(prefix="assetfetch_")
        img = os.path.join(d, "img.jpg")
        Image.new("RGB", (800, 600), (120, 90, 60)).save(img, quality=80)
        img2 = os.path.join(d, "img2.jpg")
        Image.new("RGB", (800, 600), (60, 90, 120)).save(img2, quality=80)
        cache_root = os.path.join(d, "cache")
        results = []

        def fake_download(url, cache_path, timeout=25):
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            shutil.copy(img if url.endswith("/1.jpg") else img2, cache_path)
            return cache_path, None

        def fake_get(url, timeout=25):
            results.append(url)
            # Issue #32 root cause (verified against the official
            # https://api.openverse.org/v1/ getimages_search contract):
            # license CODES (cc0, pdm, ...) belong in `license`; the old code
            # sent them to `license_type`, which is the USAGE filter
            # (all/all-cc/commercial/modification) — so Openverse could never
            # return our result set. The corrected request:
            self.assertIn("license=cc0%2Cpdm", url,
                          "the API must be asked for the public-domain set only")
            self.assertNotIn("license_type=cc0", url,
                             "license_type is the usage filter, not license codes")
            # a CC-BY result listed FIRST: the fetcher must skip it
            payload = {"results": [
                {"id": 1, "url": "https://cdn.example.org/img/by.jpg",
                 "foreign_landing_url": "https://example.org/photographer-x",
                 "creator": "Jane Doe", "license": "by", "title": "ccby"},
                {"id": 2, "url": "https://cdn.example.org/img/1.jpg",
                 "foreign_landing_url": "https://example.org/photographer-y",
                 "creator": "Public Domain", "license": "cc0",
                 "title": "street interview", "width": 800, "height": 600},
                {"id": 3, "url": "https://cdn.example.org/img/2.jpg",
                 "foreign_landing_url": "https://example.org/photographer-z",
                 "creator": "PD", "license": "pdm", "title": "second",
                 "width": 800, "height": 600},
            ]}
            return json.dumps(payload).encode()

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get), \
             mock.patch.object(af, "_download_image_with_reason", fake_download), \
             mock.patch.object(af, "_cache_dir", lambda: cache_root):
            r = af.fetch_image("user interviews")
            r2 = af.fetch_image("user interviews",
                                skip_urls=("https://cdn.example.org/img/1.jpg",))
        self.assertIsNotNone(r)
        self.assertEqual(r["license"], "cc0")
        self.assertEqual(r["asset_url"], "https://cdn.example.org/img/1.jpg",
                         "the preceding CC-BY result must be skipped")
        self.assertEqual(r["origin"], "openverse-api")
        self.assertEqual(r["query_sanitized"], "user interviews")
        self.assertEqual(len(r["sha256"]), 64)
        self.assertIn("retrieved_utc", r)
        self.assertTrue(r["asset_url"].startswith("https://"))
        self.assertTrue(os.path.exists(r["path"]))
        self.assertEqual(r["id"], f"ext-{r['sha256'][:12]}")
        # diversity: skip_urls moves to the next result (PDM is allowed)
        self.assertIsNotNone(r2)
        self.assertEqual(r2["asset_url"], "https://cdn.example.org/img/2.jpg")
        self.assertEqual(r2["license"], "pdm")
        # bounded: one API attempt per fetch_image call, no retries
        self.assertEqual(len(results), 2)
        # disabled → always None (the default local-safe path)
        with mock.patch.object(af, "enabled", lambda: False):
            self.assertIsNone(af.fetch_image("user interviews"))
        shutil.rmtree(d, ignore_errors=True)

    def test_cc_by_can_never_reach_publication(self):
        """Three independent layers reject CC-BY: the fetcher never returns
        it, the pre-render gate blocks it, and the renderer refuses a plan
        carrying it — so publication is impossible without public credit."""
        from PIL import Image
        d = tempfile.mkdtemp(prefix="ccby_")
        img = os.path.join(d, "img.jpg")
        Image.new("RGB", (800, 600), (90, 90, 90)).save(img, quality=80)

        def fake_download(url, cache_path, timeout=25):
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            shutil.copy(img, cache_path)
            return cache_path, None

        def fake_get_ccby_only(url, timeout=25):
            payload = {"results": [
                {"id": 1, "url": "https://cdn.example.org/img/by.jpg",
                 "foreign_landing_url": "https://example.org/x",
                 "creator": "Jane Doe", "license": "by", "title": "x"}]}
            return json.dumps(payload).encode()

        # layer 1: the fetcher
        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get_ccby_only), \
             mock.patch.object(af, "_download_image_with_reason", fake_download), \
             mock.patch.object(af, "_cache_dir", lambda: os.path.join(d, "cache")):
            self.assertIsNone(af.fetch_image("user interviews"),
                              "CC-BY must never be fetchable in production")
        # layer 2: the pre-render visual-semantic gate
        plan, sc = scene_for_mutation(vp.build_visual_plan(script_0919()[0], POL,
                                                           allow_external=False), "s5")
        sc["asset"] = self._ext_asset(license="cc-by")
        issues = vp.visual_semantic_issues(plan, script_0919()[0], POL)
        self.assertTrue(any("license" in i and "not in" in i for i in issues),
                        f"the gate must block CC-BY: {issues}")
        # layer 3: the renderer re-runs the gate on load and refuses
        ep = tempfile.mkdtemp(prefix="ccby_ep_")
        try:
            script, _ = script_0919()
            common.save_json(os.path.join(ep, "script.json"), script)
            make_timing(script, ep)
            silent_wav(ep, 90)
            common.save_json(os.path.join(ep, "visual_plan.json"), plan)
            from reel_engine import Reel
            with self.assertRaises(RuntimeError) as ctx:
                Reel(ep, POL)
            self.assertIn("visual plan blocked before render", str(ctx.exception))
        finally:
            shutil.rmtree(ep, ignore_errors=True)
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Variety and duplicate control
# ---------------------------------------------------------------------------
class VarietyAndDuplicates(unittest.TestCase):
    def setUp(self):
        self.script = script_0919()[0]
        self.plan = vp.build_visual_plan(self.script, POL, allow_external=False)

    def test_repeated_underlying_image_is_blocked(self):
        p = copy.deepcopy(self.plan)
        a = p["scenes"][1]["asset"]["id"]
        p["scenes"][2]["asset"] = copy.deepcopy(p["scenes"][1]["asset"])
        issues = vp.visual_semantic_issues(p, self.script, POL)
        self.assertTrue(any(f"asset {a!r} reused" in i for i in issues), issues)

    def test_too_few_scenes_is_blocked(self):
        p = copy.deepcopy(self.plan)
        p["scenes"] = p["scenes"][:3]
        issues = vp.visual_semantic_issues(p, self.script, POL)
        self.assertTrue(any("only 3 scenes" in i for i in issues), issues)

    def test_dominant_asset_is_blocked(self):
        p = copy.deepcopy(self.plan)
        aid = p["scenes"][1]["asset"]["id"]
        for sc in p["scenes"]:
            if sc["visual_category"] not in vp.BRAND_CATEGORIES:
                sc["asset"] = copy.deepcopy(p["scenes"][1]["asset"])
                sc["expected_duration"] = 10.0
        issues = vp.visual_semantic_issues(p, self.script, POL)
        self.assertTrue(any("dominates" in i or "reused" in i for i in issues), issues)

    def test_perceptual_hash_near_duplicates(self):
        from PIL import Image, ImageDraw
        import numpy as np
        base = np.zeros((H, W, 3), dtype=np.uint8)
        yy, xx = np.mgrid[0:H, 0:W]
        base[..., 0] = (xx // 8).astype(np.uint8)
        base[..., 1] = (yy // 8).astype(np.uint8)
        a = Image.fromarray(base, "RGB")
        d = ImageDraw.Draw(a)
        d.rectangle([200, 300, 880, 1600], outline=(255, 200, 90), width=6)
        d.ellipse([300, 700, 700, 1100], fill=(240, 190, 80))
        b = a.copy()                                   # near-identical (same)
        b2 = b.copy()
        d2 = ImageDraw.Draw(b2)
        d2.line([540, 0, 540, H], fill=(200, 160, 70), width=10)  # small change
        c = Image.new("RGB", (W, H), (180, 130, 60))
        dc = ImageDraw.Draw(c)
        dc.rectangle([100, 100, 400, 400], fill=(14, 12, 10))     # different layout
        self.assertLessEqual(vp.hamming(vp.perceptual_hash(a), vp.perceptual_hash(b)), 14,
                             "identical frames are near-duplicates")
        self.assertLess(vp.hamming(vp.perceptual_hash(a), vp.perceptual_hash(b2)), 30)
        self.assertGreater(vp.hamming(vp.perceptual_hash(a), vp.perceptual_hash(c)), 24,
                           "a different composition is not a duplicate")


# ---------------------------------------------------------------------------
# 5. Palette: no blue/navy/cyan/purple
# ---------------------------------------------------------------------------
class PaletteGuard(unittest.TestCase):
    def test_registry_colors_are_all_warm(self):
        for name, spec in vp.C.items():
            vp.assert_no_cold_colors(name, (233, 180, 74))  # smoke: warm passes
        for name, rgb in vp.BRAND_PALETTE.items():
            self.assertFalse(vp._is_cold(tuple(rgb)),
                             f"brand palette entry {name} {rgb} is cold")

    def test_cold_palette_declaration_is_blocked(self):
        plan = copy.deepcopy(vp.build_visual_plan(script_0919()[0], POL, allow_external=False))
        plan["palette"]["gold"] = [40, 70, 190]  # blue — the forbidden family
        issues = vp.visual_semantic_issues(plan, script_0919()[0], POL)
        self.assertTrue(any("cold hue in the declared palette" in i for i in issues), issues)

    def test_cold_pixel_fraction_detects_blue_family(self):
        import numpy as np
        blue = np.full((64, 36, 3), (30, 60, 170), dtype=np.uint8)
        navy = np.full((64, 36, 3), (18, 24, 60), dtype=np.uint8)
        purple = np.full((64, 36, 3), (90, 40, 120), dtype=np.uint8)
        warm = np.full((64, 36, 3), (233, 180, 74), dtype=np.uint8)
        for frame in (blue, navy, purple):
            self.assertGreater(vp.cold_pixel_fraction(frame), 0.5,
                               "a cold-frame must register as cold")
        self.assertLess(vp.cold_pixel_fraction(warm), 0.01)

    def test_brand_grade_stays_warm(self):
        import numpy as np
        rng = np.random.default_rng(7)
        src = rng.integers(0, 256, (120, 80, 3), dtype=np.uint8)
        graded = vp.brand_grade(src)
        self.assertLess(vp.cold_pixel_fraction(graded), 0.01,
                        "the duotone grade must never produce a cold hue")
        r, g, b = graded[..., 0].mean(), graded[..., 1].mean(), graded[..., 2].mean()
        self.assertGreater(r, b, "graded imagery must stay warm (red channel above blue)")


# ---------------------------------------------------------------------------
# 6. Text-heavy slides and subtitle-band obstruction
# ---------------------------------------------------------------------------
class TextAndOverlay(unittest.TestCase):
    def test_registry_never_exceeds_the_text_budget(self):
        for name, spec in vp.C.items():
            words = sum(len(w.split()) for ls in spec["label_sets"] for w in ls)
            self.assertLessEqual(words, vp.MAX_TEXT_WORDS_PER_SCENE,
                                 f"category {name} declares {words} on-screen words")

    def test_subtitle_band_geometry_blocks_overlays(self):
        plan = vp.build_visual_plan(script_0919()[0], POL, allow_external=False)
        sc = plan["scenes"][1]
        sc["content_top"] = 300  # into the subtitle band (y 180-454)
        issues = vp.visual_semantic_issues(plan, script_0919()[0], POL)
        self.assertTrue(any("obstructs the subtitle band" in i for i in issues), issues)

    def test_photo_scenes_are_exempt_from_plan_geometry_but_still_bounded(self):
        plan, sc = scene_for_mutation(vp.build_visual_plan(script_0919()[0], POL,
                                                           allow_external=False), "s5")
        sc["asset"] = self._ext_full_frame()
        sc["content_top"] = 0      # full-frame photo
        sc["content_bottom"] = 1920
        issues = [i for i in vp.visual_semantic_issues(plan, script_0919()[0], POL)
                  if "obstructs" in i]
        self.assertEqual(issues, [], "full-frame photos carry the renderer's "
                                     "subtitle shade; final QA checks the rendered band")

    @staticmethod
    def _ext_full_frame(**over):
        a = {"kind": "external", "id": "ext-full",
             "url": "https://example.org/photographer",
             "asset_url": "https://cdn.example.org/img/full.jpg",
             "creator": "Public Domain", "license": "cc0",
             "retrieved_utc": "2026-09-16T00:00:00+00:00",
             "query_sanitized": "user interviews", "sha256": "b" * 64,
             "origin": "openverse-api"}
        a.update(over)
        return a

    def test_rendered_band_check_detects_obstruction(self):
        import numpy as np
        arr = np.full((H, W, 3), (14, 12, 10), dtype=np.int16)
        arr[150:500, 60:W - 60] = (250, 240, 220)  # bright wall across the subtitle band
        L = POL["layout"]
        top = int(L["en_top"]) - 10
        bot = int(L["en_top"]) + 3 * int(L["en_row_height"]) + 10
        band = arr[top:bot, 60:W - 60]
        lum = 0.2126 * band[..., 0] + 0.7152 * band[..., 1] + 0.0722 * band[..., 2]
        self.assertGreater(float(np.percentile(lum, 25)), 96,
                           "a bright overlay across the band must be detectable")
        arr2 = np.full((H, W, 3), (14, 12, 10), dtype=np.int16)
        band2 = arr2[top:bot, 60:W - 60]
        lum2 = 0.2126 * band2[..., 0] + 0.7152 * band2[..., 1] + 0.0722 * band2[..., 2]
        self.assertLess(float(np.percentile(lum2, 25)), 96)


# ---------------------------------------------------------------------------
# 7. Rendered-frame detectors (cold card / cursor / legacy vs clean)
# ---------------------------------------------------------------------------
class RenderedFrameDetectors(unittest.TestCase):
    def _arr(self, rgb):
        import numpy as np
        return np.full((H, W, 3), rgb, dtype=np.int16)

    def test_legacy_cold_card_is_detected(self):
        arr = self._arr((14, 12, 10))
        arr[600:1200, 120:960] = (18, 20, 24)  # the old card color
        self.assertGreater(qa.detect_code_card(arr), qa.COLD_CARD_PX)
        self.assertLess(qa.detect_code_card(self._arr((14, 12, 10))), qa.COLD_CARD_PX)

    def test_isolated_caret_is_detected(self):
        arr = self._arr((18, 20, 24))
        arr[650:690, 400:404] = (233, 180, 74)  # solid 4x40 bar on the cold card
        self.assertTrue(qa.detect_cursor(arr))
        warm = self._arr((24, 20, 14))
        warm[900:950, 300:306] = (255, 200, 100)  # decorative caret on warm ground
        self.assertTrue(qa.detect_cursor(warm))

    def test_brand_geometry_is_not_a_caret(self):
        import numpy as np
        dot = self._arr((14, 12, 10))
        yy, xx = np.mgrid[0:H, 0:W]
        dot[(yy - 700) ** 2 + (xx - 500) ** 2 < 121] = (233, 180, 74)
        self.assertFalse(qa.detect_cursor(dot), "a round particle is not a caret")
        bar = self._arr((14, 12, 10))
        bar[1000:1064, 300:900] = (233, 180, 74)
        self.assertFalse(qa.detect_cursor(bar), "a bar EDGE is not a caret")
        line = self._arr((14, 12, 10))
        line[600:700, 500:501] = (233, 180, 74)
        self.assertFalse(qa.detect_cursor(line), "a 1px network line is not a caret")
        spine = self._arr((14, 12, 10))
        spine[500:1300, 500:504] = (233, 180, 74)
        self.assertFalse(qa.detect_cursor(spine), "a long continuous spine is not a caret")
        bright = self._arr((120, 100, 70))
        bright[650:690, 400:404] = (233, 180, 74)
        self.assertFalse(qa.detect_cursor(bright), "photo texture is not a caret")

    def test_check_visuals_with_no_frames_runs_the_plan_gate(self):
        ep = tempfile.mkdtemp(prefix="qa_noframes_")
        try:
            script, _ = script_0919()
            common.save_json(os.path.join(ep, "script.json"), script)
            plan = vp.build_visual_plan(script, POL, allow_external=False)
            common.save_json(os.path.join(ep, "visual_plan.json"), plan)
            rep = qa.Report(POL)
            qa.check_visuals(rep, ep, script, None, POL, no_frames=True)
            self.assertEqual(rep.blocking, [])
            self.assertTrue(rep.details["visuals"]["present"])
            self.assertFalse(rep.details["visuals"]["rendered_frames_checked"])
        finally:
            shutil.rmtree(ep, ignore_errors=True)

    def test_check_visuals_blocks_a_tampered_plan(self):
        ep = tempfile.mkdtemp(prefix="qa_tampered_")
        try:
            script, _ = script_0919()
            common.save_json(os.path.join(ep, "script.json"), script)
            plan = vp.build_visual_plan(script, POL, allow_external=False)
            plan["scenes"][1]["visual_category"] = "stack-trace"
            common.save_json(os.path.join(ep, "visual_plan.json"), plan)
            rep = qa.Report(POL)
            qa.check_visuals(rep, ep, script, None, POL, no_frames=True)
            self.assertTrue(any("visual_semantics" in b for b in rep.blocking), rep.blocking)
        finally:
            shutil.rmtree(ep, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. Pipeline: the visual gate runs before TTS/render, fail-closed
# ---------------------------------------------------------------------------
class PipelineVisualGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pipeline_visual_")
        self._orig = (common.ROOT, common.CONTENT, common.MEMORY_PATH)
        common.ROOT = self.tmp
        common.CONTENT = os.path.join(self.tmp, "content")
        common.MEMORY_PATH = os.path.join(self.tmp, "content", "editorial_memory.json")
        os.makedirs(common.CONTENT, exist_ok=True)
        self.tag = "2077-03-03"
        self.ep = common.episode_dir(self.tag)
        os.makedirs(self.ep, exist_ok=True)
        self.topic = calendar_topic(12, with_evidence=False, date=self.tag)
        self.commands = []

    def tearDown(self):
        common.ROOT, common.CONTENT, common.MEMORY_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _produce(self, scripts, gate_issues):
        def fake_run(cmd, stage, env=None, timeout=1800):
            self.commands.append((stage, " ".join(str(c) for c in cmd)))
            if stage == "trend":
                common.save_json(os.path.join(self.ep, "topic.json"), self.topic)
            elif stage == "script":
                i = min(sum(1 for s, _ in self.commands if s == "script") - 1,
                        len(scripts) - 1)
                common.save_json(os.path.join(self.ep, "script.json"), scripts[i])
            return ""

        a = argparse.Namespace(tag=self.tag, fixture=None, calendar_only=True,
                               synthetic_tts=False, skip_network=True, dry_run=True)
        with mock.patch.object(pl, "run", fake_run), \
             mock.patch.object(pl, "run_qa", lambda cmd, stage="qa": False), \
             mock.patch.object(pl.vp, "visual_semantic_issues",
                               lambda plan, script, pol: list(gate_issues)):
            return pl.produce(a)

    def _clean_script(self):
        pb = cp.PLAYBOOKS["context-switching"]
        t = self.topic
        return cp.build_script_from_playbook(t, POL, pb, "context-switching")

    def test_visual_block_skips_before_every_media_stage(self):
        s = self._clean_script()
        rc = self._produce([s, s],
                           gate_issues=["[visual_semantics] s1: wiring-test blocker"])
        self.assertEqual(rc, 10)
        stages = [st for st, _ in self.commands]
        self.assertEqual(stages, ["trend", "script", "script"],
                         f"only the producer stage + its ONE retry may run: {self.commands}")
        joined = " ".join(c for _, c in self.commands)
        for media in ("tts_edge.py", "tts_synthetic.py", "timing.py", "caption.py",
                      "render_auto.py", "poster_auto.py", "ffmpeg", "ffprobe"):
            self.assertNotIn(media, joined, f"{media} must never run for a visual-blocked script")
        paths = common.output_paths(self.tag)
        for key in ("mp4", "caption", "poster", "poster_4x5", "qa_json", "marker"):
            self.assertFalse(os.path.exists(paths[key]),
                             f"{key} must not exist after a pre-render visual skip")
        st = pl.load_state(self.tag)
        self.assertEqual(st["retries"]["script"], 1, "bounded: the existing single script retry")
        self.assertIn("pre-render visual gate", st["error"])
        self.assertIn("skipped before TTS/render (no media, no Buffer)", st["error"])
        self.assertIn("wiring-test blocker", st["error"])

    def test_visual_pass_proceeds_to_media(self):
        s = self._clean_script()
        run_calls = []

        def run2(cmd, stage, env=None, timeout=1800):
            run_calls.append(stage)
            if stage == "trend":
                common.save_json(os.path.join(self.ep, "topic.json"), self.topic)
            elif stage == "script":
                common.save_json(os.path.join(self.ep, "script.json"), s)
            elif stage == "tts":
                raise pl.Stage("tts", "test stop after the visual gate passed")

        a = argparse.Namespace(tag=self.tag, fixture=None, calendar_only=True,
                               synthetic_tts=False, skip_network=True, dry_run=True)
        with mock.patch.object(pl, "run", run2), \
             mock.patch.object(pl, "run_qa", lambda cmd, stage="qa": False), \
             mock.patch.object(pl.vp, "visual_semantic_issues",
                               lambda plan, script, pol: []):
            rc = pl.produce(a)
        self.assertEqual(rc, 10)
        self.assertEqual(run_calls, ["trend", "script", "tts"],
                         "a clean visual plan proceeds straight to TTS")
        st = pl.load_state(self.tag)
        self.assertIn("visual_plan", st, "the plan summary is recorded in state")
        self.assertEqual(st["visual_plan"]["pillar"], "ATTENTION")


# ---------------------------------------------------------------------------
# 9. English-only report
# ---------------------------------------------------------------------------
class EnglishOnlyReport(unittest.TestCase):
    def test_subtitles_section_is_english_only(self):
        ep, script, topic, plan = episode_0919()
        try:
            root_orig = common.ROOT
            common.ROOT = tempfile.mkdtemp(prefix="report_")
            try:
                epdir = os.path.join(common.ROOT, "content", "episodes", "auto-2026-09-19")
                os.makedirs(epdir, exist_ok=True)
                common.save_json(os.path.join(epdir, "script.json"), script)
                section = report_issue.english_subtitles_section("2026-09-19")
                text = "\n".join(section)
                self.assertIn("English subtitles — synchronized, LTR, safe-zone validated", text)
                for banned in ("FA", "Persian", "RTL", "⇄", "فارسی", "translation_source"):
                    self.assertNotIn(banned, text,
                                     f"legacy bilingual wording {banned!r} must not survive")
            finally:
                common.ROOT = root_orig
        finally:
            shutil.rmtree(ep, ignore_errors=True)

    def test_bilingual_table_is_gone(self):
        with open(os.path.join(ROOT, "build", "report_issue.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("def bilingual_table", src)
        self.assertNotIn("translation_source", src)
        self.assertNotIn("<th>FA</th>", src)
        self.assertNotIn("Persian (FA)", src)

    def test_workflow_header_is_english_only(self):
        with open(os.path.join(ROOT, ".github", "workflows", "daily-trend-draft.yml"),
                  encoding="utf-8") as f:
            yml = f.read()
        self.assertIn("English-only", yml)
        for banned in ("Persian subtitles", "FA subtitles", "RTL subtitles"):
            self.assertNotIn(banned, yml)


# ---------------------------------------------------------------------------
# 10. Root-cause regression: the "code visual" string is gone for good
# ---------------------------------------------------------------------------
class RootCauseRegression(unittest.TestCase):
    def test_no_legacy_code_visual_directive_survives(self):
        # the exact root cause: the static fallback appended a fixed
        # "code visual" directive to EVERY pillar's visual_direction, and the
        # renderer triggered the generic code card on the substring "code"
        with open(os.path.join(ROOT, "build", "content_producer.py"),
                  encoding="utf-8") as f:
            prod = f.read()
        self.assertNotIn(' + code visual + ', prod,
                         "the fixed directive concatenation must not survive")
        self.assertNotIn('"code visual" +', prod)
        self.assertNotIn('+ "code visual"', prod)
        # and the producer must set the direction from the pillar only
        self.assertIn('vp.pillar_visual_direction(pb["pillar"])', prod)
        # the renderer no longer picks templates from that string: it only
        # honors the gated plan (verified end-to-end by the 0919 replay)
        with open(os.path.join(ROOT, "build", "reel_engine.py"),
                  encoding="utf-8") as f:
            eng = f.read()
        self.assertNotIn('"code" in', eng,
                         "template selection by the 'code' substring is gone")

    def test_llm_visual_request_is_recorded_but_never_honored(self):
        pb = cp.PLAYBOOKS["confirmation-bias-research"]
        topic = topic_0919()
        llm_out = {
            "title": "Confirmation bias", "technology_angle": pb["technology_angle"],
            "metacognition_concept": "confirmation bias", "hook": pb["hook"],
            "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
            "narration": {"hook": pb["hook"], "problem": list(pb["problem"]),
                          "explain": list(pb["explain"]), "example": list(pb["example"]),
                          "technique": list(pb["technique"]), "ending": [pb["ending"]]},
            "on_screen_text": pb["web"],
            # the LLM asks for exactly what broke the reel:
            "visual_direction": "terminal window with animated code visual and typing cursor",
            "actionable_technique": pb["technique"][0], "ending": pb["ending"],
            "caption": {"hook": pb["hook"], "intro": pb["problem"][0], "sections": [],
                        "hashtags": []}, "claims": [], "sources": []}
        script = cp.build_script_from_llm(topic, POL, llm_out, generation_mode="groq")
        self.assertNotIn("code visual", script["visual_direction"].lower(),
                         "an LLM request for code must never set the visual direction")
        self.assertEqual(script["visual_direction"], vp.pillar_visual_direction("PRODUCT"))
        self.assertIn("animated code visual",
                      script["meta"]["visual_direction_request"],
                      "the request IS recorded — for the record, never for the render")
        plan = vp.build_visual_plan(script, POL, allow_external=False)
        issues = vp.visual_semantic_issues(plan, script, POL)
        self.assertEqual(issues, [], f"the deterministic direction must gate-clean: {issues}")
        self.assertEqual(vp.plan_summary(plan)["code_scenes"], [])

    def test_static_fallback_direction_is_pillar_specific(self):
        fb = llm_provider.StaticEnglishFallback(POL)
        out = fb.produce({})
        self.assertNotIn("code visual", out["visual_direction"].lower())
        self.assertEqual(out["visual_direction"], vp.pillar_visual_direction("PRODUCT"))



# ---------------------------------------------------------------------------
# 11. Balanced production mix: explicit photo designations, $0 source,
#     procedural fallback, no repo-hero backgrounds (issue #24 follow-up)
# ---------------------------------------------------------------------------
def _synth_photo(i, w=1000, h=1500):
    """Deterministic synthetic 'photograph' with strong, structurally distinct
    luminance layout (stands in for a CC0/PDM download; the fetch layer is
    mocked, so no network is involved)."""
    import numpy as np
    from PIL import Image
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3), dtype=np.uint8)
    if i == 0:  # vertical slabs (facade-like)
        sl = (xx // 70).astype(np.uint8)
        img[..., 0] = (sl * 37) % 255
        img[..., 1] = (sl * 29) % 255
        img[..., 2] = (sl * 19) % 255
        img[h // 3:, :, 0] = (img[h // 3:, :, 0].astype(int) + 40) % 255
    elif i == 1:  # horizontal bands + a large disc (skyline-like)
        bw = (yy // 90).astype(np.uint8)
        img[..., 0] = (bw * 31) % 255
        img[..., 1] = (bw * 23) % 255
        img[..., 2] = (bw * 17) % 255
        img[(yy - h // 2) ** 2 + (xx - w // 2) ** 2 < (w // 3) ** 2] = (200, 170, 120)
    else:  # diagonal gradient + grid (street-like)
        img[..., 0] = ((xx + yy) // 40) % 255
        img[..., 1] = ((xx - yy + 4000) // 55) % 255
        img[..., 2] = ((xx * 2 + yy) // 90) % 255
        img[(xx % 130 < 8) | (yy % 130 < 8)] = (240, 230, 210)
    return Image.fromarray(img, "RGB")


class ProductionPhotoMix(unittest.TestCase):
    """A normal reel EXPLICITLY plans 2-3 photo-designated scenes (distinct
    topic-relevant CC0/PDM photos when safely retrievable, else a DISTINCT
    procedural visual), the rest as procedural diagrams, brand only at
    hook/ending, and no repository hero image as a scene background."""

    @classmethod
    def setUpClass(cls):
        from PIL import Image
        cls.tmp = tempfile.mkdtemp(prefix="photomix_")
        cls.photos = []
        for i in range(3):
            p = os.path.join(cls.tmp, f"photo_{i}.jpg")
            _synth_photo(i).save(p, quality=88)
            cls.photos.append(p)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _fake_fetch(self, calls=None):
        """Rotates through the three synthetic photos, honoring skip_urls so
        every scene gets a DIFFERENT photograph (the per-reel diversity rule)."""
        def fake(query, timeout=af.TIMEOUT, skip_urls=()):
            if calls is not None:
                calls.append((query, tuple(skip_urls)))
            for i, path in enumerate(self.photos):
                url = f"https://cdn.openverse.test/photos/{i}.jpg"
                if url not in set(skip_urls):
                    return {"kind": "external",
                            "id": f"ext-{i:012d}",
                            "url": "https://example.org/photographer",
                            "asset_url": url,
                            "creator": "Public Domain", "license": "cc0",
                            "retrieved_utc": "2026-09-16T00:00:00+00:00",
                            "query_sanitized": af.sanitize_query(query),
                            "sha256": f"{i:01d}" + "0" * 63,
                            "path": path, "origin": "openverse-api"}
            return None
        return fake

    def test_photo_designations_are_deterministic_across_all_pillars(self):
        for key, pb in cp.PLAYBOOKS.items():
            topic = {"content_date": "2077-01-01", "content_id": "reel-test",
                     "title": pb["technology_angle"], "normalized_topic": "",
                     "pillar": pb["pillar"],
                     "technology_angle": pb["technology_angle"],
                     "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
                     "evidence_mode": "calendar",
                     "calendar": {"id": 1, "title": pb["technology_angle"], "sources": []}}
            script = cp.build_script_from_playbook(topic, POL, pb, key)
            plan = vp.build_visual_plan(script, POL, allow_external=False)
            des = [sc for sc in plan["scenes"] if sc.get("photo_designated")]
            self.assertGreaterEqual(len(des), vp.PHOTO_MIN, f"{key}: at least the minimum")
            self.assertLessEqual(len(des), vp.PHOTO_TARGET, f"{key}: at most the target")
            beats = {}
            for sc in des:
                self.assertNotIn(sc["beat"], ("hook", "ending"),
                                 f"{key}: brand moments never carry photos")
                self.assertFalse(vp.C[sc["visual_category"]]["code"],
                                 f"{key}: a code scene can never be photo-designated")
                self.assertTrue(vp.C[sc["visual_category"]].get("photo_ok"),
                                f"{key}: {sc['visual_category']} is not photo-capable")
                self.assertEqual(beats.get(sc["beat"], 0) + 1, 1,
                                 f"{key}: at most one photo per beat")
                beats[sc["beat"]] = beats.get(sc["beat"], 0) + 1
            pp = plan["photo_policy"]
            self.assertEqual(pp["target"], vp.PHOTO_TARGET)
            self.assertEqual(pp["minimum"], vp.PHOTO_MIN)
            self.assertEqual(pp["repo_hero_as_background"], False)
            self.assertEqual(sorted(pp["designated"]),
                             sorted(sc["scene_id"] for sc in des),
                             f"{key}: photo_policy.designated must match the scenes")
            # determinism: rebuilding gives the identical designations
            plan2 = vp.build_visual_plan(script, POL, allow_external=False)
            self.assertEqual(pp["designated"], plan2["photo_policy"]["designated"],
                             f"{key}: photo designations must be deterministic")
            self.assertEqual(vp.visual_semantic_issues(plan, script, POL), [],
                             f"{key}: the mix must stay gate-clean")

    def test_no_repo_hero_can_appear_in_any_plan(self):
        for key, pb in cp.PLAYBOOKS.items():
            self.assertNotIn("hero_brain", vp.REPO_ASSETS)
            self.assertNotIn("hero_desk", vp.REPO_ASSETS)
            topic = {"content_date": "2077-01-01", "content_id": "reel-test",
                     "title": pb["technology_angle"], "normalized_topic": "",
                     "pillar": pb["pillar"],
                     "technology_angle": pb["technology_angle"],
                     "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
                     "evidence_mode": "calendar",
                     "calendar": {"id": 1, "title": pb["technology_angle"], "sources": []}}
            script = cp.build_script_from_playbook(topic, POL, pb, key)
            plan = vp.build_visual_plan(script, POL, allow_external=False)
            for sc in plan["scenes"]:
                a = sc["asset"]
                if a["kind"] == "repo":
                    self.assertIn(a["path"], vp.REPO_ASSETS.values(),
                                  f"{key}/{sc['scene_id']}: only the two profile brand "
                                  "assets may come from the repository")
                    self.assertIn(sc["visual_category"], vp.BRAND_CATEGORIES,
                                  f"{key}/{sc['scene_id']}: a repo image is a brand "
                                  "reference, never a content-scene background")
                    self.assertFalse(sc.get("photo_designated"),
                                     f"{key}/{sc['scene_id']}: a photo-designated scene "
                                     "must never use a brand asset")

    def test_tampered_repo_hero_is_blocked_by_the_gate(self):
        plan, sc = scene_for_mutation(vp.build_visual_plan(script_0919()[0], POL,
                                                           allow_external=False), "s2")
        sc["asset"] = {"kind": "repo", "id": "repo:assets/img/hero_brain.png",
                       "path": "assets/img/hero_brain.png",
                       "origin": "repository-assets", "license": "internal"}
        issues = vp.visual_semantic_issues(plan, script_0919()[0], POL)
        self.assertTrue(any("existing hero images are not scene backgrounds" in i
                            for i in issues), issues)

    def test_unavailable_source_falls_back_to_distinct_procedural(self):
        script, _ = script_0919()
        calls = []
        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "fetch_image",
                               lambda query, timeout=af.TIMEOUT, skip_urls=(): None):
            plan = vp.build_visual_plan(script, POL, allow_external=True)
        des = [sc for sc in plan["scenes"] if sc.get("photo_designated")]
        self.assertGreaterEqual(len(des), vp.PHOTO_MIN)
        self.assertLessEqual(len(des), vp.PHOTO_TARGET)
        for sc in des:
            self.assertEqual(sc["asset"]["kind"], "procedural",
                             f"{sc['scene_id']}: an unavailable source must fall back "
                             "to the topic-specific procedural visual, never a hero image")
        ids = [sc["asset"]["id"] for sc in plan["scenes"]
               if sc["visual_category"] not in vp.BRAND_CATEGORIES]
        self.assertEqual(len(ids), len(set(ids)),
                         "every fallback visual must be distinct")
        summary = vp.plan_summary(plan)
        self.assertEqual(summary["external"], 0)
        self.assertGreaterEqual(summary["procedural"], 4)
        self.assertEqual(vp.visual_semantic_issues(plan, script, POL), [],
                         "the all-procedural fallback must stay gate-clean")

    def test_rendered_mix_end_to_end_with_distinct_photos(self):
        """Deterministic e2e fixture: 3 distinct synthetic CC0 photos are
        fetched (mocked) into the plan, rendered, and verified frame-by-frame;
        the contact sheet is written to /tmp (test artifact, never committed)."""
        import numpy as np
        from PIL import Image
        script, topic = script_0919()
        calls = []
        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "fetch_image", self._fake_fetch(calls)):
            plan = vp.build_visual_plan(script, POL, allow_external=True)

        des = [sc for sc in plan["scenes"] if sc.get("photo_designated")]
        self.assertEqual(len(des), 3, "the 0919 PRODUCT reel designs 3 photo slots")
        exts = [sc for sc in plan["scenes"] if sc["asset"]["kind"] == "external"]
        self.assertEqual(len(exts), 3)
        urls = [sc["asset"]["asset_url"] for sc in exts]
        self.assertEqual(len(urls), len(set(urls)),
                         "each photo slot retrieves a DIFFERENT photograph")
        for sc in exts:
            a = sc["asset"]
            self.assertIn(a["license"], ("cc0", "pdm"))
            self.assertTrue(os.path.exists(a["path"]))
            subject = vp.C[sc["visual_category"]]["keywords"][0]
            self.assertIn(subject, a["query_sanitized"].split(),
                          f"{sc['scene_id']}: the query must carry the scene subject")
        self.assertEqual(vp.visual_semantic_issues(plan, script, POL), [])

        d = tempfile.mkdtemp(prefix="mixep_")
        try:
            common.save_json(os.path.join(d, "script.json"), script)
            common.save_json(os.path.join(d, "visual_plan.json"), plan)
            make_timing(script, d)
            silent_wav(d, 90)
            from reel_engine import Reel
            reel = Reel(d, POL)

            photo_hashes, photo_frames = [], []
            all_hashes = []
            proc_frames = 0
            brand_frames = 0
            for sc, a_t, b_t in reel.scene_times:
                t = (a_t + b_t) / 2
                arr = np.array(reel.frame(t).convert("RGB"), dtype=np.int16)
                self.assertLess(qa.detect_code_card(arr), qa.COLD_CARD_PX)
                self.assertFalse(qa.detect_cursor(arr))
                h = arr.shape[0]
                band = arr[int(h * 0.25):int(h * 0.85), 40:arr.shape[1] - 40]
                r, g, bb = band[..., 0], band[..., 1], band[..., 2]
                self.assertLess(float(((bb > r + 4) & (bb >= g - 6)).mean()), 0.002,
                                f"{sc['scene_id']}: a rendered frame must stay warm")
                img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
                all_hashes.append(vp.perceptual_hash(img))
                if sc["asset"]["kind"] == "external":
                    photo_hashes.append(vp.perceptual_hash(img))
                    photo_frames.append(img)
                elif sc["visual_category"] in vp.BRAND_CATEGORIES:
                    brand_frames += 1
                else:
                    proc_frames += 1
            # distinct photos: no two photo frames are perceptual near-duplicates
            for i in range(len(photo_hashes)):
                for j in range(i + 1, len(photo_hashes)):
                    self.assertGreater(vp.hamming(photo_hashes[i], photo_hashes[j]), 24,
                                       "two rendered photo frames must be recognizable "
                                       "as DIFFERENT photographs")
            # scene-to-scene change everywhere
            for i in range(len(all_hashes) - 1):
                self.assertGreater(vp.hamming(all_hashes[i], all_hashes[i + 1]), 14,
                                   f"scene change #{i + 1} collapsed")
            self.assertEqual(brand_frames, 2, "brand assets render only at hook/ending")
            self.assertGreaterEqual(proc_frames, 3,
                                    "the remaining content scenes stay procedural")

            # contact sheet (test artifact, /tmp only — never committed media)
            sheet = os.path.join(tempfile.gettempdir(), "visual_mix_contact_sheet.png")
            tiles = [im.resize((162, 288)) for im in
                     [reel.frame((a + b) / 2) for _, a, b in reel.scene_times]]
            canvas = Image.new("RGB", (162 * len(tiles), 288), (14, 12, 10))
            for k, tile in enumerate(tiles):
                canvas.paste(tile, (162 * k, 0))
            canvas.save(sheet)
            self.assertTrue(os.path.exists(sheet))
            self.assertTrue(sheet.startswith(tempfile.gettempdir()))
            self.assertGreater(os.path.getsize(sheet), 10000)
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 12. Photo retrieval: default-enabled in Actions, keyless, bounded
# ---------------------------------------------------------------------------
class PhotoRuntimeDefault(unittest.TestCase):
    WORKFLOW = os.path.join(ROOT, ".github", "workflows", "daily-trend-draft.yml")

    def test_workflow_produces_with_asset_fetch_default_on(self):
        with open(self.WORKFLOW, encoding="utf-8") as f:
            yml = f.read()
        self.assertIn('ASSET_FETCH: "1"', yml,
                      "the production Actions path must fetch photos by default")
        self.assertIn("CC0", yml)
        self.assertIn("CC-BY", yml, "the policy comment must document the CC-BY state")
        self.assertNotIn("OPENVERSE_API_KEY", yml, "no paid/keyed service in Actions")

    def test_enabled_flag_is_default_off_locally_on_in_actions(self):
        for val, want in ((None, False), ("0", False), ("1", True), ("", False),
                          ("true", False)):
            env = {} if val is None else {"ASSET_FETCH": val}
            with mock.patch.dict(os.environ, env, clear=False):
                if val is None:
                    os.environ.pop("ASSET_FETCH", None)
                self.assertEqual(af.enabled(), want, f"ASSET_FETCH={val!r}")

    def test_keyless_and_bounded(self):
        for mod in (af,):
            with open(os.path.join(ROOT, "build", "asset_fetch.py"),
                      encoding="utf-8") as f:
                src = f.read()
        self.assertNotIn("Authorization", src, "the Openverse public API needs no key")
        self.assertNotIn("api_key", src.lower())
        self.assertIn("no retries", src.lower(),
                      "the one-attempt budget is documented policy")
        # one attempt, then None: a failing transport is tried exactly once
        attempts = []

        def fake_get(url, timeout=af.TIMEOUT):
            attempts.append(url)
            raise OSError("network down")

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get):
            self.assertIsNone(af.fetch_image("user interviews"))
        self.assertEqual(len(attempts), 1, "exactly ONE attempt — no retries")
        self.assertTrue(attempts[0].startswith(af.API_URL),
                        "only the official Openverse endpoint is ever called")
        # and a malformed payload fails soft to None (procedural fallback)
        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", lambda url, timeout=af.TIMEOUT: b"not json"):
            self.assertIsNone(af.fetch_image("user interviews"))


# ---------------------------------------------------------------------------
# 13. Brand treatment of photos: warm, recognizable, blue-neutralized
# ---------------------------------------------------------------------------
class BrandTreatmentOfPhotos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image
        cls.tmp = tempfile.mkdtemp(prefix="brandgrade_")
        cls.srcs = []
        for i in range(3):
            p = os.path.join(cls.tmp, f"src_{i}.jpg")
            _synth_photo(i).save(p, quality=90)
            cls.srcs.append(p)
        # a blue-dominant source (the worst case the grade must neutralize)
        import numpy as np
        b = np.zeros((600, 400, 3), dtype=np.uint8)
        b[..., 0] = 40
        b[..., 1] = 90
        b[..., 2] = 210
        b[450:, :, :] = (220, 180, 120)
        Image.fromarray(b, "RGB").save(os.path.join(cls.tmp, "blue.jpg"), quality=90)
        cls.blue = os.path.join(cls.tmp, "blue.jpg")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_graded_photos_stay_warm_and_recognizable(self):
        import numpy as np
        from PIL import Image
        graded = []
        for p in self.srcs:
            arr = np.array(Image.open(p).convert("RGB"), np.uint8)
            g = vp.brand_grade(arr)
            self.assertLess(vp.cold_pixel_fraction(g), 0.01,
                            "the duotone grade must never leave a cold hue")
            self.assertGreater(int(g[..., 0].mean()), int(g[..., 2].mean()),
                               "graded imagery must stay warm (red above blue)")
            graded.append(Image.fromarray(g, "RGB"))
        for i in range(3):
            for j in range(i + 1, 3):
                self.assertGreater(vp.hamming(vp.perceptual_hash(graded[i]),
                                              vp.perceptual_hash(graded[j])), 24,
                                   "grading must preserve recognizability: "
                                   "different photos stay different photos")

    def test_blue_dominant_source_is_neutralized(self):
        import numpy as np
        from PIL import Image
        arr = np.array(Image.open(self.blue).convert("RGB"), np.uint8)
        self.assertGreater(vp.cold_pixel_fraction(arr), 0.5,
                           "the fixture must actually be blue-dominant")
        g = vp.brand_grade(arr)
        self.assertLess(vp.cold_pixel_fraction(g), 0.01,
                        "a blue-dominant source cannot leave a blue-dominant frame")
        self.assertGreater(int(g[..., 0].mean()), int(g[..., 2].mean()))

    def test_rendered_photo_frame_is_warm_and_band_clear(self):
        import numpy as np
        from PIL import Image
        # a minimal episode whose ONE content photo is the blue-dominant source
        script, _ = script_0919()
        blue_manifest = {
            "kind": "external", "id": "ext-blue0000001",
            "url": "https://example.org/photographer",
            "asset_url": "https://cdn.openverse.test/blue.jpg",
            "creator": "Public Domain", "license": "cc0",
            "retrieved_utc": "2026-09-16T00:00:00+00:00",
            "query_sanitized": "user interviews",
            "sha256": "c" * 64, "path": self.blue, "origin": "openverse-api"}

        def fake(query, timeout=af.TIMEOUT, skip_urls=()):
            m = dict(blue_manifest)
            m["sha256"] = hashlib.sha256((query + str(len(skip_urls))).encode()).hexdigest()
            m["id"] = f"ext-{m['sha256'][:12]}"
            m["query_sanitized"] = af.sanitize_query(query)
            return m

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "fetch_image", fake):
            plan = vp.build_visual_plan(script, POL, allow_external=True)
        self.assertEqual(vp.visual_semantic_issues(plan, script, POL), [])
        d = tempfile.mkdtemp(prefix="blueep_")
        try:
            common.save_json(os.path.join(d, "script.json"), script)
            common.save_json(os.path.join(d, "visual_plan.json"), plan)
            make_timing(script, d)
            silent_wav(d, 90)
            from reel_engine import Reel
            reel = Reel(d, POL)
            photo_frames = 0
            L = POL["layout"]
            top = int(L["en_top"]) - 10
            bot = int(L["en_top"]) + 3 * int(L["en_row_height"]) + 10
            for sc, a, b in reel.scene_times:
                arr = np.array(reel.frame((a + b) / 2).convert("RGB"), dtype=np.int16)
                h = arr.shape[0]
                band = arr[int(h * 0.25):int(h * 0.85), 40:arr.shape[1] - 40]
                r, g, bb = band[..., 0], band[..., 1], band[..., 2]
                self.assertLess(float(((bb > r + 4) & (bb >= g - 6)).mean()), 0.002,
                                f"{sc['scene_id']}: rendered frame must not be blue")
                if sc["asset"]["kind"] == "external":
                    photo_frames += 1
                    b2 = arr[top:bot, 60:arr.shape[1] - 60]
                    lum = 0.2126 * b2[..., 0] + 0.7152 * b2[..., 1] + 0.0722 * b2[..., 2]
                    self.assertLess(float(np.percentile(lum, 25)), 96,
                                    f"{sc['scene_id']}: the subtitle band over a photo "
                                    "must stay a dark scrim")
            self.assertGreaterEqual(photo_frames, vp.PHOTO_MIN)
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 14. QA weight invariance: 0-100 normalization, 81 stays 81, blocker veto,
#     the 85 bar is unchanged (issue #24 follow-up)
# ---------------------------------------------------------------------------
class QAWeightInvariance(unittest.TestCase):
    def _report_with_19_points(self):
        rep = qa.Report(POL)
        rep.warn("script_quality", "fixture deduction", 5)
        rep.warn("english_quality", "fixture deduction", 3)
        rep.warn("topic_relevance", "fixture deduction", 2)
        rep.warn("subtitle_layout", "fixture deduction", 4)
        rep.warn("caption_quality", "fixture deduction", 4)
        rep.warn("video_quality", "fixture deduction", 1)
        self.assertEqual(rep.score(), 81)
        return rep

    def _episode_with_plan(self, plan=None):
        ep = tempfile.mkdtemp(prefix="qa_winv_")
        script, _ = script_0919()
        common.save_json(os.path.join(ep, "script.json"), script)
        if plan is None:
            plan = vp.build_visual_plan(script, POL, allow_external=False)
        common.save_json(os.path.join(ep, "visual_plan.json"), plan)
        return ep, script

    def test_scoring_stays_normalized_0_to_100(self):
        rep = qa.Report(POL)
        self.assertEqual(rep.score(), 100)
        rep2 = qa.Report(POL)
        for c in qa.CHECKS:
            rep2.block(c, "fixture")
        # 145 = 135 legacy budget + 10 for the minimal-contract blocker
        # family (issue #33: banner/CTA/safe-bounds/font-floor hard gates)
        self.assertEqual(sum(qa.WEIGHTS.values()), 145,
                         "the deduction budget must stay bounded by the weights")
        self.assertEqual(rep2.score(), 0, "a fully-blocked report floors at 0, never < 0")
        rep3 = qa.Report(POL)
        for c in qa.CHECKS:
            rep3.warn(c, "fixture", 100)  # capped at each weight
        self.assertEqual(rep3.score(), 0)
        self.assertTrue(0 <= rep3.score() <= 100)

    def test_81_stays_81_when_visual_semantics_passes(self):
        ep, script = self._episode_with_plan()
        try:
            # issue #32: a degraded photo mix is now an INTENTIONAL warning,
            # so this invariance fixture uses a non-degraded plan (no
            # photo-designated scenes) to isolate the weight-invariance
            # property it is named for.
            plan = common.load_json(os.path.join(ep, "visual_plan.json"))
            for sc in plan["scenes"]:
                sc["photo_designated"] = False
            (plan.get("photo_policy") or {})["degraded"] = False
            common.save_json(os.path.join(ep, "visual_plan.json"), plan)
            rep = self._report_with_19_points()
            self.assertEqual(rep.deductions["visual_semantics"], 0)
            qa.check_visuals(rep, ep, script, None, POL, no_frames=True)
            self.assertEqual(rep.deductions["visual_semantics"], 0,
                             "a PASSING visual_semantics check adds zero deduction")
            self.assertEqual(rep.checks["visual_semantics"], "pass")
            self.assertEqual(rep.score(), 81,
                             "a passing new check must not raise any score")
            approved = (not rep.blocking) and rep.score() >= common.min_qa_score(POL)
            self.assertFalse(approved, "81 < 85 must still not be approved")
        finally:
            shutil.rmtree(ep, ignore_errors=True)

    def test_failing_visual_semantics_only_deducts_its_own_weight(self):
        ep, script = self._episode_with_plan()
        try:
            plan = vp.build_visual_plan(script, POL, allow_external=False)
            plan["scenes"][1]["visual_category"] = "stack-trace"  # code on PRODUCT
            common.save_json(os.path.join(ep, "visual_plan.json"), plan)
            rep = self._report_with_19_points()
            qa.check_visuals(rep, ep, script, None, POL, no_frames=True)
            self.assertEqual(rep.deductions["visual_semantics"], qa.WEIGHTS["visual_semantics"],
                             "the failing check deducts exactly its own weight")
            self.assertEqual(rep.score(), 81 - qa.WEIGHTS["visual_semantics"])
            self.assertTrue(any("visual_semantics" in b for b in rep.blocking))
            approved = (not rep.blocking) and rep.score() >= common.min_qa_score(POL)
            self.assertFalse(approved)
        finally:
            shutil.rmtree(ep, ignore_errors=True)

    def test_a_blocker_still_vetoes_at_high_score(self):
        rep = qa.Report(POL)
        rep.warn("buffer_readiness", "fixture deduction", 2)
        self.assertEqual(rep.score(), 98)
        rep.block("duplicate_check", "fixture blocker")
        self.assertEqual(rep.score(), 98 - qa.WEIGHTS["duplicate_check"], ">= 85")
        self.assertGreaterEqual(rep.score(), 85)
        approved = (not rep.blocking) and rep.score() >= common.min_qa_score(POL)
        self.assertFalse(approved, "a blocker vetoes regardless of the score")

    def test_reviewer_threshold_is_85_and_unchanged(self):
        self.assertEqual(common.min_qa_score(POL), 85, "the QA approval bar is 85")
        with open(os.path.join(ROOT, "build", "qa_supervisor.py"),
                  encoding="utf-8") as f:
            sup = f.read()
        self.assertIn("if score < 85:", sup, "the reviewer bar stays hard-coded 85")
        script, topic = script_0919()
        for score, should_block in ((84, True), (85, False), (100, False)):
            rep = qa.Report(POL)
            qa.check_reviewer_output(rep, script, topic, {
                "approved": True, "score": score, "technology_relevance": True,
                "metacognition_relevance": True, "blocking_errors": [],
                "unsupported_claims": []}, POL)
            hit = [b for b in rep.blocking if "< 85" in b]
            self.assertEqual(bool(hit), should_block, f"score {score}: {rep.blocking}")
        # a reviewer blocker still blocks even at a near-perfect score
        rep = qa.Report(POL)
        qa.check_reviewer_output(rep, script, topic, {
            "approved": False, "score": 98, "technology_relevance": True,
            "metacognition_relevance": True, "blocking_errors": ["x"],
            "unsupported_claims": []}, POL)
        self.assertTrue(any("reviewer" in b for b in rep.blocking), rep.blocking)


if __name__ == "__main__":
    unittest.main()
