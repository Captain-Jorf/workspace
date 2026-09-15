"""Central Groq HTTP transport for @metacognition.hq — English-only production.

WHY THIS MODULE EXISTS
----------------------
`api.groq.com` sits behind Cloudflare. Cloudflare's Browser Integrity Check
rejects a request whose *client signature* looks like a generic script.
Python's stdlib announces itself as `User-Agent: Python-urllib/3.x`, and the
edge answers **HTTP 403 with Cloudflare error code 1010** — "The owner of this
website has banned your access based on your browser's signature" — *before*
the request ever reaches Groq's authentication layer.

Observed in Actions run 35030569096 (`groq-connection-check`, branch main,
run_attempt 3): DNS resolution OK, HTTPS probe OK, then
`model discovery: Groq HTTP 403 ... error code: 1010`. Rotating
`GROQ_API_KEY` cannot fix an edge-level client block, which is exactly why the
same failure repeated after the key was rotated and the Secret updated.

EVERY Groq request in this repository goes through `request()` / `probe()`
below, so the explicit project headers can never be forgotten on one code
path:

  * GET  /openai/v1/models          (model discovery)
  * POST /openai/v1/chat/completions (Producer)
  * POST /openai/v1/chat/completions (Reviewer)
  * POST /openai/v1/chat/completions (revision request)
  * the unauthenticated HTTPS/TLS probe
  * the real connection check (build/groq_check.py)

Headers sent on EVERY request:

    User-Agent: metacognition-hq/1.0 (+https://github.com/Captain-Jorf/workspace)
    Accept: application/json

plus, for authenticated calls:

    Authorization: Bearer <GROQ_API_KEY>        (value is NEVER logged)

and, for calls that carry a JSON body (POST chat completions):

    Content-Type: application/json

`Python-urllib/*` and `python-requests/*` are NOT acceptable User-Agent
values; tests/test_groq_http_client.py enforces the explicit project value
against a local HTTP stub server for all four Groq call sites.

Error taxonomy (safe, no secrets):
    403 + Cloudflare code 1010 -> cloudflare-client-blocked   (NO retry)
    401                        -> invalid-or-missing-api-key  (no retry)
    other 403                  -> permission-or-account-restriction (no retry)
    429                        -> rate-limited (Retry-After honored, bounded)
    5xx / timeout / network    -> upstream-error / network-unreachable (bounded retry)

Safe logging: hostname, HTTP status, attempt numbers and a scrubbed,
truncated error body. Never the key, never the Authorization header, never a
full prompt or a full response body.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402  (path bootstrap above, like every other build module)

# --------------------------------------------------------------------- constants

# The explicit project User-Agent. This is the value that stops Cloudflare's
# Browser Integrity Check from discarding the request at the edge (code 1010).
PROJECT_NAME = "metacognition-hq"
PROJECT_VERSION = "1.0"
PROJECT_URL = "https://github.com/Captain-Jorf/workspace"
PROJECT_USER_AGENT = f"{PROJECT_NAME}/{PROJECT_VERSION} (+{PROJECT_URL})"

# Default User-Agent values that MUST NOT be sent to Groq (blocked at the edge).
FORBIDDEN_USER_AGENT_PREFIXES = ("Python-urllib/", "python-requests/", "curl/", "Go-http-client/")

ACCEPT_JSON = "application/json"
CONTENT_TYPE_JSON = "application/json"

# Error categories (safe, stable identifiers — logged and recorded).
CLOUDFLARE_CLIENT_BLOCKED = "cloudflare-client-blocked"
INVALID_OR_MISSING_API_KEY = "invalid-or-missing-api-key"
PERMISSION_OR_ACCOUNT_RESTRICTION = "permission-or-account-restriction"
RATE_LIMITED = "rate-limited"
UPSTREAM_ERROR = "upstream-error"
NETWORK_UNREACHABLE = "network-unreachable"
INVALID_RESPONSE = "invalid-response"

CLOUDFLARE_ERROR_CODE = 1010

# Retry policy: 1 initial attempt + 2 retries, exponential backoff.
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0
MAX_429_RETRIES = 2
RETRY_AFTER_CAP_SECONDS = 30
REQUEST_TIMEOUT = 60
MODELS_TIMEOUT = 20
PROBE_TIMEOUT = 15
ERROR_BODY_LOG_LIMIT = 200

# --------------------------------------------------------------------- exceptions


class GroqAPIError(RuntimeError):
    """A Groq/edge failure with a SAFE, classified one-line message.

    Attributes:
      category    — stable identifier (see the taxonomy above)
      http_status — int HTTP status, or None for transport-level failures
      cf_code     — Cloudflare error code (int) when one was detected
      retryable   — whether a retry could plausibly help
      attempts    — number of HTTP attempts actually made
    The message never contains the API key, the Authorization header, a full
    prompt or a full response body.
    """

    def __init__(self, category, message, http_status=None, cf_code=None,
                 retryable=False, attempts=1):
        super().__init__(common.scrub_secrets(message))
        self.category = category
        self.http_status = http_status
        self.cf_code = cf_code
        self.retryable = retryable
        self.attempts = attempts


class MissingAPIKeyError(GroqAPIError):
    """GROQ_API_KEY is not available. The value is never part of the message."""

    def __init__(self, message=None):
        super().__init__(
            INVALID_OR_MISSING_API_KEY,
            message or ("GROQ_API_KEY not set — real Groq call impossible "
                        "(mock requires explicit MOCK_GROQ=1)"),
            http_status=None, retryable=False, attempts=0)


# --------------------------------------------------------------------- headers


def get_api_key():
    """Return the Groq API key or ''. Callers must NEVER log the value."""
    return os.environ.get("GROQ_API_KEY", "") or ""


def require_api_key():
    """Raise MissingAPIKeyError when no key is available. Never logs it."""
    key = get_api_key()
    if not key.strip():
        raise MissingAPIKeyError()
    return key


def user_agent():
    """The explicit project User-Agent (never the Python default).

    `GROQ_USER_AGENT` exists only so a test can prove that a *bad* value is
    detected; production and workflows never set it.
    """
    override = os.environ.get("GROQ_USER_AGENT", "").strip()
    return override or PROJECT_USER_AGENT


def is_acceptable_user_agent(value):
    """False for the default Python/curl signatures that Cloudflare blocks."""
    ua = (value or "").strip()
    if not ua:
        return False
    return not ua.startswith(FORBIDDEN_USER_AGENT_PREFIXES)


def build_headers(with_auth=True, with_body=False):
    """Headers for EVERY Groq request.

    Always: explicit project User-Agent + Accept: application/json.
    with_auth: Authorization: Bearer <key> (value never logged anywhere).
    with_body: Content-Type: application/json (POST chat completions).
    """
    headers = {
        "User-Agent": user_agent(),
        "Accept": ACCEPT_JSON,
    }
    if with_auth:
        headers["Authorization"] = f"Bearer {require_api_key()}"
    if with_body:
        headers["Content-Type"] = CONTENT_TYPE_JSON
    return headers


def safe_header_names(headers):
    """Header NAMES only (safe diagnostic). Values are never returned."""
    return sorted((headers or {}).keys())


# --------------------------------------------------------------------- classification

_CF_CODE_RES = (
    re.compile(r"(?i)\berror\s*code\b[^0-9]{0,20}(\d{3,5})"),
    re.compile(r"(?i)cf-code-label[^0-9]{0,40}(\d{3,5})"),
    re.compile(r"(?i)cloudflare[^0-9]{0,60}\b(10\d\d)\b"),
)
_CF_SIGNATURE_RES = (
    re.compile(r"(?i)banned your access based on your browser"),
    re.compile(r"(?i)browser integrity check"),
    re.compile(r"(?i)attention required!\s*cloudflare"),
    re.compile(r"(?i)cloudflare-ray"),
)


def _header_get(headers, name):
    if not headers:
        return ""
    try:
        return str(headers.get(name, "") or "")
    except Exception:  # noqa: BLE001 — defensive, header objects vary
        return ""


def looks_like_cloudflare(headers=None, body=""):
    """True when the response was produced by the Cloudflare edge."""
    server = _header_get(headers, "Server")
    if "cloudflare" in server.lower():
        return True
    if _header_get(headers, "CF-RAY") or _header_get(headers, "cf-ray"):
        return True
    text = body or ""
    return any(rx.search(text) for rx in _CF_SIGNATURE_RES)


def detect_cloudflare_code(body):
    """Extract a Cloudflare error code (e.g. 1010) from an HTML/JSON/text body."""
    text = body or ""
    codes = set()
    # JSON edge payloads: {"errors":[{"code":1010,...}]}
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001 — HTML block page is not JSON
        obj = None
    if isinstance(obj, dict):
        items = []
        for key in ("errors", "error"):
            val = obj.get(key)
            items.extend(val if isinstance(val, list) else [val])
        for item in items:
            if isinstance(item, dict):
                code = item.get("code")
                if isinstance(code, bool):
                    continue
                if isinstance(code, int):
                    codes.add(code)
                elif isinstance(code, str) and code.strip().isdigit():
                    codes.add(int(code.strip()))
    # HTML / plain-text block pages: "Error code: 1010", <span ...>1010</span>
    for rx in _CF_CODE_RES:
        for m in rx.finditer(text):
            codes.add(int(m.group(1)))
    return codes


def classify(status, body="", headers=None):
    """Map an HTTP failure to a (category, cf_code) pair. Never raises."""
    codes = detect_cloudflare_code(body) if status == 403 else set()
    cf_edge = looks_like_cloudflare(headers, body)
    if status == 403 and (CLOUDFLARE_ERROR_CODE in codes or (cf_edge and 1010 in codes)):
        return CLOUDFLARE_CLIENT_BLOCKED, CLOUDFLARE_ERROR_CODE
    if status == 401:
        return INVALID_OR_MISSING_API_KEY, None
    if status == 403:
        return PERMISSION_OR_ACCOUNT_RESTRICTION, (CLOUDFLARE_ERROR_CODE if CLOUDFLARE_ERROR_CODE in codes else None)
    if status == 429:
        return RATE_LIMITED, None
    if isinstance(status, int) and 500 <= status <= 599:
        return UPSTREAM_ERROR, None
    return INVALID_RESPONSE, None


# --------------------------------------------------------------------- safe text


def scrub(text):
    """Central secret scrubber (delegates to common.scrub_secrets)."""
    return common.scrub_secrets(text)


def _read_body(exc, limit=4096):
    """Read an HTTP error body EXACTLY ONCE. Never raises.

    The raw text is used only for classification; anything that reaches a log
    goes through `safe_body()` (scrubbed + truncated) first.
    """
    try:
        raw = exc.read(limit)
    except Exception:  # noqa: BLE001 — unreadable/absent body
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def safe_body(raw, limit=ERROR_BODY_LOG_LIMIT):
    """Scrub + truncate an error body so it is safe to log."""
    return scrub((raw or "")[: max(limit * 4, limit)])[:limit]

def _read_response_body(resp, limit=2048):
    """Read a success-response body defensively (file-like objects vary)."""
    for reader in (lambda: resp.read(limit), lambda: resp.read()):
        try:
            raw = reader()
            if isinstance(raw, bytes):
                return raw.decode("utf-8", "replace")
            return str(raw or "")
        except TypeError:
            continue
        except Exception:  # noqa: BLE001 — unreadable body is not fatal for a probe
            return ""
    return ""


def parse_retry_after(headers):
    """Parse Retry-After (seconds) from response headers. Returns 0 if absent."""
    if not headers:
        return 0
    raw = _header_get(headers, "Retry-After") or _header_get(headers, "retry-after")
    try:
        return max(0, int(str(raw).strip().split(",")[0]))
    except (ValueError, TypeError):
        return 0


def hostname_of(url):
    """Hostname of a URL — safe to log (no secret, no key)."""
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------- transport


def _open(req, timeout):
    """Single urlopen seam. Kept tiny so tests can patch urllib.request.urlopen."""
    return urllib.request.urlopen(req, timeout=timeout)


def request(method, url, payload=None, timeout=REQUEST_TIMEOUT, with_auth=True,
            max_attempts=MAX_ATTEMPTS, max_rate_retries=MAX_429_RETRIES):
    """THE central Groq HTTP call. Every Groq request in the repo uses this.

    - Sends the explicit project User-Agent + Accept on every attempt.
    - Sends Authorization: Bearer <key> when `with_auth` (never logged).
    - Sends Content-Type: application/json when a JSON body is present.
    - 403 + Cloudflare 1010 -> cloudflare-client-blocked, NO retry, raises.
    - 401 -> invalid-or-missing-api-key, no retry.
    - other 403 -> permission-or-account-restriction, no retry.
    - 429 -> rate-limited, Retry-After honored, at most `max_rate_retries`.
    - 5xx / timeout / network -> bounded retry, then raises.

    Returns (parsed_json, meta). `meta` is safe: mock/host/http_status/attempt
    and the request header NAMES that were sent (never their values).
    """
    if with_auth:
        require_api_key()  # fail closed before any socket is opened
    if not is_acceptable_user_agent(user_agent()):
        # A blocked signature would only produce a confusing edge 403/1010.
        raise GroqAPIError(
            CLOUDFLARE_CLIENT_BLOCKED,
            "refusing to send a Groq request with a blocked default User-Agent "
            "signature — set an explicit project User-Agent",
            http_status=None, retryable=False, attempts=0)

    hostname = hostname_of(url)
    has_body = payload is not None
    headers = build_headers(with_auth=with_auth, with_body=has_body)
    data = json.dumps(payload).encode("utf-8") if has_body else None
    header_names = safe_header_names(headers)

    last_err = ""
    rate_retries = 0
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            with _open(req, timeout) as r:
                status = getattr(r, "status", 200)
                try:
                    resp = json.load(r)
                except Exception:  # noqa: BLE001 — non-JSON body on a 2xx
                    raise GroqAPIError(
                        INVALID_RESPONSE,
                        f"Groq invalid response envelope: host={hostname} "
                        f"http_status={status} attempt={attempt}",
                        http_status=status, attempts=attempt)
                return resp, {
                    "mock": False,
                    "host": hostname,
                    "http_status": status,
                    "attempt": attempt,
                    "attempts": attempt,
                    "request_headers": header_names,
                    "user_agent_acceptable": True,
                }
        except urllib.error.HTTPError as e:
            raw_body = _read_body(e)
            body = safe_body(raw_body)
            category, cf_code = classify(e.code, raw_body, e.headers)
            base = (f"Groq HTTP {e.code}: {category} host={hostname} attempt={attempt}")
            if cf_code:
                base += f" cloudflare_code={cf_code}"
            if category == CLOUDFLARE_CLIENT_BLOCKED:
                # Edge-level client block: retrying is pointless and the API key
                # is NOT the cause. No body dump (it is only a block page).
                raise GroqAPIError(
                    CLOUDFLARE_CLIENT_BLOCKED,
                    base + " — blocked at the Cloudflare edge before "
                           "authentication; check User-Agent/header configuration "
                           "(do not rotate the API key for this error)",
                    http_status=e.code, cf_code=cf_code, retryable=False, attempts=attempt)
            if category == INVALID_OR_MISSING_API_KEY:
                raise GroqAPIError(
                    INVALID_OR_MISSING_API_KEY,
                    f"Groq HTTP 401: authentication failed "
                    f"({INVALID_OR_MISSING_API_KEY}) host={hostname} attempt={attempt}"
                    + (f" detail={body}" if body else ""),
                    http_status=401, retryable=False, attempts=attempt)
            if category == PERMISSION_OR_ACCOUNT_RESTRICTION:
                raise GroqAPIError(
                    PERMISSION_OR_ACCOUNT_RESTRICTION,
                    f"Groq HTTP 403: {PERMISSION_OR_ACCOUNT_RESTRICTION} "
                    f"host={hostname} attempt={attempt}"
                    + (f" detail={body}" if body else ""),
                    http_status=403, retryable=False, attempts=attempt)
            if category == RATE_LIMITED:
                rate_retries += 1
                if rate_retries > max_rate_retries or attempt >= max_attempts:
                    raise GroqAPIError(
                        RATE_LIMITED,
                        f"Groq quota 429 exhausted: {RATE_LIMITED} host={hostname} "
                        f"attempts={attempt}" + (f" detail={body}" if body else ""),
                        http_status=429, retryable=True, attempts=attempt)
                wait = parse_retry_after(e.headers) or RETRY_BASE_SECONDS * (2 ** (rate_retries - 1))
                time.sleep(min(wait, RETRY_AFTER_CAP_SECONDS))
                continue
            if category == UPSTREAM_ERROR:
                last_err = f"HTTP {e.code}"
                if attempt < max_attempts:
                    time.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
                    continue
                raise GroqAPIError(
                    UPSTREAM_ERROR,
                    f"Groq HTTP {e.code}: {UPSTREAM_ERROR} host={hostname} "
                    f"attempts={attempt}" + (f" detail={body}" if body else ""),
                    http_status=e.code, retryable=True, attempts=attempt)
            raise GroqAPIError(
                category,
                base + (f" detail={body}" if body else ""),
                http_status=e.code, retryable=False, attempts=attempt)
        except GroqAPIError:
            raise
        except Exception as e:  # noqa: BLE001 — URLError/DNS/timeout/SSL: bounded retry
            last_err = f"{type(e).__name__}"
            if attempt < max_attempts:
                time.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    raise GroqAPIError(
        NETWORK_UNREACHABLE,
        f"Groq call failed: {last_err} host={hostname} attempts={max_attempts}",
        http_status=None, retryable=True, attempts=max_attempts)


def probe(url, timeout=PROBE_TIMEOUT, max_attempts=MAX_ATTEMPTS):
    """Unauthenticated HTTPS/TLS reachability probe using the SAME safe client.

    Sends the explicit project User-Agent + Accept but NO Authorization header,
    so the key is never exposed to an unauthenticated request. Any HTTP status
    (even 401/403/5xx) proves DNS + TLS + edge work.

    Returns (reachable, http_status_or_None, safe_detail).
    """
    hostname = hostname_of(url)
    headers = build_headers(with_auth=False, with_body=False)
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, method="GET", headers=dict(headers))
        try:
            with _open(req, timeout) as r:
                status = getattr(r, "status", 200)
                body = _read_response_body(r)
                category, cf_code = classify(status, body, getattr(r, "headers", None))
                return True, status, _probe_detail(hostname, status, attempt, category, cf_code)
        except urllib.error.HTTPError as e:
            # An HTTP status code IS reachability: the edge/server answered.
            category, cf_code = classify(e.code, _read_body(e), e.headers)
            return True, e.code, _probe_detail(hostname, e.code, attempt, category, cf_code)
        except Exception as e:  # noqa: BLE001 — URLError/timeout/SSL
            last_err = f"{type(e).__name__}"
            if attempt < max_attempts:
                time.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    return False, None, (f"HTTPS probe: FAILED host={hostname} error={last_err} "
                         f"attempts={max_attempts}")


def _probe_detail(hostname, status, attempt, category, cf_code):
    detail = f"HTTPS probe: OK host={hostname} status={status} attempt={attempt}"
    if cf_code:
        detail += f" cloudflare_code={cf_code}"
    if category == CLOUDFLARE_CLIENT_BLOCKED:
        detail += (f" edge_category={category} (unauthenticated probe was blocked "
                   f"at the edge — check the User-Agent/header configuration)")
    return detail
