# metacognition.hq — daily reel factory + Buffer publishing

Instagram reel factory (1080×1920, 30 fps, ~75–95 s) for **@metacognition.hq**:
English conversational voice-over (Edge TTS), word-by-word karaoke captions
(top, LTR), clean Persian RTL subtitles (bottom), animated concept-web visuals.

**Publishing is 100% human-approved and goes through the official Buffer API
only.** No Selenium, no instagrapi, no cookies, no Instagram passwords. The
Meta Graph API path (`build/insta_publish.py`) is parked — see
`content/insta_setup.md`.

## Daily flow (all free)

1. **`daily-trend-draft.yml`** (cron `0 6 * * *` UTC ≈ 09:30 Tehran, or manual):
   - `build/trend_radar.py` scans Google Trends RSS, Reddit JSON, Hacker News
     (Algolia) and arXiv; low-relevance days fall back to `content/calendar.json`.
   - `build/episode_from_trend.py` writes the episode script (EN narration +
     FA subtitles via Google→MyMemory translator chain).
   - `build/tts_edge.py` → `build/timing.py` → `build/caption.py` →
     `build/preview_stills.py` → `build/render.py` produce audio, word timings,
     caption (≤2200 chars), previews and the full 9:16 MP4.
   - Everything is pushed to the public branch `drafts/<date>` and **exactly one**
     review issue `trend reel draft <date>` is opened/updated (label `auto-draft`).
   - **This workflow never sees `BUFFER_TOKEN` and never calls Buffer.**
2. **You review** the issue: topic, sources, caption, previews, MP4 link.
3. **You comment exactly `/publish`** on that issue (owner/collaborators only —
   anonymous comments are ignored by `build/publish_gate.py`).
4. **`publish-approved-draft.yml`** verifies the video URL is public, resolves
   the `metacognition.hq` Instagram channel through the official Buffer GraphQL
   API and calls `createPost` once (`schedulingType: automatic`,
   `mode: addToQueue`, hashtags as Instagram first comment with a safe fallback).
   Idempotency: `queued-in-buffer` label + `output/auto-<date>.buffer.json`
   marker on the draft branch + a per-issue concurrency group — a reel can never
   be queued twice.
5. **Buffer publishes** at the channel's schedule slot: **19:30 Asia/Tehran**,
   one post per day (configured in the Buffer UI).

## Buffer connection test (read-only)

GitHub → **Actions** → **buffer-connection-check** → **Run workflow**
(`workflow_dispatch` only; prints `Buffer connection: OK` and the Instagram
channel name — never the token, never creates a post).
Requires the repo secret `BUFFER_TOKEN`.

## Repository layout

- `content/script.json`, `content/episodes/` — narration scripts (EN + FA).
- `content/calendar.json` — evergreen fallback topics.
- `build/trend_radar.py` — free trend radar (gtrends/reddit/HN/arXiv).
- `build/episode_from_trend.py` — trend → episode script (+FA translation chain).
- `build/tts_edge.py`, `build/timing.py` — Edge TTS + word-level timing map.
- `build/engine.py`, `build/scenes.py`, `build/render.py` — visual system → MP4.
- `build/caption.py`, `build/preview_stills.py`, `build/poster.py` — caption/preview assets.
- `build/logo_make.py`, `build/fetch_fonts.sh` — brand emblem + fonts (Vazirmatn/Sora).
- `build/buffer_publish.py` — **official Buffer GraphQL client** (check / publish).
- `build/publish_gate.py` — `/publish` authorization + validity gate.
- `build/make_issue_body.py` — daily review-issue body builder.
- `build/insta_publish.py` — parked Meta Graph API publisher (not used).
- `tests/` — unit tests: mocked Buffer GraphQL, gate rules, workflow safety
  invariants, caption limit (`python3 -m unittest discover -s tests`).

## Secrets

| Secret | Used by | Purpose |
| --- | --- | --- |
| `BUFFER_TOKEN` | `publish-approved-draft.yml` (1 step), `buffer-connection-check.yml` | Buffer personal access key — never printed |

Large binaries (video, posters, wav, fonts) stay out of git on `main` — the
daily MP4 lives only on its `drafts/<date>` branch (needed for Buffer's public
URL fetch). Don't delete a `drafts/<date>` branch before Buffer has published
its reel.
