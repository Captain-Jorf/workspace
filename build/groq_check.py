"""Fail-closed Groq connection check — real mode by default.

Real mode (default): requires GROQ_API_KEY, resolves the official endpoint
hostname, probes HTTPS without credentials, reads authenticated /models,
auto-selects a real producer model and a different reviewer model, then runs
one small non-sensitive Producer request (structured JSON, validated) and one
separate Reviewer request (validated). ANY failure exits nonzero. NEVER falls
back to mock or static.

Mock mode ONLY when explicitly requested (--mock flag or MOCK_GROQ=1):
validates pipeline plumbing with fixtures and exits 0 with an unambiguous
"MOCK MODE" banner. It NEVER prints the real-success line, so a naive grep for
"Groq connection: OK" only matches a genuine real-mode success.

Safe logging: endpoint hostname, model IDs, HTTP status and attempt numbers
only. Never the key, Authorization header, full prompt or full response.
No Buffer, no publishing calls of any kind — this module never imports Buffer
code and performs no publish action.

usage:
  python3 build/groq_check.py [--producer-model ID|auto] [--reviewer-model ID|auto] [--mock]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import llm_provider

EXIT_KEY = 2
EXIT_DNS = 3
EXIT_HTTP = 4
EXIT_MODEL = 5
EXIT_JSON = 6
EXIT_REVIEWER = 7

PRODUCER_REQUIRED_KEYS = ["title", "technology_angle", "metacognition_concept", "hook"]


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


def classify_error(exc):
    """Map an exception to a (exit_code, safe_one_line) pair. Never leaks secrets."""
    msg = common.scrub_secrets(str(exc))
    low = msg.lower()
    if isinstance(exc, ValueError) and ("invalid model" in low or "unavailable" in low or "no models" in low or "none of the" in low):
        return EXIT_MODEL, msg
    if "groq_api_key not set" in low:
        return EXIT_KEY, msg
    if "malformed json" in low or "invalid response envelope" in low or "empty message content" in low:
        return EXIT_JSON, msg
    if "quota 429" in low or "http 4" in low or "http 5" in low or "http " in low:
        return EXIT_HTTP, msg
    if "dns" in low or "name or service not known" in low or "nodename nor servname" in low:
        return EXIT_DNS, msg
    if "gaierror" in low or "urlerror" in low:
        return EXIT_DNS, msg
    return 1, msg


def run_mock(producer_model, reviewer_model):
    """Explicit mock only: plumbing validation, unambiguous banner, exit 0."""
    os.environ["MOCK_GROQ"] = "1"
    packet = build_evidence_packet()
    prod = llm_provider.GroqProducer(model=producer_model)
    out, _ = prod.produce(packet)
    rev = llm_provider.GroqReviewer(model=reviewer_model)
    review_out, _ = rev.review(out, packet)
    print(f"MOCK MODE: pipeline plumbing OK producer={prod.model} reviewer={rev.model}")
    print(f"MOCK MODE: producer_keys={len(out)} reviewer_approved={review_out.get('approved')}")
    print("Mock used: true")
    print("Note: explicit mock only — NOT a real Groq connection.")
    return 0


def run_real(producer_model, reviewer_model):
    """Real connection check. Returns exit code (0 only on full success)."""
    common.assert_content_language_en()
    hostname = llm_provider.get_hostname()
    print(f"Endpoint hostname: {hostname}")
    print(f"producer model requested: {producer_model}")
    print(f"reviewer model requested: {reviewer_model}")

    # 1. Key presence (boolean only — value/length never logged).
    key_present = bool(llm_provider.get_groq_key())
    print(f"GROQ_API_KEY present: {str(key_present).lower()}")
    if not key_present:
        print("Groq connection: FAILED (GROQ_API_KEY missing)")
        print("Mock used: false")
        return EXIT_KEY

    # 2. DNS diagnostic (no credentials).
    dns_ok, dns_detail = llm_provider.resolve_hostname(hostname)
    print(dns_detail)
    if not dns_ok:
        print("Groq connection: FAILED (DNS resolution failed)")
        print("Mock used: false")
        return EXIT_DNS

    # 3. Unauthenticated HTTPS probe (no Authorization header).
    reachable, status, probe_detail = llm_provider.https_probe()
    print(probe_detail)
    if not reachable:
        print("Groq connection: FAILED (HTTPS unreachable)")
        print("Mock used: false")
        return EXIT_HTTP

    # 4. Authenticated /models discovery.
    try:
        discovered, disc_meta = llm_provider.discover_models()
    except Exception as e:  # noqa: BLE001 — fail closed with safe message
        code, safe = classify_error(e)
        print(f"Groq connection: FAILED (model discovery: {safe})")
        print("Mock used: false")
        return code
    print(f"Models discovered: {len(discovered)} (http_status={disc_meta.get('http_status', '?')})")

    # 5+6. Select a real producer and a different reviewer.
    try:
        producer_id, reviewer_id, selection = llm_provider.select_models(
            discovered, producer_model, reviewer_model)
    except ValueError as e:
        print(f"Groq connection: FAILED ({common.scrub_secrets(str(e))})")
        print("Mock used: false")
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
        print(f"Groq connection: FAILED (producer: {safe})")
        print("Mock used: false")
        return code
    if isinstance(raw, dict) and raw.get("mock"):
        print("Groq connection: FAILED (mock output in real mode — refused)")
        print("Mock used: false")
        return 1
    missing = [k for k in PRODUCER_REQUIRED_KEYS if k not in out]
    if missing:
        print(f"Groq connection: FAILED (producer missing keys: {missing})")
        print("Mock used: false")
        return EXIT_JSON
    http_status = raw.get("http_status", "?") if isinstance(raw, dict) else "?"
    attempt = raw.get("attempt", "?") if isinstance(raw, dict) else "?"
    print(f"Producer model: {prod.model}")
    print(f"Producer structured output: OK (http_status={http_status} attempt={attempt})")

    # 8. Real Reviewer request: separate call, validated independently.
    try:
        rev = llm_provider.GroqReviewer(model=reviewer_id)
        review_out, review_raw = rev.review(out, packet, _discovered=discovered,
                                            producer_model=producer_id)
    except Exception as e:  # noqa: BLE001 — reviewer failure is fatal
        code, safe = classify_error(e)
        if code == 1:
            code = EXIT_REVIEWER
        print(f"Groq connection: FAILED (reviewer: {safe})")
        print("Mock used: false")
        return code
    if isinstance(review_raw, dict) and review_raw.get("mock"):
        print("Groq connection: FAILED (mock reviewer output in real mode — refused)")
        print("Mock used: false")
        return EXIT_REVIEWER
    if not isinstance(review_out.get("approved"), bool) or not isinstance(review_out.get("score"), int):
        print("Groq connection: FAILED (reviewer structured output invalid)")
        print("Mock used: false")
        return EXIT_REVIEWER
    r_status = review_raw.get("http_status", "?") if isinstance(review_raw, dict) else "?"
    print(f"Reviewer model: {rev.model}")
    print(f"Reviewer structured output: OK (http_status={r_status})")
    print(f"Reviewer approved: {str(review_out.get('approved')).lower()}")

    # 9. Success — printed ONLY after genuine real Producer+Reviewer calls.
    print("Groq connection: OK")
    print(f"Endpoint hostname: {hostname}")
    print(f"Producer model: {prod.model}")
    print("Producer structured output: OK")
    print(f"Reviewer model: {rev.model}")
    print("Reviewer structured output: OK")
    print(f"Reviewer approved: {str(review_out.get('approved')).lower()}")
    print("Mock used: false")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fail-closed Groq connection check")
    ap.add_argument("--producer-model", default=os.environ.get("PRODUCER_MODEL", llm_provider.DEFAULT_PRODUCER_MODEL))
    ap.add_argument("--reviewer-model", default=os.environ.get("REVIEWER_MODEL", llm_provider.DEFAULT_REVIEWER_MODEL))
    ap.add_argument("--mock", action="store_true",
                    help="Explicit mock only (plumbing test). Default: real mode.")
    args = ap.parse_args(argv)

    mock_requested = bool(args.mock) or llm_provider.is_mock_enabled()
    if mock_requested:
        try:
            return run_mock(args.producer_model, args.reviewer_model)
        except Exception as e:  # noqa: BLE001 — even mock failures are explicit
            print(f"MOCK MODE: FAILED ({common.scrub_secrets(str(e))})")
            return 1
    return run_real(args.producer_model, args.reviewer_model)


if __name__ == "__main__":
    sys.exit(main())
