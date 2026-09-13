"""Unit tests for build/publish_gate.py — the /publish authorization gate."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import publish_gate as pg  # noqa: E402

TITLE = "trend reel draft 2026-09-14"


class GateTests(unittest.TestCase):
    def decide(self, assoc="OWNER", body="/publish", title=TITLE,
               labels="auto-draft", is_pr=False):
        return pg.decide(author_assoc=assoc, body=body, title=title,
                         labels=labels, is_pr=is_pr)

    # --- happy paths -----------------------------------------------------
    def test_owner_exact_publish_proceeds(self):
        self.assertEqual(self.decide()[0], pg.EXIT_PROCEED)

    def test_surrounding_whitespace_ok(self):
        self.assertEqual(self.decide(body="  /publish \n")[0], pg.EXIT_PROCEED)
        self.assertEqual(self.decide(body="\r\n/publish\r\n")[0], pg.EXIT_PROCEED)

    def test_member_and_collaborator_ok(self):
        self.assertEqual(self.decide(assoc="MEMBER")[0], pg.EXIT_PROCEED)
        self.assertEqual(self.decide(assoc="COLLABORATOR")[0], pg.EXIT_PROCEED)

    def test_label_only_draft_issue_ok(self):
        self.assertEqual(self.decide(title="something else",
                                     labels="auto-draft,other")[0], pg.EXIT_PROCEED)

    # --- rejection: command must be exactly /publish ----------------------
    def test_body_must_be_exact(self):
        for body in ("/publish please", "please /publish", "/publish now",
                     "/PUBLISH", "/Publish", "/publish /publish", "/queue",
                     "publish", "/ publish", "", "لغو /publish"):
            self.assertEqual(self.decide(body=body)[0], pg.EXIT_BAD_COMMAND,
                             msg=f"body={body!r} should be rejected")

    # --- rejection: anonymous / unauthorized authors ----------------------
    def test_unauthorized_associations(self):
        for assoc in ("NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR",
                      "CONTRIBUTOR", "MANNEQUIN", "BOT", "", "owner"):
            code, _ = self.decide(assoc=assoc)
            self.assertEqual(code, pg.EXIT_UNAUTHORIZED,
                             msg=f"assoc={assoc!r} must not publish")

    # --- rejection: wrong issue -------------------------------------------
    def test_not_a_draft_issue(self):
        self.assertEqual(self.decide(title="random issue", labels="")[0],
                         pg.EXIT_NOT_DRAFT)
        self.assertEqual(self.decide(title="trend reel drafts 2026-09-14",
                                     labels="")[0], pg.EXIT_NOT_DRAFT)

    def test_pull_request_comment_rejected(self):
        self.assertEqual(self.decide(is_pr=True)[0], pg.EXIT_NOT_DRAFT)

    # --- idempotency -------------------------------------------------------
    def test_already_queued(self):
        self.assertEqual(self.decide(labels="auto-draft,queued-in-buffer")[0],
                         pg.EXIT_ALREADY_QUEUED)

    def test_authorization_checked_before_already_queued(self):
        # an anonymous user must get "unauthorized", not information about state
        self.assertEqual(self.decide(assoc="NONE",
                                     labels="auto-draft,queued-in-buffer")[0],
                         pg.EXIT_UNAUTHORIZED)

    def test_normalize_body(self):
        self.assertEqual(pg.normalize_body("  /publish\r\n "), "/publish")
        self.assertEqual(pg.normalize_body(None), "")


class MainExitCodeTests(unittest.TestCase):
    def test_main_exit_codes(self):
        env = {"GATE_AUTHOR_ASSOC": "OWNER", "GATE_BODY": "/publish",
               "GATE_TITLE": TITLE, "GATE_LABELS": "auto-draft",
               "GATE_AUTHOR": "captain", "GATE_IS_PR": ""}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                pg.main()
            self.assertEqual(ctx.exception.code, 0)
        env2 = dict(env, GATE_AUTHOR_ASSOC="NONE")
        with mock.patch.dict(os.environ, env2, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                pg.main()
            self.assertEqual(ctx.exception.code, pg.EXIT_UNAUTHORIZED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
