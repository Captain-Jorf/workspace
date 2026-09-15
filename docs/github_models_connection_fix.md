# GitHub Models false-success fix (connection-check fail-closed)

## What happened

After PR #15 merged, the real `github-models-connection-check` run
(`actions/runs/35026364229`) failed its real API call but the workflow still
went green via mock:

- Real call failed with `URLError: [Errno -2] Name or service not known`.
- The workflow's `except` branch then set `MOCK_GITHUB_MODELS=1`, printed
  `MOCK MODE: GitHub Models connection: OK`, and exited 0.

That is a false success and a release blocker. Nothing was published to Buffer
(the dry-run path held), but the LLM connection was never actually verified.

## Precise root cause of Errno -2

1. `build/llm_provider.py::call_github_models` used the legacy Azure hostname
   as its primary URL: host `models.inference.ai.azure.com`.
2. That DNS record no longer resolves (the GitHub Models docs at
   `docs.github.com/en/github-models` state the service was retired on
   2026-07-30), so `urlopen` raised `URLError(gaierror Errno -2)`.
3. The code only tried the second endpoint inside `except HTTPError`, so a
   DNS/URLError never reached any fallback and was re-raised as `RuntimeError`.
4. The workflow caught that exception and silently switched to mock with exit 0.

Additionally, `GitHubModelsProducer/Reviewer.produce()/review()` treated
"no GITHUB_TOKEN" as an implicit mock trigger, so any unauthenticated run
looked green.

## Official endpoint (verified 2026-09-15)

- REST docs (`docs.github.com/en/rest/models/inference`): inference endpoint
  `POST https://models.github.ai/inference/chat/completions`.
- Hostname in use: `models.github.ai` (only the hostname is ever logged).
- The legacy Azure hostname has been removed from all of `build/` (enforced by
  test). Note the service retirement above: until inference is reachable again
  (or migrated to Azure AI Foundry, out of scope here), the real check is
  EXPECTED to be red — and red is now the honest, correct signal.

## What changed (fail-closed)

- `build/llm_provider.py`
  - Single official endpoint; dead hostname removed.
  - Mock ONLY with explicit `MOCK_GITHUB_MODELS=1` (default false). Missing
    token in real mode raises instead of returning fixtures.
  - Strict model validation: unknown IDs raise `ValueError` (no silent
    substitution).
  - Transient DNS/network errors: max 2 retries with 1s/2s backoff, then raise.
    HTTP errors fail immediately with status in the message.
  - Safe errors/logs only: hostname, model ID, HTTP status, attempt count,
    content length. Never token, `Authorization`, prompt, or response text.
  - New diagnostics: `resolve_hostname()` (no credentials) and `https_probe()`
    (unauthenticated, no `Authorization` header).
- `build/github_models_check.py` (new): real-by-default CLI used by the
  workflow. Prints `GitHub Models connection: OK` ONLY after genuine Producer
  + Reviewer calls; any failure exits nonzero with `Mock used: false`.
  Explicit `--mock` prints an unambiguous `MOCK MODE` banner and never the
  real-success line. No Buffer imports/calls.
- `.github/workflows/github-models-connection-check.yml`
  - Minimal permissions (`contents: read`, `models: read`), `GITHUB_TOKEN`
    from secrets, token boolean gate, DNS diagnostic step, then the fail-closed
    script. `MOCK_GITHUB_MODELS` is never set here; `mock=true` dispatch input
    (default false) is the only explicit mock path.
- `.github/workflows/daily-trend-draft.yml`
  - New `forbid mock LLM in production` guard + `unset MOCK_GITHUB_MODELS` in
    the produce step. GitHub Models failure still falls back to the curated
    static playbooks (`generation_mode=static-fallback`); mock can never become
    production content.
- `build/content_producer.py`: mock output in the daily path is rejected and
  falls back to static, so `generation_mode=github-models` is recorded ONLY
  after a genuine API response.
- `build/pipeline.py`: `assert_no_mock_in_ci()` refuses mock content when
  `GITHUB_ACTIONS=true`.
- Tests: `tests/test_github_models_failclosed.py` (26 tests) + strict-model
  update in `tests/test_english_llm.py`. Full suite: 141 tests, 0 failures.

## What "green" means now

- `github-models-connection-check` green on default settings == REAL Producer
  and Reviewer calls succeeded (no mock). Any other outcome is red.
- Do NOT report `GitHub Models connection: OK` as success until that workflow
  is green on `main` WITHOUT mock.

## Exact real-test command after merge (do NOT run daily production)

```bash
gh workflow run github-models-connection-check --ref main
sleep 45
gh run list --workflow=github-models-connection-check --limit 1
gh run view --log $(gh run list --workflow=github-models-connection-check \
  --limit 1 --json databaseId --jq '.[0].databaseId') | grep -E \
  "endpoint hostname|DNS resolution|HTTPS probe|GITHUB_TOKEN present|Producer model|Producer structured|Reviewer model|Reviewer structured|Reviewer approved|GitHub Models connection|Mock used"
```

Pass criteria: exit success AND log contains `GitHub Models connection: OK`
AND `Mock used: false` AND no `MOCK MODE` line. Anything else (including red
while the retired service is unreachable) is a truthful signal, not a
regression — do not paper over it with mock.
