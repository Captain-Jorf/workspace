"""Unit tests for build/buffer_publish.py — everything mocked, zero real API calls.

Covers: missing token, read-only check, channels-query fallback, caption limit,
non-public video URL, createPost payload shape, firstComment rejection fallback,
idempotency marker, dry-run without --yes, token scrubbing from error messages.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import buffer_publish as bp  # noqa: E402

FAKE_TOKEN = "buffr_SUPER_SECRET_TEST_TOKEN_xyz"
VIDEO_URL = "https://raw.githubusercontent.com/o/r/drafts/2026-09-14/output/auto-2026-09-14.mp4"

ORG_RESP = {"data": {"account": {"id": "a1", "organizations": [
    {"id": "org1", "name": "metacognition-hq"}]}}}
CH_RESP = {"data": {"channels": [
    {"id": "ch1", "name": "metacognition.hq", "service": "instagram",
     "descriptor": "Instagram Account"},
    {"id": "ch2", "name": "Some Page", "service": "facebook", "descriptor": "Facebook Page"}]}}
POST_OK = {"data": {"createPost": {"post": {
    "id": "post123", "status": "pending", "dueAt": "2026-09-14T16:00:00.000Z"}}}}


class FakeResp:
    def __init__(self, payload=None, status=200, headers=None, raw_body=None):
        self._payload = payload
        self.status = status
        self.headers = headers or {"Content-Length": "12345"}
        self._raw = raw_body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        if self._raw is not None:
            return self._raw.encode()
        return json.dumps(self._payload).encode()


class MockBuffer:
    """Routes fake urlopen calls: GraphQL by query content, HEAD by method."""
    def __init__(self, head_status=200, createpost_responses=None,
                 channels_error_first=False, http_error_body=None,
                 channels_resp=None):
        self.head_status = head_status
        self.createpost_responses = list(createpost_responses or [POST_OK])
        self.channels_error_first = channels_error_first
        self.http_error_body = http_error_body
        self.channels_resp = channels_resp
        self.calls = []          # list of (kind, payload-or-url)

    def urlopen(self, req, timeout=None):
        url = req.full_url
        if req.get_method() == "HEAD":
            self.calls.append(("head", url))
            if self.http_error_body is not None:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {},
                                             io.BytesIO(self.http_error_body.encode()))
            if self.head_status != 200:
                raise urllib.error.HTTPError(url, self.head_status, "err", {}, None)
            return FakeResp(status=200)
        payload = json.loads(req.data.decode())
        query = payload.get("query", "")
        # auth header sanity — the fake server demands the bearer token
        assert req.get_header("Authorization") == f"Bearer {FAKE_TOKEN}"
        if self.http_error_body is not None:
            self.calls.append(("graphql", query))
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {},
                                         io.BytesIO(self.http_error_body.encode()))
        if "account" in query and "organizations" in query:
            self.calls.append(("account", query))
            return FakeResp(ORG_RESP)
        if "channels(" in query:
            self.calls.append(("channels", query))
            if self.channels_resp is not None:
                return FakeResp(self.channels_resp)
            if self.channels_error_first and "name" in query and "displayName" not in query:
                return FakeResp({"errors": [{"message": "Cannot query field \"name\" on type \"Channel\"."}]})
            if "displayName" in query:      # fallback variant response
                return FakeResp({"data": {"channels": [
                    {"id": "ch1", "displayName": "metacognition.hq",
                     "service": "instagram", "descriptor": "Instagram Account"}]}})
            return FakeResp(CH_RESP)
        if "createPost" in query:
            self.calls.append(("createPost", payload))
            resp = self.createpost_responses.pop(0) if self.createpost_responses else POST_OK
            if isinstance(resp, Exception):
                raise resp
            return FakeResp(resp)
        raise AssertionError(f"unexpected query: {query[:80]}")

    # helpers -------------------------------------------------------------
    def kinds(self):
        return [k for k, _ in self.calls]

    def createpost_inputs(self):
        return [p["variables"]["input"] for k, p in self.calls if k == "createPost"]


def write_caption(tmpdir, body="Hello caption body.\nSecond line.",
                  tags="#metacognition #trendwatch"):
    path = os.path.join(tmpdir, "caption.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body + "\n\n" + tags + "\n")
    return path


class TokenTests(unittest.TestCase):
    def test_missing_token_exits_2(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BUFFER_TOKEN", None)
            with self.assertRaises(SystemExit) as ctx:
                bp.cmd_check([])
            self.assertEqual(ctx.exception.code, bp.EXIT_NO_TOKEN)

    def test_token_scrubbed_from_http_errors(self):
        m = MockBuffer(http_error_body=f"invalid token {FAKE_TOKEN} rejected")
        with mock.patch.dict(os.environ, {"BUFFER_TOKEN": FAKE_TOKEN}), \
             mock.patch("urllib.request.urlopen", m.urlopen):
            with self.assertRaises(bp.BufferError) as ctx:
                bp.organizations(FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))


class CheckTests(unittest.TestCase):
    def test_check_prints_safe_summary(self):
        m = MockBuffer()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"BUFFER_TOKEN": FAKE_TOKEN}), \
             mock.patch("urllib.request.urlopen", m.urlopen), \
             mock.patch("sys.stdout", out):
            code = bp.cmd_check([])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Buffer connection: OK", text)
        self.assertIn("Instagram channel found: metacognition.hq", text)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("ch1", text)          # ids stay out of the logs
        self.assertNotIn("createPost", " ".join(m.kinds()))   # read-only!

    def test_channels_query_fallback(self):
        m = MockBuffer(channels_error_first=True)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"BUFFER_TOKEN": FAKE_TOKEN}), \
             mock.patch("urllib.request.urlopen", m.urlopen), \
             mock.patch("sys.stdout", out):
            code = bp.cmd_check([])
        self.assertEqual(code, 0)
        self.assertIn("Instagram channel found: metacognition.hq", out.getvalue())
        self.assertEqual(m.kinds().count("channels"), 2)

    def test_no_instagram_channel(self):
        m = MockBuffer(channels_resp={"data": {"channels": [
            {"id": "x", "name": "fb", "service": "facebook", "descriptor": ""}]}})
        with mock.patch.dict(os.environ, {"BUFFER_TOKEN": FAKE_TOKEN}), \
             mock.patch("urllib.request.urlopen", m.urlopen):
            with self.assertRaises(bp.BufferError):
                bp.find_instagram_channel(FAKE_TOKEN)


class PublishGuardTests(unittest.TestCase):
    def _publish(self, m, extra_args=(), caption_path=None, marker=None, tmpdir=None):
        argv = ["--tag", "2026-09-14", "--video", VIDEO_URL,
                "--caption", caption_path, "--marker", marker or "", "--yes", *extra_args]
        argv = [a for a in argv if a != ""]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"BUFFER_TOKEN": FAKE_TOKEN}), \
             mock.patch("urllib.request.urlopen", m.urlopen), \
             mock.patch("sys.stdout", out):
            code = bp.cmd_publish(argv)
        return code, out.getvalue()

    def test_video_url_not_public(self):
        m = MockBuffer(head_status=404)
        with tempfile.TemporaryDirectory() as d:
            cap = write_caption(d)
            code, text = self._publish(m, caption_path=cap,
                                       marker=os.path.join(d, "m.json"))
        self.assertEqual(code, bp.EXIT_URL)
        self.assertNotIn("createPost", m.kinds())     # failed BEFORE any mutation
        self.assertIn("not publicly reachable", text)

    def test_http_url_rejected(self):
        m = MockBuffer()
        with tempfile.TemporaryDirectory() as d:
            cap = write_caption(d)
            argv = ["--video", "http://example.com/x.mp4", "--caption", cap,
                    "--marker", os.path.join(d, "m.json"), "--yes"]
            with mock.patch.dict(os.environ, {"BUFFER_TOKEN": FAKE_TOKEN}), \
                 mock.patch("urllib.request.urlopen", m.urlopen):
                code = bp.cmd_publish(argv)
        self.assertEqual(code, bp.EXIT_URL)
        self.assertEqual(m.calls, [])     # rejected before any network call

    def test_localhost_url_rejected(self):
        m = MockBuffer()
        with tempfile.TemporaryDirectory() as d:
            cap = write_caption(d)
            argv = ["--video", "https://localhost:8080/x.mp4", "--caption", cap, "--yes"]
            with mock.patch.dict(os.environ, {"BUFFER_TOKEN": FAKE_TOKEN}), \
                 mock.patch("urllib.request.urlopen", m.urlopen):
                code = bp.cmd_publish(argv)
        self.assertEqual(code, bp.EXIT_URL)
        self.assertEqual(m.calls, [])        # rejected without any network call

    def test_caption_too_long(self):
        m = MockBuffer()
        with tempfile.TemporaryDirectory() as d:
            cap = write_caption(d, body="x" * (bp.CAPTION_LIMIT + 50), tags="")
            code, text = self._publish(m, caption_path=cap,
                                       marker=os.path.join(d, "m.json"))
        self.assertEqual(code, bp.EXIT_CAPTION)
        self.assertIn("2200", text)
        self.assertNotIn("createPost", m.kinds())

    def test_caption_empty(self):
        m = MockBuffer()
        with tempfile.TemporaryDirectory() as d:
            cap = write_caption(d, body="   \n\n", tags="#a")
            code, _ = self._publish(m, caption_path=cap,
                                    marker=os.path.join(d, "m.json"))
        self.assertEqual(code, bp.EXIT_CAPTION)

    def test_idempotency_marker_blocks_second_publish(self):
        m = MockBuffer()
        with tempfile.TemporaryDirectory() as d:
            cap = write_caption(d)
            marker = os.path.join(d, "m.json")
            code1, _ = self._publish(m, caption_path=cap, marker=marker)
            self.assertEqual(code1, 0)
            self.assertTrue(os.path.exists(marker))
            n_calls_first = len(m.calls)

            m2 = MockBuffer()               # fresh mock: any call would be a bug
            code2, text2 = self._publish(m2, caption_path=cap, marker=marker)
            self.assertEqual(code2, 0)
            self.assertEqual(m2.calls, [])  # ZERO API calls on the second run
            self.assertIn("already queued", text2)
            self.assertGreater(n_calls_first, 0)


class CreatePostTests(unittest.TestCase):
    def _run(self, m, tmpdir, args_extra=()):
        cap = write_caption(tmpdir)
        marker = os.path.join(tmpdir, "m.json")
        argv = ["--tag", "2026-09-14", "--video", VIDEO_URL, "--caption", cap,
                "--marker", marker, *args_extra]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"BUFFER_TOKEN": FAKE_TOKEN}), \
             mock.patch("urllib.request.urlopen", m.urlopen), \
             mock.patch("sys.stdout", out):
            code = bp.cmd_publish(argv)
        return code, out.getvalue(), cap, marker

    def test_happy_path_payload(self):
        m = MockBuffer()
        with tempfile.TemporaryDirectory() as d:
            code, text, cap, marker = self._run(m, d, ["--yes"])
            data = json.load(open(marker))
        self.assertEqual(code, 0)
        self.assertIn("Queued in Buffer: post id post123", text)
        self.assertNotIn(FAKE_TOKEN, text)
        inputs = m.createpost_inputs()
        self.assertEqual(len(inputs), 1)
        inp = inputs[0]
        self.assertEqual(inp["channelId"], "ch1")
        self.assertEqual(inp["schedulingType"], "automatic")
        self.assertEqual(inp["mode"], "addToQueue")
        self.assertEqual(inp["assets"], [{"video": {"url": VIDEO_URL}}])
        self.assertEqual(inp["text"], "Hello caption body.\nSecond line.")
        self.assertNotIn("#", inp["text"])                     # hashtags not in caption
        self.assertEqual(inp["metadata"]["instagram"]["type"], "reel")
        self.assertTrue(inp["metadata"]["instagram"]["shouldShareToFeed"])
        self.assertEqual(inp["metadata"]["instagram"]["firstComment"],
                         "#metacognition #trendwatch")
        self.assertEqual(data["buffer_post_id"], "post123")
        self.assertTrue(data["first_comment_used"])

    def test_dry_run_without_yes(self):
        m = MockBuffer()
        with tempfile.TemporaryDirectory() as d:
            code, text, cap, marker = self._run(m, d)          # no --yes
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", text)
        self.assertNotIn("createPost", m.kinds())
        self.assertFalse(os.path.exists(marker))

    def test_first_comment_rejected_fallback(self):
        m = MockBuffer(createpost_responses=[
            {"data": {"createPost": {
                "message": "Invalid input: metadata.instagram.firstComment is not supported"}}},
            POST_OK,
        ])
        with tempfile.TemporaryDirectory() as d:
            code, text, cap, marker = self._run(m, d, ["--yes"])
            data = json.load(open(marker))
        self.assertEqual(code, 0)
        inputs = m.createpost_inputs()
        self.assertEqual(len(inputs), 2)
        self.assertNotIn("firstComment", inputs[1].get("metadata", {}).get("instagram", {}))
        self.assertIn("safe fallback", text)
        self.assertFalse(data["first_comment_used"])
        self.assertEqual(data["input_variant"], 1)

    def test_metadata_rejected_bare_fallback(self):
        m = MockBuffer(createpost_responses=[
            {"data": {"createPost": {"message": "Invalid input: metadata not allowed"}}},
            {"data": {"createPost": {"message": "Invalid input: metadata not allowed"}}},
            POST_OK,
        ])
        with tempfile.TemporaryDirectory() as d:
            code, text, cap, marker = self._run(m, d, ["--yes"])
        self.assertEqual(code, 0)
        inputs = m.createpost_inputs()
        self.assertEqual(len(inputs), 3)
        self.assertNotIn("metadata", inputs[2])

    def test_all_variants_fail(self):
        err = {"data": {"createPost": {"message": "Invalid input: queue is full"}}}
        m = MockBuffer(createpost_responses=[err, err, err])
        with tempfile.TemporaryDirectory() as d:
            code, text, cap, marker = self._run(m, d, ["--yes"])
        self.assertEqual(code, bp.EXIT_API)
        self.assertFalse(os.path.exists(marker))    # no marker on failure

    def test_graphql_transport_error_is_api_failure(self):
        m = MockBuffer(createpost_responses=[
            urllib.error.HTTPError(bp.API, 500, "boom", {}, io.BytesIO(b"oops"))])
        with tempfile.TemporaryDirectory() as d:
            code, text, cap, marker = self._run(m, d, ["--yes"])
        self.assertEqual(code, bp.EXIT_API)
        self.assertIn("HTTP 500", text)
        self.assertFalse(os.path.exists(marker))


class CaptionSplitTests(unittest.TestCase):
    def test_split_caption(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_caption(d, body="Line one.\n\nLine two.", tags="#a #b #c")
            body, tags = bp.split_caption(p)
        self.assertEqual(body, "Line one.\n\nLine two.")
        self.assertEqual(tags, "#a #b #c")


if __name__ == "__main__":
    unittest.main(verbosity=2)
