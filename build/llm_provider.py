"""LLM Provider Interface for @metacognition.hq — English-only, Technology × Metacognition.

Providers:
- GroqProducer: Groq Free Tier (GROQ_API_KEY), models auto-discovered via /models
- GroqReviewer: independent reviewer, preferably a different model
- StaticEnglishFallback: curated English playbooks filtered to tech domain

No billing, no paid endpoints, no auto-upgrade. The free tier is rate-limited
without SLA; on quota/auth/outage failure the daily pipeline falls back to
static English (generation_mode=static-fallback).

Historical note: GitHub Models was retired on 2026-07-30 and removed as a
provider (see docs/groq_migration.md). Nothing here uses GITHUB_TOKEN, the
models: read scope, or the retired endpoints.

Max daily requests: 1 producer, 1 reviewer, 1 revision, 1 final reviewer.
429 → honor Retry-After, max 2 short retries, then static fallback.
401/403 → no retry, authentication error recorded, static fallback.
5xx/timeout → limited retry, then static fallback.

Evidence packet is sanitized — web content is untrusted and must not inject prompt instructions.
"""
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import groq_http
common.assert_content_language_en()  # fail-closed EN-only

# The explicit project User-Agent sent on EVERY Groq request. api.groq.com sits
# behind Cloudflare: the stdlib default `Python-urllib/3.x` signature is
# rejected at the edge with HTTP 403 + Cloudflare error code 1010 ("banned your
# access based on your browser's signature") before authentication is even
# reached. See build/groq_http.py and docs/groq_cloudflare_1010_fix.md.
PROJECT_USER_AGENT = groq_http.PROJECT_USER_AGENT
GroqAPIError = groq_http.GroqAPIError

# Official Groq OpenAI-compatible API.
# Docs: https://console.groq.com/docs/api-reference
GROQ_HOSTNAME = "api.groq.com"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODELS_PATH = "/models"
GROQ_CHAT_PATH = "/chat/completions"

# Test-only override (local HTTP stub servers). Never set in workflows.
_TEST_BASE_ENV = "GROQ_BASE_URL"

# Retry policy: 1 initial attempt + 2 retries, exponential backoff.
# Single source of truth lives in build/groq_http.py (the central client).
MAX_ATTEMPTS = groq_http.MAX_ATTEMPTS
RETRY_BASE_SECONDS = groq_http.RETRY_BASE_SECONDS
MAX_429_RETRIES = groq_http.MAX_429_RETRIES
RETRY_AFTER_CAP_SECONDS = groq_http.RETRY_AFTER_CAP_SECONDS
REQUEST_TIMEOUT = groq_http.REQUEST_TIMEOUT
MODELS_TIMEOUT = groq_http.MODELS_TIMEOUT

AUTO_MODEL = "auto"

# --------------------------------------------------------------------- reasoning models
# Groq's `openai/gpt-oss-*` models are REASONING models: hidden chain-of-thought
# tokens are drawn from the SAME completion budget as the visible answer. When
# the budget runs out during reasoning, Groq answers **HTTP 200** with
# `choices[0].message.content == ""`, the reasoning in `message.reasoning`, and
# `finish_reason == "length"` — no error code, no error body.
#
# That is exactly what the real connection check hit on api.groq.com after the
# Cloudflare 403/1010 was fixed (captured from a live run annotation):
#   "reviewer: Groq empty message content: host=api.groq.com
#    model=openai/gpt-oss-20b http_status=200 attempt=1"
#
# Documented remedy (https://console.groq.com/docs/reasoning):
#   * use `max_completion_tokens` (`max_tokens` is the deprecated name),
#   * `reasoning_effort: "low"` — accepted ONLY by GPT-OSS 20B/120B and
#     Qwen 3.8 27B; Qwen 3.6 27B accepts only "none"/"default", and
#     non-reasoning models accept no such parameter at all, so it is sent
#     only to the families whose docs list it.
REASONING_EFFORT_LOW_MODELS = ("openai/gpt-oss-", "gpt-oss-", "qwen/qwen3.8-")
MAX_COMPLETION_TOKENS_CAP = 4096
EMPTY_CONTENT_RETRIES = 1


def is_reasoning_model(model):
    """True for model families that spend completion tokens on hidden reasoning."""
    mid = (model or "").strip().lower()
    return any(mid.startswith(p) for p in
               ("openai/gpt-oss-", "gpt-oss-", "qwen/qwen3.6-", "qwen/qwen3.8-"))


def reasoning_effort_for(model):
    """'low' only for families whose docs list it; None means do not send it.

    Sending `reasoning_effort` to a model that does not document it makes Groq
    answer HTTP 400, so the gate is deliberately narrow.
    """
    mid = (model or "").strip().lower()
    if any(mid.startswith(p) for p in REASONING_EFFORT_LOW_MODELS):
        return "low"
    return None

# Candidate instruction-capable text models, ordered by producer preference
# (strongest first). ONLY models confirmed by the authenticated /models call
# are ever used — this list is intersected with live discovery, never trusted
# blindly. Verified against Groq docs (tool-use table + models API), 2026-09.
GROQ_CANDIDATE_MODELS = [
    "openai/gpt-oss-120b",
    "moonshotai/kimi-k2-instruct",
    "qwen/qwen3-32b",
    "qwen/qwen3.6-27b",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.1-8b-instant",
]

DEFAULT_PRODUCER_MODEL = os.environ.get("PRODUCER_MODEL", AUTO_MODEL)
DEFAULT_REVIEWER_MODEL = os.environ.get("REVIEWER_MODEL", AUTO_MODEL)

# ---------------------------------------------------------------- fail-closed helpers

def is_mock_enabled():
    """Mock is allowed ONLY with the explicit flag MOCK_GROQ=1.

    Missing key, DNS failure, HTTP error, invalid model or malformed JSON must
    NEVER silently switch to mock in real mode. Default: False.
    """
    return os.environ.get("MOCK_GROQ") == "1"


def get_base_url():
    """Official Groq base URL. Test-only override via GROQ_BASE_URL."""
    override = os.environ.get(_TEST_BASE_ENV, "").strip().rstrip("/")
    return override or GROQ_BASE_URL


