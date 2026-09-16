"""Central Groq HTTP client tests — Cloudflare 403/1010 root cause + fix.

Everything here runs against a LOCAL HTTP stub server. No real network, no real
API key, no Buffer, no publishing, no Instagram.

The stub can emulate the Cloudflare edge that sits in front of api.groq.com:
a request whose User-Agent is the Python stdlib default (`Python-urllib/3.x`)
is answered with **HTTP 403 + Cloudflare error code 1010** before any
credential is inspected. That is the exact failure seen in Actions run
35030569096 (`groq-connection-check`, main, run_attempt 3), and it is why
rotating GROQ_API_KEY changed nothing.

Mandated matrix covered below:
  * GET /models, Producer, Reviewer and Revision all send the explicit project
    User-Agent (and the same central client builds every one of them)
  * Authorization is present on authenticated calls, and its value never
    appears in a log, a CLI run, an exception or a test failure diff
  * GET carries Accept: application/json; POST carries Content-Type
  * HTTP 403 + code 1010 -> cloudflare-client-blocked, NO retry, red check,
    safe User-Agent/header hint, no mock
  * HTTP 401 -> invalid-or-missing-api-key
  * other HTTP 403 -> permission-or-account-restriction
  * HTTP 429 -> rate-limited, Retry-After honored, bounded retries
  * timeout -> bounded retries, then failure
  * the connection check NEVER falls back to mock; mock needs an explicit flag;
    a scheduled production run cannot enable mock
  * a Groq failure in the daily pipeline records static-fallback only and can
    never record generation_mode=groq
  * the connection check imports/calls no Buffer or createPost code
  * Issue #14 quarantine preserved; AUTO_PUBLISH_ENABLED untouched
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common            # noqa: E402
import content_producer as cp   # noqa: E402
import groq_http         # noqa: E402
import groq_check        # noqa: E402
import llm_provider      # noqa: E402
import pipeline as pl    # noqa: E402

CHECK_WF = os.path.join(ROOT, ".github", "workflows", "groq-connection-check.yml")
DAILY_WF = os.path.join(ROOT, ".github", "workflows", "daily-trend-draft.yml")
CHECK_SCRIPT = os.path.join(ROOT, "build", "groq_check.py")

PROJECT_UA = "metacognition-hq/1.0 (+https://github.com/Captain-Jorf/workspace)"
FAKE_KEY = "gsk_" + "T0KENVALUE0NLYF0RTESTS" + "Z" * 32   # realistic shape, fake
BAD_KEY_FRAGMENTS_LEN = 16

# A faithful copy of a Cloudflare edge block page (error code 1010).
CLOUDFLARE_1010_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Attention Required! | Cloudflare</title></head>
<body>
<div class="cf-error-details cf-error-1010">
  <h1>Sorry, you have been blocked</h1>
  <p>You are unable to access api.groq.com</p>
  <h2>Why have I been blocked?</h2>
  <p>The owner of this website has banned your access based on your browser's
     signature.</p>
  <div id="cf-error-details" class="cf-code-label">Error code: 1010</div>
</div>
<p>Cloudflare Ray ID: 8f0a1b2c3d4e5f60 &middot; Server: cloudflare</p>
</body>
</html>
"""

CLOUDFLARE_1010_JSON = json.dumps({
    "errors": [{"code": 1010,
                "message": "The owner of this website has banned your access "
                           "based on your browser's signature."}],
})

GROQ_401_JSON = json.dumps({
    "error": {"message": "Invalid API key provided", "type": "authentication_error",
              "code": "invalid_api_key"},
})

GROQ_403_OTHER_JSON = json.dumps({
    "error": {"message": "This account is restricted for model access",
              "type": "permission_error", "code": 1020},
})

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

REJECTING_REVIEWER_JSON = json.dumps({
    "approved": False, "score": 72, "technology_relevance": True,
    "metacognition_relevance": True, "source_grounding": True,
    "unsupported_claims": [], "hook_quality": "weak",
    "spoken_english_quality": "good", "novelty": "low",
    "practical_value": "high", "safety": "safe",
    "required_changes": ["MARKER-REVISION-REQUEST stronger hook"],
    "blocking_errors": ["hook_quality weak"],
})

# A generic CDN/edge error PAGE — captured shape from a real api.groq.com run
# (35042629998): HTTP 409 whose body was HTML instead of a Groq JSON answer.
# Note it carries NO Cloudflare error code, so it must not be mistaken for the
# 403/1010 client block.
EDGE_409_HTML = """<!DOCTYPE html>
<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<!--[if IE 7]>    <html class="no-js ie7 oldie" lang="en-US"> <![endif]-->
<!--[if IE 8]>    <html class="no-js ie8 oldie" lang="en-US"> <![endif]-->
<!--[if gt IE 8]><!--> <html class="no-js" lang="en-US"> <!--<![endif]-->
<head><title>edge | request failed</title></head>
<body>
<div id="cf-error-details"><h1>Something went wrong at the edge.</h1>
<p>The origin returned an unexpected response. Please retry shortly.</p>
</div></body></html>
"""

SERVED_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"]


def clean_env(**overrides):
    e = {k: v for k, v in os.environ.items()
         if k not in ("GROQ_API_KEY", "MOCK_GROQ", "GROQ_BASE_URL", "GROQ_USER_AGENT",
                      "PRODUCER_MODEL", "REVIEWER_MODEL", "GITHUB_ACTIONS",
                      "GITHUB_EVENT_NAME", "BUFFER_TOKEN")}
    e["CONTENT_LANGUAGE"] = "en"
    e.update(overrides)
    return e


