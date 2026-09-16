"""Fail-closed tests for the Groq provider (GitHub Models retired 2026-07-30).

Covers the required matrix:
  * missing GROQ_API_KEY / DNS failure / 401/403 / 429+Retry-After / 5xx /
    timeout / malformed JSON / unavailable model -> nonzero, never mock
  * model discovery via /models, candidates intersected (never blind)
  * Producer/Reviewer differ when two valid models exist
  * no Mock/static fallback in the real connection check
  * Mock explicit only (unit test or dispatch mock=true)
  * production cron cannot enable Mock
  * secret never leaked (key, Authorization, prompt, response)
  * Groq failure -> static-fallback in daily pipeline
  * failed Groq request cannot claim generation_mode=groq
  * connection-check contains no Buffer/createPost/Instagram
  * quarantine Issue #14 preserved
  * migration completeness: no retired provider in live code/workflows

No network, no real keys, no Buffer. Local stub HTTP server only.
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
CHECK_WF = os.path.join(WF_DIR, "groq-connection-check.yml")
DAILY_WF = os.path.join(WF_DIR, "daily-trend-draft.yml")
CHECK_SCRIPT = os.path.join(ROOT, "build", "groq_check.py")

OFFICIAL_BASE = "https://api.groq.com/openai/v1"
OFFICIAL_HOSTNAME = "api.groq.com"
RETIRED_HOSTS = ("models.github.ai", "models.inference.ai.azure.com")
RETIRED_IDENTIFIERS = ("GitHubModelsProducer", "GitHubModelsReviewer",
                       "call_github_models", "MOCK_GITHUB_MODELS")

FAKE_KEY = "FAKE_GROQ_KEY_FOR_TESTS_ONLY_" + "X" * 48
PROD_CANDIDATE = "openai/gpt-oss-20b"

VALID_PRODUCER_JSON = json.dumps({
    "title": "Test reel",
    "technology_angle": "automation bias in AI assistants",
    "metacognition_concept": "automation bias",
    "hook": "When does your AI assistant make you think less?",
    "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
    "narration": {"hook": "When does your AI assistant make you think less?",
                  "problem": ["You ask for code, the answer lands instantly, checking feels slower than pasting.",
                              "The speed hides the one step that matters: verifying what actually arrived.",
                              "It isn't laziness; it's a calibration swap the interface performs on you."],
                  "explain": ["Your brain reads the model's fluency as your own understanding.",
                              "That's automation bias: the tool sounds sure, so you stop doubting.",
                              "Every unchecked answer makes the trust a little less earned.",
                              "Fluency is not accuracy, but in the moment it feels exactly like it."],
                  "example": ["Think of the last time autocomplete finished your function.",
                              "Did you read it line by line, or just accept the shape of it?",
                              "Most people accept, because a surprise-free streak feels like proof.",
                              "That pause you skip is where the learning would have happened."],
                  "technique": ["Try this: before you accept AI code, explain it out loud in one sentence.",
                                "Then run one edge case yourself, not the demo case.",
                                "If you can't explain it, you haven't learned it, and it isn't yours yet.",
                                "The pause costs ten seconds; the bug costs your afternoon."],
                  "ending": "Where did AI make you skip the thinking this week?"},
    "on_screen_text": ["A", "B", "C", "D", "E", "F"],
    "visual_direction": "code visual",
    "actionable_technique": "Explain before accept",
    "ending": "Where did AI make you skip the thinking this week?",
    "caption": {"hook": "When does AI make you think less?", "intro": "Fluency is not understanding.",
                "sections": [], "hashtags": ["#metacognition", "#AI"]},
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
         if k not in ("GROQ_API_KEY", "MOCK_GROQ", "GROQ_BASE_URL",
                      "PRODUCER_MODEL", "REVIEWER_MODEL", "GITHUB_ACTIONS")}
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


def wf_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_source(relpath):
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


def http_error(code, body=b"boom", headers=None):
    return urllib.error.HTTPError(
        "https://api.groq.com/openai/v1/chat/completions", code,
        "err", headers or {}, io.BytesIO(body))


class FakeResp:
    def __init__(self, obj, status=200):
        self._data = json.dumps(obj).encode()
        self.status = status

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class StubHandler(BaseHTTPRequestHandler):
    models_mode = "ok"   # ok | single | nomatch | empty | e401 | e500
    chat_mode = "ok"     # ok | e500 | e401 | r429_once | r429_always | producer_bad | reviewer_bad
    post_count = 0
    SERVED = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"]

    def _send_json(self, obj, code=200, extra_headers=None):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self):
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and len(auth) > 10

    def do_GET(self):  # noqa: N802 — stdlib handler name
        if self.path != "/openai/v1/models":
            self.send_response(404)
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        if not self._auth_ok():
            self._send_json({"error": "auth required"}, code=401)
            return
        mode = StubHandler.models_mode
        if mode == "e401":
            self._send_json({"error": "bad key"}, code=401)
        elif mode == "e500":
            self._send_json({"error": "boom"}, code=500)
        elif mode == "empty":
            self._send_json({"data": []})
        elif mode == "single":
            self._send_json({"data": [{"id": "openai/gpt-oss-20b"}]})
        elif mode == "nomatch":
            self._send_json({"data": [{"id": "some-unrelated-model"}]})
        else:
            self._send_json({"data": [{"id": m} for m in StubHandler.SERVED]})

    def do_POST(self):  # noqa: N802 — stdlib handler name
        StubHandler.post_count += 1
        if self.path != "/openai/v1/chat/completions":
            self._send_json({"error": "not found"}, code=404)
            return
        if not self._auth_ok():
            self._send_json({"error": "auth required"}, code=401)
            return
        mode = StubHandler.chat_mode
        if mode == "e500":
            self._send_json({"error": "boom"}, code=500)
            return
        if mode == "e401":
            self._send_json({"error": "bad key"}, code=401)
            return
        if mode == "r429_always":
            self._send_json({"error": "rate"}, code=429, extra_headers={"Retry-After": "1"})
            return
        if mode == "r429_once" and StubHandler.post_count == 1:
            self._send_json({"error": "rate"}, code=429, extra_headers={"Retry-After": "1"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            model = json.loads(raw).get("model", "")
        except Exception:
            model = ""
        if model and model not in StubHandler.SERVED:
            self._send_json({"error": "model not found"}, code=404)
            return
        is_reviewer = "GroqReviewer" in raw
        if mode == "producer_bad" and not is_reviewer:
            content = "not json {{"
        elif mode == "reviewer_bad" and is_reviewer:
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
        cls.base = f"http://127.0.0.1:{cls.port}/openai/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        StubHandler.models_mode = "ok"
        StubHandler.chat_mode = "ok"
        StubHandler.post_count = 0


def evidence_packet():
    return {"topic": "t", "technology_angle": "AI", "discovery_source": {},
            "evidence_source": {}, "trusted_excerpt": "", "recent_topics": [],
            "editorial_policy": {"brand": "b", "pillars": [], "tone": "",
                                 "length_target": [70, 105], "forbidden_openers": []}}


class MigrationCompletenessTests(unittest.TestCase):
    def test_retired_provider_files_gone_new_files_exist(self):
        for gone in ("build/github_models_check.py",
                     ".github/workflows/github-models-connection-check.yml",
                     "tests/test_github_models_failclosed.py",
                     "docs/github_models_connection_fix.md"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, gone)), gone)
        for present in ("build/groq_check.py",
                        ".github/workflows/groq-connection-check.yml",
                        "tests/test_groq_failclosed.py",
                        "docs/groq_migration.md"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, present)), present)

    def test_no_retired_endpoints_or_identifiers_in_live_code(self):
        for rel in os.listdir(os.path.join(ROOT, "build")):
            if not rel.endswith(".py"):
                continue
            src = read_source(os.path.join("build", rel))
            for host in RETIRED_HOSTS:
                self.assertNotIn(host, src, f"build/{rel} references retired {host}")
            for ident in RETIRED_IDENTIFIERS:
                self.assertNotIn(ident, src, f"build/{rel} references retired {ident}")
        for name in os.listdir(WF_DIR):
            if not name.endswith((".yml", ".yaml")):
                continue
            code = wf_code(os.path.join(WF_DIR, name))
            for host in RETIRED_HOSTS:
                self.assertNotIn(host, code, f"{name} references retired {host}")
            self.assertNotIn("models: read", code, f"{name} still requests models: read")

    def test_generic_abstraction_classes_exist(self):
        self.assertTrue(hasattr(llm_provider, "LLMProvider"))
        self.assertTrue(hasattr(llm_provider, "GroqProducer"))
        self.assertTrue(hasattr(llm_provider, "GroqReviewer"))
        self.assertTrue(hasattr(llm_provider, "StaticEnglishFallback"))
        self.assertTrue(issubclass(llm_provider.GroqProducer, llm_provider.LLMProvider))
        self.assertTrue(issubclass(llm_provider.GroqReviewer, llm_provider.LLMProvider))

    def test_official_endpoint_and_hostname(self):
        self.assertEqual(llm_provider.GROQ_BASE_URL, OFFICIAL_BASE)
        self.assertEqual(llm_provider.GROQ_HOSTNAME, OFFICIAL_HOSTNAME)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_BASE_URL", None)
            self.assertEqual(llm_provider.get_base_url(), OFFICIAL_BASE)
            self.assertEqual(llm_provider.get_hostname(), OFFICIAL_HOSTNAME)

    def test_candidate_list_sane(self):
        cands = llm_provider.GROQ_CANDIDATE_MODELS
        for required in ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"):
            self.assertIn(required, cands)
        self.assertGreaterEqual(len(cands), 4)
        self.assertEqual(cands[0], "openai/gpt-oss-120b")  # strongest first


class RealModeFailureTests(unittest.TestCase):
    def test_missing_key_raises_and_cli_nonzero(self):
        with mock.patch.dict(os.environ, clean_env(), clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                llm_provider.call_groq_chat("hi", PROD_CANDIDATE)
            self.assertIn("GROQ_API_KEY not set", str(ctx.exception))
            with self.assertRaises(RuntimeError):
                llm_provider.GroqProducer(model=PROD_CANDIDATE).produce(evidence_packet())
        r = run_check(env=clean_env())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("GROQ_API_KEY present: false", r.stdout)
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("MOCK MODE", r.stdout)

    def test_dns_failure_nonzero_with_retries(self):
        err = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=err) as m_open, \
                    mock.patch("time.sleep") as m_sleep:
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_groq_chat("hi", PROD_CANDIDATE)
        self.assertIn("attempts=3", str(ctx.exception))
        self.assertEqual(m_open.call_count, 3)
        self.assertEqual([c.args[0] for c in m_sleep.call_args_list], [1.0, 2.0])

    def test_dns_failure_cli_nonzero_no_mock(self):
        r = run_check(env=clean_env(
            GROQ_API_KEY=FAKE_KEY,
            GROQ_BASE_URL="https://nonexistent-host-xyz12345.test/openai/v1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("MOCK MODE", r.stdout)
        self.assertNotIn("Groq connection: OK", r.stdout)
        self.assertIn("Mock used: false", r.stdout)

    def test_401_no_retry_auth_error(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=http_error(401, b"invalid key")) as m_open:
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_groq_chat("hi", PROD_CANDIDATE)
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertIn("authentication failed", str(ctx.exception))
        self.assertEqual(m_open.call_count, 1)

    def test_403_no_retry(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=http_error(403, b"forbidden")) as m_open:
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_groq_chat("hi", PROD_CANDIDATE)
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertEqual(m_open.call_count, 1)

    def test_429_honors_retry_after_then_succeeds(self):
        ok = {"choices": [{"message": {"content": "hello"}}]}
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=[
                    http_error(429, b"slow down", {"Retry-After": "2"}),
                    FakeResp(ok)]) as m_open, \
                    mock.patch("time.sleep") as m_sleep:
                content, meta = llm_provider.call_groq_chat("hi", PROD_CANDIDATE)
        self.assertEqual(content, "hello")
        self.assertEqual(m_open.call_count, 2)
        m_sleep.assert_called_once_with(2)

    def test_429_exhausted_after_two_retries(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=http_error(429, b"slow", {"Retry-After": "1"})) as m_open, \
                    mock.patch("time.sleep") as m_sleep:
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_groq_chat("hi", PROD_CANDIDATE)
        self.assertIn("quota 429 exhausted", str(ctx.exception))
        self.assertEqual(m_open.call_count, 3)  # 1 + 2 retries
        self.assertEqual(m_sleep.call_count, 2)

    def test_5xx_limited_retry_then_raise(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=http_error(500)) as m_open, \
                    mock.patch("time.sleep"):
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_groq_chat("hi", PROD_CANDIDATE)
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertIn("attempts=3", str(ctx.exception))
        self.assertEqual(m_open.call_count, 3)

    def test_5xx_then_success(self):
        ok = {"choices": [{"message": {"content": "recovered"}}]}
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=[http_error(503), FakeResp(ok)]) as m_open, \
                    mock.patch("time.sleep"):
                content, _ = llm_provider.call_groq_chat("hi", PROD_CANDIDATE)
        self.assertEqual(content, "recovered")
        self.assertEqual(m_open.call_count, 2)

    def test_timeout_retries_then_raise(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=socket.timeout("timed out")) as m_open, \
                    mock.patch("time.sleep"):
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.call_groq_chat("hi", PROD_CANDIDATE)
        self.assertIn("attempts=3", str(ctx.exception))
        self.assertEqual(m_open.call_count, 3)

    def test_invalid_model_constructor_raises(self):
        with self.assertRaises(ValueError) as ctx:
            llm_provider.GroqProducer(model="not-a-real/model")
        self.assertIn("Invalid model", str(ctx.exception))

    def test_unavailable_model_raises(self):
        with self.assertRaises(ValueError) as ctx:
            llm_provider.select_models(["some-unrelated-model"], "openai/gpt-oss-120b", "auto")
        self.assertIn("unavailable", str(ctx.exception))

    def test_no_candidates_matched_raises(self):
        with self.assertRaises(ValueError) as ctx:
            llm_provider.select_models(["some-unrelated-model"], "auto", "auto")
        self.assertIn("none of the", str(ctx.exception))

    def test_malformed_json_nonzero(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch.object(llm_provider, "call_groq_chat",
                                   return_value=("not json {{", {"mock": False})), \
                    mock.patch.object(llm_provider, "discover_models",
                                      return_value=([PROD_CANDIDATE], {"http_status": 200})):
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.GroqProducer(model=PROD_CANDIDATE).produce(evidence_packet())
        self.assertIn("malformed JSON", str(ctx.exception))


class ModelDiscoveryTests(unittest.TestCase):
    def test_auto_selects_strongest_and_differs(self):
        discovered = ["llama-3.1-8b-instant", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
        prod, rev, report = llm_provider.select_models(discovered, "auto", "auto")
        self.assertEqual(prod, "openai/gpt-oss-120b")
        self.assertNotEqual(rev, prod)
        self.assertIn(rev, discovered)
        self.assertIn("producer_model", report)
        self.assertIn("selected_at_utc", report)
        self.assertIn("reason", report)
        self.assertTrue(report["reason"])

    def test_single_model_reviewer_reuses_with_reason(self):
        prod, rev, report = llm_provider.select_models([PROD_CANDIDATE], "auto", "auto")
        self.assertEqual(prod, PROD_CANDIDATE)
        self.assertEqual(rev, PROD_CANDIDATE)
        self.assertIn("only one", report["reason"])

    def test_explicit_models_honored_when_discovered(self):
        discovered = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
        prod, rev, _ = llm_provider.select_models(
            discovered, "openai/gpt-oss-20b", "openai/gpt-oss-120b")
        self.assertEqual(prod, "openai/gpt-oss-20b")
        self.assertEqual(rev, "openai/gpt-oss-120b")

    def test_retired_candidate_skipped_for_next(self):
        # Top candidate retired server-side → next matched candidate selected.
        discovered = ["openai/gpt-oss-20b", "llama-3.3-70b-versatile"]
        prod, _, _ = llm_provider.select_models(discovered, "auto", "auto")
        self.assertEqual(prod, "llama-3.3-70b-versatile")


class MockGatingTests(unittest.TestCase):
    def test_mock_only_with_explicit_flag(self):
        with mock.patch.dict(os.environ, clean_env(), clear=True):
            self.assertFalse(llm_provider.is_mock_enabled())
        for val in ("0", "true", "yes", "True", ""):
            with mock.patch.dict(os.environ, clean_env(MOCK_GROQ=val), clear=True):
                self.assertFalse(llm_provider.is_mock_enabled(), val)
        with mock.patch.dict(os.environ, clean_env(MOCK_GROQ="1"), clear=True):
            self.assertTrue(llm_provider.is_mock_enabled())

    def test_explicit_mock_cli_ok_with_unambiguous_banner(self):
        r = run_check("--mock", env=clean_env())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("MOCK MODE", r.stdout)
        self.assertIn("Mock used: true", r.stdout)
        self.assertIn("NOT a real", r.stdout)
        self.assertNotIn("Groq connection: OK", r.stdout)

    def test_connection_check_workflow_never_enables_mock(self):
        code = wf_code(CHECK_WF)
        wf = wf_yaml(CHECK_WF)
        self.assertNotIn("MOCK_GROQ=1", code.replace("MOCK_GROQ is deliberately NEVER set", ""))
        self.assertNotIn("MOCK MODE", code)
        self.assertNotIn("mock used", code.lower())
        for step in wf["jobs"]["check"]["steps"]:
            env = step.get("env", {}) or {}
            self.assertNotIn("MOCK_GROQ", env, step.get("name"))
        triggers = wf.get("on", wf.get(True))
        self.assertEqual(list(triggers), ["workflow_dispatch"])
        self.assertIs(triggers["workflow_dispatch"]["inputs"]["mock"]["default"], False)

    def test_connection_check_permissions_minimal_no_models_scope(self):
        wf = wf_yaml(CHECK_WF)
        self.assertEqual(wf.get("permissions"), {})
        self.assertEqual(wf["jobs"]["check"]["permissions"], {"contents": "read"})
        code = wf_code(CHECK_WF)
        self.assertIn("secrets.GROQ_API_KEY", code)
        self.assertNotIn("secrets.GITHUB_TOKEN", code)
        self.assertNotIn("GITHUB_TOKEN", code)
        # Key in exactly one step.
        holders = [s.get("name") for s in wf["jobs"]["check"]["steps"]
                   if "GROQ_API_KEY" in str((s.get("env", {}) or {}))]
        self.assertEqual(len(holders), 1, holders)


class ProductionMockBanTests(unittest.TestCase):
    def test_daily_workflow_cannot_enable_mock(self):
        code = wf_code(DAILY_WF)
        wf = wf_yaml(DAILY_WF)
        for line in code.splitlines():
            if "MOCK_GROQ" not in line:
                continue
            ok = ("${MOCK_GROQ:-}" in line or
                  "unset MOCK_GROQ" in line or
                  "MOCK_GROQ not enabled" in line or
                  "MOCK_GROQ must not be enabled" in line)
            self.assertTrue(ok, f"mock-enabling line in daily workflow: {line!r}")
        self.assertNotRegex(code, r"(?<![:-])\bMOCK_GROQ\s*=\s*1\b")
        self.assertNotRegex(code, r"MOCK_GROQ\s*:\s*[\"']?1")
        for step in wf["jobs"]["reel"]["steps"]:
            env = step.get("env", {}) or {}
            self.assertNotIn("MOCK_GROQ", env, step.get("name"))
        names = [s.get("name", "") for s in wf["jobs"]["reel"]["steps"]]
        self.assertTrue(any("forbid mock" in n for n in names), names)
        self.assertIn("unset MOCK_GROQ", code)
        triggers = wf.get("on", wf.get(True))
        self.assertIn("schedule", triggers)

    def test_daily_groq_key_only_in_produce_step(self):
        wf = wf_yaml(DAILY_WF)
        self.assertNotIn("GROQ_API_KEY", str(wf.get("env", {}) or {}))
        self.assertNotIn("GROQ_API_KEY", str(wf["jobs"]["reel"].get("env", {}) or {}))
        holders = [s.get("id") or s.get("name") for s in wf["jobs"]["reel"]["steps"]
                   if "GROQ_API_KEY" in str((s.get("env", {}) or {}))]
        self.assertEqual(holders, ["produce"], holders)
        produce = next(s for s in wf["jobs"]["reel"]["steps"] if s.get("id") == "produce")
        self.assertEqual(produce["env"].get("CONTENT_PRODUCER"), "groq")
        self.assertEqual(produce["env"].get("CONTENT_FALLBACK"), "static-english")
        self.assertEqual(produce["env"].get("PRODUCER_MODEL"), "auto")
        self.assertEqual(produce["env"].get("REVIEWER_MODEL"), "auto")

    def test_pipeline_refuses_mock_in_ci(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "MOCK_GROQ": "1"}):
            with self.assertRaises(pl.Stage) as ctx:
                pl.assert_no_mock_in_ci()
            self.assertIn("forbidden", str(ctx.exception))
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False):
            os.environ.pop("MOCK_GROQ", None)
            pl.assert_no_mock_in_ci()  # no raise
        with mock.patch.dict(os.environ, {"MOCK_GROQ": "1"}, clear=False):
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

    def test_failed_real_request_cannot_claim_groq(self):
        tmp = tempfile.mkdtemp(prefix="honesty_")
        try:
            with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
                with mock.patch.object(llm_provider, "discover_models",
                                       return_value=([PROD_CANDIDATE], {"http_status": 200})), \
                     mock.patch.object(llm_provider.GroqProducer, "produce",
                                       side_effect=RuntimeError("Groq HTTP 500: host=x")):
                    script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"))
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
            report = common.load_json(os.path.join(tmp, "ep", "producer_report.json"), {})
            self.assertNotEqual(report.get("mode"), "groq")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_daily_model_dns_failure_goes_static_fallback(self):
        tmp = tempfile.mkdtemp(prefix="fallback_")
        try:
            err = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
            with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
                with mock.patch("urllib.request.urlopen", side_effect=err), \
                        mock.patch("time.sleep"):
                    script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"))
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
            self.assertEqual(script["meta"]["language"], "en")
            self.assertTrue(script["chunks"])
            text = script["meta"]["technology_angle"] + " ".join(
                l["t"] for ch in script["chunks"] for l in ch["en"])
            self.assertIn("AI", text)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_daily_missing_key_goes_static_fallback(self):
        tmp = tempfile.mkdtemp(prefix="nokey_")
        try:
            with mock.patch.dict(os.environ, clean_env(), clear=True):
                script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"))
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mock_output_rejected_in_daily_path(self):
        tmp = tempfile.mkdtemp(prefix="mockrej_")
        try:
            with mock.patch.dict(os.environ,
                                 clean_env(MOCK_GROQ="1", GROQ_API_KEY=FAKE_KEY), clear=True):
                with mock.patch.object(llm_provider, "discover_models",
                                       return_value=([PROD_CANDIDATE], {"http_status": 200})):
                    script = self._run_main(self._topic_file(tmp), os.path.join(tmp, "ep"))
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class NoBufferDuringCheckTests(unittest.TestCase):
    def test_no_buffer_instagram_in_connection_check(self):
        code = wf_code(CHECK_WF)
        for bad in ("createPost", "buffer_publish", "BUFFER_TOKEN", "AUTO_PUBLISH",
                    "instagram", "INSTAGRAM", "insta_publish", "graph.facebook"):
            self.assertNotIn(bad, code, bad)
        script_src = read_source("build/groq_check.py")
        self.assertNotIn("buffer_publish", script_src)
        self.assertNotIn("createPost", script_src)
        self.assertNotIn("import buffer", script_src)
        provider_src = read_source("build/llm_provider.py")
        self.assertNotIn("buffer_publish", provider_src)
        self.assertNotIn("createPost", provider_src)


class NoSecretLeakageTests(unittest.TestCase):
    def test_cli_output_contains_no_secrets(self):
        r = run_check(env=clean_env(
            GROQ_API_KEY=FAKE_KEY,
            GROQ_BASE_URL="https://nonexistent-host-xyz12345.test/openai/v1"))
        self.assertNotEqual(r.returncode, 0)
        combined = r.stdout + r.stderr
        self.assertNotIn(FAKE_KEY, combined)
        self.assertNotIn("Bearer", combined)
        self.assertNotIn("Authorization", combined)

    def test_http_error_body_is_scrubbed(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=http_error(500, f"upstream {FAKE_KEY} bad".encode())):
                with mock.patch("time.sleep"):
                    with self.assertRaises(RuntimeError) as ctx:
                        llm_provider.call_groq_chat("prompt mentioning nothing", PROD_CANDIDATE)
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_malformed_json_error_has_no_response_text(self):
        secret_snippet = "SECRET_RESPONSE_SNIPPET_abc123"
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            with mock.patch.object(llm_provider, "call_groq_chat",
                                   return_value=(secret_snippet + " {{", {"mock": False})), \
                    mock.patch.object(llm_provider, "discover_models",
                                      return_value=([PROD_CANDIDATE], {"http_status": 200})):
                with self.assertRaises(RuntimeError) as ctx:
                    llm_provider.GroqProducer(model=PROD_CANDIDATE).produce(evidence_packet())
        self.assertNotIn(secret_snippet, str(ctx.exception))
        self.assertIn("content_len=", str(ctx.exception))


class StubServerContractTests(StubServerMixin, unittest.TestCase):
    def test_success_contract_end_to_end(self):
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for line in ("Groq connection: OK",
                     "Endpoint hostname: 127.0.0.1",
                     "Producer model: openai/gpt-oss-120b",
                     "Producer structured output: OK",
                     "Reviewer model: llama-3.3-70b-versatile",
                     "Reviewer structured output: OK",
                     "Reviewer approved: true",
                     "Mock used: false",
                     "Models discovered: 3",
                     "Model selection: producer=openai/gpt-oss-120b"):
            self.assertIn(line, r.stdout)
        self.assertNotIn("MOCK MODE", r.stdout)
        self.assertNotIn(FAKE_KEY, r.stdout + r.stderr)

    def test_explicit_valid_models_honored(self):
        r = run_check("--producer-model", "openai/gpt-oss-20b",
                      "--reviewer-model", "llama-3.3-70b-versatile",
                      env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Producer model: openai/gpt-oss-20b", r.stdout)
        self.assertIn("Reviewer model: llama-3.3-70b-versatile", r.stdout)
        self.assertIn("Groq connection: OK", r.stdout)

    def test_single_model_reviewer_reuse_contract(self):
        StubHandler.models_mode = "single"
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Producer model: openai/gpt-oss-20b", r.stdout)
        self.assertIn("Reviewer model: openai/gpt-oss-20b", r.stdout)
        self.assertIn("only one", r.stdout)
        self.assertIn("Groq connection: OK", r.stdout)

    def test_http_500_contract_nonzero(self):
        StubHandler.chat_mode = "e500"
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HTTP 500", r.stdout)
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("MOCK MODE", r.stdout)
        self.assertNotIn("Groq connection: OK", r.stdout)

    def test_401_contract_nonzero(self):
        StubHandler.models_mode = "e401"
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HTTP 401", r.stdout)
        self.assertIn("authentication failed", r.stdout)
        self.assertIn("Mock used: false", r.stdout)

    def test_429_once_recovers_contract(self):
        StubHandler.chat_mode = "r429_once"
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Groq connection: OK", r.stdout)

    def test_429_always_contract_nonzero(self):
        StubHandler.chat_mode = "r429_always"
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("quota 429 exhausted", r.stdout)
        self.assertIn("Mock used: false", r.stdout)

    def test_producer_malformed_contract_nonzero(self):
        StubHandler.chat_mode = "producer_bad"
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("malformed JSON", r.stdout)
        self.assertIn("Mock used: false", r.stdout)

    def test_reviewer_failure_contract_nonzero(self):
        StubHandler.chat_mode = "reviewer_bad"
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reviewer", r.stdout.lower())
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("Groq connection: OK", r.stdout)

    def test_unavailable_model_contract_nonzero(self):
        StubHandler.models_mode = "nomatch"
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("Groq connection: OK", r.stdout)

    def test_invalid_model_cli_nonzero(self):
        r = run_check("--producer-model", "not-a-real/model",
                      env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("MOCK MODE", r.stdout)

    def test_real_groq_response_claims_groq_in_daily_path(self):
        # End-to-end daily honesty: genuine stubbed API responses → groq.
        tmp = tempfile.mkdtemp(prefix="groqreal_")
        try:
            cal = next(c for c in common.calendar()["episodes"] if c["id"] == 2)
            topic = {"content_date": "2026-09-16", "content_id": "reel-2026-09-16",
                     "title": cal["title"], "normalized_topic": common.normalize_title(cal["title"]),
                     "pillar": cal.get("pillar", "AI_JUDGMENT"),
                     "technology_angle": "automation bias in AI assistants",
                     "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
                     "evidence_mode": "calendar", "calendar": cal}
            topic_path = os.path.join(tmp, "topic.json")
            common.save_json(topic_path, topic)
            out_dir = os.path.join(tmp, "ep")
            with mock.patch.dict(os.environ,
                                 clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base),
                                 clear=True):
                with mock.patch.object(sys, "argv", ["content_producer.py", "--topic", topic_path,
                                                     "--out", out_dir, "--variant", "0"]):
                    cp.main()
            script = common.load_json(os.path.join(out_dir, "script.json"))
            self.assertEqual(script["meta"]["generation_mode"], "groq")
            report = common.load_json(os.path.join(out_dir, "producer_report.json"), {})
            self.assertEqual(report.get("mode"), "groq")
            self.assertEqual(report["selection"]["producer_model"], "openai/gpt-oss-120b")
            self.assertIn("reason", report["selection"])
            self.assertIn("selected_at_utc", report["selection"])
            review = common.load_json(os.path.join(out_dir, "reviewer_report.json"), {})
            self.assertNotEqual(review.get("model"), report.get("model"))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class QuarantinePreservedTests(unittest.TestCase):
    def test_quarantine_issue14_preserved(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, "content", "quarantine.json")))
        self.assertTrue(common.is_quarantined("reel-2026-09-15", "2026-09-15"))
        self.assertTrue(common.is_quarantined("reel-2026-09-15", None))
        self.assertTrue(common.is_quarantined(None, "2026-09-15"))
        self.assertFalse(common.is_quarantined("reel-2026-09-16", "2026-09-16"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
