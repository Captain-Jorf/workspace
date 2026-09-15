"""Fail-closed Groq connection check — real mode by default.

Real mode (default): requires GROQ_API_KEY, resolves the official endpoint
hostname, probes HTTPS without credentials, reads authenticated /models,
auto-selects a real producer model and a different reviewer model, then runs
one small non-sensitive Producer request (structured JSON, validated) and one
separate Reviewer request (validated). EVERY one of those HTTP calls goes
through the single central client in build/groq_http.py, which always sends the
explicit project User-Agent plus Accept/Content-Type. ANY failure exits
nonzero. NEVER falls back to mock or static.

Real success prints exactly this block, in this order, and nothing can print it
except a genuine authenticated run:

    Groq connection: OK
    Endpoint hostname: api.groq.com
    Models discovered: <count>
    Producer model: <id>
    Producer structured output: OK
    Reviewer model: <id>
    Reviewer structured output: OK
    Reviewer approved: true/false
    Mock used: false

Mock mode ONLY when explicitly requested (--mock flag or MOCK_GROQ=1, and never
on a scheduled production run): validates pipeline plumbing with fixtures,
prints a large unambiguous "MOCK MODE" banner and exits 0. It NEVER prints the
real-success line, so a naive grep for "Groq connection: OK" only matches a
genuine real-mode success.

Error classification (safe, from the central client):
    HTTP 403 + Cloudflare code 1010 -> cloudflare-client-blocked, exit 8,
        no retry, no mock, plus a safe hint to check the User-Agent/header
        configuration (and NOT to rotate the key: an edge block happens before
        authentication).
    HTTP 401                        -> invalid-or-missing-api-key
    other HTTP 403                  -> permission-or-account-restriction
    HTTP 429                        -> rate-limited (Retry-After honored)
    5xx / timeout                   -> bounded retry, then red

Safe logging: endpoint hostname, the explicit client User-Agent, model IDs,
HTTP status and attempt numbers only. Never the key (not even its length,
prefix or suffix), never the Authorization header value, never a full prompt or
a full response body. No Buffer, no publishing calls of any kind — this module
never imports Buffer code and performs no publish action.

usage:
  python3 build/groq_check.py [--producer-model ID|auto] [--reviewer-model ID|auto] [--mock]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import groq_http
import llm_provider

EXIT_KEY = 2
EXIT_DNS = 3
EXIT_HTTP = 4
EXIT_MODEL = 5
EXIT_JSON = 6
EXIT_REVIEWER = 7
EXIT_CLOUDFLARE = 8          # HTTP 403 + Cloudflare error code 1010 (edge client block)

PRODUCER_REQUIRED_KEYS = ["title", "technology_angle", "metacognition_concept", "hook"]

# The exact real-success summary. Printed as one contiguous block and ONLY
# after a genuine authenticated /models discovery plus genuine Producer and
# Reviewer chat completions. A mock run can never print it.
SUCCESS_SUMMARY_ORDER = (
    "Groq connection: OK",
    "Endpoint hostname: {hostname}",
    "Models discovered: {models}",
    "Producer model: {producer}",
    "Producer structured output: OK",
    "Reviewer model: {reviewer}",
    "Reviewer structured output: OK",
    "Reviewer approved: {approved}",
    "Mock used: false",
)

# Safe, secret-free guidance for the Cloudflare edge block. It deliberately does
# NOT mention rotating the key as a remedy: a 403/1010 is decided by the client
# signature at the edge, before Groq's authentication layer ever sees the key.
CLOUDFLARE_HINTS = (
    "Hint: HTTP 403 with Cloudflare error code 1010 is an EDGE client block, "
    "not an authentication failure.",
    "Hint: api.groq.com sits behind Cloudflare; the request was discarded because "
    "of the HTTP client signature, before Groq could check any credential.",
    "Hint: verify the User-Agent/Accept/Content-Type header configuration used by "
    "build/groq_http.py (all Groq calls must share that one client).",
    "Hint: expected explicit User-Agent: " + groq_http.PROJECT_USER_AGENT,
    "Hint: default client signatures such as Python-urllib/* or python-requests/* "
    "are blocked and are not acceptable.",
    "Hint: do NOT rotate GROQ_API_KEY for this error, and do NOT enable mock — "
    "this check must stay red until the client headers are fixed.",
)


def build_evidence_packet():
    """Small non-sensitive evidence packet for the connection check."""
    return {
        "topic": "automation bias in AI assistants",
        "technology_angle": "automation bias in AI assistants",
        "discovery_source": {"name": "test", "url": "", "tier": "C"},
        "evidence_source": {"label": "test evidence", "tier": "B"},
        "trusted_excerpt": "Test excerpt about automation bias and metacognition for AI age",
        "recent_topics": [],
        "editorial_policy": {
            "brand": "Metacognition for the AI age",
            "pillars": ["AI_JUDGMENT", "CODING"],
            "tone": "conversational English",
            "length_target": [70, 105],
            "forbidden_openers": ["in today's video"],
        },
    }


CATEGORY_EXIT = {
    groq_http.CLOUDFLARE_CLIENT_BLOCKED: EXIT_CLOUDFLARE,
    groq_http.INVALID_OR_MISSING_API_KEY: EXIT_HTTP,
    groq_http.PERMISSION_OR_ACCOUNT_RESTRICTION: EXIT_HTTP,
    groq_http.RATE_LIMITED: EXIT_HTTP,
    groq_http.UPSTREAM_ERROR: EXIT_HTTP,
    groq_http.NETWORK_UNREACHABLE: EXIT_DNS,
    groq_http.INVALID_RESPONSE: EXIT_JSON,
}


def classify_error(exc):
    """Map an exception to a (exit_code, safe_one_line) pair. Never leaks secrets."""
    msg = common.scrub_secrets(str(exc))
    low = msg.lower()
    # Preferred path: the central client already classified the failure.
    category = getattr(exc, "category", "") or error_category(msg)
    if category == groq_http.CLOUDFLARE_CLIENT_BLOCKED:
        return EXIT_CLOUDFLARE, msg
    if category in CATEGORY_EXIT and not isinstance(exc, ValueError):
        return CATEGORY_EXIT[category], msg
    if isinstance(exc, ValueError) and ("invalid model" in low or "unavailable" in low or "no models" in low or "none of the" in low):
        return EXIT_MODEL, msg
    if "groq_api_key not set" in low:
        return EXIT_KEY, msg
    if "cloudflare-client-blocked" in low or "error code: 1010" in low or "cloudflare_code=1010" in low:
        return EXIT_CLOUDFLARE, msg
    if "malformed json" in low or "invalid response envelope" in low or "empty message content" in low:
        return EXIT_JSON, msg
    if "quota 429" in low or "http 4" in low or "http 5" in low or "http " in low:
        return EXIT_HTTP, msg
    if "dns" in low or "name or service not known" in low or "nodename nor servname" in low:
        return EXIT_DNS, msg
    if "gaierror" in low or "urlerror" in low:
        return EXIT_DNS, msg
    return 1, msg


def error_category(msg):
    """Classify a plain message (already scrubbed) into a safe category."""
    low = (msg or "").lower()
    if "cloudflare-client-blocked" in low or "cloudflare_code=1010" in low or "error code: 1010" in low:
        return groq_http.CLOUDFLARE_CLIENT_BLOCKED
    if "invalid-or-missing-api-key" in low or "http 401" in low:
        return groq_http.INVALID_OR_MISSING_API_KEY
    if "permission-or-account-restriction" in low:
        return groq_http.PERMISSION_OR_ACCOUNT_RESTRICTION
    if "rate-limited" in low or "quota 429" in low:
        return groq_http.RATE_LIMITED
    return ""


def print_failure(reason, extra_hints=()):
    """Print a fail-closed failure block. Never falls back to mock or static."""
    print(f"Groq connection: FAILED ({reason})")
    for line in extra_hints:
        print(line)
    print("Mock used: false")


MOCK_BANNER = (
    "=" * 74,
    "===  MOCK MODE  —  MOCK MODE  —  MOCK MODE  —  MOCK MODE  —  MOCK MODE  ===",
    "===  This is NOT a real Groq connection check. No API call was made.    ===",
    "===  Nothing below may be reported as a real connection result.         ===",
    "=" * 74,
)


def run_mock(producer_model, reviewer_model):
    """Explicit mock only: plumbing validation, unambiguous banner, exit 0.

    Never prints the real-success line, so a grep for "Groq connection: OK"
    only ever matches a genuine real-mode success.
    """
    os.environ["MOCK_GROQ"] = "1"
    for line in MOCK_BANNER:
        print(line)
    packet = build_evidence_packet()
    prod = llm_provider.GroqProducer(model=producer_model)
    out, _ = prod.produce(packet)
    rev = llm_provider.GroqReviewer(model=reviewer_model)
    review_out, _ = rev.review(out, packet)
    print(f"MOCK MODE: pipeline plumbing OK producer={prod.model} reviewer={rev.model}")
    print(f"MOCK MODE: producer_keys={len(out)} reviewer_approved={review_out.get('approved')}")
    print("Groq connection: NOT CHECKED (mock mode — no API call was made)")
    print("Mock used: true")
    print("Note: explicit mock only — NOT a real Groq connection.")
    return 0


def print_success_summary(hostname, models, producer, reviewer, approved):
    """Print the exact real-success summary block (and only that block)."""
    values = {"hostname": hostname, "models": models, "producer": producer,
              "reviewer": reviewer, "approved": str(bool(approved)).lower()}
    for line in SUCCESS_SUMMARY_ORDER:
        print(line.format(**values))


def run_real(producer_model, reviewer_model):
    """Real connection check. Returns exit code (0 only on full success).

    Fail-closed: there is NO mock and NO static fallback anywhere in here. Any
    failure prints a safe one-line reason plus "Mock used: false" and returns a
    nonzero exit code, so the GitHub Actions run goes RED.
    """
    common.assert_content_language_en()
    hostname = llm_provider.get_hostname()
    print(f"Endpoint hostname: {hostname}")
    print(f"producer model requested: {producer_model}")
    print(f"reviewer model requested: {reviewer_model}")
    # The client signature is safe to log (it carries no credential) and it is
    # the single most useful line when the Cloudflare edge rejects a client.
    print(f"Client User-Agent: {groq_http.user_agent()}")
    # Header NAMES only. The credential header is deliberately never named or
    # printed here, so no grep over a run log can ever land next to a value.
    print(f"Request header names (unauthenticated): "
          f"{groq_http.safe_header_names(groq_http.build_headers(with_auth=False))}")
    print("Request headers (authenticated): the same two plus one credential "
          "header, and one JSON content-type header on POST — values never logged")

    # 1. Key presence (boolean only — value/length/prefix never logged).
    key_present = bool(llm_provider.get_groq_key())
    print(f"GROQ_API_KEY present: {str(key_present).lower()}")
    if not key_present:
        print_failure("GROQ_API_KEY missing")
        return EXIT_KEY

    # 2. DNS diagnostic (no credentials).
    dns_ok, dns_detail = llm_provider.resolve_hostname(hostname)
    print(dns_detail)
    if not dns_ok:
        print_failure("DNS resolution failed")
        return EXIT_DNS

    # 3. Unauthenticated HTTPS probe — same central client, explicit User-Agent,
    #    and still NO Authorization header (the key is never sent unauthenticated).
    reachable, status, probe_detail = llm_provider.https_probe()
    print(probe_detail)
    if not reachable:
        print_failure("HTTPS unreachable")
        return EXIT_HTTP

    # 4. Authenticated /models discovery through the central client.
    try:
        discovered, disc_meta = llm_provider.discover_models()
    except Exception as e:  # noqa: BLE001 — fail closed with safe message
        code, safe = classify_error(e)
        print_failure(f"model discovery: {safe}",
                      CLOUDFLARE_HINTS if code == EXIT_CLOUDFLARE else ())
        return code
    print(f"Model discovery: {len(discovered)} model(s) "
          f"(http_status={disc_meta.get('http_status', '?')})")

    # 5+6. Select a real producer and a different reviewer.
    try:
        producer_id, reviewer_id, selection = llm_provider.select_models(
            discovered, producer_model, reviewer_model)
    except ValueError as e:
        print_failure(common.scrub_secrets(str(e)))
        return EXIT_MODEL
    print(f"Model selection: producer={producer_id} reviewer={reviewer_id}")
    print(f"Model selection reason: {selection['reason']}")
    print(f"Model selection at: {selection['selected_at_utc']}")

    # 7. Real Producer request: small, non-sensitive, structured JSON.
    packet = build_evidence_packet()
    try:
        prod = llm_provider.GroqProducer(model=producer_id)
        out, raw = prod.produce(packet, _discovered=discovered)
    except Exception as e:  # noqa: BLE001 — fail closed with safe message
        code, safe = classify_error(e)
        print_failure(f"producer: {safe}",
                      CLOUDFLARE_HINTS if code == EXIT_CLOUDFLARE else ())
        return code
    if isinstance(raw, dict) and raw.get("mock"):
        print_failure("mock output in real mode — refused")
        return 1
    missing = [k for k in PRODUCER_REQUIRED_KEYS if k not in out]
    if missing:
        print_failure(f"producer missing keys: {missing}")
        return EXIT_JSON
    p_status = raw.get("http_status", "?") if isinstance(raw, dict) else "?"
    p_attempt = raw.get("attempt", "?") if isinstance(raw, dict) else "?"
    print(f"Producer probe: structured JSON OK (http_status={p_status} attempt={p_attempt})")

    # 8. Real Reviewer request: separate call, validated independently.
    try:
        rev = llm_provider.GroqReviewer(model=reviewer_id)
        review_out, review_raw = rev.review(out, packet, _discovered=discovered,
                                            producer_model=producer_id)
    except Exception as e:  # noqa: BLE001 — reviewer failure is fatal
        code, safe = classify_error(e)
        if code == 1:
            code = EXIT_REVIEWER
        print_failure(f"reviewer: {safe}",
                      CLOUDFLARE_HINTS if code == EXIT_CLOUDFLARE else ())
        return code
    if isinstance(review_raw, dict) and review_raw.get("mock"):
        print_failure("mock reviewer output in real mode — refused")
        return EXIT_REVIEWER
    if not isinstance(review_out.get("approved"), bool) or not isinstance(review_out.get("score"), int):
        print_failure("reviewer structured output invalid")
        return EXIT_REVIEWER
    r_status = review_raw.get("http_status", "?") if isinstance(review_raw, dict) else "?"
    print(f"Reviewer probe: structured JSON OK (http_status={r_status})")

    # 9. Success summary — printed ONLY after genuine real Producer+Reviewer
    #    calls, and exactly in the documented order.
    print_success_summary(hostname, len(discovered), prod.model, rev.model,
                          review_out.get("approved"))
    return 0


def mock_allowed_in_production_ci():
    """False on a scheduled (cron) GitHub Actions run — mock is banned there."""
    return not (os.environ.get("GITHUB_ACTIONS") == "true"
                and os.environ.get("GITHUB_EVENT_NAME") == "schedule")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fail-closed Groq connection check")
    ap.add_argument("--producer-model", default=os.environ.get("PRODUCER_MODEL", llm_provider.DEFAULT_PRODUCER_MODEL))
    ap.add_argument("--reviewer-model", default=os.environ.get("REVIEWER_MODEL", llm_provider.DEFAULT_REVIEWER_MODEL))
    ap.add_argument("--mock", action="store_true",
                    help="Explicit mock only (plumbing test). Default: real mode.")
    args = ap.parse_args(argv)

    mock_requested = bool(args.mock) or llm_provider.is_mock_enabled()
    if mock_requested and not mock_allowed_in_production_ci():
        # A scheduled production run can never validate plumbing with fixtures
        # and report it as a connection result.
        print("MOCK MODE refused: scheduled production runs can never use mock "
              "— running the real fail-closed check instead")
        os.environ.pop("MOCK_GROQ", None)
        mock_requested = False
    if mock_requested:
        try:
            return run_mock(args.producer_model, args.reviewer_model)
        except Exception as e:  # noqa: BLE001 — even mock failures are explicit
            print(f"MOCK MODE: FAILED ({common.scrub_secrets(str(e))})")
            return 1
    return run_real(args.producer_model, args.reviewer_model)


if __name__ == "__main__":
    sys.exit(main())
