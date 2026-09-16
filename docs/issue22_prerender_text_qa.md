# Issue #22 follow-up — deterministic pre-render TEXT QA gate

Run [`35054292820`](https://github.com/Captain-Jorf/workspace/actions/runs/35054292820)
produced `reel-2026-09-18` (`qa-failed`, score 81) whose sole blocker was:

```
[source_quality] claim words ['researchers'] without tier A/B
```

Only after a full TTS + timing + subtitle + FFmpeg render cycle. This document
records where the blocker came from, which field it evaluates, how the
pipeline now catches it **before** any media stage, and what deliberately did
**not** change.

## 1. The exact evaluated field

`qa_supervisor.check_sources` builds its claim-word scan text from the **spoken
narration only**:

```python
text = " ".join(l["t"] for ch in script["chunks"] for l in ch.get("en", [])).lower()
```

i.e. `script.json → chunks[].en[].t` (the lines TTS will speak). `on_screen` /
`web` labels, `visual_direction` and the caption body are **not** scanned for
claim words. In run 35054292820 the word "researchers" appeared inside a
narration line of the GROQ-produced script, so the matcher fired. The manifest's
only source entry — label `Mark, Gudith & Klocke research on interrupted work`,
`url: ""`, plus a stored `"tier": "A"` — evaluates to tier **`?`**, because QA
derives tiers with `source_tier(url, label)` (HTTPS tier domains, dated labels)
and never trusts a claimed tier field. No Tier A/B ⇒ any policy claim-word
pattern in the narration blocks.

## 2. Root causes fixed

1. **Gate scope.** The PR #21 pre-render gate was deliberately narrow
   (word count 150–260 + duration preflight). A well-formed 210-word script
   with an ungrounded attribution sailed through it and burned a full render.
2. **Prompt guidance.** The producer prompt contained
   *"Reference research by name only (e.g. researchers studying LLM
   uncertainty)"* — an example that itself includes a pattern the QA matcher
   blocks. Removed; replaced by evidence-conditional rule `1b` (below).
3. **Dropped evidence.** `build_script_from_llm` ignored the calendar
   entry's curated dated sources, so legitimately grounded attributions could
   still end up tier-less; and `build_evidence_packet` fabricated
   `tier = "A" if calendar else "C"` instead of deriving it. Both fixed: the
   producer script now merges the packet's grounded sources (verbatim labels,
   derived tiers, deduped) and the packet's tier is derived with the same
   `common.source_tier` matcher QA uses, which can be `?` — and `?` grounds
   nothing.

## 3. The single-sourced pre-render text gate

`qa_supervisor.pre_render_text_gate(script, topic, pol, *, ep_dir=None,
reviewer_output=None, caption_text=None)` runs, before any media exists,
**the exact final-QA blocker functions** for every check that is evaluable on
text alone (`TEXT_QA_GATE_CHECKS`):