def run_check(*args, env):
    return subprocess.run([sys.executable, CHECK_SCRIPT, *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)


class EdgeHandler(BaseHTTPRequestHandler):
    """Stub Groq endpoint sitting behind a Cloudflare-style UA filter."""

    # ok | cf1010 | cf1010_json | e401 | e403_other | r429 | r429_once | e500 |
    # timeout | revision | e409html | e409html_once | html200
    mode = "ok"
    edge_ua_filter = True      # emulate Cloudflare Browser Integrity Check
    recorded = []              # every request the edge actually saw
    post_count = 0
    get_count = 0

    protocol_version = "HTTP/1.1"

    def version_string(self):
        # BaseHTTPRequestHandler sends its own Server header first; make it the
        # edge's so `Server: cloudflare` is what a client really observes.
        return "cloudflare"

    # ---- helpers
    def _record(self, body):
        auth = self.headers.get("Authorization", "")
        EdgeHandler.recorded.append({
            "method": self.command,
            "path": self.path,
            "user_agent": self.headers.get("User-Agent", ""),
            "accept": self.headers.get("Accept", ""),
            "content_type": self.headers.get("Content-Type", ""),
            "has_authorization": bool(auth),
            "authorization": auth,               # kept in memory ONLY, never printed
            "body": body,
        })

    def _ua_blocked(self):
        ua = self.headers.get("User-Agent", "")
        if not EdgeHandler.edge_ua_filter:
            return False
        return (not ua) or ua.startswith("Python-urllib/") or ua.startswith("python-requests/")

    def _send(self, payload, code=200, raw=None, ctype="application/json",
              cloudflare=False, retry_after=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if cloudflare:
            self.send_header("Server", "cloudflare")
            self.send_header("CF-RAY", "8f0a1b2c3d4e5f60-IAD")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _blocked_1010(self):
        mode = EdgeHandler.mode
        if mode == "cf1010_json":
            self._send(None, code=403, raw=CLOUDFLARE_1010_JSON.encode(),
                       cloudflare=True)
        else:
            self._send(None, code=403, raw=CLOUDFLARE_1010_HTML.encode(),
                       ctype="text/html; charset=UTF-8", cloudflare=True)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        self._record(raw)
        return raw

    # ---- verbs
    def do_GET(self):   # noqa: N802 — stdlib handler name
        self._read_body()
        EdgeHandler.get_count += 1
        if self._ua_blocked():
            return self._blocked_1010()
        if self.path != "/openai/v1/models":
            return self._send({"error": "not found"}, code=404)
        mode = EdgeHandler.mode
        if mode == "timeout":
            import select
            select.select([], [], [], 0.6)   # longer than the 0.2 s client timeout
            return self._send({"error": "late"}, code=200)
        if mode == "r429_once" and EdgeHandler.get_count == 1:
            return self._send({"error": "rate"}, code=429, retry_after=1, cloudflare=True)
        if mode in ("cf1010", "cf1010_json"):
            return self._blocked_1010()
        if mode == "e401":
            return self._send(None, code=401, raw=GROQ_401_JSON.encode(), cloudflare=True)
        if mode == "e403_other":
            return self._send(None, code=403, raw=GROQ_403_OTHER_JSON.encode(),
                              cloudflare=True)
        if mode == "r429":
            return self._send({"error": "rate"}, code=429, retry_after=1, cloudflare=True)
        if mode == "e500":
            return self._send({"error": "boom"}, code=500, cloudflare=True)
        if mode == "e409html":
            return self._send(None, code=409, raw=EDGE_409_HTML.encode(),
                              ctype="text/html; charset=UTF-8", cloudflare=True)
        if mode == "e409html_once" and EdgeHandler.get_count == 1:
            return self._send(None, code=409, raw=EDGE_409_HTML.encode(),
                              ctype="text/html; charset=UTF-8", cloudflare=True)
        if mode == "html200":
            return self._send(None, code=200, raw=EDGE_409_HTML.encode(),
                              ctype="text/html; charset=UTF-8", cloudflare=True)
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._send(None, code=401, raw=GROQ_401_JSON.encode())
        return self._send({"object": "list", "data": [{"id": m} for m in SERVED_MODELS]})

    def do_POST(self):  # noqa: N802 — stdlib handler name
        raw = self._read_body()
        EdgeHandler.post_count += 1
        if self._ua_blocked():
            return self._blocked_1010()
        if self.path != "/openai/v1/chat/completions":
            return self._send({"error": "not found"}, code=404)
        mode = EdgeHandler.mode
        if mode in ("cf1010", "cf1010_json"):
            return self._blocked_1010()
        if mode == "timeout":
            # NOT time.sleep: the tests patch time.sleep globally in-process.
            import select
            select.select([], [], [], 0.6)   # longer than the 0.2 s client timeout
            return self._send({"error": "late"}, code=200)
        if mode == "e401":
            return self._send(None, code=401, raw=GROQ_401_JSON.encode(), cloudflare=True)
        if mode == "e403_other":
            return self._send(None, code=403, raw=GROQ_403_OTHER_JSON.encode(),
                              cloudflare=True)
        if mode == "e500":
            return self._send({"error": "boom"}, code=500, cloudflare=True)
        if mode == "r429":
            return self._send({"error": "rate"}, code=429, retry_after=1, cloudflare=True)
        if mode == "r429_once" and EdgeHandler.post_count == 1:
            return self._send({"error": "rate"}, code=429, retry_after=1, cloudflare=True)
        if mode == "e409html":
            return self._send(None, code=409, raw=EDGE_409_HTML.encode(),
                              ctype="text/html; charset=UTF-8", cloudflare=True)
        if mode == "e409html_once" and EdgeHandler.post_count == 1:
            return self._send(None, code=409, raw=EDGE_409_HTML.encode(),
                              ctype="text/html; charset=UTF-8", cloudflare=True)
        if mode == "html200":
            return self._send(None, code=200, raw=EDGE_409_HTML.encode(),
                              ctype="text/html; charset=UTF-8", cloudflare=True)
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._send(None, code=401, raw=GROQ_401_JSON.encode())
        is_reviewer = "GroqReviewer" in raw
        if mode == "revision" and is_reviewer and EdgeHandler.post_count == 2:
            content = REJECTING_REVIEWER_JSON
        else:
            content = VALID_REVIEWER_JSON if is_reviewer else VALID_PRODUCER_JSON
        return self._send({"id": "chatcmpl-test", "object": "chat.completion",
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": content}}]})

    def log_message(self, *args):            # silence the stub server
        pass


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """A client that timed out leaves a broken pipe — never noisy."""
        pass


class EdgeServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), EdgeHandler)
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
        EdgeHandler.mode = "ok"
        EdgeHandler.edge_ua_filter = True
        EdgeHandler.recorded = []
        EdgeHandler.post_count = 0
        EdgeHandler.get_count = 0

    # ---- assertions over what the edge actually saw
    def by_path(self, path, method=None):
        return [r for r in EdgeHandler.recorded
                if r["path"] == path and (method is None or r["method"] == method)]

    def assert_project_ua(self, req, label):
        self.assertEqual(req["user_agent"], PROJECT_UA,
                         f"{label}: wrong User-Agent {req['user_agent']!r}")
        self.assertTrue(groq_http.is_acceptable_user_agent(req["user_agent"]), label)
        self.assertFalse(req["user_agent"].startswith("Python-urllib/"), label)
        self.assertFalse(req["user_agent"].startswith("python-requests/"), label)

    def assert_no_secret_anywhere(self, *texts):
        for text in texts:
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn(f"Bearer {FAKE_KEY}", text)
            self.assertNotIn(FAKE_KEY[:BAD_KEY_FRAGMENTS_LEN], text)
            self.assertNotIn(FAKE_KEY[-BAD_KEY_FRAGMENTS_LEN:], text)


class ClientSignatureTests(EdgeServerMixin, unittest.TestCase):
    """Stage 2/6: the explicit project User-Agent on every Groq call site."""

    def test_central_client_builds_the_required_headers(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            get_h = groq_http.build_headers(with_auth=True, with_body=False)
            post_h = groq_http.build_headers(with_auth=True, with_body=True)
            anon_h = groq_http.build_headers(with_auth=False, with_body=False)
        self.assertEqual(get_h["User-Agent"], PROJECT_UA)
        self.assertEqual(get_h["Accept"], "application/json")
        self.assertEqual(get_h["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertNotIn("Content-Type", get_h)
        self.assertEqual(post_h["Content-Type"], "application/json")
        self.assertEqual(post_h["User-Agent"], PROJECT_UA)
        self.assertEqual(post_h["Accept"], "application/json")
        self.assertNotIn("Authorization", anon_h)
        self.assertEqual(anon_h["User-Agent"], PROJECT_UA)

    def test_models_get_sends_project_ua_accept_and_credential(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            ids, meta = llm_provider.discover_models()
        self.assertEqual(ids, SERVED_MODELS)
        self.assertFalse(meta["mock"])
        gets = self.by_path("/openai/v1/models", "GET")
        self.assertEqual(len(gets), 1)
        self.assert_project_ua(gets[0], "GET /models")
        self.assertEqual(gets[0]["accept"], "application/json")
        self.assertTrue(gets[0]["has_authorization"])
        self.assertTrue(gets[0]["authorization"].startswith("Bearer "))

    def test_producer_post_sends_project_ua_and_content_type(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            discovered = llm_provider.discover_models()[0]
            out, raw = llm_provider.GroqProducer().produce(
                groq_check.build_evidence_packet(), _discovered=discovered)
        self.assertFalse(raw["mock"])
        self.assertEqual(out["metacognition_concept"], "automation bias")
        posts = self.by_path("/openai/v1/chat/completions", "POST")
        self.assertEqual(len(posts), 1)
        self.assert_project_ua(posts[0], "Producer POST")
        self.assertEqual(posts[0]["content_type"], "application/json")
        self.assertEqual(posts[0]["accept"], "application/json")
        self.assertIn("GroqProducer", posts[0]["body"])
        self.assertTrue(posts[0]["has_authorization"])

    def test_reviewer_post_sends_project_ua(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            discovered = llm_provider.discover_models()[0]
            packet = groq_check.build_evidence_packet()
            prod_out, _ = llm_provider.GroqProducer().produce(packet, _discovered=discovered)
            review, raw = llm_provider.GroqReviewer().review(
                prod_out, packet, _discovered=discovered,
                producer_model="openai/gpt-oss-120b")
        self.assertTrue(review["approved"])
        self.assertFalse(raw["mock"])
        posts = self.by_path("/openai/v1/chat/completions", "POST")
        self.assertEqual(len(posts), 2)
        self.assert_project_ua(posts[1], "Reviewer POST")
        self.assertEqual(posts[1]["content_type"], "application/json")
        self.assertIn("GroqReviewer", posts[1]["body"])

    def test_revision_post_sends_project_ua_and_the_reviewer_changes(self):
        """The daily path: Producer -> Reviewer rejects -> ONE revision -> approve."""
        EdgeHandler.mode = "revision"
        tmp = tempfile.mkdtemp(prefix="revision_")
        try:
            cal = next(c for c in common.calendar()["episodes"] if c["id"] == 2)
            topic = {"content_date": "2026-09-16", "content_id": "reel-2026-09-16",
                     "title": cal["title"],
                     "normalized_topic": common.normalize_title(cal["title"]),
                     "pillar": cal.get("pillar", "AI_JUDGMENT"),
                     "technology_angle": "automation bias in AI assistants",
                     "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
                     "evidence_mode": "calendar", "calendar": cal}
            topic_path = os.path.join(tmp, "topic.json")
            common.save_json(topic_path, topic)
            out_dir = os.path.join(tmp, "ep")
            with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                       GROQ_BASE_URL=self.base), clear=True):
                with mock.patch.object(sys, "argv", ["content_producer.py", "--topic",
                                                     topic_path, "--out", out_dir,
                                                     "--variant", "0"]):
                    cp.main()
            script = common.load_json(os.path.join(out_dir, "script.json"))
            self.assertEqual(script["meta"]["generation_mode"], "groq")
            posts = self.by_path("/openai/v1/chat/completions", "POST")
            self.assertEqual(len(posts), 4,
                             "producer, reviewer, revision, final reviewer")
            revisions = [p for p in posts if "MARKER-REVISION-REQUEST" in p["body"]]
            self.assertEqual(len(revisions), 1, "revision request must carry the changes")
            for i, req in enumerate(posts):
                self.assert_project_ua(req, f"chat POST #{i + 1}")
                self.assertEqual(req["content_type"], "application/json")
                self.assertTrue(req["has_authorization"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unauthenticated_probe_still_sends_project_ua_and_no_credential(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            reachable, status, detail = llm_provider.https_probe()
        self.assertTrue(reachable)
        self.assertEqual(status, 401)      # reached the API, sent no credential
        self.assertIn("HTTPS probe: OK", detail)
        probes = self.by_path("/openai/v1/models", "GET")
        self.assertEqual(len(probes), 1)
        self.assert_project_ua(probes[0], "HTTPS probe")
        self.assertFalse(probes[0]["has_authorization"],
                         "the probe must never send the credential")

    def test_edge_ua_filter_reproduces_403_1010_without_the_project_ua(self):
        """Root-cause reproduction: the SAME stub, raw urllib, no project UA."""
        url = self.base + "/models"
        req = urllib.request.Request(url, method="GET",
                                     headers={"Authorization": f"Bearer {FAKE_KEY}"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=20)
        err = ctx.exception
        body = err.read().decode("utf-8", "replace")
        self.assertEqual(err.code, 403)
        self.assertIn("1010", body)
        self.assertIn("banned your access based on your browser", body.lower())
        self.assertEqual(err.headers.get("Server"), "cloudflare")
        # ... and the very next request with the project UA reaches the API.
        req2 = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {FAKE_KEY}",
            "User-Agent": PROJECT_UA,
            "Accept": "application/json"})
        with urllib.request.urlopen(req2, timeout=20) as ok:
            self.assertEqual(ok.status, 200)
            self.assertEqual([m["id"] for m in json.load(ok)["data"]], SERVED_MODELS)

    def test_blocked_default_user_agent_is_refused_before_any_socket(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base,
                                                   GROQ_USER_AGENT="Python-urllib/3.11"),
                             clear=True):
            with self.assertRaises(groq_http.GroqAPIError) as ctx:
                groq_http.request("GET", self.base + "/models")
        self.assertEqual(ctx.exception.category, groq_http.CLOUDFLARE_CLIENT_BLOCKED)
        self.assertEqual(EdgeHandler.recorded, [],
                         "a blocked client signature must never be sent")
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_no_new_third_party_http_dependency(self):
        with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
            reqs = f.read().lower()
        for lib in ("requests", "httpx", "groq", "aiohttp", "urllib3"):
            self.assertNotRegex(reqs, rf"(^|\n)\s*{lib}\b",
                                f"{lib} must not be added — stdlib urllib is enough")
        for rel in ("llm_provider.py", "groq_http.py", "groq_check.py"):
            with open(os.path.join(ROOT, "build", rel), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("import requests", src, rel)
            self.assertNotIn("from groq import", src, rel)
            self.assertNotIn("import httpx", src, rel)


class CloudflareBlockTests(EdgeServerMixin, unittest.TestCase):
    """Stage 3/6: HTTP 403 + code 1010 -> cloudflare-client-blocked."""

    def test_403_1010_html_is_classified_and_not_retried(self):
        EdgeHandler.mode = "cf1010"
        EdgeHandler.edge_ua_filter = False     # block regardless of UA
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with self.assertRaises(groq_http.GroqAPIError) as ctx:
                llm_provider.discover_models()
        err = ctx.exception
        self.assertEqual(err.category, groq_http.CLOUDFLARE_CLIENT_BLOCKED)
        self.assertEqual(err.http_status, 403)
        self.assertEqual(err.cf_code, 1010)
        self.assertFalse(err.retryable)
        self.assertEqual(len(self.by_path("/openai/v1/models")), 1,
                         "a Cloudflare client block must NOT be retried")
        self.assertIn("HTTP 403", str(err))
        self.assertIn("cloudflare-client-blocked", str(err))
        self.assertNotIn(FAKE_KEY, str(err))

    def test_403_1010_json_envelope_is_classified_the_same(self):
        EdgeHandler.mode = "cf1010_json"
        EdgeHandler.edge_ua_filter = False
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with self.assertRaises(groq_http.GroqAPIError) as ctx:
                llm_provider.discover_models()
        self.assertEqual(ctx.exception.category, groq_http.CLOUDFLARE_CLIENT_BLOCKED)
        self.assertEqual(ctx.exception.cf_code, 1010)
        self.assertEqual(len(self.by_path("/openai/v1/models")), 1)

    def test_403_1010_on_chat_completion_is_not_retried(self):
        EdgeHandler.mode = "ok"
        EdgeHandler.edge_ua_filter = True
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            discovered = llm_provider.discover_models()[0]
            EdgeHandler.mode = "cf1010"
            EdgeHandler.recorded = []
            with self.assertRaises(groq_http.GroqAPIError) as ctx:
                llm_provider.GroqProducer().produce(groq_check.build_evidence_packet(),
                                                    _discovered=discovered)
        self.assertEqual(ctx.exception.category, groq_http.CLOUDFLARE_CLIENT_BLOCKED)
        self.assertEqual(len(self.by_path("/openai/v1/chat/completions")), 1)

    def test_check_cli_goes_red_with_a_safe_header_hint_and_no_mock(self):
        EdgeHandler.mode = "cf1010"
        EdgeHandler.edge_ua_filter = False
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.returncode, groq_check.EXIT_CLOUDFLARE)
        combined = r.stdout + r.stderr
        self.assertIn("cloudflare-client-blocked", combined)
        self.assertIn("User-Agent", combined)
        self.assertIn(PROJECT_UA, combined)
        self.assertIn("do NOT rotate GROQ_API_KEY", combined)
        self.assertIn("Mock used: false", combined)
        self.assertNotIn("Groq connection: OK", combined)
        self.assertNotIn("MOCK MODE", combined)
        self.assertNotIn("static-fallback", combined)
        self.assert_no_secret_anywhere(combined)
        self.assertNotIn("Authorization", combined)

    def test_classify_maps_every_documented_category(self):
        cases = [
            (403, CLOUDFLARE_1010_HTML, {"Server": "cloudflare"},
             groq_http.CLOUDFLARE_CLIENT_BLOCKED, 1010),
            (403, CLOUDFLARE_1010_JSON, {"CF-RAY": "x"},
             groq_http.CLOUDFLARE_CLIENT_BLOCKED, 1010),
            (401, GROQ_401_JSON, {"Server": "cloudflare"},
             groq_http.INVALID_OR_MISSING_API_KEY, None),
            (403, GROQ_403_OTHER_JSON, {"Server": "cloudflare"},
             groq_http.PERMISSION_OR_ACCOUNT_RESTRICTION, None),
            (429, '{"error":"rate"}', {"Retry-After": "3"}, groq_http.RATE_LIMITED, None),
            (500, '{"error":"boom"}', {}, groq_http.UPSTREAM_ERROR, None),
            (503, '{"error":"overloaded"}', {}, groq_http.UPSTREAM_ERROR, None),
            (400, '{"error":"bad"}', {}, groq_http.INVALID_RESPONSE, None),
        ]
        for status, body, headers, category, cf_code in cases:
            got_cat, got_cf = groq_http.classify(status, body, headers)
            self.assertEqual(got_cat, category, f"status={status} body={body[:40]}")
            self.assertEqual(got_cf, cf_code, f"status={status}")

    def test_401_is_invalid_or_missing_api_key_and_not_retried(self):
        EdgeHandler.mode = "e401"
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with self.assertRaises(groq_http.GroqAPIError) as ctx:
                llm_provider.discover_models()
        self.assertEqual(ctx.exception.category, groq_http.INVALID_OR_MISSING_API_KEY)
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertIn("authentication failed", str(ctx.exception))
        self.assertEqual(len(self.by_path("/openai/v1/models")), 1)
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("invalid-or-missing-api-key", r.stdout)
        self.assertIn("Mock used: false", r.stdout)

    def test_403_other_is_permission_or_account_restriction(self):
        EdgeHandler.mode = "e403_other"
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with self.assertRaises(groq_http.GroqAPIError) as ctx:
                llm_provider.discover_models()
        self.assertEqual(ctx.exception.category,
                         groq_http.PERMISSION_OR_ACCOUNT_RESTRICTION)
        self.assertEqual(len(self.by_path("/openai/v1/models")), 1)
        self.assertNotIn("cloudflare-client-blocked", str(ctx.exception))

    def test_429_honors_retry_after_and_is_bounded(self):
        EdgeHandler.mode = "r429_once"
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            ids, meta = llm_provider.discover_models()
        self.assertEqual(ids, SERVED_MODELS)
        self.assertEqual(meta["attempt"], 2)
        self.assertEqual(len(self.by_path("/openai/v1/models")), 2)

        EdgeHandler.mode = "r429"
        EdgeHandler.recorded = []
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with mock.patch("time.sleep") as slept:
                with self.assertRaises(groq_http.GroqAPIError) as ctx:
                    llm_provider.discover_models()
        self.assertEqual(ctx.exception.category, groq_http.RATE_LIMITED)
        self.assertIn("quota 429 exhausted", str(ctx.exception))
        self.assertEqual(len(self.by_path("/openai/v1/models")), 3, "1 + 2 retries max")
        self.assertEqual([c.args[0] for c in slept.call_args_list], [1, 1],
                         "Retry-After must be honored")

    def test_retry_after_cap_is_applied(self):
        self.assertEqual(groq_http.parse_retry_after({"Retry-After": "900"}), 900)
        self.assertEqual(min(900, groq_http.RETRY_AFTER_CAP_SECONDS),
                         groq_http.RETRY_AFTER_CAP_SECONDS)
        self.assertEqual(groq_http.parse_retry_after({}), 0)
        self.assertEqual(groq_http.parse_retry_after(None), 0)
        self.assertEqual(groq_http.parse_retry_after({"Retry-After": "garbage"}), 0)

    def test_5xx_is_bounded_then_red(self):
        EdgeHandler.mode = "e500"
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with mock.patch("time.sleep"):
                with self.assertRaises(groq_http.GroqAPIError) as ctx:
                    llm_provider.discover_models()
        self.assertEqual(ctx.exception.category, groq_http.UPSTREAM_ERROR)
        self.assertEqual(len(self.by_path("/openai/v1/models")), groq_http.MAX_ATTEMPTS)
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Mock used: false", r.stdout)
        self.assertNotIn("Groq connection: OK", r.stdout)

    def test_timeout_is_bounded(self):
        EdgeHandler.mode = "timeout"
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with mock.patch("time.sleep"):
                with self.assertRaises(groq_http.GroqAPIError) as ctx:
                    groq_http.request("GET", self.base + "/models", timeout=0.2)
        self.assertIn(ctx.exception.category,
                      (groq_http.NETWORK_UNREACHABLE, groq_http.UPSTREAM_ERROR))
        self.assertEqual(ctx.exception.attempts, groq_http.MAX_ATTEMPTS)
        self.assertEqual(len(self.by_path("/openai/v1/models")), groq_http.MAX_ATTEMPTS)
        self.assertLessEqual(groq_http.REQUEST_TIMEOUT, 60)
        self.assertLessEqual(groq_http.MODELS_TIMEOUT, 20)


class SecretLeakageTests(EdgeServerMixin, unittest.TestCase):
    """Stage 4/6: the credential can never reach a log, diff or artifact."""

    def test_authorization_value_never_reaches_stdout_stderr_or_exception(self):
        EdgeHandler.mode = "cf1010"
        EdgeHandler.edge_ua_filter = False
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        combined = r.stdout + r.stderr
        self.assert_no_secret_anywhere(combined)
        self.assertFalse(EdgeHandler.recorded[0]["has_authorization"],
                         "the first call is the unauthenticated probe")
        authed = [r for r in EdgeHandler.recorded if r["has_authorization"]]
        self.assertTrue(authed, "the request itself must be authenticated")
        self.assertEqual(authed[0]["authorization"], f"Bearer {FAKE_KEY}")

        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with self.assertRaises(groq_http.GroqAPIError) as ctx:
                llm_provider.discover_models()
        self.assert_no_secret_anywhere(str(ctx.exception))
        # A traceback of the same failure must also stay clean.
        import traceback
        tb = "".join(traceback.format_exception(type(ctx.exception), ctx.exception,
                                                ctx.exception.__traceback__))
        self.assert_no_secret_anywhere(tb)

    def test_error_body_is_scrubbed_and_truncated(self):
        leaking = f"upstream said {FAKE_KEY} while handling Bearer {FAKE_KEY}"
        err = urllib.error.HTTPError("https://api.groq.com/openai/v1/models", 500, "err",
                                     {"Server": "cloudflare"}, io.BytesIO(leaking.encode()))
        raw = groq_http._read_body(err)
        self.assertIn(FAKE_KEY, raw)                       # the stub body really leaks
        safe = groq_http.safe_body(raw)
        self.assertNotIn(FAKE_KEY, safe)
        self.assertLessEqual(len(safe), groq_http.ERROR_BODY_LOG_LIMIT)
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            self.assertNotIn(FAKE_KEY, common.scrub_secrets(leaking))

    def test_scrubber_removes_full_value_and_identifiable_fragments(self):
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY), clear=True):
            self.assertEqual(common.scrub_secrets(f"key={FAKE_KEY}"), "key=[redacted:GROQ_API_KEY]")
            for fragment in (FAKE_KEY[:16], FAKE_KEY[-16:], FAKE_KEY[10:26], FAKE_KEY[20:36]):
                out = common.scrub_secrets(f"noise {fragment} noise")
                self.assertNotIn(fragment, out, f"fragment survived: {fragment!r}")
            self.assertIn("[redacted:", common.scrub_secrets(FAKE_KEY[10:30]))
            self.assertNotIn(FAKE_KEY, common.scrub_secrets(f"Bearer {FAKE_KEY}"))
            self.assertEqual(common.scrub_secrets("gsk_abcdefghijklmnopqrstuvwxyz"),
                             "[redacted-key]")
            # the presence report is boolean-only
            self.assertNotIn(FAKE_KEY, "GROQ_API_KEY present: true")
            self.assertEqual(len(str(bool(FAKE_KEY))), 4)    # "True" — nothing else

    def test_scrubber_reports_presence_without_length_prefix_or_suffix(self):
        src = open(CHECK_SCRIPT, encoding="utf-8").read()
        self.assertIn('f"GROQ_API_KEY present: {str(key_present).lower()}"', src)
        for bad in ("len(key", "len(llm_provider.get_groq_key", "key[:", "key[-",
                    "startswith(\"gsk"):
            self.assertNotIn(bad, src, bad)

    def test_no_github_annotation_carries_the_secret(self):
        wf = open(CHECK_WF, encoding="utf-8").read()
        code = "\n".join(l for l in wf.splitlines() if not l.strip().startswith("#"))
        self.assertIn("secrets.GROQ_API_KEY", code)
        self.assertNotIn("echo $GROQ_API_KEY", code)
        self.assertNotIn("echo ${GROQ_API_KEY", code)
        self.assertNotIn("::error::$GROQ_API_KEY", code)
        self.assertNotIn("GITHUB_TOKEN", code)
        self.assertNotIn("BUFFER_TOKEN", code)


class MockGatingTests(EdgeServerMixin, unittest.TestCase):
    """Stage 5/6: real by default, mock only on an explicit flag, never in cron."""

    def test_connection_check_never_falls_back_to_mock_on_a_real_failure(self):
        for mode in ("cf1010", "cf1010_json", "e401", "e403_other", "e500", "r429",
                     "e409html", "html200"):
            EdgeHandler.mode = mode
            EdgeHandler.edge_ua_filter = False
            r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
            combined = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0, f"mode={mode} must be red")
            self.assertIn("Mock used: false", combined, f"mode={mode}")
            self.assertNotIn("MOCK MODE", combined, f"mode={mode}")
            self.assertNotIn("Groq connection: OK", combined, f"mode={mode}")
            self.assertNotIn("Mock used: true", combined, f"mode={mode}")

    def test_real_success_prints_exactly_the_documented_summary(self):
        EdgeHandler.mode = "ok"
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        start = max(i for i, l in enumerate(lines) if l == "Groq connection: OK")
        end = max(i for i, l in enumerate(lines) if l == "Mock used: false")
        block = lines[start:end + 1]
        self.assertEqual(block, [
            "Groq connection: OK",
            f"Endpoint hostname: 127.0.0.1",
            "Models discovered: 3",
            "Producer model: openai/gpt-oss-120b",
            "Producer structured output: OK",
            "Reviewer model: llama-3.3-70b-versatile",
            "Reviewer structured output: OK",
            "Reviewer approved: true",
            "Mock used: false",
        ])
        self.assertEqual(len(block), len(groq_check.SUCCESS_SUMMARY_ORDER))
        self.assertNotIn("MOCK MODE", r.stdout)
        self.assert_no_secret_anywhere(r.stdout + r.stderr)
        # all four Groq calls that produced this summary used the project UA
        for req in EdgeHandler.recorded:
            self.assert_project_ua(req, f"{req['method']} {req['path']}")

    def test_mock_requires_an_explicit_flag_and_banners_loudly(self):
        r = run_check("--mock", env=clean_env(GROQ_BASE_URL=self.base))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("MOCK MODE", r.stdout)
        self.assertIn("Mock used: true", r.stdout)
        self.assertIn("NOT a real", r.stdout)
        self.assertIn("Groq connection: NOT CHECKED", r.stdout)
        self.assertNotIn("Groq connection: OK", r.stdout)
        self.assertEqual(EdgeHandler.recorded, [], "mock must not touch the network")

        with mock.patch.dict(os.environ, clean_env(GROQ_BASE_URL=self.base, MOCK_GROQ="1"),
                             clear=True):
            self.assertTrue(llm_provider.is_mock_enabled())
        for value in ("", "0", "false", "True", "yes"):
            with mock.patch.dict(os.environ, clean_env(GROQ_BASE_URL=self.base,
                                                       MOCK_GROQ=value), clear=True):
                self.assertFalse(llm_provider.is_mock_enabled(), value)

    def test_scheduled_production_run_cannot_enable_mock(self):
        with mock.patch.dict(os.environ, clean_env(GITHUB_ACTIONS="true",
                                                   GITHUB_EVENT_NAME="schedule",
                                                   MOCK_GROQ="1"), clear=True):
            self.assertFalse(groq_check.mock_allowed_in_production_ci())
            r = run_check(env=clean_env(GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="schedule",
                                        MOCK_GROQ="1", GROQ_API_KEY=FAKE_KEY,
                                        GROQ_BASE_URL=self.base))
        self.assertNotIn("Mock used: true", r.stdout)
        self.assertNotIn("MOCK MODE:", r.stdout)
        self.assertIn("MOCK MODE refused", r.stdout)
        self.assertIn("Mock used: false", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Groq connection: OK", r.stdout)
        with mock.patch.dict(os.environ, clean_env(GITHUB_ACTIONS="true",
                                                   GITHUB_EVENT_NAME="workflow_dispatch",
                                                   MOCK_GROQ="1"), clear=True):
            self.assertTrue(groq_check.mock_allowed_in_production_ci())
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "MOCK_GROQ": "1"}):
            with self.assertRaises(pl.Stage):
                pl.assert_no_mock_in_ci()

    def test_workflow_is_dispatch_only_five_minutes_and_read_only(self):
        import yaml
        with open(CHECK_WF, encoding="utf-8") as f:
            wf = yaml.safe_load(f)
        triggers = wf.get("on", wf.get(True))
        self.assertEqual(list(triggers), ["workflow_dispatch"])
        self.assertIs(triggers["workflow_dispatch"]["inputs"]["mock"]["default"], False)
        self.assertEqual(wf.get("permissions"), {})
        job = wf["jobs"]["check"]
        self.assertEqual(job["timeout-minutes"], 5)
        self.assertEqual(job["permissions"], {"contents": "read"})
        holders = [s.get("name") for s in job["steps"]
                   if "GROQ_API_KEY" in str(s.get("env", {}) or {})]
        self.assertEqual(len(holders), 1, holders)
        self.assertNotIn("BUFFER_TOKEN", open(CHECK_WF, encoding="utf-8").read())

    def test_connection_check_has_no_buffer_or_createpost_code(self):
        wf = open(CHECK_WF, encoding="utf-8").read()
        code = "\n".join(l for l in wf.splitlines() if not l.strip().startswith("#"))
        for bad in ("createPost", "buffer_publish", "BUFFER_TOKEN", "AUTO_PUBLISH",
                    "instagram", "insta_publish", "graph.facebook", "daily-trend-draft"):
            self.assertNotIn(bad, code, bad)
        for rel in ("build/groq_check.py", "build/groq_http.py", "build/llm_provider.py"):
            src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            for bad in ("createPost", "buffer_publish", "import buffer", "insta_publish",
                        "BUFFER_TOKEN"):
                self.assertNotIn(bad, src, f"{rel}: {bad}")
        for mod in (common, groq_http, llm_provider, groq_check):
            self.assertNotIn("buffer", mod.__name__)
            self.assertFalse(any("buffer" in n for n in dir(mod)), mod.__name__)


class DailyPipelineHonestyTests(EdgeServerMixin, unittest.TestCase):
    """Stage 6: a Groq failure records static-fallback and never groq."""

    def _topic(self, tmp):
        cal = next(c for c in common.calendar()["episodes"] if c["id"] == 2)
        topic = {"content_date": "2026-09-16", "content_id": "reel-2026-09-16",
                 "title": cal["title"],
                 "normalized_topic": common.normalize_title(cal["title"]),
                 "pillar": cal.get("pillar", "AI_JUDGMENT"),
                 "technology_angle": "automation bias in AI assistants",
                 "discovery_source": {"name": "content-calendar", "url": "", "tier": "cal"},
                 "evidence_mode": "calendar", "calendar": cal}
        path = os.path.join(tmp, "topic.json")
        common.save_json(path, topic)
        return path

    def _run_producer(self, tmp):
        out_dir = os.path.join(tmp, "ep")
        with mock.patch.object(sys, "argv", ["content_producer.py", "--topic",
                                             self._topic(tmp), "--out", out_dir,
                                             "--variant", "0"]):
            cp.main()
        return (common.load_json(os.path.join(out_dir, "script.json")),
                common.load_json(os.path.join(out_dir, "producer_report.json"), {}))

    def test_cloudflare_block_in_daily_path_records_static_fallback_only(self):
        EdgeHandler.mode = "cf1010"
        EdgeHandler.edge_ua_filter = False
        tmp = tempfile.mkdtemp(prefix="cf_daily_")
        try:
            with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                       GROQ_BASE_URL=self.base), clear=True):
                script, report = self._run_producer(tmp)
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
            self.assertEqual(script["meta"]["language"], "en")
            self.assertTrue(script["chunks"])
            self.assertNotEqual(report.get("mode"), "groq")
            self.assertEqual(report.get("error_category"),
                             groq_http.CLOUDFLARE_CLIENT_BLOCKED)
            self.assert_no_secret_anywhere(json.dumps(report), json.dumps(script))
            self.assertEqual(len(self.by_path("/openai/v1/models")), 1,
                             "a Cloudflare client block must not be retried in the daily path")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_key_in_daily_path_records_static_fallback(self):
        tmp = tempfile.mkdtemp(prefix="nokey_daily_")
        try:
            with mock.patch.dict(os.environ, clean_env(GROQ_BASE_URL=self.base), clear=True):
                script, report = self._run_producer(tmp)
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
            self.assertNotEqual(report.get("mode"), "groq")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reviewer_rejection_after_revision_never_claims_groq(self):
        EdgeHandler.mode = "revision"
        tmp = tempfile.mkdtemp(prefix="rej_daily_")
        try:
            with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                       GROQ_BASE_URL=self.base), clear=True):
                # force BOTH reviewer calls to reject
                with mock.patch.object(EdgeHandler, "mode", "revision"), \
                        mock.patch.object(llm_provider.GroqReviewer, "review",
                                          side_effect=[
                                              (json.loads(REJECTING_REVIEWER_JSON), {"mock": False}),
                                              (json.loads(REJECTING_REVIEWER_JSON), {"mock": False})]):
                    script, report = self._run_producer(tmp)
            self.assertEqual(script["meta"]["generation_mode"], "static-fallback")
            self.assertNotEqual(report.get("mode"), "groq")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_genuine_success_still_records_groq(self):
        EdgeHandler.mode = "ok"
        tmp = tempfile.mkdtemp(prefix="ok_daily_")
        try:
            with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                       GROQ_BASE_URL=self.base), clear=True):
                script, report = self._run_producer(tmp)
            self.assertEqual(script["meta"]["generation_mode"], "groq")
            self.assertEqual(report.get("mode"), "groq")
            self.assertEqual(report["selection"]["producer_model"], "openai/gpt-oss-120b")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class EdgeHtmlResponseTests(EdgeServerMixin, unittest.TestCase):
    """A real api.groq.com failure mode captured AFTER the 1010 fix.

    Run 35042629998 (session branch, real key, no mock) went red with:

        reviewer: Groq HTTP 409: invalid-response host=api.groq.com attempt=1
                  detail=<!DOCTYPE html> / <!--[if lt IE 7]> ...

    The body was the CDN/edge error PAGE, not a Groq answer, and 409 was not in
    the retryable set, so the very first transient blip failed the check. An
    HTML page means the request never got an application-level answer: it is
    transient infrastructure noise, so it gets the same bounded retry as a 5xx
    and then fails RED (never a mock, never a guessed classification).
    """

    def test_classify_maps_html_pages_to_edge_html_response(self):
        cases = [
            (409, EDGE_409_HTML, {"Server": "cloudflare"}, groq_http.EDGE_HTML_RESPONSE),
            (400, EDGE_409_HTML, {}, groq_http.EDGE_HTML_RESPONSE),
            (200, EDGE_409_HTML, {"CF-RAY": "x"}, groq_http.EDGE_HTML_RESPONSE),
            (404, "<html><body>nope</body></html>", {}, groq_http.EDGE_HTML_RESPONSE),
        ]
        for status, body, headers, expected in cases:
            self.assertEqual(groq_http.classify(status, body, headers)[0], expected,
                             f"status={status}")

    def test_credential_and_block_statuses_keep_priority_over_html(self):
        """A 403/1010 page must stay no-retry; 401/429/5xx keep their category."""
        self.assertEqual(groq_http.classify(403, CLOUDFLARE_1010_HTML,
                                            {"Server": "cloudflare"}),
                         (groq_http.CLOUDFLARE_CLIENT_BLOCKED, 1010))
        self.assertEqual(groq_http.classify(401, EDGE_409_HTML, {})[0],
                         groq_http.INVALID_OR_MISSING_API_KEY)
        self.assertEqual(groq_http.classify(403, EDGE_409_HTML, {})[0],
                         groq_http.PERMISSION_OR_ACCOUNT_RESTRICTION)
        self.assertEqual(groq_http.classify(429, EDGE_409_HTML, {})[0],
                         groq_http.RATE_LIMITED)
        self.assertEqual(groq_http.classify(500, EDGE_409_HTML, {})[0],
                         groq_http.UPSTREAM_ERROR)
        self.assertEqual(groq_http.classify(400, '{"error":"bad"}', {})[0],
                         groq_http.INVALID_RESPONSE)

    def test_is_html_body_detection(self):
        for html in (EDGE_409_HTML, "<html><body>x</body></html>",
                     "  <!DOCTYPE html><head></head></html>"):
            self.assertTrue(groq_http.is_html_body(html), html[:30])
        for other in ('{"error":"boom"}', "", "   ", "[1,2,3]", "plain text"):
            self.assertFalse(groq_http.is_html_body(other), other[:30])

    def test_409_html_on_post_is_bounded_then_red(self):
        """The exact captured failure: reviewer POST, 409 + HTML page."""
        EdgeHandler.mode = "e409html"
        EdgeHandler.edge_ua_filter = False
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with mock.patch("time.sleep"):
                with self.assertRaises(groq_http.GroqAPIError) as ctx:
                    llm_provider.call_groq_chat("GroqReviewer prompt", SERVED_MODELS[1])
        self.assertEqual(ctx.exception.category, groq_http.EDGE_HTML_RESPONSE)
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.attempts, groq_http.MAX_ATTEMPTS)
        posts = self.by_path("/openai/v1/chat/completions", "POST")
        self.assertEqual(len(posts), groq_http.MAX_ATTEMPTS,
                         "bounded retry, exactly MAX_ATTEMPTS requests")
        # Every retry still carries the explicit project signature.
        for req in posts:
            self.assert_project_ua(req, "retry")
            self.assertEqual(req["accept"], "application/json")
            self.assertEqual(req["content_type"], "application/json")
            self.assertTrue(req["has_authorization"])
        # The message summarises the page; it never dumps markup or secrets.
        msg = str(ctx.exception)
        self.assertIn("edge-html-response", msg)
        self.assertIn("body=html-page", msg)
        self.assertIn("cloudflare_edge=true", msg)
        self.assertNotIn("<html", msg)
        self.assertNotIn("<!DOCTYPE", msg)
        self.assertNotIn("no-js", msg)
        self.assert_no_secret_anywhere(msg)

    def test_html_page_on_a_200_is_also_bounded_then_red(self):
        """A 2xx challenge/error page is not a valid envelope either."""
        EdgeHandler.mode = "html200"
        EdgeHandler.edge_ua_filter = False
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with mock.patch("time.sleep"):
                with self.assertRaises(groq_http.GroqAPIError) as ctx:
                    llm_provider.discover_models()
        self.assertEqual(ctx.exception.category, groq_http.EDGE_HTML_RESPONSE)
        self.assertEqual(ctx.exception.http_status, 200)
        self.assertEqual(len(self.by_path("/openai/v1/models")), groq_http.MAX_ATTEMPTS)
        self.assert_no_secret_anywhere(str(ctx.exception))

    def test_transient_html_page_recovers_on_retry_and_stays_real(self):
        """One blip, then a real answer: the retry must recover, not fail."""
        EdgeHandler.mode = "e409html_once"
        EdgeHandler.edge_ua_filter = False
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with mock.patch("time.sleep"):
                models, meta = llm_provider.discover_models()
        self.assertEqual(list(models), SERVED_MODELS)
        self.assertEqual(meta["mock"], False)
        self.assertEqual(meta["attempt"], 2, "recovered on the bounded retry")
        self.assertEqual(len(self.by_path("/openai/v1/models")), 2)

    def test_connection_check_is_red_and_never_mocks_on_html_edge(self):
        EdgeHandler.mode = "e409html"
        EdgeHandler.edge_ua_filter = False
        r = run_check(env=clean_env(GROQ_API_KEY=FAKE_KEY, GROQ_BASE_URL=self.base))
        combined = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Mock used: false", combined)
        self.assertNotIn("Mock used: true", combined)
        self.assertNotIn("MOCK MODE", combined)
        self.assertNotIn("Groq connection: OK", combined)
        self.assertIn("edge-html-response", combined)
        self.assert_no_secret_anywhere(combined)

    def test_daily_pipeline_falls_back_to_static_not_mock(self):
        """The daily path may static-fallback on an upstream/edge failure."""
        EdgeHandler.mode = "e409html"
        EdgeHandler.edge_ua_filter = False
        with mock.patch.dict(os.environ, clean_env(GROQ_API_KEY=FAKE_KEY,
                                                   GROQ_BASE_URL=self.base), clear=True):
            with mock.patch("time.sleep"):
                with self.assertRaises(groq_http.GroqAPIError) as ctx:
                    llm_provider.GroqProducer(model="auto").produce(
                        groq_check.build_evidence_packet(), _discovered=None)
        self.assertEqual(ctx.exception.category, groq_http.EDGE_HTML_RESPONSE)
        self.assert_no_secret_anywhere(str(ctx.exception))


