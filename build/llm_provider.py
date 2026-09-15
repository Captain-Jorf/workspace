"""LLM Provider Interface for @metacognition.hq — English-only, Technology × Metacognition.

Providers:
- GitHubModelsProducer: uses GitHub Models (GITHUB_TOKEN, models: read permission)
- GitHubModelsReviewer: independent reviewer, preferably different model
- StaticEnglishFallback: curated English playbooks filtered to tech domain

No billing, no paid usage, no external secret. Uses secrets.GITHUB_TOKEN.

Max daily requests: 1 producer, 1 reviewer, 1 revision, 1 final reviewer.
Quota/429/outage → limited retry then static fallback.

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
common.assert_content_language_en()  # fail-closed EN-only

# Official GitHub Models inference endpoint (OpenAI-compatible).
# Docs: https://docs.github.com/en/rest/models/inference
#   POST https://models.github.ai/inference/chat/completions
# NOTE (2026-09): docs at /en/github-models state the service was retired on
# 2026-07-30. The legacy Azure hostname no longer resolves (DNS Errno -2) and
# MUST NOT be used. Single official endpoint below; fail closed on any error.
GITHUB_MODELS_HOSTNAME = "models.github.ai"
GITHUB_MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"

# Test-only override (local HTTP stub servers). Never set in workflows.
_TEST_ENDPOINT_ENV = "GITHUB_MODELS_ENDPOINT"

# Retry policy for transient network/DNS failures: 1 initial + 2 retries.
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0
REQUEST_TIMEOUT = 60

# Supported model IDs (publisher/name). Validated strictly: unknown IDs raise
# instead of silently substituting another model (fail closed).
# Last verified against the public catalog before its retirement; the IDs below
# were the documented chat models for Producer/Reviewer use.
SUPPORTED_MODELS = [
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/gpt-4o-mini-2024-07-18",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-70b-instruct",
    "mistral-ai/mistral-large-2407",
    "mistral-ai/mistral-small-2503",
    "microsoft/phi-4",
    "cohere/command-r-plus",
    "deepseek/deepseek-v3-0324",
    "deepseek/deepseek-r1",
]

DEFAULT_PRODUCER_MODEL = os.environ.get("PRODUCER_MODEL", "openai/gpt-4o-mini")
DEFAULT_REVIEWER_MODEL = os.environ.get("REVIEWER_MODEL", "meta/llama-3.3-70b-instruct")

# ---------------------------------------------------------------- fail-closed helpers

def is_mock_enabled():
    """Mock is allowed ONLY with the explicit flag MOCK_GITHUB_MODELS=1.

    Missing token, DNS failure, HTTP error, invalid model or malformed JSON must
    NEVER silently switch to mock in real mode. Default: False.
    """
    return os.environ.get("MOCK_GITHUB_MODELS") == "1"


def get_endpoint():
    """Official endpoint URL. Test-only override via GITHUB_MODELS_ENDPOINT."""
    override = os.environ.get(_TEST_ENDPOINT_ENV, "").strip()
    return override or GITHUB_MODELS_ENDPOINT


def get_hostname():
    """Hostname of the endpoint in use. Safe to log (no secret, no token)."""
    try:
        return urllib.parse.urlparse(get_endpoint()).hostname or GITHUB_MODELS_HOSTNAME
    except Exception:
        return GITHUB_MODELS_HOSTNAME


def get_token():
    """Return the bearer token or ''. Callers must NEVER log its value."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def require_real_token():
    """Raise (nonzero in workflows) when no token is available. Never logs it."""
    token = get_token()
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN not set — real GitHub Models call impossible "
            "(mock requires explicit MOCK_GITHUB_MODELS=1)"
        )
    return token


def validate_model_id(model):
    """Strict model validation: unknown IDs raise ValueError (fail closed)."""
    if model not in SUPPORTED_MODELS:
        raise ValueError(
            f"Invalid model ID: {model!r} — not in SUPPORTED_MODELS "
            f"({len(SUPPORTED_MODELS)} known IDs)"
        )
    return model


