"""Structural tests: YAML validity + hard safety invariants for the workflows.

Invariants of the autonomous design:
  * daily-trend-draft.yml: cron + dispatch only, per-day concurrency, pinned
    actions, least privilege (top-level `permissions: {}`), dry_run input default
    true, BUFFER_TOKEN only in the Buffer step, publish gated on the supervisor
    step outcome, AUTO_PUBLISH_ENABLED comes from repo vars, exactly one issue
    step, no Meta credentials anywhere;
  * buffer-connection-check.yml: dispatch-only, read-only (`check` subcommand);
  * publish-approved-draft.yml is retired (no issue_comment trigger, no secrets);
  * every build/*.py compiles; the old Meta publisher cannot publish.
"""
import compileall
import os
import re
import subprocess
import sys
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
    return wf.get("on", wf.get(True))       # YAML 1.1 parses bare `on:` as True


def steps(wf, job):
    return wf["jobs"][job]["steps"]


class YamlTests(unittest.TestCase):
    def test_all_workflows_parse(self):
        names = sorted(n for n in os.listdir(WF) if n.endswith((".yml", ".yaml")))
        self.assertGreaterEqual(len(names), 3)
        for n in names:
            wf, _ = load_wf(n)
            self.assertIn("jobs", wf, n)
            self.assertTrue(triggers(wf), n)

    def test_all_actions_pinned_to_sha(self):
        for n in os.listdir(WF):
            for m in re.finditer(r"uses:\s*([^\s@]+)@([^\s#]+)", raw(n)):
                self.assertRegex(m.group(2), r"^[0-9a-f]{40}$", f"{n}: {m.group(0)} is not SHA-pinned")

    def test_top_level_permissions_empty(self):
        for n in os.listdir(WF):
            wf, _ = load_wf(n)
            self.assertEqual(wf.get("permissions"), {}, f"{n} must declare top-level permissions: {{}}")


class DailyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.wf, _ = load_wf("daily-trend-draft.yml")
        self.text = raw("daily-trend-draft.yml")
        self.steps = steps(self.wf, "reel")

    def test_schedule_and_dispatch_only(self):
        t = triggers(self.wf)
        self.assertEqual(set(t), {"schedule", "workflow_dispatch"})
        self.assertEqual(t["schedule"][0]["cron"], "0 6 * * *")       # 09:30 Tehran, hours before 16:00 UTC

    def test_dry_run_input_defaults_true(self):
        inp = triggers(self.wf)["workflow_dispatch"]["inputs"]["dry_run"]
        self.assertIs(inp["default"], True)
        self.assertEqual(inp["type"], "boolean")

    def test_per_day_concurrency(self):
        self.assertIn("daily-reel-", self.wf["concurrency"]["group"])
        self.assertIs(self.wf["concurrency"]["cancel-in-progress"], False)

    def test_job_permissions_least_privilege(self):
        perms = self.wf["jobs"]["reel"]["permissions"]
        self.assertEqual(set(perms), {"contents", "issues"})

    def test_buffer_token_only_in_buffer_steps(self):
        holders = [s["name"] for s in self.steps if "BUFFER_TOKEN" in str(s.get("env", {}))]
        self.assertEqual(len(holders), 2, holders)                # queue step + read-only metrics sync
        self.assertTrue(all("Buffer" in h for h in holders))
        for s in self.steps:                                       # never in a step that also runs gh/issue code
            if "BUFFER_TOKEN" in str(s.get("env", {})):
                self.assertNotIn("report_issue", s.get("run", ""))
                self.assertNotIn("GH_TOKEN", str(s.get("env", {})))
        # never in a shell line where it could be echoed
        self.assertNotIn("echo $BUFFER_TOKEN", self.text)
        self.assertNotIn("echo ${BUFFER_TOKEN", self.text)

    def test_buffer_step_gated_on_verify_and_uses_publisher(self):
        b = next(s for s in self.steps if s.get("id") == "buffer")
        self.assertIn("steps.verify.outputs.code == '0'", b["if"])
        self.assertIn("buffer_publish.py publish", b["run"])
        self.assertIn("vars.AUTO_PUBLISH_ENABLED", str(b["env"]["AUTO_PUBLISH_ENABLED"]))
        self.assertIn('[ "$DRY" = "true" ] && ARGS="--dry-run"', b["run"])

    def test_verify_depends_on_produce_and_push(self):
        v = next(s for s in self.steps if s.get("id") == "verify")
        self.assertIn("steps.produce.outputs.code == '0'", v["if"])
        self.assertIn("steps.push.outcome == 'success'", v["if"])
        self.assertIn("pipeline.py verify", v["run"])

    def test_single_issue_step_uses_report_issue(self):
        issue_steps = [s for s in self.steps if "report_issue.py" in s.get("run", "")]
        self.assertEqual(len(issue_steps), 1)
        self.assertNotIn("gh issue comment", self.text)        # no duplicate comments
        self.assertNotIn("make_issue_body", self.text)

    def test_no_meta_credentials(self):
        for bad in ("IG_USER_ID", "IG_PAGE_TOKEN", "insta_publish", "graph.facebook.com"):
            self.assertNotIn(bad, self.text)

    def test_public_files_pushed_to_orphan_branch(self):
        p = next(s for s in self.steps if s.get("id") == "push")
        self.assertIn("commit-tree", p["run"])                    # plumbing: working tree untouched
        self.assertNotIn("git checkout", p["run"])
        self.assertIn("refs/heads/drafts/$TAG", p["run"])
        self.assertIn("raw.githubusercontent.com", p["run"])
        self.assertIn("HTTP", p["run"])

    def test_artifact_upload_and_fail_closed(self):
        self.assertIn("upload-artifact", self.text)
        last = self.steps[-1]
        self.assertIn("exit 1", last["run"])
        self.assertIn("queued-in-buffer|approved-dry-run", last["run"])

    def test_no_test_only_modes_in_ci(self):
        self.assertNotIn("--synthetic-tts", self.text)
        self.assertNotIn("--fixture-translation", self.text)
        self.assertNotIn("trends_sample", self.text)


class ConnectionCheckTests(unittest.TestCase):
    def test_dispatch_only_and_read_only(self):
        wf, _ = load_wf("buffer-connection-check.yml")
        self.assertEqual(list(triggers(wf)), ["workflow_dispatch"])
        text = raw("buffer-connection-check.yml")
        self.assertIn("buffer_publish.py check", text)
        self.assertNotIn("publish --", text)
        self.assertNotIn("createPost", text)
        self.assertEqual(wf["jobs"]["check"]["permissions"], {"contents": "read"})


class RetiredWorkflowTests(unittest.TestCase):
    def test_publish_workflow_is_inert(self):
        wf, _ = load_wf("publish-approved-draft.yml")
        self.assertEqual(list(triggers(wf)), ["workflow_dispatch"])
        text = raw("publish-approved-draft.yml")
        self.assertNotIn("secrets.", text)
        self.assertNotIn("buffer_publish", text)
        self.assertNotIn("issue_comment", text)


class SourceTests(unittest.TestCase):
    def test_all_sources_compile(self):
        ok = compileall.compile_dir(os.path.join(ROOT, "build"), quiet=1, force=True)
        self.assertTrue(ok)

    def test_meta_publisher_cannot_publish(self):
        src = open(os.path.join(ROOT, "build", "insta_publish.py"), encoding="utf-8").read()
        self.assertNotIn("graph.facebook.com", src)
        self.assertNotIn("media_publish", src)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "build", "insta_publish.py"), "publish"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("retired", r.stdout)

    def test_gitignore_keeps_secrets_and_renders_out(self):
        gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
        for pat in ("secrets/", "output/*", "assets/fonts/*.ttf", "*.env"):
            self.assertIn(pat, gi)
        self.assertNotIn("editorial_memory", gi)


if __name__ == "__main__":
    unittest.main(verbosity=2)
