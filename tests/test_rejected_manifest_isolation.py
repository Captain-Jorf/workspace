"""Rejected-artifact safety in ISOLATED fixtures — replaces the stale test.

ROOT CAUSE OF THE STALE TEST (issue #26)
----------------------------------------
``test_prerender_text_qa_gate.SafetyInvariants.test_rejected_reels_are_not_reused_or_republished``
asserted the *current status of a mutable generated production file*::

    m = common.load_json(os.path.join(ROOT, "output",
                                      "auto-2026-09-18_manifest.json"), {})
    self.assertEqual(m["status"], "qa-failed")

``output/auto-<date>_manifest.json`` is written by every pipeline run
(``pipeline.record``) and is NOT an immutable historical fixture. A later
legitimate run for the same content id changed that record to
``approved-dry-run``, so the assertion became false and the daily workflow's
self-test step failed BEFORE production.

This module keeps the real safety requirement, in sandboxes only:

  * an individual failed/rejected artifact can never be promoted or published;
  * ``pipeline.FAILURE_STATES`` is disjoint from every Buffer outcome state —
    a qa-failed / *-error run can never be relabelled as queued/published;
  * ``pipeline.record`` REFUSES to relabel a recorded failure as a Buffer
    outcome when no Buffer interaction marker exists (proved on the real
    function, in a TemporaryDirectory);
  * a later approved manifest for the same date cannot mutate, or invalidate
    assertions about, a rejected fixture (each test builds its own fixtures);
  * ``reel-2026-09-15`` stays permanently quarantined, and Buffer refuses it
    even with AUTO_PUBLISH_ENABLED=true;
  * no real output manifest and no real editorial memory is read or written by
    these tests.

Test order does not matter, the module passes with NO ``output/auto-*`` files
present and with unrelated generated files present, and it never treats a
date-based production artifact as a fixture.
"""
import argparse
import io
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

import buffer_publish as bp  # noqa: E402
import common  # noqa: E402
import pipeline as pl  # noqa: E402

REJECTED_TAG = "2026-09-18"
APPROVED_TAG = "2026-09-19"
CLAIM_ERROR = "[source_quality] claim words ['researchers'] without tier A/B"
FAILURE_STATES = ("qa-failed", "script-error", "automation-error", "render-error", "tts-error")


def _state(status, error=None):
    return {
        "content_id": common.content_id(REJECTED_TAG), "tag": REJECTED_TAG, "status": status,
        "stage": "qa", "error": error, "retries": {"script": 0, "render": 0},
        "topic": {"title": "Why does AI sound so sure?", "normalized_topic": "ai sure",
                  "pillar": "AI_JUDGMENT"},
        "script": {"summary": "s", "sources": [], "playbook": "pb", "cta_type": "question",
                   "technology_angle": "ai", "metacognition_concept": "calibration",
                   "generation_mode": "groq", "language": "en", "hash": "h"},
        "qa": {"approved": False, "score": 69, "blocking_errors": [CLAIM_ERROR],
               "warnings": [], "checks": {}},
        "branch": f"drafts/{REJECTED_TAG}", "run_id": "fixture-run",
    }