def get_models_url():
    return get_base_url() + GROQ_MODELS_PATH


def get_chat_url():
    return get_base_url() + GROQ_CHAT_PATH


def get_hostname():
    """Hostname of the endpoint in use. Safe to log (no secret, no key)."""
    try:
        return urllib.parse.urlparse(get_base_url()).hostname or GROQ_HOSTNAME
    except Exception:
        return GROQ_HOSTNAME


def get_groq_key():
    """Return the API key or ''. Callers must NEVER log its value."""
    return groq_http.get_api_key()


def require_groq_key():
    """Raise (nonzero in workflows) when no key is available. Never logs it."""
    return groq_http.require_api_key()


def groq_headers(with_auth=True, with_body=False):
    """Headers used for EVERY Groq request (explicit project User-Agent)."""
    return groq_http.build_headers(with_auth=with_auth, with_body=with_body)


def validate_candidate_model(model):
    """Fail-fast check: 'auto' or a known candidate ID. Live /models is authoritative."""
    if model == AUTO_MODEL:
        return model
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Invalid model: empty model ID")
    if model not in GROQ_CANDIDATE_MODELS:
        raise ValueError(
            f"Invalid model ID: {model!r} — not a known Groq candidate "
            f"({len(GROQ_CANDIDATE_MODELS)} known IDs, or {AUTO_MODEL!r})"
        )
    return model


def resolve_hostname(hostname=None, timeout=10):
    """DNS diagnostic without credentials. Returns (ok, detail).

    Retries transient failures up to 2 times with exponential backoff.
    Only the hostname (safe) is ever reported, never IPs or keys.
    """
    hostname = hostname or get_hostname()
    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            infos = socket.getaddrinfo(hostname, 443)
            return True, f"DNS resolution: OK host={hostname} records={len(infos)} attempt={attempt}"
        except Exception as e:  # noqa: BLE001 — diagnostic, report type only
            last_err = f"{type(e).__name__}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    return False, f"DNS resolution: FAILED host={hostname} error={last_err} attempts={MAX_ATTEMPTS}"


def https_probe(timeout=None):
    """Unauthenticated HTTPS/TLS reachability check — SAME safe client.

    Sends the explicit project User-Agent + Accept but NO Authorization header,
    so the key is never exposed on an unauthenticated request. Any HTTP
    response (even 401/403/4xx/5xx) proves DNS + TLS + edge work.
    Returns (reachable, http_status_or_None, detail). Safe to log.
    """
    return groq_http.probe(get_models_url(),
                           timeout=timeout if timeout is not None else groq_http.PROBE_TIMEOUT,
                           max_attempts=MAX_ATTEMPTS)


def parse_retry_after(headers):
    """Parse Retry-After (seconds) from response headers. Returns 0 if absent."""
    return groq_http.parse_retry_after(headers)


def groq_request(method, url, payload=None, timeout=60):
    """Authenticated Groq request — thin wrapper over the central HTTP client.

    ALL Groq traffic (model discovery, Producer, Reviewer, revision) uses
    build/groq_http.request(), which always sends the explicit project
    User-Agent + Accept, adds Authorization/Content-Type when needed, and
    applies the safe error taxonomy + bounded retry policy:

    - 403 + Cloudflare code 1010: cloudflare-client-blocked, NO retry.
    - 401: invalid-or-missing-api-key, no retry.
    - other 403: permission-or-account-restriction, no retry.
    - 429: rate-limited, Retry-After honored, max 2 short retries.
    - 5xx/timeout/network: limited retry (1+2), then raise.
    Returns (parsed_json, meta). Messages carry only safe fields.
    """
    return groq_http.request(method, url, payload=payload, timeout=timeout,
                            with_auth=True, max_attempts=MAX_ATTEMPTS,
                            max_rate_retries=MAX_429_RETRIES)


def discover_models(timeout=MODELS_TIMEOUT):
    """Authenticated GET /models. Returns (ids, meta). Raises on any failure."""
    hostname = get_hostname()
    resp, meta = groq_request("GET", get_models_url(), timeout=timeout)
    try:
        ids = [m["id"] for m in resp.get("data", [])
               if isinstance(m, dict) and m.get("id")]
    except Exception:
        ids = []
    if not ids:
        raise RuntimeError(
            f"Groq model discovery returned no models: host={hostname} "
            f"http_status={meta.get('http_status', '?')}"
        )
    meta["models"] = ids
    return ids, meta


def select_models(discovered_ids, producer_want=None, reviewer_want=None):
    """Intersect candidates with live /models; pick producer + reviewer.

    - 'auto' producer → strongest matched candidate.
    - 'auto' reviewer → strongest matched candidate DIFFERENT from producer
      (reuses producer only when a single candidate is available).
    - explicit IDs must be known candidates AND present in discovery.
    Returns (producer, reviewer, report). Report is non-sensitive.
    """
    producer_want = producer_want or DEFAULT_PRODUCER_MODEL
    reviewer_want = reviewer_want or DEFAULT_REVIEWER_MODEL
    discovered = list(discovered_ids or [])
    dset = set(discovered)
    matched = [c for c in GROQ_CANDIDATE_MODELS if c in dset]

    if producer_want == AUTO_MODEL:
        if not matched:
            raise ValueError(
                f"Groq model discovery: none of the {len(GROQ_CANDIDATE_MODELS)} "
                f"candidate models are available ({len(discovered)} discovered)"
            )
        producer = matched[0]
        p_reason = (f"auto: strongest available candidate "
                    f"({len(matched)} matched of {len(discovered)} discovered)")
    else:
        validate_candidate_model(producer_want)
        if producer_want not in dset:
            raise ValueError(
                f"Groq model unavailable: {producer_want!r} not in discovered "
                f"/models ({len(discovered)} discovered)"
            )
        producer = producer_want
        p_reason = "explicit PRODUCER_MODEL confirmed by /models"

    if reviewer_want == AUTO_MODEL:
        others = [m for m in matched if m != producer]
        if others:
            reviewer = others[0]
            r_reason = "auto: strongest available candidate different from producer"
        else:
            reviewer = producer
            r_reason = "auto: only one candidate available; reviewer reuses producer model"
    else:
        validate_candidate_model(reviewer_want)
        if reviewer_want not in dset:
            raise ValueError(
                f"Groq model unavailable: {reviewer_want!r} not in discovered "
                f"/models ({len(discovered)} discovered)"
            )
        reviewer = reviewer_want
        r_reason = "explicit REVIEWER_MODEL confirmed by /models"

    report = {
        "producer_model": producer,
        "reviewer_model": reviewer,
        "selected_at_utc": common.utc_now(),
        "reason": f"producer: {p_reason}; reviewer: {r_reason}",
        "discovered_count": len(discovered),
        "candidates_matched": matched,
    }
    return producer, reviewer, report


