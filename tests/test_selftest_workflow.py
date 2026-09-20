"""Issue #34 regression: the CI self-test must stay diagnosable + env-pinned.

Run 35491557513 (2026-09-25) failed ONE self-test in 145.681 s and the whole
run was undiagnosable, for two deterministic clean-environment reasons:

1. The self-test step piped its output through `| tail -3`, so the job log
   contains only the last 3 lines ("Ran 685 tests ... FAILED (failures=1)").
   The failing test's name and traceback were destroyed at the source —
   they never existed anywhere in the run (log or artifact).
2. The environment drifted: `python-version: "3.11"` resolves to the NEWEST
   3.11.x at run time (that run executed 3.11.16), requirements.txt was
   unpinned (latest wheels at run time), and apt ffmpeg is unpinned — i.e.
   the CI executed a toolchain no full-suite green run was verified on.

The same tree + verified env (Python 3.11.2, pinned deps) passes the full
685-test suite 5+ consecutive times, so no deterministic code-level failing
test exists on the verified env; the fixes below make the NEXT failure
diagnosable and keep the env on the verified toolchain:

  * the FULL self-test output is written to output/selftest.log (uploaded by
    the QA-artifact step) — never truncated at the source;
  * only the console tail is shortened (tail -40); the step stays
    fail-closed (set -euo pipefail);
  * python-version is pinned to the verified 3.11.2;
  * requirements.txt pins every core dependency to the verified version.

These are structural text guards: they fail if the self-test output capture
or the env pins are ever weakened again.
"""
import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "daily-trend-draft.yml")
REQS = os.path.join(ROOT, "requirements.txt")


def wf_raw():
    with open(WF, encoding="utf-8") as f:
        return f.read()


def selftest_step():
    """The raw `run` block of the 'dependencies, ... self-test' step."""
    with open(WF, encoding="utf-8") as f:
        text = f.read()
    m = re.search(
        r"- name: dependencies, fonts, stand-in logo, self-test\n"
        r"(?:        .*\n)*?        run: \|\n((?:          .*\n)+)", text)
    if m is None:
        raise AssertionError("self-test step missing from daily workflow")
    return m.group(1)


class SelfTestDiagnosability(unittest.TestCase):
    def test_full_selftest_output_is_captured_to_log(self):
        block = selftest_step()
        self.assertIn("output/selftest.log", block,
                      "the FULL self-test output must be written to "
                      "output/selftest.log (run 35491557513 was "
                      "undiagnosable because the log only kept the last 3 "
                      "lines)")
        # the capture must go through the actual unittest run, not a stub
        self.assertRegex(block, r"unittest discover -s tests")
        self.assertRegex(block, r"tee\s+output/selftest\.log")

    def test_only_console_tail_is_truncated_not_the_log(self):
        block = selftest_step()
        # the truncated pipe must apply to the CONSOLE (after tee), never
        # as the only place the output goes
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        unit_idx = next(i for i, ln in enumerate(lines)
                        if "unittest discover" in ln)
        unit_line = lines[unit_idx]
        self.assertNotIn("tail -3", unit_line,
                         "self-test output must not be reduced to the last "
                         "3 lines (the run-35491557513 defect)")
        self.assertNotRegex(unit_line, r"^\s*python3 -m unittest[^|]*\|\s*tail\b(?!.*tee)",
                            "unittest output must pass through tee to the "
                            "log file; a bare pipe to tail destroys failures")
        self.assertIn("tee", unit_line,
                      "full output must be tee'd to the log before any tail")

    def test_step_stays_fail_closed(self):
        block = selftest_step()
        self.assertIn("set -euo pipefail", block,
                      "the self-test step must stay fail-closed: pipefail "
                      "makes the unittest|tee|tail pipeline fail on any "
                      "test failure")


class SelfTestLogArtifact(unittest.TestCase):
    def test_selftest_log_is_uploaded(self):
        text = wf_raw()
        m = re.search(r"QA artifacts.*?path: \|\n((?:\s{12}.*\n)+)", text,
                      re.S)
        self.assertIsNotNone(m, "QA-artifact upload step missing")
        self.assertIn("output/selftest.log", m.group(1),
                      "output/selftest.log must be uploaded so a failed "
                      "self-test is diagnosable from the artifact")

    def test_upload_runs_on_any_outcome(self):
        text = wf_raw()
        m = re.search(r"- name: QA artifacts.*?(?=\n      - name:)", text, re.S)
        self.assertIsNotNone(m, "QA-artifact step missing")
        self.assertIn("if: always()", m.group(0),
                      "the artifact upload must run even when the self-test "
                      "fails, otherwise the log is lost exactly when it "
                      "matters")


class EnvPinning(unittest.TestCase):
    def test_python_version_pinned_to_verified_env(self):
        wf = yaml.safe_load(wf_raw())
        steps = wf["jobs"]["reel"]["steps"]
        setup = [s for s in steps if isinstance(s.get("uses"), str)
                 and s["uses"].startswith("actions/setup-python@")]
        self.assertTrue(setup, "setup-python step missing")
        ver = setup[0].get("with", {}).get("python-version")
        self.assertEqual(ver, "3.11.2",
                         "python-version must stay pinned to the verified "
                         "env 3.11.2 — a floating minor ('3.11') resolved "
                         "to unverified 3.11.16 on run 35491557513")

    def test_requirements_pinned(self):
        with open(REQS, encoding="utf-8") as f:
            pins = {}
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                self.assertRegex(ln, r"^[A-Za-z0-9._-]+==\d",
                                 f"unpinned requirement {ln!r} — the "
                                 f"run-35491557513 defect was an unverified "
                                 f"floating dependency set")
                pins[ln.split("==")[0].lower().replace("_", "-")] = \
                    ln.split("==", 1)[1]
        # the verified core set (all core deps must be pinned)
        for dep in ("pillow", "numpy", "fonttools", "brotli",
                    "imageio-ffmpeg", "edge-tts", "pyyaml"):
            self.assertIn(dep, pins, f"core dependency {dep!r} missing/unpinned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
