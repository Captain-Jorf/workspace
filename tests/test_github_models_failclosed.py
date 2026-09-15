"""Fail-closed tests for the GitHub Models connection fix (false-success removal).

Covers the required matrix:
  * real mode + missing token / DNS failure / HTTP failure / invalid model /
    malformed JSON -> nonzero, never mock
  * connection-check never falls back to Mock
  * Mock only with the explicit flag (default false)
  * production cron cannot enable Mock
  * failed real request cannot claim generation_mode=github-models
  * daily model failure -> static-fallback
  * no Buffer createPost during connection-check
  * no secret leakage (token, Authorization, prompt, response)

Plus: official endpoint/hostname checks, retry policy (1+2 backoff), reviewer
failure handling, workflow permission minimality, and an end-to-end stub-server
success contract for build/github_models_check.py.

No network, no real tokens, no Buffer. Local stub HTTP server only.
"""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
import urllib.error

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import content_producer as cp  # noqa: E402
import llm_provider  # noqa: E402
import pipeline as pl  # noqa: E402

WF_DIR = os.path.join(ROOT, ".github", "workflows")
CHECK_WF = os.path.join(WF_DIR, "github-models-connection-check.yml")
DAILY_WF = os.path.join(WF_DIR, "daily-trend-draft.yml")
CHECK_SCRIPT = os.path.join(ROOT, "build", "github_models_check.py")

OFFICIAL_ENDPOINT = "https://models.github.ai/inference/chat/completions"
OFFICIAL_HOSTNAME = "models.github.ai"
RETIRED_HOSTNAME = "models.inference.ai.azure.com"  # must appear nowhere in build/

FAKE_TOKEN = "FAKE_SECRET_TOKEN_FOR_TESTS_ONLY_" + "X" * 48

VALID_PRODUCER_JSON = json.dumps({
    "title": "Test reel",
    "technology_angle": "automation bias in AI assistants",
    "metacognition_concept": "automation bias",
    "hook": "When does your AI assistant make you think less?",
    "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
    "narration": {"hook": "Hook?", "problem": ["p1"], "explain": ["e1"],
                  "example": ["x1"], "technique": ["t1"], "ending": "End?"},
    "on_screen_text": ["A", "B", "C", "D", "E", "F"],
    "visual_direction": "code visual",
    "actionable_technique": "Explain before accept",
    "ending": "End?",
    "caption": {"hook": "Hook?", "intro": "Intro", "sections": [], "hashtags": ["#a"]},
    "claims": [],
    "sources": [{"label": "s", "url": "", "tier": "B"}],
})
VALID_REVIEWER_JSON = json.dumps({
    "approved": True, "score": 90, "technology_relevance": True,
    "metacognition_relevance": True, "source_grounding": True,
    "unsupported_claims": [], "hook_quality": "good",
    "spoken_english_quality": "good", "novelty": "high",
    "practical_value": "high", "safety": "safe",
    "required_changes": [], "blocking_errors": [],
})


def clean_env(**overrides):
    e = {k: v for k, v in os.environ.items()
         if k not in ("GITHUB_TOKEN", "GH_TOKEN", "MOCK_GITHUB_MODELS",
                      "GITHUB_MODELS_ENDPOINT", "PRODUCER_MODEL",
                      "REVIEWER_MODEL", "GITHUB_ACTIONS")}
    e["CONTENT_LANGUAGE"] = "en"
    e.update(overrides)
    return e