def build_chat_payload(prompt, model, max_tokens, temperature):
    """Chat-completion payload for the Groq OpenAI-compatible endpoint.

    `max_completion_tokens` (not the deprecated `max_tokens`) so the limit means
    what we intend on reasoning models, plus `reasoning_effort: "low"` for the
    families whose docs accept it, so hidden reasoning cannot silently eat the
    whole visible-answer budget.
    """
    budget = int(max_tokens)
    if reasoning_effort_for(model):
        budget = max(budget, 1024)
    budget = min(budget, MAX_COMPLETION_TOKENS_CAP)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant for @metacognition.hq, English-only, technology\u00d7metacognition. Output valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": budget,
        "temperature": temperature,
    }
    effort = reasoning_effort_for(model)
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def _extract_completion(resp):
    """Pull (content, finish_reason, reasoning_tokens) out of a chat response.

    Only counts and text LENGTH are ever returned — never the reasoning text
    itself, so a diagnostic can never carry model output into a log.
    """
    content = ""
    finish = ""
    reasoning_tokens = None
    try:
        choice = (resp or {}).get("choices", [{}])[0] or {}
        message = choice.get("message", {}) or {}
        raw = message.get("content", "")
        content = raw if isinstance(raw, str) else ""
        finish = str(choice.get("finish_reason", "") or "")
        if not content.strip():
            reasoning = message.get("reasoning") or ""
            if isinstance(reasoning, str) and reasoning.strip():
                finish = finish or "reasoning-only"
        details = ((resp or {}).get("usage", {}) or {}).get(
            "completion_tokens_details", {}) or {}
        if isinstance(details.get("reasoning_tokens"), int):
            reasoning_tokens = details["reasoning_tokens"]
    except Exception:  # noqa: BLE001 — malformed envelope handled by the caller
        pass
    return content, finish, reasoning_tokens


