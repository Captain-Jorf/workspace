#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_groq_reasoning_models.py — Groq REASONING-model budget behaviour.

A real connection check against api.groq.com (captured from a live run
annotation) failed with:

    reviewer: Groq empty message content: host=api.groq.com
              model=openai/gpt-oss-20b http_status=200 attempt=1

That is not a connectivity failure. Groq's `openai/gpt-oss-*` models are
REASONING models: the hidden chain-of-thought is drawn from the SAME completion
budget as the visible answer, and when it runs out the API answers **HTTP 200**
with `choices[0].message.content == ""`, the reasoning in `message.reasoning`
and `finish_reason == "length"` — no error code and no error body to classify.

Documented remedy (https://console.groq.com/docs/reasoning), all asserted here:
  * `max_completion_tokens` instead of the deprecated `max_tokens`;
  * `reasoning_effort: "low"` — sent ONLY to the families whose docs accept it
    (GPT-OSS 20B/120B, Qwen 3.8 27B); Qwen 3.6 27B takes only "none"/"default"
    and non-reasoning models take no such parameter, and sending it to them
    makes Groq answer HTTP 400;
  * exactly ONE bounded retry with a larger budget when the answer came back
    empty because reasoning consumed it — still a real API call, never a fixture;
  * diagnostics that carry only counts and text LENGTH, never the reasoning text
    or any secret.
"""
from __future__ import annotations

import io
import json
import os
import sys
import types
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BUILD = ROOT / "build"
if str(BUILD) not in sys.path:
    sys.path.insert(0, str(BUILD))

import groq_check  # noqa: E402
import llm_provider as lp  # noqa: E402
from common import scrub_secrets  # noqa: E402

TOKEN = "gsk_REASONING_TEST_TOKEN_1234567890abcdef"
REASONER = "openai/gpt-oss-20b"
PLAIN = "llama-3.1-8b-instant"


@contextmanager
def env(**kw):
    saved = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def stub_transport(responses):
    """Patch llm_provider.groq_request; yield the list of recorded payloads."""
    sent = []
    queue = list(responses)

    def fake(method, url, payload=None, timeout=None):
        sent.append({"method": method, "url": url, "payload": payload})
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item, {"http_status": 200, "attempt": 1, "elapsed_ms": 5}

    original = lp.groq_request
    lp.groq_request = fake
    try:
        yield sent
    finally:
        lp.groq_request = original


def empty_reasoning_response(reasoning_tokens: int = 900, reasoning_text: str = "HIDDEN"):
    """HTTP 200 with empty content because reasoning ate the budget."""
    return {
        "choices": [{
            "message": {"role": "assistant", "content": "",
                        "reasoning": reasoning_text * 40},
            "finish_reason": "length",
        }],
        "usage": {"completion_tokens": 1000,
                  "completion_tokens_details": {"reasoning_tokens": reasoning_tokens}},
    }


class TestPayloadConstruction(unittest.TestCase):
    def test_uses_max_completion_tokens_never_max_tokens(self):
        for model in (REASONER, PLAIN):
            payload = lp.build_chat_payload("hi", model, 1200, 0.7)
            self.assertIn("max_completion_tokens", payload)
            self.assertNotIn("max_tokens", payload,
                             "deprecated `max_tokens` must not be sent")
            self.assertEqual(payload["model"], model)
            self.assertEqual(payload["messages"][-1]["content"], "hi")

    def test_reasoning_effort_low_only_for_documented_families(self):
        """GPT-OSS 20B/120B + Qwen 3.8 27B accept "low"; others must not get it."""
        low = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "gpt-oss-20b",
               "gpt-oss-120b", "qwen/qwen3.8-27b"]
        for m in low:
            self.assertEqual(lp.reasoning_effort_for(m), "low", m)
            self.assertEqual(lp.build_chat_payload("x", m, 800, 0.3)["reasoning_effort"], "low", m)
        # Qwen 3.6 27B takes only "none"/"default"; non-reasoning models take
        # no reasoning_effort at all -> sending it yields HTTP 400.
        forbidden = ["qwen/qwen3.6-27b", PLAIN, "meta-llama/llama-4-scout-17b",
                     "mistralai/mistral-small-3.1-24b", ""]
        for m in forbidden:
            self.assertIsNone(lp.reasoning_effort_for(m), repr(m))
            payload = lp.build_chat_payload("x", m or PLAIN, 800, 0.3)
            self.assertNotIn("reasoning_effort", payload, repr(m))

    def test_reasoning_budget_has_floor_and_cap(self):
        """A reasoning model below the documented 1024 floor is raised; never above the cap."""
        self.assertGreaterEqual(lp.build_chat_payload("x", REASONER, 300, 0.3)
                                ["max_completion_tokens"], 1024)
        self.assertEqual(lp.build_chat_payload("x", REASONER, 999_999, 0.3)
                         ["max_completion_tokens"], lp.MAX_COMPLETION_TOKENS_CAP)
        # A non-reasoning model keeps exactly the budget it was given.
        self.assertEqual(lp.build_chat_payload("x", PLAIN, 700, 0.3)
                         ["max_completion_tokens"], 700)

    def test_is_reasoning_model_detection(self):
        self.assertTrue(lp.is_reasoning_model(REASONER))
        self.assertTrue(lp.is_reasoning_model("openai/gpt-oss-120b"))
        self.assertTrue(lp.is_reasoning_model("qwen/qwen3.6-27b"))
        self.assertFalse(lp.is_reasoning_model(PLAIN))
        self.assertFalse(lp.is_reasoning_model(""))


class TestEmptyContentRetry(unittest.TestCase):
    def test_one_bounded_retry_with_bigger_budget_then_real_content(self):
        """Budget-exhausted 200 -> ONE retry, and the retry is a real API call."""
        ok = {"choices": [{"message": {"content": '{"decision":"approve"}'},
                           "finish_reason": "stop"}]}
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None), \
             stub_transport([empty_reasoning_response(), ok]) as sent:
            content, meta = lp.call_groq_chat("prompt", REASONER, max_tokens=1400)
        self.assertEqual(content, '{"decision":"approve"}')
        self.assertEqual(len(sent), 2, "exactly one bounded retry")
        first = sent[0]["payload"]["max_completion_tokens"]
        second = sent[1]["payload"]["max_completion_tokens"]
        self.assertGreater(second, first, "retry must raise the budget")
        self.assertLessEqual(second, lp.MAX_COMPLETION_TOKENS_CAP)
        self.assertEqual(meta["completion_attempts"], 2)
        self.assertEqual(meta["finish_reason"], "stop")

    def test_retry_never_falls_back_to_a_fixture(self):
        """If the retry also comes back empty it must raise — not invent content."""
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None), \
             stub_transport([empty_reasoning_response()]) as sent:
            with self.assertRaises(RuntimeError) as ctx:
                lp.call_groq_chat("prompt", REASONER, max_tokens=1400)
        self.assertEqual(len(sent), 1 + lp.EMPTY_CONTENT_RETRIES)
        msg = str(ctx.exception)
        for field in ("host=", "model=", "http_status=", "attempt=",
                      "finish_reason=", "reasoning_tokens=", "max_completion_tokens="):
            self.assertIn(field, msg, msg)

    def test_non_reasoning_empty_content_raises_without_retry(self):
        """Empty content that is NOT budget exhaustion must not be retried."""
        empty = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None), stub_transport([empty]) as sent:
            with self.assertRaises(RuntimeError):
                lp.call_groq_chat("prompt", PLAIN, max_tokens=700)
        self.assertEqual(len(sent), 1, "no retry when reasoning did not eat the budget")

    def test_diagnostics_never_carry_reasoning_text_or_secrets(self):
        """The hidden reasoning text and the key must not reach the error message."""
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None), \
             stub_transport([empty_reasoning_response(reasoning_text="TOPSECRETREASONING")]):
            with self.assertRaises(RuntimeError) as ctx:
                lp.call_groq_chat("prompt", REASONER, max_tokens=1400)
        msg = str(ctx.exception)
        self.assertNotIn("TOPSECRETREASONING", msg)
        self.assertNotIn("gsk_", msg)
        self.assertEqual(scrub_secrets(msg), msg)

    def test_extract_completion_returns_counts_not_text(self):
        content, finish, reasoning_tokens = lp._extract_completion(
            empty_reasoning_response(reasoning_tokens=1234, reasoning_text="PRIVATE"))
        self.assertEqual(content, "")
        self.assertEqual(finish, "length")
        self.assertEqual(reasoning_tokens, 1234)
        # A response with reasoning but no finish_reason still reports as such.
        c2, f2, r2 = lp._extract_completion({
            "choices": [{"message": {"content": "", "reasoning": "PRIVATE"}}]})
        self.assertEqual((c2, f2), ("", "reasoning-only"))
        self.assertIsNone(r2)

    def test_malformed_envelope_is_a_safe_error(self):
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None), \
             stub_transport([{"unexpected": True}]):
            with self.assertRaises(RuntimeError) as ctx:
                lp.call_groq_chat("prompt", REASONER, max_tokens=1400)
        self.assertNotIn("unexpected", str(ctx.exception))
        self.assertIn("empty message content", str(ctx.exception))


def _packet():
    """Evidence packet built the same way the real connection check builds it."""
    return groq_check.build_evidence_packet()


def _ok_response(body):
    return {"choices": [{"message": {"content": body}, "finish_reason": "stop"}]}


PRODUCER_BODY = json.dumps({
    "title": "T", "technology_angle": "automation bias in AI assistants",
    "metacognition_concept": "automation bias", "hook": "H",
    "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
    "narration": {"hook": "H", "problem": ["a"], "explain": ["b"],
                  "example": ["c"], "technique": ["d"], "ending": "E"},
    "on_screen_text": ["A"], "visual_direction": "V",
    "actionable_technique": "T", "ending": "E",
    "caption": {"hook": "H", "intro": "I", "sections": [], "hashtags": ["#metacognition"]},
    "claims": [], "sources": [{"label": "L", "url": "", "tier": "A"}],
})
REVIEWER_BODY = json.dumps({
    "approved": True, "score": 92, "technology_relevance": True,
    "metacognition_relevance": True, "source_grounding": True,
    "unsupported_claims": [], "hook_quality": "good",
    "spoken_english_quality": "good", "novelty": "high",
    "practical_value": "high", "safety": "safe",
    "required_changes": [], "blocking_errors": [],
})


class TestProducerReviewerBudgets(unittest.TestCase):
    """The two real call sites must go out with reasoning-safe payloads."""

    def test_producer_and_reviewer_payloads(self):
        discovered = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", PLAIN]
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None), \
             stub_transport([_ok_response(PRODUCER_BODY), _ok_response(REVIEWER_BODY)]) as sent:
            producer = lp.GroqProducer(model="auto")
            script, praw = producer.produce(_packet(), _discovered=discovered)
            reviewer = lp.GroqReviewer(model="auto")
            review, rraw = reviewer.review(script, _packet(), _discovered=discovered,
                                           producer_model=producer.model)

        self.assertEqual(script["metacognition_concept"], "automation bias")
        self.assertTrue(review["approved"])
        payloads = [c["payload"] for c in sent]
        self.assertEqual(len(payloads), 2, "one real Producer call + one real Reviewer call")
        for p_ in payloads:
            self.assertNotIn("max_tokens", p_)
            self.assertIn("max_completion_tokens", p_)
        # Both resolved to gpt-oss reasoning models -> effort must be "low",
        # and budgets must be above the pre-fix values (producer 1500 / reviewer 800).
        self.assertEqual(payloads[0]["model"], "openai/gpt-oss-120b")
        self.assertEqual(payloads[1]["model"], "openai/gpt-oss-20b")
        self.assertEqual(payloads[0].get("reasoning_effort"), "low")
        self.assertEqual(payloads[1].get("reasoning_effort"), "low")
        self.assertGreaterEqual(payloads[0]["max_completion_tokens"], 2200)
        self.assertGreaterEqual(payloads[1]["max_completion_tokens"], 1400)
        # Successful real calls keep their existing meta shape.
        self.assertIn("selection", praw)
        self.assertIn("selection", rraw)
        self.assertEqual(praw.get("content_len"), len(PRODUCER_BODY))

    def test_non_reasoning_selection_sends_no_reasoning_effort(self):
        """A llama-only account must not receive reasoning_effort (would be HTTP 400)."""
        discovered = [PLAIN]
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None), \
             stub_transport([_ok_response(PRODUCER_BODY)]) as sent:
            lp.GroqProducer(model="auto").produce(_packet(), _discovered=discovered)
        payload = sent[0]["payload"]
        self.assertEqual(payload["model"], PLAIN)
        self.assertNotIn("reasoning_effort", payload)
        self.assertIn("max_completion_tokens", payload)

    def test_empty_content_survives_as_provider_error_at_call_site(self):
        """Producer must propagate the safe RuntimeError (fail closed, no fixture)."""
        discovered = ["openai/gpt-oss-20b"]
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None), \
             stub_transport([empty_reasoning_response()]):
            with self.assertRaises(RuntimeError) as ctx:
                lp.GroqProducer(model="auto").produce(_packet(), _discovered=discovered)
        msg = str(ctx.exception)
        self.assertIn("empty message content", msg)
        self.assertEqual(scrub_secrets(msg), msg)


class TestMockModeUnaffected(unittest.TestCase):
    def test_mock_path_never_touches_the_transport(self):
        """MOCK_GROQ=1 must not be routed through the retry logic or the network."""
        def boom(*a, **k):
            raise AssertionError("mock mode must never perform an HTTP call")

        original = lp.groq_request
        lp.groq_request = boom
        try:
            with env(MOCK_GROQ="1", GROQ_API_KEY=None):
                script, raw = lp.GroqProducer(model="auto").produce(_packet())
                review, rraw = lp.GroqReviewer(model="auto").review(script, _packet())
        finally:
            lp.groq_request = original
        self.assertEqual(raw["mock"], True)
        self.assertEqual(rraw["mock"], True)
        self.assertEqual(script["metacognition_concept"], "automation bias")
        self.assertIn("approved", review)

    def test_missing_key_still_fails_closed(self):
        with env(GROQ_API_KEY=None, MOCK_GROQ=None):
            with self.assertRaises(RuntimeError) as ctx:
                lp.call_groq_chat("prompt", REASONER)
            self.assertIn("GROQ_API_KEY", str(ctx.exception))

    def test_empty_model_id_rejected(self):
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None):
            with self.assertRaises(ValueError):
                lp.call_groq_chat("prompt", "   ")


class TestNoSecretsInStdout(unittest.TestCase):
    def test_reasoning_failure_path_prints_no_token_or_reasoning_text(self):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with env(GROQ_API_KEY=TOKEN, MOCK_GROQ=None), \
             stub_transport([empty_reasoning_response(reasoning_text="TOPSECRETREASONING")]), \
             redirect_stdout(buf_out), redirect_stderr(buf_err):
            with self.assertRaises(RuntimeError) as ctx:
                lp.GroqProducer(model="auto").produce(
                    _packet(), _discovered=["openai/gpt-oss-20b"])
        combined = buf_out.getvalue() + buf_err.getvalue() + str(ctx.exception)
        self.assertNotIn(TOKEN, combined)
        self.assertNotIn("gsk_", combined)
        self.assertNotIn("TOPSECRETREASONING", combined)
        self.assertNotIn("HIDDEN", combined)
        self.assertEqual(scrub_secrets(combined), combined)


class TestJsonExtractionTolerance(unittest.TestCase):
    def test_fenced_and_prose_wrapped_json_both_parse(self):
        fenced = '```json\n{"decision": "approve", "reasons": ["ok"]}\n```'
        prose = 'Here is the review: {"decision": "approve", "nested": {"x": "}"}} — done.'
        for body in (fenced, prose):
            parsed = lp._parse_json_content(body, "reviewer", REASONER)
            self.assertEqual(parsed["decision"], "approve")
            self.assertIsInstance(parsed, dict)

    def test_malformed_json_error_never_echoes_content(self):
        secretish = "gsk_NOT_A_REAL_TOKEN_but_looks_like_one"
        body = f'{{"decision": "approve" BROKEN {secretish}'
        with self.assertRaises(RuntimeError) as ctx:
            lp._parse_json_content(body, "reviewer", REASONER)
        msg = str(ctx.exception)
        self.assertNotIn("BROKEN", msg)
        self.assertNotIn(secretish, msg)
        self.assertIn("content_len=", msg)
        self.assertIn("reviewer", msg)

    def test_non_object_json_rejected(self):
        with self.assertRaises(RuntimeError):
            lp._parse_json_content("[1, 2, 3]", "reviewer", REASONER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