def run_check(*args, env):
    return subprocess.run(
        [sys.executable, CHECK_SCRIPT, *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def wf_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def wf_code(path):
    """Workflow text without comment lines (assertions target real code)."""
    return "\n".join(l for l in wf_text(path).splitlines()
                     if not l.strip().startswith("#"))


def read_source(relpath):
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


def wf_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class StubHandler(BaseHTTPRequestHandler):
    mode = "ok"  # ok | http500 | producer_bad | reviewer_bad

    def _send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 — stdlib handler name
        self.send_response(404)
        self.send_header("Content-Length", "9")
        self.end_headers()
        self.wfile.write(b"not found")

    def do_POST(self):  # noqa: N802 — stdlib handler name
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        if StubHandler.mode == "http500":
            self._send_json({"error": "boom"}, code=500)
            return
        is_reviewer = "GitHubModelsReviewer" in body
        if StubHandler.mode == "producer_bad" and not is_reviewer:
            content = "not json {{"
        elif StubHandler.mode == "reviewer_bad" and is_reviewer:
            content = "oops not json {{"
        else:
            content = VALID_REVIEWER_JSON if is_reviewer else VALID_PRODUCER_JSON
        self._send_json({"choices": [{"message": {"content": content}}]})

    def log_message(self, *args):  # silence test server
        pass


class StubServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.port}/chat/completions"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)


def http_error(code, body=b"boom"):
    return urllib.error.HTTPError(
        "https://models.github.ai/inference/chat/completions", code,
        "err", {}, io.BytesIO(body))


class OfficialEndpointTests(unittest.TestCase):
    def test_official_endpoint_and_hostname(self):
        self.assertEqual(llm_provider.GITHUB_MODELS_ENDPOINT, OFFICIAL_ENDPOINT)
        self.assertEqual(llm_provider.GITHUB_MODELS_HOSTNAME, OFFICIAL_HOSTNAME)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_MODELS_ENDPOINT", None)
            self.assertEqual(llm_provider.get_endpoint(), OFFICIAL_ENDPOINT)
            self.assertEqual(llm_provider.get_hostname(), OFFICIAL_HOSTNAME)

    def test_retired_dead_hostname_absent_from_build(self):
        for name in os.listdir(os.path.join(ROOT, "build")):
            if not name.endswith(".py"):
                continue
            src = read_source(os.path.join("build", name))
            self.assertNotIn(RETIRED_HOSTNAME, src, f"{name} still references the dead host")


class RealModeFailureTests(unittest.TestCase):
    def test_missing_token_raises_and_cli_nonzero(self):
        with mock.patch.dict(os.environ, clean_env(), clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                llm_provider.call_github_models("hi", "openai/gpt-4o-mini")
            self.assertIn("GITHUB_TOKEN not set", str(ctx.exception))
            # Producer must raise too — never silently return fixtures.
            with self.assertRaises(RuntimeError):
                llm_provider.GitHubModelsProducer(model="openai/gpt-4o-mini").produce(
                    {"topic": "t", "technology_angle": "AI", "discovery_source": {},
                     "evidence_source": {}, "trusted_excerpt": "", "recent_topics": [],
                     "editorial_policy": {"brand": "b", "pillars": [], "tone": "",
                                          "length_target": [70, 105], "forbidden_openers": []}})
        r = run_check(env=clean_env())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("GITHUB_TOKEN present: false", r.stdout)
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("MOCK MODE", r.stdout)

    def test_dns_failure_nonzero_with_retries(self):
        err = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        with mock.patch.dict(os.environ, clean_env(GITHUB_TOKEN=FAKE_TOKEN), clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=err) as m_open, \
                    mock.patch("time.sleep") as m_sleep:
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_github_models("hi", "openai/gpt-4o-mini")
        self.assertIn("attempts=3", str(ctx.exception))
        self.assertEqual(m_open.call_count, 3)  # 1 initial + 2 retries
        self.assertEqual([c.args[0] for c in m_sleep.call_args_list], [1.0, 2.0])

    def test_dns_failure_cli_nonzero_no_mock(self):
        r = run_check(env=clean_env(
            GITHUB_TOKEN=FAKE_TOKEN,
            GITHUB_MODELS_ENDPOINT="https://nonexistent-host-xyz12345.test/inference/chat/completions"))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("MOCK MODE", r.stdout)
        self.assertNotIn("GitHub Models connection: OK", r.stdout)
        self.assertIn("Mock used: false", r.stdout)

    def test_http_failure_nonzero_no_retry(self):
        with mock.patch.dict(os.environ, clean_env(GITHUB_TOKEN=FAKE_TOKEN), clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=http_error(500)) as m_open:
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_github_models("hi", "openai/gpt-4o-mini")
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertEqual(m_open.call_count, 1)  # HTTP errors fail immediately

    def test_http_401_nonzero(self):
        with mock.patch.dict(os.environ, clean_env(GITHUB_TOKEN=FAKE_TOKEN), clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=http_error(401, b"unauthorized")):
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_github_models("hi", "openai/gpt-4o-mini")
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_invalid_model_nonzero(self):
        with self.assertRaises(ValueError) as ctx:
            llm_provider.GitHubModelsProducer(model="not-a-real/model")
        self.assertIn("Invalid model", str(ctx.exception))
        r = run_check("--producer-model", "not-a-real/model",
                      env=clean_env(GITHUB_TOKEN=FAKE_TOKEN))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("MOCK MODE", r.stdout)

    def test_malformed_json_nonzero(self):
        with mock.patch.dict(os.environ, clean_env(GITHUB_TOKEN=FAKE_TOKEN), clear=True):
            with mock.patch.object(llm_provider, "call_github_models",
                                   return_value=("not json {{", {"mock": False})):
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.GitHubModelsProducer(model="openai/gpt-4o-mini").produce(
                        {"topic": "t", "technology_angle": "AI", "discovery_source": {},
                         "evidence_source": {}, "trusted_excerpt": "", "recent_topics": [],
                         "editorial_policy": {"brand": "b", "pillars": [], "tone": "",
                                              "length_target": [70, 105], "forbidden_openers": []}})
        self.assertIn("malformed JSON", str(ctx.exception))


class MockGatingTests(unittest.TestCase):
    def test_mock_only_with_explicit_flag(self):
        with mock.patch.dict(os.environ, clean_env(), clear=True):
            self.assertFalse(llm_provider.is_mock_enabled())
        for val in ("0", "true", "yes", "True", ""):
            with mock.patch.dict(os.environ, clean_env(MOCK_GITHUB_MODELS=val), clear=True):
                self.assertFalse(llm_provider.is_mock_enabled(), val)
        with mock.patch.dict(os.environ, clean_env(MOCK_GITHUB_MODELS="1"), clear=True):
            self.assertTrue(llm_provider.is_mock_enabled())

    def test_explicit_mock_cli_ok_with_unambiguous_banner(self):
        r = run_check("--mock", env=clean_env())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("MOCK MODE", r.stdout)
        self.assertIn("Mock used: true", r.stdout)
        self.assertIn("NOT a real", r.stdout)
        # The real-success line must never appear in mock output.
        self.assertNotIn("GitHub Models connection: OK", r.stdout)

    def test_connection_check_workflow_never_enables_mock(self):
        code = wf_code(CHECK_WF)
        wf = wf_yaml(CHECK_WF)
        self.assertNotIn("MOCK_GITHUB_MODELS=1", code)
        self.assertNotIn("MOCK MODE", code)
        self.assertNotIn("mock used", code.lower())
        self.assertNotIn('os.environ["MOCK_GITHUB_MODELS"]', code)
        for step in wf["jobs"]["check"]["steps"]:
            env = step.get("env", {}) or {}
            self.assertNotIn("MOCK_GITHUB_MODELS", env, step.get("name"))
        # Manual dispatch with mock defaulting to false.
        triggers = wf.get("on", wf.get(True))
        self.assertEqual(list(triggers), ["workflow_dispatch"])
        self.assertIs(triggers["workflow_dispatch"]["inputs"]["mock"]["default"], False)

    def test_connection_check_permissions_minimal(self):
        wf = wf_yaml(CHECK_WF)
        self.assertEqual(wf.get("permissions"), {})
        self.assertEqual(wf["jobs"]["check"]["permissions"],
                         {"contents": "read", "models": "read"})
        text = wf_text(CHECK_WF)
        self.assertIn("secrets.GITHUB_TOKEN", text)
        self.assertNotIn("secrets.BUFFER_TOKEN", text)


class ProductionMockBanTests(unittest.TestCase):
    def test_daily_workflow_cannot_enable_mock(self):
        code = wf_code(DAILY_WF)
        wf = wf_yaml(DAILY_WF)
        # Every live reference to MOCK_GITHUB_MODELS must be the guard (fail
        # when enabled), the unset, or the error message — never an assignment.
        import re
        for line in code.splitlines():
            if "MOCK_GITHUB_MODELS" not in line:
                continue
            ok = ("${MOCK_GITHUB_MODELS:-}" in line or
                  "unset MOCK_GITHUB_MODELS" in line or
                  "MOCK_GITHUB_MODELS not enabled" in line or
                  "MOCK_GITHUB_MODELS must not be enabled" in line)
            self.assertTrue(ok, f"mock-enabling line in daily workflow: {line!r}")
        self.assertNotRegex(code, r"(?<![:-])\bMOCK_GITHUB_MODELS\s*=\s*1\b")
        self.assertNotRegex(code, r"MOCK_GITHUB_MODELS\s*:\s*[\"']?1")
        for step in wf["jobs"]["reel"]["steps"]:
            env = step.get("env", {}) or {}
            self.assertNotIn("MOCK_GITHUB_MODELS", env, step.get("name"))
        names = [s.get("name", "") for s in wf["jobs"]["reel"]["steps"]]
        self.assertTrue(any("forbid mock" in n for n in names), names)
        self.assertIn("unset MOCK_GITHUB_MODELS", code)
        # Cron trigger still present (production schedule).
        triggers = wf.get("on", wf.get(True))
        self.assertIn("schedule", triggers)

    def test_pipeline_refuses_mock_in_ci(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "MOCK_GITHUB_MODELS": "1"}):
            with self.assertRaises(pl.Stage) as ctx:
                pl.assert_no_mock_in_ci()
            self.assertIn("forbidden", str(ctx.exception))
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False):
            os.environ.pop("MOCK_GITHUB_MODELS", None)
            pl.assert_no_mock_in_ci()  # no raise
        with mock.patch.dict(os.environ, {"MOCK_GITHUB_MODELS": "1"}, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            pl.assert_no_mock_in_ci()  # local tests allowed
        src = read_source("build/pipeline.py")
        self.assertIn("assert_no_mock_in_ci()", src)


class GenerationModeHonestyTests(unittest.TestCase):
    def _topic_file(self, tmp):
        cal = next(c for c in common.calendar()["episodes"] if c["id"] == 2)
        topic = {"content_date": "2026-09-16", "content_id": "reel-2026-09-16",
                 "title": cal["title"], "normalized_topic": common.normalize_title(cal["title"]),
                 "pillar": cal.get("pillar", "AI_JUDGMENT"),
                 "technology_angle": "automation bias in AI assistants",
                 "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
                 "evidence_mode": "calendar", "calendar": cal}
        path = os.path.join(tmp, "topic.json")
        common.save_json(path, topic)
        return path

    def _run_main(self, topic_path, out_dir):
        with mock.patch.object(sys, "argv", ["content_producer.py", "--topic", topic_path,
                                             "--out", out_dir, "--variant", "0"]):
            cp.main()
        return common.load_json(os.path.join(out_dir, "script.json"))

    def test_failed_real_request_cannot_claim_github_models(self):
        tmp = tempfile.mkdtemp(prefix="honesty_")
        try:
            with mock.patch.dict(os.environ, clean_env(GITHUB_TOKEN=FAKE_TOKEN), clear=True):
                with mock.patch.object(llm_provider.GitHubModelsProducer, "produce",
                                       side_effect=RuntimeError("GitHub Models HTTP 500: host=x")):
                    script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"))
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
            report = common.load_json(os.path.join(tmp, "ep", "producer_report.json"), {})
            self.assertNotEqual(report.get("mode"), "github-models")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_daily_model_dns_failure_goes_static_fallback(self):
        tmp = tempfile.mkdtemp(prefix="fallback_")
        try:
            err = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
            with mock.patch.dict(os.environ, clean_env(GITHUB_TOKEN=FAKE_TOKEN), clear=True):
                with mock.patch("urllib.request.urlopen", side_effect=err), \
                        mock.patch("time.sleep"):
                    script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"))
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
            self.assertEqual(script["meta"]["language"], "en")
            self.assertTrue(script["chunks"])
            self.assertIn("AI", script["meta"]["technology_angle"] +
                          " ".join(l["t"] for ch in script["chunks"] for l in ch["en"]))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mock_output_rejected_in_daily_path(self):
        tmp = tempfile.mkdtemp(prefix="mockrej_")
        try:
            with mock.patch.dict(os.environ, clean_env(MOCK_GITHUB_MODELS="1"), clear=True):
                script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"))
            # Even with the explicit flag, the daily path must not label
            # fixture output as github-models.
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class NoBufferDuringCheckTests(unittest.TestCase):
    def test_no_buffer_in_connection_check(self):
        code = wf_code(CHECK_WF)
        self.assertNotIn("createPost", code)
        self.assertNotIn("buffer_publish", code)
        self.assertNotIn("BUFFER_TOKEN", code)
        script_src = read_source("build/github_models_check.py")
        self.assertNotIn("buffer_publish", script_src)
        self.assertNotIn("createPost", script_src)
        self.assertNotIn("import buffer", script_src)
        provider_src = read_source("build/llm_provider.py")
        self.assertNotIn("buffer_publish", provider_src)
        self.assertNotIn("createPost", provider_src)


class NoSecretLeakageTests(unittest.TestCase):
    def test_cli_output_contains_no_secrets(self):
        r = run_check(env=clean_env(
            GITHUB_TOKEN=FAKE_TOKEN,
            GITHUB_MODELS_ENDPOINT="https://nonexistent-host-xyz12345.test/inference/chat/completions"))
        self.assertNotEqual(r.returncode, 0)
        combined = r.stdout + r.stderr
        self.assertNotIn(FAKE_TOKEN, combined)
        self.assertNotIn("Bearer", combined)
        self.assertNotIn("Authorization", combined)

    def test_http_error_body_is_scrubbed(self):
        with mock.patch.dict(os.environ, clean_env(GITHUB_TOKEN=FAKE_TOKEN), clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=http_error(500, f"upstream {FAKE_TOKEN} bad".encode())):
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_github_models("prompt mentioning nothing", "openai/gpt-4o-mini")
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_malformed_json_error_has_no_response_text(self):
        secret_snippet = "SECRET_RESPONSE_SNIPPET_abc123"
        with mock.patch.dict(os.environ, clean_env(GITHUB_TOKEN=FAKE_TOKEN), clear=True):
            with mock.patch.object(llm_provider, "call_github_models",
                                   return_value=(secret_snippet + " {{", {"mock": False})):
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.GitHubModelsProducer(model="openai/gpt-4o-mini").produce(
                        {"topic": "t", "technology_angle": "AI", "discovery_source": {},
                         "evidence_source": {}, "trusted_excerpt": "", "recent_topics": [],
                         "editorial_policy": {"brand": "b", "pillars": [], "tone": "",
                                              "length_target": [70, 105], "forbidden_openers": []}})
        self.assertNotIn(secret_snippet, str(ctx.exception))
        self.assertIn("content_len=", str(ctx.exception))


class StubServerContractTests(StubServerMixin, unittest.TestCase):
    def test_success_contract_end_to_end(self):
        StubHandler.mode = "ok"
        r = run_check(env=clean_env(GITHUB_TOKEN=FAKE_TOKEN, GITHUB_MODELS_ENDPOINT=self.endpoint))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for line in ("GitHub Models connection: OK",
                     "Producer model: openai/gpt-4o-mini",
                     "Producer structured output: OK",
                     "Reviewer model: meta/llama-3.3-70b-instruct",
                     "Reviewer structured output: OK",
                     "Reviewer approved: true",
                     "Mock used: false"):
            self.assertIn(line, r.stdout)
        self.assertNotIn("MOCK MODE", r.stdout)
        self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr)

    def test_http_500_contract_nonzero(self):
        StubHandler.mode = "http500"
        r = run_check(env=clean_env(GITHUB_TOKEN=FAKE_TOKEN, GITHUB_MODELS_ENDPOINT=self.endpoint))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HTTP 500", r.stdout)
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("MOCK MODE", r.stdout)
        self.assertNotIn("GitHub Models connection: OK", r.stdout)

    def test_producer_malformed_contract_nonzero(self):
        StubHandler.mode = "producer_bad"
        r = run_check(env=clean_env(GITHUB_TOKEN=FAKE_TOKEN, GITHUB_MODELS_ENDPOINT=self.endpoint))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("malformed JSON", r.stdout)
        self.assertIn("Mock used: false", r.stdout)

    def test_reviewer_failure_contract_nonzero(self):
        StubHandler.mode = "reviewer_bad"
        r = run_check(env=clean_env(GITHUB_TOKEN=FAKE_TOKEN, GITHUB_MODELS_ENDPOINT=self.endpoint))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reviewer", r.stdout.lower())
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("GitHub Models connection: OK", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