def call_groq_chat(prompt, model, max_tokens=1200, temperature=0.7, timeout=REQUEST_TIMEOUT):
    """Real Groq chat completion. Fail-closed, never mock.

    Reasoning models can answer HTTP 200 with EMPTY content when the hidden
    reasoning consumed the whole completion budget (`finish_reason: "length"`).
    That is a budget problem, not a connectivity or content problem, so it gets
    exactly ONE bounded retry with a larger budget — still a real API call, never
    a fixture. Anything else raises.

    Returns (content, meta). Raises on: missing key, empty model, network
    failure (after retries), HTTP error, invalid/empty envelope.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Invalid model: empty model ID")
    hostname = get_hostname()
    budget = int(max_tokens)
    last = None
    for completion_attempt in range(1, EMPTY_CONTENT_RETRIES + 2):
        payload = build_chat_payload(prompt, model, budget, temperature)
        resp, meta = groq_request("POST", get_chat_url(), payload, timeout=timeout)
        content, finish, reasoning_tokens = _extract_completion(resp)
        if isinstance(content, str) and content.strip():
            meta.update({"model": model, "content_len": len(content),
                         "completion_tokens_budget": payload.get("max_completion_tokens"),
                         "reasoning_effort": payload.get("reasoning_effort"),
                         "finish_reason": finish,
                         "reasoning_tokens": reasoning_tokens,
                         "completion_attempts": completion_attempt})
            if isinstance(resp, dict):
                meta["envelope"] = resp
            return content, meta
        last = (meta, finish, reasoning_tokens, payload.get("max_completion_tokens"))
        budget_exhausted = (finish == "length" or bool(reasoning_tokens)
                            or finish == "reasoning-only"
                            or bool(reasoning_effort_for(model)))
        if completion_attempt <= EMPTY_CONTENT_RETRIES and budget_exhausted:
            # ONE bounded retry with more room for reasoning + the visible answer.
            budget = min(budget * 2, MAX_COMPLETION_TOKENS_CAP)
            continue
        break
    meta, finish, reasoning_tokens, used_budget = last
    raise RuntimeError(
        f"Groq empty message content: host={hostname} model={model} "
        f"http_status={meta.get('http_status', '?')} attempt={meta.get('attempt', '?')} "
        f"finish_reason={finish or '?'} reasoning_tokens={reasoning_tokens} "
        f"max_completion_tokens={used_budget} reasoning_model={is_reasoning_model(model)}"
    )


def sanitize_untrusted(text, max_len=800):
    """Sanitize untrusted web content to prevent prompt injection."""
    if not text:
        return ""
    # Remove potential prompt injection patterns
    text = re.sub(r"(?i)(system|assistant|user):", "", text)
    text = re.sub(r"(?i)ignore previous instructions", "[filtered]", text)
    text = re.sub(r"(?i)do anything now", "[filtered]", text)
    # Limit length
    text = text[:max_len]
    # Escape JSON-breaking chars will be handled by json dumps
    return text.strip()


def build_evidence_packet(topic, policy, recent_topics=None, recent_ctas=None):
    """Build limited sanitized evidence packet for LLM producer."""
    cal = topic.get("calendar") or {}
    discovery = topic.get("discovery_source") or {}
    # Trusted excerpt from calendar or discovery (sanitized)
    trusted_excerpt = ""
    if cal.get("beats"):
        trusted_excerpt = "; ".join(cal.get("beats", [])[:3])
    if discovery.get("title"):
        trusted_excerpt += f" | Discovery: {sanitize_untrusted(discovery.get('title',''), 200)}"

    # Explicit numeric-grounding statement. Calendar/evergreen packets contain
    # NO statistics, so the producer must never introduce percentages, study
    # results or precise numbers. The value list below is derived from the
    # packet itself (common.packet_numeric_evidence) — normally empty.
    ev_label = sanitize_untrusted((cal.get("sources") or [""])[0][:200] if cal.get("sources") else "", 200)
    disc_url = discovery.get("url", "")[:300]
    numeric_evidence = sorted(common.packet_numeric_evidence(
        {"trusted_excerpt": trusted_excerpt,
         "evidence_source": {"label": ev_label},
         "discovery_source": {"name": discovery.get("name", "")}}))

    # EVIDENCE GROUNDING (issue #22): the packet's tier is DERIVED with QA's own
    # matcher (common.source_tier via common.best_tier), never asserted from the
    # mere existence of a calendar entry. The old `"A" if cal else "C"` claimed
    # Tier A for every calendar topic — the prompt then permitted research
    # attribution while the supervisor's matcher evaluated the script's sources
    # as tier "?" and blocked the reel after a wasted render. A tier here is
    # only ever as strong as the URL/dated label the packet actually carries;
    # nothing is invented, and a discovery source counts as evidence only when
    # its own URL lands in a policy Tier A/B domain (HN/Trends stay discovery).
    tier_candidates = []
    if ev_label:
        tier_candidates.append(common.source_tier("", ev_label, policy))
    if disc_url:
        tier_candidates.append(common.source_tier(disc_url, discovery.get("name", ""), policy))
    evidence_tier = common.best_tier(tier_candidates) if tier_candidates else "?"
    has_ab = evidence_tier in ("A", "B")

    packet = {
        "topic": sanitize_untrusted(topic.get("title",""), 200),
        "technology_angle": sanitize_untrusted(topic.get("technology_angle") or topic.get("pillar",""), 200),
        "discovery_source": {
            "name": sanitize_untrusted(discovery.get("name",""), 100),
            "url": disc_url,
            "tier": discovery.get("tier",""),
        },
        "evidence_source": {
            "label": ev_label,
            "tier": evidence_tier,
        },
        "trusted_excerpt": sanitize_untrusted(trusted_excerpt, 600),
        "numeric_evidence": numeric_evidence,
        "numeric_evidence_note": ("these are the ONLY numeric claims allowed: " + ", ".join(numeric_evidence)
                                   if numeric_evidence else
                                   "NONE — this evidence packet contains no statistics, percentages or "
                                   "study numbers, so the script must not introduce any"),
        # Derived with the QA matcher — the single source for claim-word patterns
        # (policy source_policy.require_evidence_for_claim_words). Prompts quote
        # this list; they never maintain a keyword list of their own.
        "evidence_tier": evidence_tier,
        "has_tier_ab_evidence": has_ab,
        "claim_word_patterns": list(policy.get("source_policy", {}).get("require_evidence_for_claim_words", [])),
        "source_urls_allowed": sorted(common.packet_evidence_urls(
            {"discovery_source": {"url": disc_url}, "evidence_source": {"url": ""}})),
        "allowed_claims": policy.get("source_policy", {}).get("require_evidence_for_claim_words", [])[:10],
        "unsupported_claims": ["no fake stats", "no invented citation", "no medical advice"],
        "recent_topics": [sanitize_untrusted(t, 100) for t in (recent_topics or [])[:5]],
        "recent_cta_types": [str(c) for c in (recent_ctas or [])[:5]],
        "cta_types_allowed": list(policy.get("cta_policy", {}).get("allowed_types", [])),
        "editorial_policy": {
            "brand": policy.get("page", {}).get("brand_positioning",""),
            "pillars": list(policy.get("pillars", {}).keys())[:6],
            "tone": policy.get("tone", {}).get("style",""),
            "length_target": policy.get("length", {}).get("target_seconds", [70,105]),
            "max_words_per_line": policy.get("length", {}).get("max_words_per_line", 20),
            "forbidden_openers": policy.get("tone", {}).get("forbidden_openers", [])[:5],
            # Deterministic pre-render gate ranges (single source: common.pre_render_params
            # reading the UNCHANGED QA policy ranges + the 175-210 target). Prompts state
            # them; the gate re-counts actual words, so a model-reported count is useless.
            "word_target": [policy.get("length", {}).get("pre_render", {}).get("word_target", [175, 210])[0],
                            policy.get("length", {}).get("pre_render", {}).get("word_target", [175, 210])[1]],
        }
    }
    gate_p = common.pre_render_params(policy)
    packet["word_target"] = [gate_p["target_min"], gate_p["target_max"]]
    packet["word_range"] = [gate_p["words_min"], gate_p["words_max"]]
    return packet


# JSON schema for producer output
PRODUCER_SCHEMA = {
    "title": "string",
    "technology_angle": "string",
    "metacognition_concept": "string",
    "hook": "string",
    "scenes": "list",
    "narration": "string or list",
    "on_screen_text": "list",
    "visual_direction": "string",
    "actionable_technique": "string or list",
    "ending": "string",
    "caption": "object",
    "claims": "list",
    "sources": "list"
}

REVIEWER_SCHEMA = {
    "approved": "bool",
    "score": "int 0-100",
    "technology_relevance": "bool",
    "metacognition_relevance": "bool",
    "source_grounding": "bool",
    "unsupported_claims": "list",
    "hook_quality": "string",
    "spoken_english_quality": "string",
    "novelty": "string",
    "practical_value": "string",
    "safety": "string",
    "required_changes": "list",
    "blocking_errors": "list"
}


def _first_balanced_object(text):
    """First balanced {...} block in `text`, or '' — for prose-wrapped JSON."""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _parse_json_content(content, who, model):
    """Parse a (fenced or prose-wrapped) JSON object.

    Safe errors: the message carries only the exception type, the model and the
    content LENGTH — never the content itself.
    """
    original_len = len(content or "")
    try:
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
        candidate = m.group(1) if m else content
        try:
            parsed = json.loads(candidate)
        except Exception:
            # Models sometimes wrap the object in prose; take the first
            # balanced {...} block before giving up.
            block = _first_balanced_object(candidate)
            if not block:
                raise
            parsed = json.loads(block)
        if not isinstance(parsed, dict):
            raise ValueError("top-level JSON is not an object")
        return parsed
    except Exception as e:
        raise RuntimeError(
            f"{who} returned malformed JSON: {type(e).__name__} "
            f"model={model} content_len={original_len}"
        )


class LLMProvider:
    """Generic provider base: model handling + explicit-mock gating.

    Subclasses implement produce()/review(). Real calls resolve 'auto' via
    live /models discovery; explicit IDs must be known candidates confirmed
    by discovery.
    """
    kind = "llm"

    def __init__(self, model=None):
        self.requested_model = model or self._default_model()
        if self.requested_model != AUTO_MODEL:
            validate_candidate_model(self.requested_model)
        self.model = self.requested_model  # concrete ID after real resolution
        self.selection = None

    def _default_model(self):
        return AUTO_MODEL

    @property
    def mock_enabled(self):
        return is_mock_enabled()

    def _mock_raw(self):
        return {"mock": True, "model": self.model}


class GroqProducer(LLMProvider):
    kind = "groq-producer"

    def _default_model(self):
        return DEFAULT_PRODUCER_MODEL

    def produce(self, evidence_packet, _discovered=None):
        # Mock ONLY with the explicit flag. Missing key in real mode raises
        # (fail closed) instead of silently returning fixture content.
        if self.mock_enabled:
            # Mocked valid response for local tests / dry-runs — expanded to meet 70-105s target
            topic = evidence_packet.get('topic','automation bias')
            tech_angle = evidence_packet.get('technology_angle') or "automation bias in AI assistants"
            mock_response = {
                "title": f"{topic} — tech angle",
                "technology_angle": tech_angle,
                "metacognition_concept": "automation bias",
                "hook": "When does your AI assistant make you think less?",
                "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
                "narration": {
                    "hook": "When does your AI assistant make you think less?",
                    "problem": ["You ask AI for code, it gives you an answer instantly.", "It feels productive, and you move on without checking.", "The speed hides the need to verify."],
                    "explain": ["Your brain treats the AI's fluency as your own understanding.", "That's automation bias: trusting the tool because it sounds confident.", "The more you use it, the less you verify.", "Fluency is not accuracy, but it feels like it."],
                    "example": ["Think of the last time autocomplete finished your function.", "Did you read it line by line, or just accept it?", "Most people accept, because checking feels slower.", "That pause you skip is where learning lives."],
                    "technique": ["Try this: before you accept AI code, explain it out loud in one sentence.", "Then run one edge-case test yourself.", "If you can't explain it, you haven't learned it.", "Make that pause your new habit."],
                    "ending": "Where did AI make you skip the thinking this week? Share one moment you caught it."
                },
                "on_screen_text": ["ASK AI", "FLUENT", "BIAS", "CHECK", "EXPLAIN", "LEARN"],
                "visual_direction": "confidence meter + code visual + human-AI network + decision tree",
                "actionable_technique": "Explain AI output before accepting, test one edge case",
                "ending": "Where did AI make you skip the thinking this week? Share one moment you caught it.",
                "caption": {
                    "hook": "When does your AI assistant make you think less?",
                    "intro": "AI fluency feels like your own understanding. That feeling is automation bias, not knowledge.",
                    "sections": [
                        {"title": "WHAT'S GOING ON", "lines": ["Your brain treats AI fluency as your own understanding.", "Automation bias: trusting the tool because it sounds confident.", "Fluency is not accuracy."]},
                        {"title": "TRY THIS", "lines": ["Before you accept AI code, explain it out loud in one sentence.", "Then run one edge-case test yourself.", "Make that pause your new habit."]}
                    ],
                    "hashtags": ["#metacognition", "#AI", "#coding", "#automationbias", "#cognitivescience"]
                },
                "claims": [],
                "sources": [{"label": "Bansal et al. (2021), AI and Human Judgment", "url": "", "tier": "B"}]
            }
            return mock_response, self._mock_raw()

        discovered = _discovered if _discovered is not None else discover_models()[0]
        producer, _, report = select_models(discovered, self.requested_model, AUTO_MODEL)
        self.model = producer
        self.selection = report

        # A revision request carries the reviewer's required changes so the
        # second Producer call is a real revision and not a blind regeneration.
        revision_block = ""
        revision_items = evidence_packet.get("revision_request") or []
        if isinstance(revision_items, str):
            revision_items = [revision_items]
        if revision_items:
            safe_items = [sanitize_untrusted(str(x), 200) for x in revision_items[:8]]
            wr = evidence_packet.get("word_range") or [150, 260]
            wt = evidence_packet.get("word_target") or [175, 210]
            flagged_patterns = ", ".join(f"\u201c{p}\u201d"
                                         for p in (evidence_packet.get("claim_word_patterns") or [])[:10])
            allowed_urls = evidence_packet.get("source_urls_allowed") or []
            url_note = ("only these packet-provided urls may appear: " + ", ".join(allowed_urls)
                        if allowed_urls else "the packet provides NO source urls — every 'url' must stay empty")
            revision_block = (
                "\nRevision requirements from the independent reviewer AND the deterministic "
                "pre-render gate (address EVERY one of them):\n- "
                + "\n- ".join(i for i in safe_items if i)
                + "\n"
                "Revision rules that always apply:\n"
                "- Numeric fix rule: if a number/percentage/statistic is flagged (or you find one), REMOVE it "
                "or REWRITE the sentence so it carries no number at all. The evidence packet's numeric "
                "evidence is: " + str(evidence_packet.get("numeric_evidence_note", "")) + ". "
                "NEVER keep a number by adding, adjusting or inventing a citation/URL.\n"
                "- Attribution fix rule (issue #22): if research-attribution wording is flagged (claim words — "
                + flagged_patterns + "…), REMOVE the attribution: rewrite the sentence as a direct, "
                "appropriately qualified observation. NEVER add, keep, adjust or invent a citation, paper, "
                "author name, URL or evidence tier to justify it — the deterministic citation guard rejects "
                "every source URL the packet itself does not provide, and " + url_note + ".\n"
                "- Length fix rule: the deterministic pre-render gate requires the TOTAL of all narration "
                f"lines to be inside {wr[0]}-{wr[1]} spoken words, targeting ~{wt[0]}-{wt[1]} (that is what "
                "makes the rendered video fit 60-120 s at the configured narration rate). Fix length with "
                "CONTENT: deepen the single main idea, its genuine metacognitive mechanism, its example and "
                "its one exercise. Preserve exactly ONE main idea and ONE actionable technique. NEVER pad "
                "with filler, disclaimers, a repeated CTA or slowed speech; never claim a word count — the "
                "gate recounts the real lines.\n"
                "- Keep the revision conversational: natural contractions, short lines (max 20 words), "
                "no academic connectors.\n"
            )

        # Evidence-grounded attribution rule (issue #22): the claim-word list is
        # the QA policy's OWN pattern list (single source — never a second list);
        # what the producer may say depends on the tier the packet DERIVABLY
        # carries, i.e. exactly the bar the source_quality blocker applies.
        claim_patterns = evidence_packet.get("claim_word_patterns") or []
        pattern_blob = ", ".join(f"\u201c{p}\u201d" for p in claim_patterns)
        if evidence_packet.get("has_tier_ab_evidence"):
            attribution_rule = (
                "Research attribution is allowed ONLY because this packet DERIVABLY carries Tier "
                f"{evidence_packet.get('evidence_tier')} evidence: \"{evidence_packet.get('evidence_source',{}).get('label','')}\". "
                "Attribute only to that exact source, by name, without numbers — reference research by name "
                "only (e.g. \u201cMark et al. (2008), cost of interrupted work\u201d), never \u201cstudies show X%\u201d, and never to any "
                "source the packet does not list.")
        else:
            attribution_rule = (
                "This packet contains NO Tier A/B evidence (derived tier: "
                f"{evidence_packet.get('evidence_tier', '?')}), so research attribution has nothing to stand on: "
                "the narration must not contain ANY of the claim-word patterns the QA source_quality matcher "
                "blocks — they are: " + pattern_blob + ". This includes the bare word \u201cresearchers\u201d. State each point as "
                "a direct, appropriately qualified observation instead of attributed research (honest hedging; "
                "no fabricated certainty), and NEVER add, cite or invent a paper, author, URL or \u201ctier\u201d to justify "
                "an attribution — a deterministic citation guard rejects any source URL the packet does not provide, "
                "and the deterministic pre-render text QA gate would block the script anyway (before any render), "
                "burning the one allowed Revision.")
        prompt = f"""
