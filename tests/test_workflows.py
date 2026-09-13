"""Structural tests: YAML validity + hard safety invariants for the workflows.

The invariants encode the project's rules:
  * the daily draft workflow must NEVER touch Buffer (no secrets.BUFFER_TOKEN,
    no buffer_publish.py invocation at all);
  * the connection-check workflow is workflow_dispatch-only and read-only;
  * the publish workflow gates on author_association + publish_gate.py,
    exposes BUFFER_TOKEN to exactly one step, and marks queued posts.
Also: every build/*.py compiles, caption.fit() respects 2200 chars, and
make_issue_body.py produces a sane daily-issue body.
"""
import compileall
import json
import os
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))
WF = os.path.join(ROOT, ".github", "workflows")


def load_wf(name):
    with open(os.path.join(WF, name), encoding="utf-8") as f:
        return yaml.safe_load(f), f.name


def raw(name):
    return open(os.path.join(WF, name), encoding="utf-8").read()


def triggers(wf):
    # YAML 1.1 parses the bare key `on:` as boolean True
    return wf.get("on", wf.get(True))


class YamlValidity(unittest.TestCase):
    def test_all_workflows_parse(self):
        for fn in sorted(os.listdir(WF)):
            if fn.endswith((".yml", ".yaml")):
                with self.subTest(fn):
                    wf, _ = load_wf(fn)
                    self.assertIn("jobs", wf)
                    self.assertIn("name", wf)


class DailyDraftSafety(unittest.TestCase):
    def test_never_touches_buffer(self):
        text = raw("daily-trend-draft.yml")
        wf, _ = load_wf("daily-trend-draft.yml")
        self.assertNotIn("secrets.BUFFER_TOKEN", text)
        self.assertNotIn("buffer_publish.py", text)
        self.assertNotIn("api.buffer.com", text)
        # the only BUFFER_TOKEN mention is the guard that asserts it is absent
        steps = wf["jobs"]["draft"]["steps"]
        guard = [s for s in steps if "BUFFER_TOKEN" in json.dumps(s)]
        self.assertEqual(len(guard), 1)
        self.assertIn("confirm no Buffer access", guard[0].get("name", ""))

    def test_schedule_and_dispatch_only(self):
        wf, _ = load_wf("daily-trend-draft.yml")
        t = triggers(wf)
        self.assertEqual(sorted(t.keys()), ["schedule", "workflow_dispatch"])
        self.assertEqual(t["schedule"][0]["cron"], "0 6 * * *")

    def test_has_concurrency_and_dedupe(self):
        wf, _ = load_wf("daily-trend-draft.yml")
        self.assertIn("concurrency", wf)
        text = raw("daily-trend-draft.yml")
        self.assertIn("select(.title == $t)", text)   # exact-title dedupe via jq

    def test_renders_and_verifies_public_url(self):
        text = raw("daily-trend-draft.yml")
        self.assertIn("build/render.py", text)
        self.assertIn("raw.githubusercontent.com", text)
        self.assertIn("git push -qf origin", text)


class ConnectionCheckSafety(unittest.TestCase):
    def test_dispatch_only_and_read_only(self):
        wf, _ = load_wf("buffer-connection-check.yml")
        self.assertEqual(list(triggers(wf).keys()), ["workflow_dispatch"])
        text = raw("buffer-connection-check.yml")
        self.assertIn("buffer_publish.py check", text)
        self.assertNotIn("publish --", text)
        self.assertNotIn("--yes", text)
        self.assertIn("secrets.BUFFER_TOKEN", text)


class PublishWorkflowSafety(unittest.TestCase):
    def test_author_association_gate(self):
        wf, _ = load_wf("publish-approved-draft.yml")
        text = raw("publish-approved-draft.yml")
        job_if = wf["jobs"]["publish"]["if"]
        self.assertIn("OWNER", job_if)
        self.assertIn("MEMBER", job_if)
        self.assertIn("COLLABORATOR", job_if)
        self.assertIn("author_association", job_if)
        self.assertIn("publish_gate.py", text)

    def test_token_exposed_to_single_step(self):
        wf, _ = load_wf("publish-approved-draft.yml")
        steps = wf["jobs"]["publish"]["steps"]
        users = [s for s in steps
                 if "secrets.BUFFER_TOKEN" in json.dumps(s)]
        self.assertEqual(len(users), 1)
        self.assertIn("Buffer", users[0]["name"])

    def test_idempotency_layers(self):
        text = raw("publish-approved-draft.yml")
        self.assertIn("queued-in-buffer", text)          # label layer
        self.assertIn(".buffer.json", text)              # marker-file layer
        self.assertIn("concurrency", text)               # race layer
        wf, _ = load_wf("publish-approved-draft.yml")
        self.assertIn("publish-draft-", wf["concurrency"]["group"])

    def test_exact_tag_validation(self):
        text = raw("publish-approved-draft.yml")
        self.assertIn("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", text)

    def test_issue_comment_trigger_only(self):
        wf, _ = load_wf("publish-approved-draft.yml")
        self.assertEqual(list(triggers(wf).keys()), ["issue_comment"])
        self.assertNotIn("schedule", triggers(wf))


class PythonCompile(unittest.TestCase):
    def test_all_sources_compile(self):
        for d in ("build", "tests"):
            ok = compileall.compile_dir(os.path.join(ROOT, d), quiet=2, force=True)
            self.assertTrue(ok, f"{d} failed to compile")


class CaptionLimit(unittest.TestCase):
    def test_fit_caps_at_2200(self):
        import caption
        long_cap = "word " * 900                       # ~4500 chars
        self.assertLessEqual(len(caption.fit(long_cap, "")), 2200)
        self.assertEqual(caption.fit("short", ""), "short")


class IssueBodyTest(unittest.TestCase):
    def test_make_issue_body(self):
        import make_issue_body as mib
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(ROOT, "content", "episodes", "auto-2099-01-01")
            os.makedirs(ep, exist_ok=True)
            try:
                json.dump({"meta": {"title": "Auto draft 2099-01-01 — Test topic",
                                    "note": ""},
                           "caption": {"sources": ["http://x"], "hashtags": ["#a"]}},
                          open(os.path.join(ep, "script.json"), "w"))
                body = mib.build("2099-01-01", "o/r", "https://raw.example/x.mp4")
                self.assertIn("Test topic", body)
                self.assertIn("https://raw.example/x.mp4", body)
                self.assertIn("/publish", body)
                self.assertIn("19:30 Asia/Tehran", body)
                self.assertIn("Nothing is sent to Buffer", body)
            finally:
                subprocess.run(["rm", "-rf", ep], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
