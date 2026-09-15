# Groq migration (GitHub Models retired)

## 1. Retirement note

GitHub Models was fully retired on **2026-07-30** (playground, catalog,
inference API). It is **no longer a usable provider** and has been completely
removed from production in this repository:

- No `GitHubModelsProducer` / `GitHubModelsReviewer` classes.
- No `GITHUB_TOKEN` use for inference, no `models: read` scope in workflows.
- No `models.github.ai` / `models.inference.ai.azure.com` references in live
  code or workflows (enforced by tests).
- The old `github-models-connection-check` workflow is deleted; the active
  check is `groq-connection-check`.

Active architecture: **provider `groq`** with **fallback `static-english`**
(`generation_mode=groq` only after a genuine API response, otherwise
`generation_mode=static-fallback`).

## 2. Create a free Groq API key

1. Open `https://console.groq.com/keys` and sign in (or create a free
   account).
2. Click **Create API Key**, give it a name (e.g. `metacognition-hq-daily`),
   and copy the key **once** — it is shown only at creation time.
3. **Never paste the key in chat, issues, screenshots, logs, or code.**
   Anyone with the key can consume your quota.
4. Free-tier notes (subject to change by the provider; nothing here claims
   "free forever guaranteed"):
   - Rate-limited (requests/day and tokens/minute per model), no SLA.
   - Model availability changes over time; this repo auto-discovers models
     via authenticated `GET /models` and intersects them with an ordered
     candidate list, so a retired model is automatically skipped.
   - On quota/auth/outage, the daily pipeline falls back to the curated
     static English playbooks (`generation_mode=static-fallback`) instead of
     failing silently or publishing mock content.

## 3. Store the key as a GitHub Secret

1. In the repository: **Settings → Secrets and variables → Actions →
   Secrets → New repository secret**.
2. Name: exactly `GROQ_API_KEY`. Value: paste the key. Click **Add secret**.
3. Verify placement (already wired in this PR):
   - `groq-connection-check.yml`: key in exactly one step (the check itself).
   - `daily-trend-draft.yml`: key only in the `produce` step env, never at
     workflow/job level.
4. The key value is never printed: workflows log only boolean presence, and
   all provider errors carry hostname/model/status/attempts only (tested).

## 4. Post-merge verification (step by step)

> Do these ONLY after this PR is merged. Keep `AUTO_PUBLISH_ENABLED` unset
> until the owner explicitly enables publishing.

```bash
# 1. Dispatch the real connection check on main (default mock=false):
gh workflow run groq-connection-check --ref main
sleep 45
gh run list --workflow=groq-connection-check --limit 1

# 2. Inspect the safe summary lines:
gh run view --log $(gh run list --workflow=groq-connection-check \
  --limit 1 --json databaseId --jq '.[0].databaseId') | grep -E \
  "Endpoint hostname|DNS resolution|HTTPS probe|GROQ_API_KEY present|Models discovered|Model selection|Producer model|Producer structured|Reviewer model|Reviewer structured|Reviewer approved|Groq connection|Mock used"
```

Pass criteria: run succeeds AND log contains `Groq connection: OK` AND
`Mock used: false` AND no `MOCK MODE` line AND producer/reviewer are real
discovered model IDs.

```bash
# 3. Real dry-run (builds everything, never publishes):
gh workflow run daily-trend-draft --ref main -f dry_run=true -f calendar_only=true
```

Then read the daily issue: with a working key expect `generation_mode=groq`
in the manifest; without a key (or on provider outage) expect an honest
`generation_mode=static-fallback` — both are valid, non-publishing outcomes.

## 5. Troubleshooting

| Symptom in `groq-connection-check` | Meaning | Action |
| --- | --- | --- |
| `GROQ_API_KEY present: false` | Secret missing on the branch | Add `GROQ_API_KEY` secret (§3), re-run |
| `HTTP 401/403 authentication failed` | Key invalid/revoked | Regenerate key, update secret |
| `quota 429 exhausted` | Free-tier rate limit | Wait, re-run; daily pipeline uses static fallback meanwhile |
| `none of the N candidate models are available` | Groq rotated models | Update `GROQ_CANDIDATE_MODELS` in `build/llm_provider.py` from `GET /models` |
| `DNS/HTTPS FAILED` | Runner network issue | Re-run; transient |

## 6. Candidate models (preference order, intersected with live /models)

1. `openai/gpt-oss-120b`
2. `moonshotai/kimi-k2-instruct`
3. `qwen/qwen3-32b`
4. `qwen/qwen3.6-27b`
5. `meta-llama/llama-4-maverick-17b-128e-instruct`
6. `llama-3.3-70b-versatile`
7. `openai/gpt-oss-20b`
8. `meta-llama/llama-4-scout-17b-16e-instruct`
9. `llama-3.1-8b-instant`

Producer = strongest matched; reviewer = strongest matched model different
from the producer (reuses it only when a single candidate is available).
Selection, timestamp, and reason are recorded in `producer_report.json` and in
the connection-check log. `PRODUCER_MODEL`/`REVIEWER_MODEL` default to `auto`;
explicit IDs must be known candidates confirmed by `/models`.