class PublishingInvariantsTests(unittest.TestCase):
    """Stage 6: quarantine preserved, AUTO_PUBLISH_ENABLED untouched, no Buffer."""

    def test_issue14_quarantine_is_preserved(self):
        path = os.path.join(ROOT, "content", "quarantine.json")
        self.assertTrue(os.path.exists(path))
        q = common.load_quarantine()
        self.assertIn("reel-2026-09-15", q.get("quarantined_content_ids", []))
        self.assertIn("2026-09-15", q.get("quarantined_dates", []))
        self.assertTrue(common.is_quarantined("reel-2026-09-15", "2026-09-15"))
        self.assertTrue(common.is_quarantined("reel-2026-09-15", None))
        self.assertTrue(common.is_quarantined(None, "2026-09-15"))
        self.assertFalse(common.is_quarantined("reel-2026-09-16", "2026-09-16"))

    def test_auto_publish_enabled_is_untouched_and_still_exact_true(self):
        with open(DAILY_WF, encoding="utf-8") as f:
            daily = f.read()
        code = "\n".join(l for l in daily.splitlines() if not l.strip().startswith("#"))
        self.assertIn("AUTO_PUBLISH_ENABLED: ${{ vars.AUTO_PUBLISH_ENABLED }}", code)
        lines = [l for l in code.splitlines() if "AUTO_PUBLISH_ENABLED" in l]
        self.assertEqual(len(lines), 1, lines)
        # nothing in this change may write the variable or flip the flag
        mutation_patterns = ("variable set", "variable edit",
                             'environ["AUTO_PUBLISH_ENABLED"] =',
                             "environ['AUTO_PUBLISH_ENABLED'] =",
                             "setdefault(\"AUTO_PUBLISH_ENABLED\", \"true\"")
        for rel in sorted(os.listdir(os.path.join(ROOT, "build"))):
            if not rel.endswith(".py"):
                continue
            with open(os.path.join(ROOT, "build", rel), encoding="utf-8") as f:
                src = f.read()
            found = [pat for pat in mutation_patterns if pat in src]
            self.assertEqual(found, [], f"{rel} must not mutate AUTO_PUBLISH_ENABLED")
        for rel in sorted(os.listdir(os.path.join(ROOT, ".github", "workflows"))):
            with open(os.path.join(ROOT, ".github", "workflows", rel), encoding="utf-8") as f:
                src = f.read()
            found = [pat for pat in ("variable set", "variable edit") if pat in src]
            self.assertEqual(found, [], f"{rel} must not write repo variables")
        with mock.patch.dict(os.environ, {"AUTO_PUBLISH_ENABLED": "true"}):
            self.assertTrue(common.env_flag_exact_true("AUTO_PUBLISH_ENABLED"))
        for value in ("True", "1", "yes", ""):
            with mock.patch.dict(os.environ, {"AUTO_PUBLISH_ENABLED": value}):
                self.assertFalse(common.env_flag_exact_true("AUTO_PUBLISH_ENABLED"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(common.env_flag_exact_true("AUTO_PUBLISH_ENABLED"))

    def test_check_workflow_does_not_run_the_daily_pipeline(self):
        with open(CHECK_WF, encoding="utf-8") as f:
            code = "\n".join(l for l in f.read().splitlines()
                             if not l.strip().startswith("#"))
        for bad in ("daily-trend-draft", "pipeline.py", "gh workflow run",
                    "buffer_publish", "performance_analyst", "retention.py"):
            self.assertNotIn(bad, code, bad)
        self.assertIn("build/groq_check.py", code)

    def test_no_temporary_branch_trigger_left_behind(self):
        names = sorted(os.listdir(os.path.join(ROOT, ".github", "workflows")))
        self.assertEqual(names, ["buffer-connection-check.yml", "daily-trend-draft.yml",
                                 "groq-connection-check.yml", "publish-approved-draft.yml"])
        import yaml
        for name in names:
            with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as f:
                wf = yaml.safe_load(f)
            triggers = wf.get("on", wf.get(True))
            self.assertNotIn("push", triggers or {}, f"{name} must not auto-run on push")
            self.assertNotIn("pull_request", triggers or {}, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