You are GroqProducer for @metacognition.hq — Metacognition for the AI age.
{revision_block}
Brand: {evidence_packet['editorial_policy']['brand']}
Pillars: {', '.join(evidence_packet['editorial_policy']['pillars'])}

Evidence packet (sanitized, untrusted web content already filtered):
Topic: {evidence_packet.get('topic','')}
Technology angle: {evidence_packet.get('technology_angle','')}
Discovery: {evidence_packet.get('discovery_source',{}).get('name','')} ({evidence_packet.get('discovery_source',{}).get('url','')})
Evidence: {evidence_packet.get('evidence_source',{}).get('label','')}
Excerpt: {evidence_packet.get('trusted_excerpt','')}
Numeric evidence in packet: {evidence_packet.get('numeric_evidence_note','')}
Recent topics to avoid: {', '.join(evidence_packet.get('recent_topics',[]))}
Allowed claims must have evidence, forbidden: {', '.join(evidence_packet.get('unsupported_claims', ['no fake stats']))}

Task: Create an English-only reel script JSON for a 70-105 s spoken video (hard render limit 60-120 s).
The TOTAL of every narration line must be {evidence_packet.get('word_target',[175,210])[0]}-{evidence_packet.get('word_target',[175,210])[1]} spoken words (absolute allowed range {evidence_packet.get('word_range',[150,260])[0]}-{evidence_packet.get('word_range',[150,260])[1]}) — a DETERMINISTIC PRE-RENDER GATE counts the actual words in your narration lines; scripts outside the range are rejected BEFORE TTS/rendering, and word counts you report are ignored. Conversational English, strong hook in 3 sec, no 'In today's video', no filler, no fake stats/citation, no medical advice, one main idea, one tech example, one practical technique, network visual relevant to tech, natural CTA.