def resolve_hostname(hostname=None, timeout=10):
    """DNS diagnostic without credentials. Returns (ok, detail).

    Retries transient failures up to 2 times with exponential backoff.
    Only the hostname (safe) is ever reported, never IPs or tokens.
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


def https_probe(timeout=15):
    """Unauthenticated HTTPS/TLS reachability check (no Authorization header).

    Any HTTP response (even 4xx/5xx) proves DNS+TLS work. Returns
    (reachable, http_status_or_None, detail). Safe to log.
    """
    endpoint = get_endpoint()
    hostname = get_hostname()
    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(endpoint, method="GET")  # no auth header
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return True, r.status, f"HTTPS probe: OK host={hostname} status={r.status} attempt={attempt}"
            except urllib.error.HTTPError as e:
                # An HTTP status code IS reachability (server answered).
                return True, e.code, f"HTTPS probe: OK host={hostname} status={e.code} attempt={attempt}"
        except Exception as e:  # noqa: BLE001 — URLError/timeout/SSL
            last_err = f"{type(e).__name__}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    return False, None, f"HTTPS probe: FAILED host={hostname} error={last_err} attempts={MAX_ATTEMPTS}"

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

def build_evidence_packet(topic, policy, recent_topics=None):
    """Build limited sanitized evidence packet for LLM producer."""
    cal = topic.get("calendar") or {}
    discovery = topic.get("discovery_source") or {}
    # Trusted excerpt from calendar or discovery (sanitized)
    trusted_excerpt = ""
    if cal.get("beats"):
        trusted_excerpt = "; ".join(cal.get("beats", [])[:3])
    if discovery.get("title"):
        trusted_excerpt += f" | Discovery: {sanitize_untrusted(discovery.get('title',''), 200)}"

    packet = {
        "topic": sanitize_untrusted(topic.get("title",""), 200),
        "technology_angle": sanitize_untrusted(topic.get("technology_angle") or topic.get("pillar",""), 200),
        "discovery_source": {
            "name": sanitize_untrusted(discovery.get("name",""), 100),
            "url": discovery.get("url","")[:300],
            "tier": discovery.get("tier",""),
        },
        "evidence_source": {
            "label": sanitize_untrusted((cal.get("sources") or [""])[0][:200] if cal.get("sources") else "", 200),
            "tier": "A" if cal else "C",
        },
        "trusted_excerpt": sanitize_untrusted(trusted_excerpt, 600),
        "allowed_claims": policy.get("source_policy", {}).get("require_evidence_for_claim_words", [])[:10],
        "unsupported_claims": ["no fake stats", "no invented citation", "no medical advice"],
        "recent_topics": [sanitize_untrusted(t, 100) for t in (recent_topics or [])[:5]],
        "editorial_policy": {
            "brand": policy.get("page", {}).get("brand_positioning",""),
            "pillars": list(policy.get("pillars", {}).keys())[:6],
            "tone": policy.get("tone", {}).get("style",""),
            "length_target": policy.get("length", {}).get("target_seconds", [70,105]),
            "forbidden_openers": policy.get("tone", {}).get("forbidden_openers", [])[:5],
        }
    }
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

def call_github_models(prompt, model, max_tokens=1200, temperature=0.7, timeout=60):
    """Real GitHub Models call using GITHUB_TOKEN. Fail-closed, never mock.

    Returns (content, raw_meta). Raises on: missing token, invalid model,
    DNS/network failure (after 2 retries), HTTP error status, invalid envelope.
    Error messages contain only safe fields: hostname, model ID, HTTP status,
    attempt count. Never the token, Authorization header, prompt or response.
    """
    token = require_real_token()
    validate_model_id(model)
    url = get_endpoint()
    hostname = get_hostname()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant for @metacognition.hq, English-only, technology×metacognition. Output valid JSON only."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    data = json.dumps(body).encode("utf-8")

    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = getattr(r, "status", 200)
                try:
                    resp = json.load(r)
                except Exception:
                    raise RuntimeError(
                        f"GitHub Models invalid response envelope: host={hostname} "
                        f"model={model} http_status={status} attempt={attempt}"
                    )
                try:
                    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                except Exception:
                    content = ""
                if not isinstance(content, str) or not content:
                    raise RuntimeError(
                        f"GitHub Models empty message content: host={hostname} "
                        f"model={model} http_status={status} attempt={attempt}"
                    )
                meta = {"mock": False, "model": model, "host": hostname,
                        "http_status": status, "attempt": attempt,
                        "content_len": len(content)}
                if isinstance(resp, dict):
                    meta["envelope"] = resp
                return content, meta
        except urllib.error.HTTPError as e:
            # HTTP status received: fail immediately, no retry (fail closed).
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            detail = common.scrub_secrets(detail)
            if e.code == 429:
                raise RuntimeError(
                    f"GitHub Models quota 429: host={hostname} model={model} "
                    f"attempt={attempt} detail={detail}"
                )
            raise RuntimeError(
                f"GitHub Models HTTP {e.code}: host={hostname} model={model} "
                f"attempt={attempt} detail={detail}"
            )
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001 — URLError/DNS/timeout/SSL: retry 2x
            last_err = f"{type(e).__name__}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    raise RuntimeError(
        f"GitHub Models call failed: {last_err} host={hostname} model={model} "
        f"attempts={MAX_ATTEMPTS}"
    )

class GitHubModelsProducer:
    def __init__(self, model=None):
        self.model = model or DEFAULT_PRODUCER_MODEL
        # Strict validation: unknown model IDs raise (fail closed, no silent
        # substitution — a wrong model must never silently become another one).
        validate_model_id(self.model)

    def produce(self, evidence_packet):
        prompt = f"""
