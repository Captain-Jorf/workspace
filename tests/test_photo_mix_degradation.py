"""Photo-mix validation: fail-soft retrieval, honest reporting (issue #32 §9).

reel-2026-09-24 designated 2 photo slots, retrieved 0, fell back twice — and
the Issue report had NO reason for the failures while the reel was still
described as visually complete. Now:

* retrieval stays fail-soft: a broken photo service NEVER fails the pipeline
  (technical success does not require network success);
* every designated slot records one of the safe failure categories;
* a zero-photo mix sets photo_mix_degraded → QA emits a human-review WARNING
  (never a blocker) and the Issue report shows the aggregate reason counts;
* when photos DO arrive, the plan keeps 2-3 DISTINCT assets and all existing
  enforcement (CC0/PDM only, https, provenance manifest) applies.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asset_fetch as af  # noqa: E402
import common  # noqa: E402
import qa_supervisor as qa  # noqa: E402
import report_issue  # noqa: E402
import visual_plan as vp  # noqa: E402
from test_glyph_regression_issue32 import make_script, load_fixture  # noqa: E402

POL = common.policy()


class FailSoftDegradation(unittest.TestCase):
    def test_disabled_service_degrades_softly_and_explains_why(self):
        script = make_script(load_fixture())
        with mock.patch.dict(os.environ, {"ASSET_FETCH": "0"}):
            plan = vp.build_visual_plan(script, POL, allow_external=True)
        designated = [sc for sc in plan["scenes"] if sc.get("photo_designated")]
        self.assertGreaterEqual(len(designated), 2,
                                "the ATTENTION fixture designates photo slots")
        # zero retrieved, every slot fell back to a PROCEDURAL visual
        retrieved = [sc for sc in plan["scenes"]
                     if (sc.get("asset") or {}).get("kind") == "external"]
        self.assertEqual(retrieved, [])
        for sc in designated:
            self.assertEqual((sc.get("asset") or {}).get("kind"), "procedural")
            self.assertEqual(sc.get("retrieval_reason"), "disabled")
        pp = plan["photo_policy"]
        self.assertEqual(pp.get("photos_retrieved"), 0)
        self.assertTrue(pp.get("degraded"),
                        "zero retrieved photos on designated slots must degrade the mix")
        self.assertGreaterEqual(pp.get("retrieval_outcomes", {}).get("disabled", 0), 2)
        # and the plan itself is still COMPLETE and gate-clean (fail-soft)
        self.assertEqual(vp.visual_semantic_issues(plan, script, POL), [])

    def test_search_failure_records_a_specific_reason_not_silence(self):
        script = make_script(load_fixture())

        def broken_fetch(query, timeout=25, skip_urls=()):
            af._outcome(af.OUTCOME_SEARCH_TIMEOUT)
            return None

        with mock.patch.dict(os.environ, {"ASSET_FETCH": "1"}), \
             mock.patch.object(af, "fetch_image", broken_fetch), \
             mock.patch.object(af, "enabled", lambda: True):
            plan = vp.build_visual_plan(script, POL, allow_external=True)
        pp = plan["photo_policy"]
        self.assertEqual(pp.get("photos_retrieved"), 0)
        self.assertTrue(pp.get("degraded"))
        self.assertGreaterEqual(pp.get("retrieval_outcomes", {}).get("search_timeout", 0), 1)
        reasons = {sc.get("retrieval_reason")
                   for sc in plan["scenes"] if sc.get("photo_designated")}
        self.assertIn("search_timeout", reasons)

    def test_successful_retrievals_are_distinct_and_not_degraded(self):
        script = make_script(load_fixture())
        calls = {"n": 0}

        def good_fetch(query, timeout=25, skip_urls=()):
            calls["n"] += 1
            n = calls["n"]
            af._outcome(af.OUTCOME_RETRIEVED)
            return {"kind": "external", "id": f"ext-sha{n:012d}",
                    "url": f"https://example.org/p{n}",
                    "asset_url": f"https://cdn.example.org/img/{n}.jpg",
                    "creator": "PD", "title": f"t{n}", "license": "cc0",
                    "retrieved_utc": "2026-09-24T00:00:00+00:00",
                    "query_sanitized": query, "sha256": f"{n:064d}"[:64],
                    "path": f"/tmp/fake{n}.jpg", "origin": "openverse-api"}

        with mock.patch.dict(os.environ, {"ASSET_FETCH": "1"}), \
             mock.patch.object(af, "fetch_image", good_fetch), \
             mock.patch.object(af, "enabled", lambda: True):
            plan = vp.build_visual_plan(script, POL, allow_external=True)
        retrieved = [sc for sc in plan["scenes"]
                     if (sc.get("asset") or {}).get("kind") == "external"]
        self.assertGreaterEqual(len(retrieved), 2,
                                "the mix wants 2-3 distinct photographs")
        self.assertLessEqual(len(retrieved), 3)
        ids = [(sc.get("asset") or {}).get("id") for sc in retrieved]
        self.assertEqual(len(ids), len(set(ids)), "retrieved photos must be distinct")
        self.assertFalse(plan["photo_policy"].get("degraded"))


class QAWarningNotBlocker(unittest.TestCase):
    def _episode(self, plan):
        ep = tempfile.mkdtemp(prefix="qa_pmix_")
        script = make_script(load_fixture())
        common.save_json(os.path.join(ep, "script.json"), script)
        common.save_json(os.path.join(ep, "visual_plan.json"), plan)
        return ep, script

    def test_degraded_mix_warns_but_does_not_block(self):
        script = make_script(load_fixture())
        with mock.patch.dict(os.environ, {"ASSET_FETCH": "0"}):
            plan = vp.build_visual_plan(script, POL, allow_external=True)
        ep, script = self._episode(plan)
        try:
            rep = qa.Report(POL)
            qa.check_visuals(rep, ep, script, None, POL, no_frames=True)
            warns = [w for w in rep.warnings if "photo_mix_degraded" in w]
            self.assertTrue(warns, f"expected a photo_mix_degraded warning: {rep.warnings}")
            self.assertIn("designated=", warns[0])
            self.assertIn("disabled", warns[0],
                          "the warning must explain WHY retrieval failed")
            self.assertFalse(any("photo" in b for b in rep.blocking),
                             "photo degradation must stay a WARNING, never a blocker")
        finally:
            shutil.rmtree(ep, ignore_errors=True)

    def test_clean_mix_adds_no_photo_warning(self):
        script = make_script(load_fixture())
        plan = vp.build_visual_plan(script, POL, allow_external=False)
        for sc in plan["scenes"]:
            sc["photo_designated"] = False
        (plan.get("photo_policy") or {})["degraded"] = False
        ep, script = self._episode(plan)
        try:
            rep = qa.Report(POL)
            qa.check_visuals(rep, ep, script, None, POL, no_frames=True)
            self.assertFalse(any("photo_mix_degraded" in w for w in rep.warnings))
        finally:
            shutil.rmtree(ep, ignore_errors=True)


class IssueReportExplainsWhy(unittest.TestCase):
    def test_report_section_shows_reason_counts_and_degraded_flag(self):
        script = make_script(load_fixture())
        with mock.patch.dict(os.environ, {"ASSET_FETCH": "0"}):
            plan = vp.build_visual_plan(script, POL, allow_external=True)
        prov = vp.photo_provenance_summary(plan)
        qa_result = {"details": {"visuals": {"photo_provenance": prov}}}
        lines = report_issue.visual_provenance_section(qa_result)
        joined = "\n".join(lines)
        self.assertIn("retrieval outcomes", joined)
        self.assertIn("disabled", joined)
        self.assertIn("photo_mix_degraded", joined)
        self.assertIn("NOT visually complete", joined)
        # counts only — never URLs or remote bodies
        self.assertNotIn("http://", joined)
        self.assertNotIn("https://", joined)

    def test_provenance_summary_survives_legacy_plans(self):
        # a plan without the new photo_policy block (old artifact) still gets
        # a safe summary via back-computation from the scenes
        legacy = {"scenes": [
            {"scene_id": "s1", "visual_category": "brand-mark",
             "asset": {"kind": "repo", "id": "repo:x", "origin": "repository-assets"},
             "photo_designated": False},
            {"scene_id": "s2", "visual_category": "focus-meter",
             "asset": {"kind": "procedural", "id": "proc:focus-meter:s2"},
             "photo_designated": True, "retrieval_reason": "search_timeout"},
        ]}
        prov = vp.photo_provenance_summary(legacy)
        self.assertEqual(prov["photo_designated"], 1)
        self.assertEqual(prov["photos_retrieved"], 0)
        self.assertTrue(prov["degraded"])
        self.assertEqual(prov["retrieval_outcomes"].get("search_timeout"), 1)


if __name__ == "__main__":
    unittest.main()
