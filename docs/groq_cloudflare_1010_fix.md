# Groq `HTTP 403 / Cloudflare error code 1010` — root cause and fix

Status: **fixed in code**, verified against a local HTTP stub that emulates the
Cloudflare edge. No real post was created, nothing was published, and
`GROQ_API_KEY` was **not** rotated for this fix.

Failing run: [`groq-connection-check` #35030569096](https://github.com/Captain-Jorf/workspace/actions/runs/35030569096)
(branch `main`, `workflow_dispatch`, `run_attempt: 3`, conclusion `failure`,
failed step: *"Groq connection check (fail-closed, no mock or static fallback)"*).

```text
Endpoint hostname: api.groq.com
GROQ_API_KEY present: true
DNS resolution: OK
HTTPS probe: OK
Groq connection: FAILED
model discovery: Groq HTTP 403
authentication failed
error code: 1010
Mock used: false
```

---

## 1. What was actually inspected

| Question | Answer from the code/run |
| --- | --- |
| Exact hostname | `api.groq.com` (`GROQ_HOSTNAME` in `build/llm_provider.py`, base `https://api.groq.com/openai/v1`) |
| HTTP client | **stdlib `urllib.request`** — `urllib.request.Request(...)` + `urlopen(...)`. No `requests`, no `httpx`, no official Groq SDK, no `groq` dependency in `requirements.txt` |
| Was a `User-Agent` sent? | **No.** `groq_request()` set only `Authorization` and `Content-Type`, so `http.client` appended its own default: `User-Agent: Python-urllib/3.11`. `https_probe()` sent **no headers at all** |
| `Accept` on GET? | **No** |
| Do `/models` and chat completion share one HTTP path? | **Yes** — both went through `llm_provider.groq_request()`. The unauthenticated probe was the *only* outlier, building its own bare `Request` |
| How is the error body sanitized? | `_read_error_body()` → `common.scrub_secrets(e.read()[:200])`. It worked: no credential reached the log |
| Is `1010` Groq's JSON error or Cloudflare's? | **Cloudflare's.** It came from the edge HTML block page (`Error code: 1010` / `cf-code-label`), not from a Groq JSON envelope. Groq's own auth error is HTTP **401** with `{"error":{"type":"authentication_error","code":"invalid_api_key"}}` |

## 2. Root cause

`api.groq.com` sits behind **Cloudflare**. Cloudflare's Browser Integrity Check
inspects the client signature — and the cheapest fingerprint is the
`User-Agent` header. A request announcing `Python-urllib/3.x` is discarded at
the edge with:

> **HTTP 403 — Error code: 1010** — "The owner of this website has banned your
> access based on your browser's signature."

The block happens **before** the request reaches Groq's authentication layer.
That explains every observation in the failing run:

* `DNS resolution: OK` and `HTTPS probe: OK` — the edge answered, and the probe
  treated *any* HTTP status (here `403`) as reachability, so it reported OK.
* `GROQ_API_KEY present: true` — the Secret was correctly configured.
* `HTTP 403 ... error code: 1010` — edge block, not a credential problem.
* **Rotating the key changed nothing** — a new key is never evaluated when the
  request is dropped at the edge. This is the decisive evidence.
* The old message said `authentication failed` for *any* 403, which sent the
  diagnosis in exactly the wrong direction.

This is not Groq-specific: any Cloudflare-fronted API returns 1010 to a
default-signature stdlib client.

## 3. The fix

### 3.1 One central, testable HTTP client — `build/groq_http.py` (new)

Every Groq request in the repository now goes through `groq_http.request()`
(and `groq_http.probe()` for the unauthenticated reachability probe):

* `GET /openai/v1/models` — model discovery
* `POST /openai/v1/chat/completions` — Producer
* `POST /openai/v1/chat/completions` — Reviewer
* `POST /openai/v1/chat/completions` — revision request
* the unauthenticated HTTPS/TLS probe
* the real connection check (`build/groq_check.py`)

Headers sent on **every** request:

```http
User-Agent: metacognition-hq/1.0 (+https://github.com/Captain-Jorf/workspace)
Accept: application/json
```

plus, for authenticated calls:

```http
Authorization: Bearer <GROQ_API_KEY>
```

and, for calls with a JSON body (POST chat completions):

```http
Content-Type: application/json
```

`Python-urllib/*` and `python-requests/*` are explicitly rejected:
`groq_http.is_acceptable_user_agent()` returns `False` for them, and
`request()` **refuses to open a socket** with a blocked signature instead of
producing another confusing 403/1010.

**No dependency was added.** The existing stdlib client was the problem, not
the absence of a library, so `requirements.txt` is unchanged (no `requests`,
no `httpx`, no `groq` SDK). `tests/test_groq_http_client.py` asserts that.

The probe keeps its safety property: it sends the project `User-Agent` but
**never** an `Authorization` header, so the key is not exposed on an
unauthenticated request.

### 3.2 Safe error taxonomy

| Condition | Category | Behaviour |
| --- | --- | --- |
| HTTP 403 + Cloudflare code 1010 | `cloudflare-client-blocked` | **no retry**, connection check goes red (exit `8`), safe hint about the User-Agent/header configuration and an explicit "do **not** rotate the key", key never printed, **no mock** |
| HTTP 401 | `invalid-or-missing-api-key` | no retry, red |
| any other HTTP 403 | `permission-or-account-restriction` | no retry, red |
| HTTP 429 | `rate-limited` | `Retry-After` honored (capped at 30 s), at most 2 retries, then red |
| HTTP 5xx / timeout / network | `upstream-error` / `network-unreachable` | bounded retry (1 + 2), connection check red; the daily pipeline may fall back to `static-fallback` |
| an HTML **page** where JSON was expected (any status, including 2xx) | `edge-html-response` | bounded retry (1 + 2), then red; the message says plainly that the edge returned a page and that the API key is **not** the cause; **no mock** |
| HTTP 200 with `content == ""` on a reasoning model | *(not an HTTP error — see §7.1)* | exactly one bounded retry with a larger completion budget, then red |

Credential- and block-specific statuses are matched **before** the HTML-page
case, so a Cloudflare 403/1010 can never be softened into something retryable.

Detection reads the code from either shape: the Cloudflare HTML block page
(`Error code: 1010`, `cf-code-label`) and a JSON edge envelope
(`{"errors":[{"code":1010}]}`). A genuine Groq 403 (e.g. code `1020`) is *not*
misclassified as an edge block.

### 3.3 Secret-leak prevention — `common.scrub_secrets()` hardened

The central scrubber now redacts, in order:

1. the exact value of any credential-bearing env var (`GROQ_API_KEY`,
   `BUFFER_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`, …) **and identifiable fragments
   of it** (12–16 char windows), so a partially copied key cannot survive;
2. `Bearer <token>` shapes;
3. provider key shapes (`gsk_…`);
4. `api_key=` / `access_token:` style assignments;
5. long opaque runs (≥ 40 chars).

Only the *name* of the redacted variable is kept — never the value, never its
length, never a real prefix/suffix. Presence is reported as the single line
`GROQ_API_KEY present: true`. Compiled fragment patterns are cached by the
SHA-256 of the value; the value itself is never stored.

Scrubbing is applied at every sink: stdout, stderr, exception messages,
tracebacks, `output/auto-*_state.json`, `producer_report.json`
(`error` / `error_category`), pipeline logs and issue bodies. The connection
check prints only header **names** and never the word `Authorization`, so no
grep over a run log can land next to a credential value.

### 3.4 `build/groq_check.py`

* new exit code `EXIT_CLOUDFLARE = 8` for `cloudflare-client-blocked`;
* classification now prefers the structured category from the central client
  and keeps the previous string matching as a fallback;
* the client signature is printed (`Client User-Agent: …`) — safe, and the most
  useful line when the edge rejects a client;
* the real-success output is a single contiguous block, in this exact order:

```text
Groq connection: OK
Endpoint hostname: api.groq.com
Models discovered: <count>
Producer model: <id>
Producer structured output: OK
Reviewer model: <id>
Reviewer structured output: OK
Reviewer approved: true/false
Mock used: false
```

* mock mode prints a large `MOCK MODE` banner, prints
  `Groq connection: NOT CHECKED (mock mode — no API call was made)`, makes no
  network call at all, and can never print the real-success line;
* a **scheduled** production run (`GITHUB_ACTIONS=true` +
  `GITHUB_EVENT_NAME=schedule`) refuses mock outright and runs the real check.

### 3.5 `.github/workflows/groq-connection-check.yml`

Unchanged guarantees: `workflow_dispatch` only, `timeout-minutes: 5`,
top-level `permissions: {}`, job `permissions: {contents: read}`,
`GROQ_API_KEY` in exactly one step, no `BUFFER_TOKEN`, no Buffer/`createPost`
code, no default mock.

Added:

* an **offline client-signature self-check** step that runs *before* the secret
  is in scope and asserts the explicit project `User-Agent`, `Accept` and
  `Content-Type` (and that `Python-urllib/*` / `python-requests/*` are
  rejected);
* `unset MOCK_GROQ || true` in the check step, so only an explicit
  `mock=true` dispatch input can select mock.

### 3.6 Honest labels in the daily path

`build/content_producer.py` now scrubs every provider error before it reaches
stdout or `producer_report.json`, records `error_category`, and — when the
script that was actually built came from the curated playbooks — downgrades
`producer_report.mode` from `groq` to `groq-failed` / `groq-rejected`.
`generation_mode=groq` is still only ever written after a genuine API
response; every failure path records `generation_mode=static-fallback`.

A revision request now carries the reviewer's `required_changes` into the
Producer prompt (sanitized), so the single allowed revision is a real revision
rather than a blind regeneration — and it is a distinguishable, UA-bearing
request in the tests.

## 4. Tests

```bash
python3 -m unittest discover -s tests          # 205 tests, 0 failures, 0 errors
```

`tests/test_groq_http_client.py` (39 new tests) drives a **local HTTP stub
server** that emulates the Cloudflare edge — including a faithful
`Error code: 1010` block page with `Server: cloudflare` + `CF-RAY`. It covers:

* the explicit project `User-Agent` on `GET /models`, Producer, Reviewer and
  the revision request (recorded per request, asserted exactly);
* the unauthenticated probe sends the project UA and **no** credential;
* `Authorization` present on authenticated calls, and its value absent from
  stdout, stderr, exception strings and tracebacks;
* `Accept: application/json` on GET, `Content-Type: application/json` on POST;
* a raw-urllib reproduction proving the same stub answers 403/1010 without the
  project UA and 200 with it;
* 403+1010 → `cloudflare-client-blocked`, **exactly one** request (no retry),
  exit code 8, safe hint, `Mock used: false`, no `Groq connection: OK`;
* the JSON-shaped 1010 envelope classified identically;
* 401 → `invalid-or-missing-api-key`; other 403 → `permission-or-account-restriction`;
* 429 → `Retry-After` honored, 1 + 2 attempts max;
* 5xx and timeout → bounded retries, then red;
* the connection check never falls back to mock across six failure modes;
* mock needs an explicit flag, makes no network call, and is refused on a
  scheduled production run;
* the daily pipeline records `static-fallback` only on a Cloudflare block,
  never `generation_mode=groq`, and still records `groq` on genuine success;
* no Buffer/`createPost` import or call in the check path;
* Issue #14 quarantine (`reel-2026-09-15` / `2026-09-15`) preserved;
* `AUTO_PUBLISH_ENABLED` untouched — still `vars.AUTO_PUBLISH_ENABLED`, still
  exact-string `"true"`, and nothing in `build/` or the workflows writes it;
* scrubber unit tests for the full key value and identifiable fragments;
* no new third-party HTTP dependency.

## 5. What was deliberately NOT done in this change

* no Buffer `createPost`, no Buffer mutation, nothing added to a queue;
* no Instagram publish;
* no real post or real content artifact was created in this session;
* the PR is opened but **not merged**;
* `AUTO_PUBLISH_ENABLED` was not touched;
* `daily-trend-draft` was not run;
* `GROQ_API_KEY` was **not** rotated and was never printed or retrieved;
* the real connection check was **not** replaced by a mock.

## 7. Two further failure modes found by live verification on the real API

Fixing 403/1010 was necessary but not sufficient. Once the real check could
actually reach Groq (same key, never rotated), two **different** intermittent
failures appeared. Both were captured from live runs on the session branch and
both are now handled explicitly, with tests.

### 7.1 HTTP 200 with empty content — reasoning-model budget (`openai/gpt-oss-*`)

Captured from run `35038594879` (6 real samples, annotation emitter in place):

```text
reviewer: Groq empty message content: host=api.groq.com
          model=openai/gpt-oss-20b http_status=200 attempt=1
```

Every HTTP call returned **200**. `openai/gpt-oss-20b` / `-120b` are *reasoning*
models: the hidden chain-of-thought is drawn from the **same** completion budget
as the visible answer. When reasoning consumes the budget, Groq answers 200 with
`choices[0].message.content == ""`, the reasoning text in `message.reasoning`
and `finish_reason == "length"` — no error code and no error body to classify, so
the old code saw an empty string and failed the check.

Remedy (per <https://console.groq.com/docs/reasoning>), implemented in
`build/llm_provider.py`:

* send `max_completion_tokens` instead of the deprecated `max_tokens`;
* send `reasoning_effort: "low"` **only** to the families whose docs accept it
  (GPT-OSS 20B/120B, Qwen 3.8 27B). Qwen 3.6 27B accepts only `none`/`default`
  and non-reasoning models accept no such parameter at all — sending it to them
  makes Groq answer HTTP 400, so the gate is deliberately narrow;
* raise the reasoning budget to a floor of 1024 (cap 4096) and lift the call-site
  budgets: Producer `1500 → 2200`, Reviewer `800 → 1400`;
* when a completion comes back empty *because reasoning consumed the budget*,
  retry exactly **once** with a doubled budget — still a real API call, never a
  fixture; any other empty answer raises immediately (fail closed);
* tolerant JSON extraction: fenced ```json``` blocks and prose-wrapped objects
  (first balanced `{...}`) are both accepted before giving up;
* diagnostics carry only counts and content **length** — never the reasoning
  text, never the key.

Covered by `tests/test_groq_reasoning_models.py` (20 tests).

### 7.2 HTTP 409 whose body was an HTML page

Captured from run `35042629998` — the same run also produced a fully green
sample (`Groq connection: OK … mock=false`), proving §7.1 was fixed:

```text
reviewer: Groq HTTP 409: invalid-response host=api.groq.com attempt=1
          detail=<!DOCTYPE html> / <!--[if lt IE 7]> <html class="no-js ie6 oldie" …
```

The body was the CDN/edge error **page**, not a Groq answer, and `409` was not in
the retryable set — so the first transient blip failed the whole check at
`attempt=1`.

Remedy, implemented in `build/groq_http.py`:

* new category `edge-html-response` for any response (including a 2xx) whose body
  is an HTML page rather than JSON;
* bounded retry exactly like a 5xx, then **red** — never a mock, never a guessed
  classification;
* the error message summarises the page (`body=html-page bytes=… cloudflare_edge=…`)
  instead of dumping markup into a log or annotation;
* credential/block statuses keep priority, so 401, 403, 403+1010 and 429 are
  classified exactly as before;
* every retry still sends the explicit project `User-Agent`, `Accept`,
  `Authorization` and (POST) `Content-Type` — asserted per recorded request.

Covered by `tests/test_groq_http_client.py::EdgeHtmlResponseTests` (8 tests),
including the case where one blip is followed by a real answer and the retry
recovers with `mock: false`.

## 6. Verification after merge

```bash
# real, fail-closed connection check on main (mock defaults to false)
gh workflow run groq-connection-check --ref main

sleep 60
gh run list --workflow=groq-connection-check --limit 1

RUN_ID=$(gh run list --workflow=groq-connection-check --limit 1 \
  --json databaseId --jq '.[0].databaseId')
gh run view --log "$RUN_ID" | grep -E \
  "Client User-Agent|Endpoint hostname|DNS resolution|HTTPS probe|GROQ_API_KEY present|Model discovery|Model selection|Producer probe|Reviewer probe|Models discovered|Producer model|Producer structured|Reviewer model|Reviewer structured|Reviewer approved|Groq connection|Mock used"
```

Pass criteria — the run is green **and** the log contains exactly:

```text
Groq connection: OK
Endpoint hostname: api.groq.com
Models discovered: <count>
Producer model: <id>
Producer structured output: OK
Reviewer model: <id>
Reviewer structured output: OK
Reviewer approved: true/false
Mock used: false
```

with `Client User-Agent: metacognition-hq/1.0 (+https://github.com/Captain-Jorf/workspace)`
and no `MOCK MODE` line anywhere.

If the run is red with `cloudflare-client-blocked` again, the edge is still
rejecting the client signature — check that the deployed branch really
contains `build/groq_http.py`. **Do not rotate the key for a 1010.**

Then, and only then, the daily dry-run (builds everything, publishes nothing):

```bash
gh workflow run daily-trend-draft --ref main -f dry_run=true -f calendar_only=true
```

Expect `generation_mode=groq` in the manifest with a working key, or an honest
`generation_mode=static-fallback` on provider failure. Both are non-publishing
outcomes while `AUTO_PUBLISH_ENABLED` is not exactly `true`.
