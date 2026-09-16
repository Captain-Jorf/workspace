"""Outcome classification: a QA rejection stays a QA/failure state.

Regression for run 35043984004: the QA supervisor rejected the reel (stage `qa`,
score 69/100), push/verify were skipped, yet the final workflow step reported
`no post today (buffer-error)` — because the buffer step ran anyway (empty
`steps.verify.outputs.code` quirk) and the record step trusted the Buffer step
status FIRST. Buffer errors are now reserved for ACTUAL Buffer failures:

  * the buffer step is gated on produce AND push AND verify all succeeding;
  * a missing public URL is a precondition failure → automation-error, never
    buffer-error, and Buffer is never touched;
  * the record cascade checks produce failure BEFORE any Buffer status;
  * pipeline.record() refuses to relabel a recorded failure state as a Buffer
    outcome when no Buffer marker exists.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import pipeline as pl  # noqa: E402

WF = os.path.join(ROOT, ".github", "workflows", "daily-trend-draft.yml")
TAG = "2077-05-05"


def wf():
    return yaml.safe_load(open(WF, encoding="utf-8"))


def step(wf, ident):
    for s in wf["jobs"]["reel"]["steps"]:
        if s.get("id") == ident or s.get("name", "").startswith(ident):
            return s
    raise AssertionError(f"step {ident!r} not found")


class WorkflowGateAssertions(unittest.TestCase):
    def setUp(self):
        self.w = wf()
        self.buffer = step(self.w, "buffer")
        self.record = step(self.w, "record outcome")

    def test_buffer_gated_on_produce_push_and_verify_success(self):
        cond = self.buffer["if"]
        for needle in ("steps.produce.outputs.code == '0'",
                       "steps.push.outcome == 'success'",
                       "steps.verify.outcome == 'success'",
                       "steps.verify.outputs.code == '0'"):
            self.assertIn(needle, cond, cond)

    def test_buffer_precondition_guard_before_python(self):
        run = self.buffer["run"]
        guard = run.find('if [ -z "${RAW_URL:-}" ]')
        py = run.find("python3 build/buffer_publish.py")
        self.assertGreaterEqual(guard, 0, "missing RAW_URL precondition guard")
        self.assertGreater(py, guard, "guard must run before the publisher")
        self.assertIn("status=automation-error", run[guard:py],
                      "empty RAW_URL must report automation-error, never buffer-error")

    def test_buffer_error_reserved_for_actual_buffer_failures(self):
        run = self.buffer["run"]
        self.assertIn("2|5|6) st=buffer-error", run)
        self.assertIn("*) st=automation-error", run)
        # caption / public-url precondition exits must NOT map to buffer-error
        self.assertNotIn("3|4) st=buffer-error", run)

    def test_record_cascade_produce_failure_first(self):
        run = self.record["run"]
        i_p = run.find('[ "$P" != "0" ]')
        i_push = run.find('[ "$PUSH" != "success" ]')
        i_v = run.find('[ "$V" != "0" ]')
        i_bst = run.find('[ -n "$BST" ]')
        for i in (i_p, i_push, i_v, i_bst):
            self.assertGreaterEqual(i, 0)
        self.assertLess(i_p, i_push)
        self.assertLess(i_push, i_v)
        self.assertLess(i_v, i_bst)

    def test_buffer_token_still_exposed_to_exactly_two_steps(self):
        holders = [s.get("id") or s.get("name") for s in self.w["jobs"]["reel"]["steps"]
                   if "BUFFER_TOKEN" in str((s.get("env", {}) or {}))]
        self.assertEqual(set(holders), {"buffer",
                                        "Buffer metrics sync + retention (read-only API; never fails the run)"})

    def test_auto_publish_flag_untouched(self):
        with open(WF, encoding="utf-8") as f:
            raw = f.read()
        self.assertIn('AUTO_PUBLISH_ENABLED: ${{ vars.AUTO_PUBLISH_ENABLED }}', raw)
        self.assertNotIn("AUTO_PUBLISH_ENABLED: true", raw.replace("${{ vars.AUTO_PUBLISH_ENABLED }}", ""))


class RecordCascadeSimulation(unittest.TestCase):
    """Run the REAL record step script against a local bare origin."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="recsim_")
        cls.origin = os.path.join(cls.tmp, "origin.git")
        cls.ws = os.path.join(cls.tmp, "ws")
        subprocess.run(["git", "clone", "-q", "--bare", ROOT, cls.origin], check=True, capture_output=True)
        subprocess.run(["git", "clone", "-q", cls.origin, cls.ws], check=True, capture_output=True)
        subprocess.run(["git", "-C", cls.ws, "checkout", "-q", "-B", "sim-branch"], check=True, capture_output=True)
        subprocess.run(["git", "-C", cls.ws, "push", "-q", "-u", "origin", "sim-branch"], check=True, capture_output=True)
        for rel in ("build", "content/editorial_policy.json", "content/editorial_memory.json",
                    "content/calendar.json"):
            src, dst = os.path.join(ROOT, rel), os.path.join(cls.ws, rel)
            if os.path.isdir(src):
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy(src, dst)
        subprocess.run(["git", "-C", cls.ws, "add", "-A", "build", "content"], check=True, capture_output=True)
        subprocess.run(["git", "-C", cls.ws, "-c", "user.name=sim", "-c", "user.email=sim@x",
                        "commit", "-q", "-m", "sim: working-tree sources"], capture_output=True)
        subprocess.run(["git", "-C", cls.ws, "push", "-q", "origin", "sim-branch"], check=True, capture_output=True)
        cls.gh_env = os.path.join(cls.tmp, "gh_env")
        cls.gh_out = os.path.join(cls.tmp, "gh_out")
        cls.state = os.path.join(cls.ws, "output", f"auto-{TAG}_state.json")
        w = wf()
        cls.record_script = step(w, "record outcome")["run"].replace("${{", "__").replace("}}", "__")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _set_state(self, status):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        json.dump({"content_id": f"reel-{TAG}", "tag": TAG, "status": status,
                   "topic": {"title": "t", "normalized_topic": "t", "pillar": "AI_JUDGMENT"},
                   "script": {"summary": "s", "sources": [], "playbook": "pb", "cta_type": "question",
                              "technology_angle": "ai", "metacognition_concept": "calibration",
                              "generation_mode": "groq", "language": "en", "hash": "h"},
                   "qa": {"approved": False, "score": 69, "blocking_errors": ["x"],
                          "warnings": [], "checks": {}},
                   "retries": {"script": 0, "render": 0}, "branch": f"drafts/{TAG}", "run_id": "t"},
                  open(self.state, "w", encoding="utf-8"))

    def _run(self, env):
        e = {k: v for k, v in os.environ.items() if k not in ("BUFFER_TOKEN", "AUTO_PUBLISH_ENABLED")}
        e.update({"GITHUB_ENV": self.gh_env, "GITHUB_OUTPUT": self.gh_out, "TAG": TAG,
                  "GITHUB_RUN_ID": "1", "GITHUB_REPOSITORY": "Captain-Jorf/workspace",
                  "GITHUB_REF_NAME": "sim-branch"})
        e.update(env)
        open(self.gh_env, "w").close()
        open(self.gh_out, "w").close()
        r = subprocess.run(["bash", "-c", self.record_script], cwd=self.ws, env=e,
                           capture_output=True, text=True, timeout=300)
        return r, open(self.gh_env).read()

    def test_qa_rejection_stays_qa_failed_even_with_buffer_error_status(self):
        # The exact shape of run 35043984004: produce exit 10 (qa), push skipped,
        # verify skipped, and a phantom buffer-error status.
        self._set_state("qa-failed")
        r, env = self._run({"P": "10", "V": "", "BST": "buffer-error", "PUSH": "skipped"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("FINAL_STATUS=qa-failed", env)
        manifest = json.load(open(os.path.join(self.ws, "output", f"auto-{TAG}_manifest.json"), encoding="utf-8"))
        self.assertEqual(manifest["status"], "qa-failed")
        mem = json.load(open(os.path.join(self.ws, "content", "editorial_memory.json"), encoding="utf-8"))
        e = next(x for x in mem["entries"] if x.get("content_id") == f"reel-{TAG}")
        self.assertEqual(e["status"], "qa-failed")

    def test_other_cascade_cases(self):
        self._set_state("approved")
        r, env = self._run({"P": "0", "V": "", "BST": "", "PUSH": "failure"})
        self.assertIn("FINAL_STATUS=automation-error", env)
        r, env = self._run({"P": "0", "V": "20", "BST": "", "PUSH": "success"})
        self.assertIn("FINAL_STATUS=qa-failed", env)
        r, env = self._run({"P": "0", "V": "0", "BST": "queue-full", "PUSH": "success"})
        self.assertIn("FINAL_STATUS=queue-full", env)
        r, env = self._run({"P": "0", "V": "0", "BST": "buffer-error", "PUSH": "success"})
        # genuine Buffer failure after a verified URL stays buffer-error
        self.assertIn("FINAL_STATUS=buffer-error", env)

    def test_produce_stage_error_uses_state_file(self):
        self._set_state("tts-error")
        r, env = self._run({"P": "30", "V": "", "BST": "buffer-error", "PUSH": "skipped"})
        self.assertIn("FINAL_STATUS=tts-error", env)


class BufferStepPrecondition(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bufpre_")
        # the buffer step runs `python3 build/buffer_publish.py` from the repo
        # root — provide the real sources (read-only; no media files needed for
        # the precondition exits 3/4 which happen before any API call)
        shutil.copytree(os.path.join(ROOT, "build"), os.path.join(self.tmp, "build"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(ROOT, "content"), os.path.join(self.tmp, "content"))
        w = wf()
        self.script = step(w, "buffer")["run"].replace("${{", "__").replace("}}", "__")
        self.gh_out = os.path.join(self.tmp, "gh_out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, env):
        open(self.gh_out, "w").close()
        e = {k: v for k, v in os.environ.items() if k not in ("BUFFER_TOKEN", "AUTO_PUBLISH_ENABLED")}
        e.update({"GITHUB_OUTPUT": self.gh_out, "GITHUB_ENV": os.path.join(self.tmp, "gh_env"),
                  "TAG": TAG, "DRY": "true", "RAW_URL": ""})
        e.update(env)
        r = subprocess.run(["bash", "-c", self.script], cwd=self.tmp, env=e,
                           capture_output=True, text=True, timeout=120)
        outputs = dict(l.split("=", 1) for l in open(self.gh_out).read().splitlines() if "=" in l)
        return r, outputs

    def test_empty_raw_url_is_automation_error_and_never_touches_buffer(self):
        r, out = self._run({})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.get("status"), "automation-error")
        self.assertIn("precondition", r.stdout)
        self.assertNotIn("buffer step exit=", r.stdout, "publisher python must never run without a public URL")

    def test_missing_caption_is_automation_error_not_buffer_error(self):
        # token present, dry-run, public URL present, but the caption file is
        # missing → buffer_publish exits 3 (precondition) → automation-error.
        r, out = self._run({"DRY": "false", "RAW_URL": "https://raw.githubusercontent.com/x/y.mp4",
                            "BUFFER_TOKEN": "FAKE_test_only_token"})
        self.assertEqual(out.get("status"), "automation-error", r.stdout + r.stderr)

    def test_non_public_url_is_automation_error_not_buffer_error(self):
        # caption present, but the "public" URL is not public (localhost) →
        # buffer_publish exits 4 (precondition) → automation-error, no API call.
        cap = os.path.join(self.tmp, "output", f"auto-{TAG}_caption.txt")
        os.makedirs(os.path.dirname(cap), exist_ok=True)
        with open(cap, "w") as f:
            f.write("Hook. " + "x" * 200 + "\n\n#metacognition #metacognitionhq #AI\n")
        r, out = self._run({"DRY": "true", "RAW_URL": "http://127.0.0.1:8000/y.mp4",
                            "BUFFER_TOKEN": "FAKE_test_only_token"})
        self.assertEqual(out.get("status"), "automation-error", r.stdout + r.stderr)
        self.assertIn("video URL check failed", r.stdout)


class RecordGuardTests(unittest.TestCase):
    """pipeline.record() must refuse to relabel a recorded failure as a Buffer
    outcome when no Buffer interaction marker exists."""

    def _setup(self, tag, state_status, marker=None):
        ep_dir = os.path.join(ROOT, "content", "episodes", f"auto-{tag}")
        paths = {
            "state": os.path.join(ROOT, "output", f"auto-{tag}_state.json"),
            "manifest": os.path.join(ROOT, "output", f"auto-{tag}_manifest.json"),
            "marker": os.path.join(ROOT, "output", f"auto-{tag}.buffer.json"),
            "ep": ep_dir,
        }
        common.save_json(paths["state"], {
            "content_id": f"reel-{tag}", "tag": tag, "status": state_status,
            "topic": {"title": "t", "normalized_topic": "t", "pillar": "AI_JUDGMENT"},
            "script": {"summary": "s", "sources": [], "playbook": "pb", "cta_type": "question",
                       "technology_angle": "ai", "metacognition_concept": "calibration",
                       "generation_mode": "groq", "language": "en", "hash": "h"},
            "qa": {"approved": False, "score": 69, "blocking_errors": ["x"],
                   "warnings": [], "checks": {}},
            "retries": {"script": 0, "render": 0}, "branch": f"drafts/{tag}", "run_id": "t",
        })
        if marker:
            common.save_json(paths["marker"], marker)
        os.makedirs(paths["ep"], exist_ok=True)
        common.save_json(os.path.join(paths["ep"], "script.json"), {"meta": {}, "caption": {"hook": "h"}})
        return paths

    def tearDown(self):
        for key, p in (self._paths or {}).items():
            if key != "ep" and os.path.exists(p):
                os.remove(p)
        for d in (self._dirs or []):
            shutil.rmtree(d, ignore_errors=True)

    def _record(self, tag, status, mem_path):
        with mock.patch.object(common, "MEMORY_PATH", mem_path):
            pl.record(type("A", (), {"tag": tag, "status": status, "error": None})())

    def test_qa_failed_never_becomes_buffer_error(self):
        tag = "2077-06-06"
        self._paths = self._setup(tag, "qa-failed")
        self._dirs = [self._paths["ep"]]
        mem_path = os.path.join(self._paths["ep"], "mem.json")
        common.save_memory(common.empty_memory(), mem_path)
        self._record(tag, "buffer-error", mem_path)
        manifest = common.load_json(self._paths["manifest"])
        self.assertEqual(manifest["status"], "qa-failed")
        st = common.load_json(self._paths["state"])
        self.assertEqual(st["status"], "qa-failed")

    def test_buffer_error_honored_when_marker_exists(self):
        tag = "2077-06-07"
        self._paths = self._setup(tag, "qa-failed",
                                  marker={"buffer_post_id": "mockpost1", "status": "queued",
                                         "due_at": "x", "channel_name": "c"})
        self._dirs = [self._paths["ep"]]
        mem_path = os.path.join(self._paths["ep"], "mem.json")
        common.save_memory(common.empty_memory(), mem_path)
        self._record(tag, "buffer-error", mem_path)
        manifest = common.load_json(self._paths["manifest"])
        self.assertEqual(manifest["status"], "buffer-error")
        self.assertEqual(manifest["buffer_post_id"], "mockpost1")

    def test_buffer_error_after_success_stays_buffer_error(self):
        tag = "2077-06-08"
        self._paths = self._setup(tag, "approved")
        self._dirs = [self._paths["ep"]]
        mem_path = os.path.join(self._paths["ep"], "mem.json")
        common.save_memory(common.empty_memory(), mem_path)
        self._record(tag, "buffer-error", mem_path)
        manifest = common.load_json(self._paths["manifest"])
        self.assertEqual(manifest["status"], "buffer-error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