HARD RULES (a violation is an automatic rejection):
1. Numeric grounding — calendar/evergreen generation must NOT introduce percentages, statistics, study results, survey figures or any precise numeric claim unless the numeric evidence above explicitly lists it. {evidence_packet.get('numeric_evidence_note','')}. Never with "studies show X%". Do NOT invent or guess URLs — including arXiv or DOI links: leave source "url" empty unless the packet provides it.
1b. Attribution grounding — {attribution_rule}
2. Conversational spoken English — use natural contractions (don't, it's, you'll, that's, can't, let's) wherever grammatical. Short spoken sentences; every narration line at most {evidence_packet['editorial_policy'].get('max_words_per_line', 20)} words; break long or formal constructions into short sentences. Never use academic connectors (furthermore, moreover, thus, hence, utilize, in conclusion, it is imperative). Natural, not sloppy: contractions must be grammatical, no slang, no filler.
3. Scope — exactly ONE main idea and ONE actionable technique. Touch the technology angle (AI, software, coding, product, digital behavior) explicitly in the narration.
4. CTA diversity — recent reels used these CTA types: {', '.join(evidence_packet.get('recent_cta_types', []) or ['(none recorded)'])}. Write the ending as a CTA of a DIFFERENT type from the most recent one. Allowed CTA types: {', '.join(evidence_packet.get('cta_types_allowed', ['question', 'try-it', 'share-experience', 'save']))} (question = ask a direct question; try-it = ask the viewer to try the technique; share-experience = ask for a personal story/experience; save = ask to save/bookmark the reel).
5. Length budget — the deterministic pre-render gate counts every narration line ({evidence_packet.get('word_range', [150, 260])[0]}-{evidence_packet.get('word_range', [150, 260])[1]} spoken words allowed, {evidence_packet.get('word_target', [175, 210])[0]}-{evidence_packet.get('word_target', [175, 210])[1]} targeted for safe duration margin). Too-short scripts never render: they cost the run one revision, then a static fallback. Fill the budget with REAL content — deeper mechanism, one concrete example, one concrete exercise — never filler sentences, disclaimers, repeated CTAs or stretched silence, and never state your own word count as a guarantee.
6. Content quality minimums (a missing item is a rejection risk, especially for coding/tech topics like autocomplete deskilling): name the CONCRETE technology context in the narration (the actual tool/workflow, e.g. accepting an autocomplete suggestion with Tab in your editor); state the GENUINE metacognitive mechanism (e.g. recognition replacing retrieval practice, monitoring the gap between "looks right" and "can produce it") rather than vague self-help; give ONE specific practical exercise the viewer can run today; write conversational English with natural contractions throughout; keep the hook a question or sharp contrast within {evidence_packet['editorial_policy'].get('hook_max_words', 15)} words; and close with ONE concise CTA that does not repeat sentences from the body.