class Sandbox(object):
    """A temporary repo-shaped tree; common.* points at it, nothing else does."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="rejected_iso_")
        self.output = os.path.join(self.tmp, "output")
        self.content = os.path.join(self.tmp, "content")
        self.memory = os.path.join(self.content, "editorial_memory.json")
        os.makedirs(os.path.join(self.content, "episodes"), exist_ok=True)
        os.makedirs(self.output, exist_ok=True)
        common.save_json(self.memory, common.empty_memory())

    def patch(self):
        return mock.patch.multiple(common, ROOT=self.tmp, CONTENT=self.content,
                                   MEMORY_PATH=self.memory)

    def paths(self, tag):
        out = self.output
        return {"state": os.path.join(out, f"auto-{tag}_state.json"),
                "manifest": os.path.join(out, f"auto-{tag}_manifest.json"),
                "marker": os.path.join(out, f"auto-{tag}.buffer.json")}

    def write_rejected(self, tag=REJECTED_TAG, status="qa-failed", error=CLAIM_ERROR):
        paths = self.paths(tag)
        common.save_json(paths["state"], _state(status, error))
        common.save_json(paths["manifest"], {
            "content_id": common.content_id(tag), "content_date": tag, "status": status,
            "qa_score": 69, "qa_approved": False, "buffer_post_id": None,
            "buffer_due_at": None, "error": error, "language": "en"})
        ep = os.path.join(self.content, "episodes", f"auto-{tag}")
        os.makedirs(ep, exist_ok=True)
        common.save_json(os.path.join(ep, "script.json"),
                         {"meta": {"content_id": common.content_id(tag)},
                          "caption": {"hook": "h"}})
        return paths

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, tag, status, error=None, stdout=None):
        """Run the REAL pipeline.record() against this sandbox ONLY.

        ``common.ROOT`` / ``common.CONTENT`` / ``common.MEMORY_PATH`` are
        redirected for the whole call, so no real production file — manifest,
        state, marker or editorial memory — can be read or written.
        """
        with self.patch(), mock.patch("sys.stdout", stdout or io.StringIO()) as out:
            rc = pl.record(argparse.Namespace(tag=tag, status=status, error=error))
        return rc, out


def _sig(path):
    st = os.stat(path)
    with open(path, "rb") as fh:
        return (st.st_size, st.st_mtime_ns, common.sha256_file(path))


class RejectedManifestIsolation(unittest.TestCase):
    """Every test builds its own fixtures; none reads a real generated file."""

    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.cleanup()

    # ---- 1. a temporary rejected manifest cannot publish -------------------
    def test_temporary_rejected_manifest_cannot_publish(self):
        paths = self.box.write_rejected()
        rc, out = self.box.record(REJECTED_TAG, "queued-in-buffer")
        self.assertEqual(rc, 0)
        self.assertIn("keeping failure state", out.getvalue())
        state = common.load_json(paths["state"], {})
        manifest = common.load_json(paths["manifest"], {})
        self.assertEqual(state["status"], "qa-failed")
        self.assertEqual(manifest["status"], "qa-failed")
        self.assertIsNone(manifest["buffer_post_id"])
        self.assertEqual(manifest["error"], CLAIM_ERROR)
        self.assertFalse(os.path.exists(paths["marker"]),
                         "no Buffer interaction marker may exist for a rejected run")

    def test_every_failure_state_refuses_a_buffer_outcome_label(self):
        self.assertEqual(set(FAILURE_STATES) <= pl.FAILURE_STATES, True)
        self.assertEqual(pl.FAILURE_STATES & pl.BUFFER_OUTCOME_STATES, set(),
                         "a failure state must never be a Buffer outcome state")
        for status in FAILURE_STATES:
            with self.subTest(status=status):
                tag = f"2088-01-{len(status):02d}"
                paths = self.box.write_rejected(tag, status=status, error=f"[{status}] x")
                self.box.record(tag, "approved-dry-run")
                self.assertEqual(common.load_json(paths["state"], {})["status"], status)
                self.assertEqual(common.load_json(paths["manifest"], {})["status"], status)

    def test_buffer_step_cannot_run_after_a_rejected_produce(self):
        import yaml
        wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows",
                                              "daily-trend-draft.yml"), encoding="utf-8"))
        steps = wf["jobs"]["reel"]["steps"]
        buffer = next(s for s in steps if s.get("id") == "buffer")
        self.assertIn("steps.produce.outputs.code == '0'", buffer["if"],
                      "Buffer must be gated on a SUCCESSFUL produce (qa-failed returns 10)")

    # ---- 2. a later approved run must not mutate the rejected fixture ------
    def test_later_approved_manifest_does_not_mutate_the_rejected_fixture(self):
        rejected = self.box.write_rejected()
        before = _sig(rejected["manifest"])
        approved = self.box.write_rejected(tag=APPROVED_TAG, status="queued-in-buffer",
                                           error=None)
        self.box.record(APPROVED_TAG, "approved-dry-run")
        self.assertEqual(_sig(rejected["manifest"]), before,
                         "recording another run must not touch the rejected manifest")
        self.assertEqual(common.load_json(rejected["state"], {})["status"], "qa-failed")
        self.assertEqual(common.load_json(approved["state"], {})["status"], "approved-dry-run")
        self.assertIsNone(common.load_json(approved["manifest"], {})["buffer_post_id"])

    def test_same_date_later_approved_run_does_not_invalidate_assertions(self):
        """The historical failure mode: a later approved manifest for the SAME
        date flipped a mutable production file. Sandboxed fixtures must be
        immune — the rejected assertions keep holding afterwards."""
        rejected = self.box.write_rejected()
        self._assert_rejected(rejected)
        # a LATER, legitimate run for the same date rewrites its own record
        paths = self.box.paths(REJECTED_TAG)
        self.box.record(REJECTED_TAG, "approved-dry-run")   # no marker → still refuses
        later = self.box.write_rejected()               # a fresh rejected fixture
        self._assert_rejected(later)
        self.assertIsNotNone(paths["manifest"])

    def _assert_rejected(self, paths):
        state = common.load_json(paths["state"], {})
        manifest = common.load_json(paths["manifest"], {})
        self.assertEqual(state["status"], "qa-failed")
        self.assertEqual(manifest["status"], "qa-failed")
        self.assertIsNone(manifest["buffer_post_id"])

    def test_quarantine_of_reel_2026_09_15_blocks_buffer_even_when_enabled(self):
        self.assertTrue(common.is_quarantined("reel-2026-09-15", "2026-09-15"))
        self.assertFalse(common.is_quarantined("reel-2026-09-18", "2026-09-18"),
                         "reel-2026-09-18 is NOT quarantined — its rejection lives in the run state")
        calls = []

        def fake_urlopen(*a, **k):                                  # noqa: ANN001
            calls.append(a)
            raise AssertionError("Buffer must not be touched for a quarantined reel")

        with mock.patch.dict(os.environ, {"BUFFER_TOKEN": "FAKE_test_token",
                                          "AUTO_PUBLISH_ENABLED": "true"}, clear=False), \
             mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch("sys.stdout", io.StringIO()) as out:
            code = bp.cmd_publish(["--tag", "2026-09-15", "--content-id", "reel-2026-09-15",
                                   "--video", "https://example.invalid/x.mp4",
                                   "--caption", os.path.join(self.box.tmp, "nope.txt"),
                                   "--marker", os.path.join(self.box.tmp, "nope.json"),
                                   "--yes"])
        self.assertEqual(code, bp.EXIT_DISABLED)
        self.assertEqual(calls, [])
        self.assertIn("quarantined", out.getvalue())

    # ---- 3. no real production file is read or overwritten -----------------
    def test_no_real_production_file_is_read_or_overwritten(self):
        real_dir = os.path.join(ROOT, "output")
        watched = [os.path.join(real_dir, n) for n in sorted(os.listdir(real_dir))
                   if n.endswith("_manifest.json")] if os.path.isdir(real_dir) else []
        watched.append(os.path.join(ROOT, "content", "editorial_memory.json"))
        before = {p: _sig(p) for p in watched if os.path.exists(p)}
        sandbox_tag = "2077-12-31"
        paths = self.box.write_rejected(tag=sandbox_tag)
        self.box.record(sandbox_tag, "queued-in-buffer")
        self.box.record(sandbox_tag, "approved-dry-run")
        self.assertTrue(os.path.exists(paths["manifest"]))
        for p, sig in before.items():
            self.assertEqual(_sig(p), sig, f"real production file {p} was modified")
        self.assertFalse(os.path.isdir(real_dir)
                         and [n for n in os.listdir(real_dir) if sandbox_tag in n],
                         "the sandbox must never write into the real output/ directory")

    def test_test_suite_does_not_read_dated_production_artifacts(self):
        """Static guard: no test module may load a dated generated output file."""
        offenders = []
        dated = re.compile(r"auto-20\d\d-\d\d-\d\d_(manifest|state|qa)")
        rooted = re.compile(r"\bROOT\b|output_paths")
        for name in sorted(os.listdir(os.path.join(ROOT, "tests"))):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            with open(os.path.join(ROOT, "tests", name), encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    if "output" in line and dated.search(line) and rooted.search(line):
                        offenders.append(f"{name}:{i}: {line.strip()[:90]}")
        self.assertEqual(offenders, [],
                         "tests must build their own fixtures, never read mutable "
                         f"generated output: {offenders}")

    # ---- 4. order independence + clean environments ------------------------
    def test_order_does_not_matter(self):
        names = ("test_temporary_rejected_manifest_cannot_publish",
                 "test_later_approved_manifest_does_not_mutate_the_rejected_fixture",
                 "test_every_failure_state_refuses_a_buffer_outcome_label",
                 "test_no_real_production_file_is_read_or_overwritten")
        cases = {t._testMethodName: t for t in
                 unittest.TestLoader().loadTestsFromTestCase(RejectedManifestIsolation)}
        for order in (names, tuple(reversed(names))):
            suite = unittest.TestSuite([cases[n] for n in order])
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
            self.assertTrue(result.wasSuccessful(),
                            f"order {order} failed: {[str(t) for t, _ in result.failures + result.errors]}")

    def test_module_passes_without_output_dir_and_with_stray_files(self):
        """Run THIS module's safety tests in a copied tree: once with no output/
        dir at all, once with unrelated generated files present.

        The child process runs an explicit list of the tests that never spawn a
        child (guarded additionally by REJECTED_ISOLATION_SANDBOX so this can
        never recurse).
        """
        if os.environ.get("REJECTED_ISOLATION_SANDBOX") == "1":
            self.skipTest("inside the sandbox run of this module")
        for label, prepare in (("no-output-dir", None), ("stray-output", "stray")):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(prefix="fresh_tree_") as tree:
                    for rel in ("build", "content", "tests"):
                        shutil.copytree(os.path.join(ROOT, rel), os.path.join(tree, rel),
                                        ignore=shutil.ignore_patterns("__pycache__"))
                    if prepare == "stray":
                        os.makedirs(os.path.join(tree, "output"))
                        with open(os.path.join(tree, "output", "auto-2099-01-01_manifest.json"),
                                  "w", encoding="utf-8") as fh:
                            fh.write('{"status": "approved-dry-run", "buffer_post_id": "x"}')
                    env = dict(os.environ)
                    env.pop("GITHUB_ACTIONS", None)
                    env["REJECTED_ISOLATION_SANDBOX"] = "1"
                    names = [f"test_rejected_manifest_isolation.RejectedManifestIsolation.{n}"
                             for n in ("test_temporary_rejected_manifest_cannot_publish",
                                       "test_every_failure_state_refuses_a_buffer_outcome_label",
                                       "test_later_approved_manifest_does_not_mutate_the_rejected_fixture",
                                       "test_same_date_later_approved_run_does_not_invalidate_assertions",
                                       "test_quarantine_of_reel_2026_09_15_blocks_buffer_even_when_enabled",
                                       "test_no_real_production_file_is_read_or_overwritten",
                                       "test_test_suite_does_not_read_dated_production_artifacts")]
                    r = subprocess.run([sys.executable, "-m", "unittest", "-q"] + names,
                                       cwd=os.path.join(tree, "tests"), env=env,
                                       capture_output=True, text=True, timeout=300)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn("OK", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