You are GitHubModelsProducer for @metacognition.hq — Metacognition for the AI age.

Brand: {evidence_packet['editorial_policy']['brand']}
Pillars: {', '.join(evidence_packet['editorial_policy']['pillars'])}

Evidence packet (sanitized, untrusted web content already filtered):
Topic: {evidence_packet.get('topic','')}
Technology angle: {evidence_packet.get('technology_angle','')}
Discovery: {evidence_packet.get('discovery_source',{}).get('name','')} ({evidence_packet.get('discovery_source',{}).get('url','')})
Evidence: {evidence_packet.get('evidence_source',{}).get('label','')}
Excerpt: {evidence_packet.get('trusted_excerpt','')}
Recent topics to avoid: {', '.join(evidence_packet.get('recent_topics',[]))}
Allowed claims must have evidence, forbidden: {', '.join(evidence_packet.get('unsupported_claims', ['no fake stats']))}

Task: Create English-only reel script JSON for 70-105 sec (max 120), conversational English, strong hook in 3 sec, no 'In today's video', no filler, no fake stats/citation, no medical advice, one main idea, one tech example, one practical technique, network visual relevant to tech, natural CTA.

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
        # Mock ONLY with the explicit flag. Missing token in real mode raises
        # (fail closed) instead of silently returning fixture content.
        if is_mock_enabled():
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
            return mock_response, {"mock": True, "model": self.model}

        content, raw = call_github_models(prompt, self.model, max_tokens=1500, temperature=0.7)
        # Try to parse JSON from content (may have markdown fences)
        try:
            # Remove ```json fences if present
            m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
            if m:
                content = m.group(1)
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("top-level JSON is not an object")
            return parsed, raw
        except Exception as e:
            # Safe error: type + length only, never the model response text.
            raise RuntimeError(
                f"Producer returned malformed JSON: {type(e).__name__} "
                f"model={self.model} content_len={len(content)}"
            )

class GitHubModelsReviewer:
    def __init__(self, model=None):
        self.model = model or DEFAULT_REVIEWER_MODEL
        # Strict validation: unknown model IDs raise (fail closed).
        validate_model_id(self.model)

    def review(self, producer_output, evidence_packet):
        prompt = f"""
You are GitHubModelsReviewer for @metacognition.hq — independent, English-only.

Producer output to review:
{json.dumps(producer_output, ensure_ascii=False)[:3000]}

Evidence packet:
Topic: {evidence_packet.get('topic','')}
Technology angle: {evidence_packet.get('technology_angle','')}
Discovery: {evidence_packet.get('discovery_source',{}).get('name','')}
Recent topics: {', '.join(evidence_packet.get('recent_topics',[]))}

Check:
- technology_relevance: is it about AI, software, coding, product, digital behavior, future of work?
- metacognition_relevance: does it have real metacognitive concept?
- source_grounding: claims have evidence? No fake URL/citation?
- hook_quality, spoken_english_quality, novelty, practical_value, safety
- No Persian, no FA layer, language=en
- No medical advice, no filler, no 'In today's video'

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

Publish requires score>=85, no blocking, tech relevance true, metacog relevance true, no unsupported claims, no fake URL, non-duplicate, hook and ending related.
"""
        if is_mock_enabled():
            # Mocked reviewer that approves if tech and metacog present — broadened to match tech policy
            # Explicit flag only; real mode without token raises in call_github_models.
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
            return mock_review, {"mock": True, "model": self.model}

        content, raw = call_github_models(prompt, self.model, max_tokens=800, temperature=0.3)
        try:
            m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
            if m:
                content = m.group(1)
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("top-level JSON is not an object")
            return parsed, raw
        except Exception as e:
            # Safe error: type + length only, never the model response text.
            raise RuntimeError(
                f"Reviewer returned malformed JSON: {type(e).__name__} "
                f"model={self.model} content_len={len(content)}"
            )

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