Output ONLY valid JSON with keys:
{{
  "title": "string",
  "technology_angle": "string, e.g., automation bias in AI assistants",
  "metacognition_concept": "string, e.g., automation bias",
  "hook": "string, question or contrast, max 15 words",
  "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
  "narration": {{
    "hook": "string",
    "problem": ["line1", "line2"],
    "explain": ["line1", "line2", "line3"],
    "example": ["line1", "line2"],
    "technique": ["line1", "line2", "line3"],
    "ending": "string"
  }},
  "on_screen_text": ["SKILL", "GAP", "TEST", ... 6 labels],
  "visual_direction": "string, e.g., confidence meter, code visual, decision tree",
  "actionable_technique": "string",
  "ending": "string, CTA question",
  "caption": {{"hook": "...", "intro": "...", "sections": [{{"title": "WHAT'S GOING ON", "lines": [...]}}], "hashtags": ["#metacognition", ...]}},
  "claims": [],
  "sources": [{{"label": "...", "url": "", "tier": "A"}}]
}}

Technology relevance required: must be about AI, software, coding, product, digital behavior, future of work.
Metacognition relevance required: must have clear metacognitive concept.
No Persian, no FA, language=en.
"""
        content, raw = call_groq_chat(prompt, producer, max_tokens=2200, temperature=0.7)
        raw["selection"] = report
        return _parse_json_content(content, "Producer", producer), raw


class GroqReviewer(LLMProvider):
    kind = "groq-reviewer"

    def _default_model(self):
        return DEFAULT_REVIEWER_MODEL

    def review(self, producer_output, evidence_packet, _discovered=None, producer_model=None):
        if self.mock_enabled:
            # Mocked reviewer that approves if tech and metacog present — broadened to match tech policy
            # Explicit flag only; real mode without key raises in call_groq_chat.
            ta = producer_output.get("technology_angle","").lower()
            tech_keywords = ["ai","code","coding","software","product","metric","automation","human","debug","bias","research","attention","notification","llm","hallucination","architecture","offload","tutorial"]
            tech = any(k in ta for k in tech_keywords) or "product" in producer_output.get("technology_angle","").lower() or "AI" in producer_output.get("technology_angle","")
            meta_concept = producer_output.get("metacognition_concept","").lower()
            meta = any(k in meta_concept for k in ["bias","metacog","calibration","illusion","offload","planning","fallacy","goodhart","switch","sunk","debug","desirable","skill","tutorial","learning"]) or len(meta_concept)>3
            approved = tech and meta
            mock_review = {
                "approved": approved,
                "score": 88 if approved else 70,
                "technology_relevance": tech,
                "metacognition_relevance": meta,
                "source_grounding": True,
                "unsupported_claims": [],
                "hook_quality": "good",
                "spoken_english_quality": "good",
                "novelty": "high",
                "practical_value": "high",
                "safety": "safe",
                "required_changes": [] if approved else ["Add clearer tech example"],
                "blocking_errors": [] if approved else ["technology_relevance weak"]
            }
            return mock_review, self._mock_raw()

        discovered = _discovered if _discovered is not None else discover_models()[0]
        # Prefer a reviewer different from the producer's model when possible.
        _, reviewer, report = select_models(
            discovered, producer_model or AUTO_MODEL, self.requested_model)
        self.model = reviewer
        self.selection = report

        claim_blob = ", ".join("\u201c" + str(p) + "\u201d"
                               for p in (evidence_packet.get("claim_word_patterns") or [])[:10])
        tier_note = ("Tier A/B evidence present" if evidence_packet.get("has_tier_ab_evidence")
                     else "NO Tier A/B evidence")
        prompt = f"""
You are GroqReviewer for @metacognition.hq — independent, English-only.

Producer output to review:
{json.dumps(producer_output, ensure_ascii=False)[:3000]}

Evidence packet:
Topic: {evidence_packet.get('topic','')}
Technology angle: {evidence_packet.get('technology_angle','')}
Discovery: {evidence_packet.get('discovery_source',{}).get('name','')}
Numeric evidence in packet: {evidence_packet.get('numeric_evidence_note','')}
Recent topics: {', '.join(evidence_packet.get('recent_topics',[]))}

