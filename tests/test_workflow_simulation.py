"""End-to-end simulation of the daily workflow's shell steps against a LOCAL bare git origin and a
mocked Buffer API. No network, no real Buffer, no GitHub. Uses tiny stand-in media files, so it
covers the *plumbing* (step contracts, git orphan branch, status mapping, idempotency, fail-closed),
not the renderer (tests/test_factory.py covers that).

Slow-ish (git + several python subprocesses): ~10 s."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "daily-trend-draft.yml")
TAG = "2077-01-01"

MOCK_SITE = r'''
import os, io, json, urllib.request, email.message
_real = urllib.request.urlopen
STATE_FILE = os.environ["MOCK_STATE"]
def _load():
    try: return json.load(open(STATE_FILE))
    except Exception: return {"posts": [], "calls": []}
def _save(s): json.dump(s, open(STATE_FILE, "w"))
class _R(io.BytesIO):
    def __init__(self, data=b"", ctype="application/json", length=None):
        super().__init__(data); self.status = 200; self.headers = email.message.Message()
        self.headers["Content-Type"] = ctype; self.headers["Content-Length"] = str(length if length is not None else len(data))
    def __enter__(self): return self
    def __exit__(self, *a): return False
def _gql(req):
    s = _load(); payload = json.loads(req.data.decode()); q = payload.get("query", "")
    auth = req.get_header("Authorization", "")
    kind = "createPost" if "createPost" in q else "posts" if "posts(" in q else "channels" if "channels" in q else "account"
    s["calls"].append({"kind": kind, "bearer": auth.startswith("Bearer "), "token_in_body": os.environ["BUFFER_TOKEN"] in json.dumps(payload)})
    if kind == "createPost":
        inp = payload["variables"]["input"]; pid = "mockpost%d" % (len(s["posts"]) + 1)
        post = {"id": pid, "text": inp["text"], "status": "scheduled", "dueAt": "2077-01-01T16:00:00.000Z"}
        s["posts"].append(post); _save(s)
        return {"data": {"createPost": {"post": post}}}
    _save(s)
    if kind == "posts":
        return {"data": {"posts": {"edges": [{"node": p} for p in s["posts"]], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
    if kind == "channels":
        return {"data": {"channels": [{"id": "chan1", "name": "metacognition.hq", "displayName": "metacognition.hq", "service": "instagram",
                 "type": "business", "descriptor": "Instagram Business", "isQueuePaused": False, "isLocked": False, "isDisconnected": False, "timezone": "Asia/Tehran"}]}}
    return {"data": {"account": {"id": "acct", "organizations": [{"id": "org1", "name": "Mock"}]}}}
def fake(req, *a, **k):
    url = req.full_url if hasattr(req, "full_url") else req
    if "raw.githubusercontent.com/" in url and "/drafts/" in url:
        local = url.split("/drafts/%s/" % os.environ["TAG"])[1]
        return _R(b"", "video/mp4", os.path.getsize(os.path.join(os.environ["WS"], local)))
    if "api.buffer.com" in url:
        return _R(json.dumps(_gql(req)).encode())
    return _real(req, *a, **k)
urllib.request.urlopen = fake
'''


def step_scripts():
    wf = yaml.safe_load(open(WF, encoding="utf-8"))
    out = {}
    for s in wf["jobs"]["reel"]["steps"]:
        if "run" in s:
            key = s.get("id") or s["name"]
            out[key] = s["run"].replace("${{", "__").replace("}}", "__")
            if key != s["name"]:
                out[s["name"]] = out[key]
            if s["name"].startswith("record outcome"):
                out["record"] = out[key]
    return out


class WorkflowSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wfsim_")
        cls.origin = os.path.join(cls.tmp, "origin.git")
        cls.ws = os.path.join(cls.tmp, "ws")
        subprocess.run(["git", "clone", "-q", "--bare", ROOT, cls.origin], check=True, capture_output=True)
        subprocess.run(["git", "clone", "-q", cls.origin, cls.ws], check=True, capture_output=True)
        subprocess.run(["git", "-C", cls.ws, "checkout", "-q", "-B", "sim-branch"], check=True, capture_output=True)
        subprocess.run(["git", "-C", cls.ws, "push", "-q", "-u", "origin", "sim-branch"], check=True, capture_output=True)
        # copy uncommitted working-tree sources so the simulation tests the current code
        for rel in ("build", "content/editorial_policy.json", "content/editorial_memory.json", "content/calendar.json"):
            src, dst = os.path.join(ROOT, rel), os.path.join(cls.ws, rel)
            if os.path.isdir(src):
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy(src, dst)
        subprocess.run(["git", "-C", cls.ws, "add", "-A", "build", "content"], check=True, capture_output=True)
        subprocess.run(["git", "-C", cls.ws, "-c", "user.name=sim", "-c", "user.email=sim@x", "commit", "-q", "-m",
                        "sim: working-tree sources"], capture_output=True)
        subprocess.run(["git", "-C", cls.ws, "push", "-q", "origin", "sim-branch"], check=True, capture_output=True)
        cls.site = os.path.join(cls.tmp, "site")
        os.makedirs(cls.site)
        with open(os.path.join(cls.site, "sitecustomize.py"), "w") as f:
            f.write(MOCK_SITE)
        cls.state_file = os.path.join(cls.tmp, "mock_state.json")
        cls.steps = step_scripts()
        cls.gh_env = os.path.join(cls.tmp, "gh_env")
        cls.gh_out = os.path.join(cls.tmp, "gh_out")
        cls._make_fake_outputs()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _make_fake_outputs(cls):
        """Tiny stand-ins for the files the push step publishes (renderer is tested elsewhere)."""
        ep = os.path.join(cls.ws, "content", "episodes", f"auto-{TAG}")
        out = os.path.join(cls.ws, "output")
        prev = os.path.join(out, "drafts", f"auto-{TAG}")
        os.makedirs(ep, exist_ok=True)
        os.makedirs(prev, exist_ok=True)
        for name in ("script.json", "timing.json", "layout.json", "topic.json"):
            json.dump({"stub": name, "title": "Simulated topic", "pillar": "MIND"}, open(os.path.join(ep, name), "w"))
        for name in (f"auto-{TAG}.mp4", f"auto-{TAG}_poster.jpg", f"auto-{TAG}_poster_4x5.jpg", f"auto-{TAG}_qa.json", f"auto-{TAG}_qa.md"):
            with open(os.path.join(out, name), "wb") as f:
                f.write(b"\0" * 2048)
        with open(os.path.join(out, f"auto-{TAG}_caption.txt"), "w") as f:
            f.write("Simulated caption body.\n\n#metacognition #thinking\n")
        for name in ("preview_start.jpg", "preview_middle.jpg", "preview_end.jpg", "qa_contact_sheet.jpg"):
            with open(os.path.join(prev, name), "wb") as f:
                f.write(b"\0" * 512)
        state = {"content_id": f"reel-{TAG}", "tag": TAG, "status": "approved", "stage": "verified",
                 "retries": {"script": 0, "render": 0}, "branch": f"drafts/{TAG}", "run_id": "sim",
                 "topic": {"title": "Simulated topic", "pillar": "MIND"}, "script": {"hash": "x", "sources": []},
                 "qa": {"approved": True, "score": 99, "blocking_errors": [], "warnings": [], "checks": {}}}
        json.dump(state, open(os.path.join(out, f"auto-{TAG}_state.json"), "w"))

    def _run(self, step, env, extra_env=None):
        e = {k: v for k, v in os.environ.items() if k not in ("BUFFER_TOKEN", "AUTO_PUBLISH_ENABLED", "PYTHONPATH")}
        e.update({"GITHUB_ENV": self.gh_env, "GITHUB_OUTPUT": self.gh_out, "TAG": TAG, "GITHUB_RUN_ID": "424242",
                  "GITHUB_REPOSITORY": "Captain-Jorf/workspace", "GITHUB_REF_NAME": "sim-branch", "WS": self.ws,
                  "MOCK_STATE": self.state_file, "PYTHONPATH": self.site,
                  "RAW_URL": f"https://raw.githubusercontent.com/Captain-Jorf/workspace/drafts/{TAG}/output/auto-{TAG}.mp4"})
        e.update(env)
        if extra_env:
            e.update(extra_env)
        open(self.gh_env, "w").close()
        open(self.gh_out, "w").close()
        script = self.steps[step]
        # the push step probes GitHub's CDN with curl; here the "CDN" is the local origin
        script = script.replace("code=$(curl -s -o /dev/null -w '%{http_code}' -I \"$RAW\" || true)", "code=200")
        r = subprocess.run(["bash", "-c", script], cwd=self.ws, env=e, capture_output=True, text=True)
        outputs = dict(l.split("=", 1) for l in open(self.gh_out).read().splitlines() if "=" in l)
        return r, outputs

    def _calls(self):
        try:
            return json.load(open(self.state_file))["calls"]
        except Exception:  # noqa: BLE001
            return []

    def test_01_mode_step(self):
        r, _ = self._run("mode", {"IN_DRY": "", "IN_CAL": "", "IN_TAG": "", "EVENT": "schedule", "DRY_RUN_CRON": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        env = open(self.gh_env).read()
        self.assertIn("DRY=false", env)                      # cron → live unless DRY_RUN_CRON
        r, _ = self._run("mode", {"IN_DRY": "true", "IN_CAL": "true", "IN_TAG": TAG, "EVENT": "workflow_dispatch", "DRY_RUN_CRON": ""})
        env = open(self.gh_env).read()
        self.assertIn("DRY=true", env)
        self.assertIn(f"TAG={TAG}", env)
        r, _ = self._run("mode", {"IN_DRY": "", "IN_CAL": "", "IN_TAG": "", "EVENT": "schedule", "DRY_RUN_CRON": "true"})
        self.assertIn("DRY=true", open(self.gh_env).read())
        r, _ = self._run("mode", {"IN_DRY": "", "IN_CAL": "", "IN_TAG": "not-a-date", "EVENT": "workflow_dispatch", "DRY_RUN_CRON": ""})
        self.assertNotEqual(r.returncode, 0)

    def test_02_push_step_orphan_branch_and_clean_tree(self):
        r, _ = self._run("push", {})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn(f"RAW_URL=https://raw.githubusercontent.com/Captain-Jorf/workspace/drafts/{TAG}/output/auto-{TAG}.mp4",
                      open(self.gh_env).read())
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.ws, capture_output=True, text=True).stdout
        self.assertEqual(status.strip(), "", "working tree must stay untouched by the orphan push")
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.ws, capture_output=True, text=True).stdout.strip()
        self.assertEqual(branch, "sim-branch")
        files = subprocess.run(["git", "ls-tree", "-r", "--name-only", f"refs/heads/drafts/{TAG}"], cwd=self.origin,
                               capture_output=True, text=True).stdout.split()
        self.assertIn(f"output/auto-{TAG}.mp4", files)
        self.assertIn(f"output/drafts/auto-{TAG}/preview_start.jpg", files)
        self.assertNotIn("build/buffer_publish.py", files)   # only public media, no code, no history
        commits = subprocess.run(["git", "rev-list", "--count", f"refs/heads/drafts/{TAG}"], cwd=self.origin,
                                 capture_output=True, text=True).stdout.strip()
        self.assertEqual(commits, "1")

    def test_03_buffer_step_dry_run_never_creates(self):
        r, out = self._run("buffer", {"DRY": "true", "BUFFER_TOKEN": "FAKE_sim_token_0123456789", "AUTO_PUBLISH_ENABLED": "true"})
        self.assertEqual(out.get("status"), "approved-dry-run", r.stdout + r.stderr)
        r, out = self._run("buffer", {"DRY": "false", "BUFFER_TOKEN": "FAKE_sim_token_0123456789", "AUTO_PUBLISH_ENABLED": ""})
        self.assertEqual(out.get("status"), "approved-dry-run")
        r, out = self._run("buffer", {"DRY": "false", "BUFFER_TOKEN": "FAKE_sim_token_0123456789", "AUTO_PUBLISH_ENABLED": "True"})
        self.assertEqual(out.get("status"), "approved-dry-run")
        self.assertEqual([c for c in self._calls() if c["kind"] == "createPost"], [])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "output", f"auto-{TAG}.buffer.json")))

    def test_04_buffer_step_missing_token_is_buffer_error(self):
        r, out = self._run("buffer", {"DRY": "false", "AUTO_PUBLISH_ENABLED": "true"})
        self.assertEqual(out.get("status"), "buffer-error")
        self.assertNotIn("FAKE_sim", r.stdout + r.stderr)

    def test_05_buffer_step_live_once_then_idempotent(self):
        r, out = self._run("buffer", {"DRY": "false", "BUFFER_TOKEN": "FAKE_sim_token_0123456789", "AUTO_PUBLISH_ENABLED": "true"})
        self.assertEqual(out.get("status"), "queued-in-buffer", r.stdout + r.stderr)
        marker = json.load(open(os.path.join(self.ws, "output", f"auto-{TAG}.buffer.json")))
        self.assertEqual(marker["buffer_post_id"], "mockpost1")
        self.assertNotIn("FAKE_sim", r.stdout + r.stderr + json.dumps(marker))
        # rerun on a "fresh runner": marker gone, queue still has the post → adopted, no duplicate
        os.remove(os.path.join(self.ws, "output", f"auto-{TAG}.buffer.json"))
        r, out = self._run("buffer", {"DRY": "false", "BUFFER_TOKEN": "FAKE_sim_token_0123456789", "AUTO_PUBLISH_ENABLED": "true"})
        self.assertEqual(out.get("status"), "queued-in-buffer")
        self.assertIn("adopting", r.stdout)
        calls = self._calls()
        self.assertEqual(sum(1 for c in calls if c["kind"] == "createPost"), 1)
        self.assertTrue(all(c["bearer"] for c in calls))
        self.assertFalse(any(c["token_in_body"] for c in calls))

    def test_06_record_step_commits_only_small_json(self):
        r, _ = self._run("record", {"P": "0", "V": "0", "BST": "queued-in-buffer", "PUSH": "success"})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("FINAL_STATUS=queued-in-buffer", open(self.gh_env).read())
        files = subprocess.run(["git", "show", "--stat", "--format=", "HEAD"], cwd=self.ws, capture_output=True, text=True).stdout
        self.assertIn("content/editorial_memory.json", files)
        self.assertIn(f"output/auto-{TAG}_manifest.json", files)
        self.assertNotIn(".mp4", files)
        local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.ws, capture_output=True, text=True).stdout.strip()
        remote = subprocess.run(["git", "rev-parse", "refs/heads/sim-branch"], cwd=self.origin, capture_output=True, text=True).stdout.strip()
        self.assertEqual(local, remote, "memory commit must be pushed")
        manifest = json.load(open(os.path.join(self.ws, "output", f"auto-{TAG}_manifest.json")))
        self.assertEqual(manifest["buffer_post_id"], "mockpost1")
        self.assertNotIn("FAKE_sim", json.dumps(manifest))

    def test_07_record_status_mapping(self):
        cases = [({"P": "0", "V": "10", "BST": "", "PUSH": "success"}, "qa-failed"),
                 ({"P": "0", "V": "", "BST": "", "PUSH": "failure"}, "automation-error"),
                 ({"P": "0", "V": "0", "BST": "queue-full", "PUSH": "success"}, "queue-full"),
                 ({"P": "0", "V": "0", "BST": "buffer-error", "PUSH": "success"}, "buffer-error")]
        for env, expected in cases:
            r, _ = self._run("record", env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"FINAL_STATUS={expected}", open(self.gh_env).read(), env)
        # produce failed → status comes from the state file written by pipeline.py
        st_path = os.path.join(self.ws, "output", f"auto-{TAG}_state.json")
        st = json.load(open(st_path))
        st["status"] = "tts-error"
        json.dump(st, open(st_path, "w"))
        r, _ = self._run("record", {"P": "10", "V": "", "BST": "", "PUSH": ""})
        self.assertIn("FINAL_STATUS=tts-error", open(self.gh_env).read())

    def test_07b_qa_rejection_is_never_buffer_error(self):
        # Regression (run 35043984004): produce exit 10 (qa rejected), push/verify
        # skipped, and a phantom buffer-error status from the buffer step — the
        # final status must stay qa-failed, never buffer-error.
        st_path = os.path.join(self.ws, "output", f"auto-{TAG}_state.json")
        st = json.load(open(st_path))
        st["status"] = "qa-failed"
        json.dump(st, open(st_path, "w"))
        r, _ = self._run("record", {"P": "10", "V": "", "BST": "buffer-error", "PUSH": "skipped"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("FINAL_STATUS=qa-failed", open(self.gh_env).read())

    def test_08_fail_closed_step(self):
        for status, code in (("queued-in-buffer", 0), ("approved-dry-run", 0), ("qa-failed", 1), ("buffer-error", 1), ("", 1)):
            r = subprocess.run(["bash", "-c", self.steps["fail the run when there is no post today"]],
                               env={**os.environ, "FINAL_STATUS": status}, capture_output=True, text=True)
            self.assertEqual(r.returncode, code, status)


if __name__ == "__main__":
    unittest.main()