| gate check | guarantees |
|---|---|
| `content_language`, `english_only` | English-only fail-closed |
| `technology_relevance`, `metacognition_relevance`, `topic` | Technology × Metacognition focus, calendar topic match |
| `sources` | claim-word matcher (policy's own `require_evidence_for_claim_words`, word-boundary, case-insensitive — **no second keyword list**), statistics verifiability, fake/example.com citations, certainty phrases, limited-claims mode |
| `script`, `english` | beat order, hook constraints, placeholders, banned openers/phrases, URLs in speech |
| caption core | exact 2200/hashtag/spam limits — over the **exact bytes** `caption.py` will emit (`build_caption_text` mirrors `caption.build` + `fit` + tag footer) |
| reviewer core | structured Reviewer output enforced: score < 85, `approved: false`, `technology_relevance`/`metacognition_relevance` false, and a non-empty `blocking_errors`/`unsupported_claims` list can never be contradicted by `approved: true` |

`source_tier` and the claim-word matcher moved to `common.py` (with the QA
alias delegating), so gate, packet derivation and final QA share **one**
implementation. `evaluate()` and its scoring are untouched; the gate is a
strictly earlier run of the same functions, so nothing can pass the gate and
then newly fail final QA on a text check.

**Excluded by design** (media-only; final QA remains mandatory and
authoritative for them): rendered-subtitle geometry (`subtitle_layout`),
`audio_quality`, `video_quality` incl. posters and freeze detection, frame
contrast sampling, `buffer_readiness`, and `duplicate_check` (needs
publish-time memory state; quarantine of `reel-2026-09-15` keeps blocking in
final QA forever).

## 4. Bounded ladder — no loops, no paid fallbacks

Sequence stays exactly `Producer → Reviewer → one Revision → final Reviewer`:

1. Producer output → numeric guard, citation guard, `gate_report` (fast length
   check **plus** the text gate; the merged `issues` list is what ships/fails).
2. Any deterministic blocker (or a structured reviewer rejection — including an
   `approved: true` that contradicts its own blocking/unsupported lists) → the
   **ONE** allowed Revision with safe, structured blocker codes
   (`text_qa_revision_instructions`): claim words → *"REMOVE the attribution:
   rewrite each affected sentence as a direct, appropriately qualified
   observation … do NOT add, restore, adjust or invent a citation, paper,
   author name, URL, statistic or evidence tier"*; statistics → remove the
   number; citation → remove the URL (only packet-provided URLs survive — the
   guard prints scrubbed/truncated ones).
3. Revision output is re-checked (Reviewer + final Reviewer, numeric,
   citation, full text gate). Clean → adopt. Anything else → **validated
   Static English Fallback** (playbook path, also runs the gate). Fallback
   blocked → exit 3, **no `script.json` is written** — TTS/timing/subtitles/
   FFmpeg never run, no Buffer.
4. `pipeline.py` (`2c`): if the producer let a text-blocked script through
   (e.g. artifact replay), the gate consumes the existing single script-retry
   budget (producer rerun with variant 1). Still blocked → stage `qa` failure
   *before* the `tts` stage; state `qa-failed`, error contains the blocker
   text; nothing else.

## 5. What did **not** change

* `source_quality` stays as strong as before — the gate only moves the same
  blocker earlier; no threshold was weakened or removed;
* reviewer ≥ 85, spoken 150–260 words, 60–120 s render band, caption ≤ 2200
  chars, 3–8 hashtags, all `qa_thresholds` intact;
* policy-defined **warnings stay warnings** (hook style, contractions,
  moderate relevance, brand tag…) — nothing was promoted to a blocker;
* `AUTO_PUBLISH_ENABLED`, Buffer paths and the quarantine of
  `reel-2026-09-15` are untouched; rejected reels `reel-2026-09-16/17/18`
  were not reused (their manifests stay as-is); no mock/GitHub-Models/Persian
  path was restored; no real daily workflow run was triggered from the branch.

## 6. Proofs (tests/test_prerender_text_qa_gate.py — 34 tests)

* exact replay of the #22 artifact: the PR #21 fast gate passes it (as in the
  run — "Script retries: 0") while `pre_render_text_gate` blocks with the
  byte-identical message `[source_quality] claim words ['researchers']
  without tier A/B`;
* field-scope proof (same word in on-screen text or caption alone → no block);
* all 31 policy patterns + case variants + word-boundary negatives
  ("researchership", "provenance", "journalism" must NOT match);
* tier matrix (dated label / doi / hbr → grounded; HN / absent → blocked) and
  real calendar-grounded attribution shipping **without** burning the revision;
* parity: gate blocking set == `qa.evaluate` blocking set filtered to the text
  checks, verbatim, across 7 fixtures; spy-patching proves the gate dispatches
  the same module-level functions; media checks demonstrably absent pre-render
  yet still enforced by final QA;
* ladder: unrepaired → one revision → repaired ships (`produce` called 2×,
  `review` 2×); revision that invents an arXiv URL → citation guard → fallback;
  revision still blocked → fallback without a 3rd call; blocked fallback →
  exit 3 before `script.json`; reviewer contradictions reject adoption;
* pipeline: both attempts blocked → commands exactly `[trend, script, script]`,
  no `tts`/`timing`/`caption`/`render`/`poster`/`ffmpeg`, `run_qa` never called,
  no mp4/caption/poster/qa.json artifacts on disk, state `qa-failed` keeps its
  error through `record` (never relabeled as a Buffer outcome); repaired-on-retry
  proceeds to media once;
* prompt/needle tests (no `experts say` anywhere — it is not a QA pattern; the
  removed "researchers studying LLM uncertainty" example stays removed);
* safety invariants (quarantine still blocks through `evaluate`, memory/manifest
  of the rejected reels unchanged, no mock/Persian/GitHub-Models path).

Full suite after this change: **359 tests, all green** (325 pre-existing,
byte-compatible behaviour preserved — including the PR #21 fast-gate replay
tests — plus 34 new).