Check:
- technology_relevance: is it about AI, software, coding, product, digital behavior, future of work?
- metacognition_relevance: does it have real metacognitive concept?
- source_grounding: claims have evidence? No fake URL/citation?
- NUMERIC CLAIMS (blocker): every percentage, statistic, study result or precise number in the narration must be EXPLICITLY listed in the numeric evidence above. {evidence_packet.get('numeric_evidence_note','')}. ANY such claim that is not in that list is an unsupported claim: add it to unsupported_claims AND blocking_errors, set source_grounding=false and approved=false. Do NOT approve a number by inventing or adjusting a citation, and do NOT assume a study backs a number.
- CLAIM ATTRIBUTIONS (blocker, issue #22): the deterministic QA source_quality blocker rejects research-attribution wording in the spoken narration whenever the script carries no Tier A/B evidence, and this packet's DERIVED evidence tier is {evidence_packet.get('evidence_tier', '?')} ({tier_note}). The patterns it matches are the policy's own list: {claim_blob} (…), matched word-boundary and case-insensitive — "researchers" alone counts. If the packet has NO Tier A/B evidence, you MUST NOT approve any of that wording: add each matched pattern to unsupported_claims AND blocking_errors, set source_grounding=false and approved=false. A structured approval cannot override the deterministic gate and never grounds a claim by itself — approving ungrounded attribution only burns the one allowed Revision and ends at qa-failed. The valid fix is REMOVING the attribution (rewrite as a direct, appropriately qualified observation); inventing a citation/URL/author/tier to justify it is rejected by the deterministic citation guard.
- hook_quality, spoken_english_quality, novelty, practical_value, safety
- spoken_english_quality: must be natural spoken English — natural contractions present (don't, it's, you'll, that's, can't, let's), short sentences, no line over 20 words, no academic/formal constructions (furthermore, moreover, thus, hence, utilize, in conclusion). If missing, put it in required_changes.
- LENGTH (blocker): add up the ACTUAL spoken words across ALL narration lines. The pipeline's deterministic pre-render gate hard-rejects anything outside {evidence_packet.get('word_range', [150, 260])[0]}-{evidence_packet.get('word_range', [150, 260])[1]} words (target ~{evidence_packet.get('word_target', [175, 210])[0]}-{evidence_packet.get('word_target', [175, 210])[1]}; that is what makes the render fit 60-120 s). A 106-word script for example MUST be rejected: if the narration is clearly outside the range, set approved=false, add "spoken words outside {evidence_packet.get('word_range', [150, 260])[0]}-{evidence_packet.get('word_range', [150, 260])[1]}" to blocking_errors, and put "fix narration word count with real content" into required_changes. Your approval CANNOT override the deterministic count — approving an out-of-range script only burns the one allowed revision. Ignore any word count the producer claims and never accept padding (filler lines, repeated CTA, silence notes) as a fix.
- CONTENT QUALITY (required items): concrete technology context named in the narration; a genuine metacognitive mechanism (not vague self-help); exactly one specific practical exercise; conversational contractions; a strong hook; one concise CTA that does not repeat body sentences. Missing items belong in required_changes.
- No Persian, no FA layer, language=en
- No medical advice, no filler, no 'In today's video'
- One main idea, one actionable technique

Output ONLY valid JSON:
{{
  "approved": true/false,
  "score": 0-100,
  "technology_relevance": true/false,
  "metacognition_relevance": true/false,
  "source_grounding": true/false,
  "unsupported_claims": [],
  "hook_quality": "good/weak",
  "spoken_english_quality": "good/weak",
  "novelty": "high/low",
  "practical_value": "high/low",
  "safety": "safe/unsafe",
  "required_changes": [],
  "blocking_errors": []
}}

Publish requires score>=85, no blocking, tech relevance true, metacog relevance true, no unsupported claims (this includes ANY number/percentage not in the packet's numeric evidence), no fake URL, natural conversational English with contractions, non-duplicate, hook and ending related, and the actual narration word count inside {evidence_packet.get('word_range', [150, 260])[0]}-{evidence_packet.get('word_range', [150, 260])[1]} (counted from the lines, not claimed).
"""
        content, raw = call_groq_chat(prompt, reviewer, max_tokens=1400, temperature=0.3)
        raw["selection"] = report
        return _parse_json_content(content, "Reviewer", reviewer), raw


class StaticEnglishFallback:
    """Fallback using curated English playbooks filtered to tech domain."""

    def __init__(self, policy=None):
        self.policy = policy or common.policy()
        # Import playbooks from content_producer (will be English-only after redesign)
        try:
            import content_producer as cp
            self.playbooks = cp.PLAYBOOKS
            self.calendar_map = cp.CALENDAR_MAP
        except Exception:
            self.playbooks = {}
            self.calendar_map = {}

    def produce(self, evidence_packet, tech_filter=True):
        # Choose a playbook that has tech relevance
        # For simplicity, pick planning-fallacy (software estimation) or overconfidence (calibration)
        # In real fallback, we filter to tech-relevant playbooks
        tech_keys = ["planning-fallacy", "overconfidence", "pre-mortem", "checklists", "superforecasters", "interleaving", "desirable-difficulties"]
        # Find first available
        for key in tech_keys:
            if key in self.playbooks:
                pb = self.playbooks[key]
                # Ensure it has tech angle
                return {
                    "title": pb.get("hook","")[:80],
                    "technology_angle": f"{key} in software/AI context",
                    "metacognition_concept": key,
                    "hook": pb["hook"],
                    "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
                    "narration": {
                        "hook": pb["hook"],
                        "problem": pb["problem"],
                        "explain": pb["explain"],
                        "example": pb["example"],
                        "technique": pb["technique"],
                        "ending": pb["ending"]
                    },
                    "on_screen_text": pb["web"],
                    "visual_direction": "code visual + confidence meter + human-AI network",
                    "actionable_technique": " ".join(pb["technique"][:2]),
                    "ending": pb["ending"],
                    "caption": {
                        "hook": pb["hook"],
                        "intro": " ".join(pb["problem"]),
                        "sections": [
                            {"title": "WHAT'S GOING ON", "lines": pb["explain"]},
                            {"title": "TRY THIS", "lines": pb["technique"]}
                        ],
                        "hashtags": ["#metacognition", "#AI", "#coding"]
                    },
                    "claims": [],
                    "sources": [{"label": "Curated playbook fallback", "url": "", "tier": "B"}],
                    "generation_mode": "static-fallback"
                }
        # If no playbook, minimal fallback
        return {
            "title": "Metacognition for the AI age",
            "technology_angle": "automation bias in AI assistants",
            "metacognition_concept": "automation bias",
            "hook": "When does your AI assistant make you think less?",
            "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
            "narration": {
                "hook": "When does your AI assistant make you think less?",
                "problem": ["You ask AI for code and it answers instantly.", "It feels productive."],
                "explain": ["Fluency feels like understanding.", "That's automation bias.", "The more you use it, the less you check."],
                "example": ["Autocomplete finished your function.", "Did you read it?"],
                "technique": ["Explain AI output before accepting.", "Test one edge case.", "If you can't explain, you haven't learned."],
                "ending": "Where did AI make you skip thinking this week?"
            },
            "on_screen_text": ["ASK AI", "FLUENT", "BIAS", "CHECK", "EXPLAIN", "LEARN"],
            "visual_direction": "code + confidence meter",
            "actionable_technique": "Explain before accept",
            "ending": "Where did AI skip thinking?",
            "caption": {"hook": "When does AI make you think less?", "intro": "Fluency vs understanding", "sections": [], "hashtags": ["#metacognition"]},
            "claims": [],
            "sources": [],
            "generation_mode": "static-fallback"
        }
