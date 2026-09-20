"""Daily report → exactly ONE GitHub issue per content id (created or updated).

Reads the run state written by the pipeline steps (state.json) plus the QA
report / manifest / marker files, renders a human-readable Markdown body with
VISIBLE previews and clickable MP4 / poster links, and creates or updates the
issue through `gh`. Never prints secrets; stack traces stay in the Actions log.

usage:
  python3 build/report_issue.py --tag 2026-09-16 --state output/auto-2026-09-16_state.json \
      [--repo OWNER/REPO] [--run-url URL] [--dry-run] [--out output/auto-2026-09-16_issue.md]

state.json (written by build/run_state.py helpers during the workflow):
  {"status": "queued-in-buffer|approved-dry-run|qa-failed|<stage>-error|queue-full|duplicate-prevented",
   "stage": "...", "error": "...", "retries": {"script": 0, "render": 0}, "topic": {...},
   "public_url": "...", "branch": "drafts/<tag>", "buffer": {...}, "qa": {...}, ...}
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

LABEL_FOR_STATUS = {
    "queued-in-buffer": "queued-in-buffer",
    "auto-published": "auto-published",
    "approved-dry-run": "approved-dry-run",
    "qa-failed": "qa-failed",
    "trend-error": "trend-error",
    "source-error": "source-error",
    "script-error": "script-error",
    "translation-error": "translation-error",
    "tts-error": "tts-error",
    "render-error": "render-error",
    "buffer-error": "buffer-error",
    "duplicate-prevented": "duplicate-prevented",
    "queue-full": "queue-full",
    "automation-error": "automation-error",
}
ALL_LABELS = sorted(set(LABEL_FOR_STATUS.values()) | {"daily-reel"})
LABEL_COLORS = {"queued-in-buffer": "0E8A16", "auto-published": "0E8A16", "approved-dry-run": "1D76DB",
                "qa-failed": "B60205", "trend-error": "D93F0B", "source-error": "D93F0B", "script-error": "D93F0B",
                "translation-error": "D93F0B", "tts-error": "D93F0B", "render-error": "D93F0B",
                "buffer-error": "B60205", "duplicate-prevented": "FBCA04", "queue-full": "FBCA04",
                "automation-error": "B60205", "daily-reel": "5319E7"}


def gh(args, check=True, input_text=None):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True, input=input_text)
    if check and r.returncode != 0:
        raise RuntimeError(common.scrub_secrets(r.stderr.strip()[:500]))
    return r.stdout


def ensure_labels(repo):
    existing = set()
    try:
        out = gh(["label", "list", "-R", repo, "--limit", "200", "--json", "name"])
        existing = {x["name"] for x in json.loads(out or "[]")}
    except Exception as e:                                    # noqa: BLE001
        print(f"[report] label list failed: {e}")
    for lab in ALL_LABELS:
        if lab in existing:
            continue
        try:
            gh(["label", "create", lab, "-R", repo, "--color", LABEL_COLORS.get(lab, "EDEDED"),
                "--description", "reel factory", "--force"])
        except Exception as e:                                # noqa: BLE001
            print(f"[report] label {lab}: {e}")


def blob_url(repo, branch, path):
    return f"https://github.com/{repo}/blob/{branch}/{path}?raw=true"


def raw_url(repo, branch, path):
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"


def visual_provenance_section(qa):
    """Safe photo/variety observability for the daily report (issue #26 §8).

    Shows ONLY counts and license names: no full URLs, no query strings, no
    remote metadata, no downloaded filenames. Procedural fallbacks are never
    reported as retrieved photographs.
    """
    vis = (qa or {}).get("details", {}).get("visuals") or {}
    prov = vis.get("photo_provenance")
    if not prov:
        return []
    lic = prov.get("licenses") or {}
    lic_txt = ", ".join(f"{k}: {v}" for k, v in sorted(lic.items())) or "none"
    rows = [
        ("photo-designated scenes", prov.get("photo_designated")),
        ("photos successfully retrieved (CC0/PDM)", prov.get("photos_retrieved")),
        ("photo slots that fell back to a procedural visual", prov.get("photo_fallbacks")),
        ("procedural scenes (of which brand moments)",
         f"{prov.get('procedural_scenes')} ({prov.get('brand_scenes')})"),
        ("distinct primary assets", prov.get("distinct_assets")),
        ("license summary", lic_txt),
        ("repository brand assets used only as accents",
         "yes" if prov.get("brand_accents_only") else "NO"),
    ]
    # Issue #32 §8/§9: WHEN retrieval fails, the report must say WHY. Only
    # aggregate safe failure categories — never URLs, never remote bodies.
    outcomes = prov.get("retrieval_outcomes") or {}
    if outcomes:
        rows.append(("retrieval outcomes",
                     ", ".join(f"{k}: {v}" for k, v in sorted(outcomes.items()))))
    degraded = bool(prov.get("degraded"))
    if degraded:
        rows.append(("photo_mix_degraded", "yes — zero retrieved photos on designated "
                     "slots; the reel is NOT visually complete as photographic variety "
                     "(human-review warning)"))
    L = ["<details><summary>Visual provenance (photo mix, licenses, brand accents)</summary>", "",
         "| metric | value |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in rows]
    L += ["", "External photos are $0, keyless, attribution-free public-domain (CC0/PDM) only; a "
          "missing photo service never fails the run — the scene falls back to a distinct "
          "topic-specific procedural visual.", "", "</details>", ""]
    return L


def checks_table(qa):
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    rows = ["| check | result |", "|---|---|"]
    for k, v in (qa.get("checks") or {}).items():
        rows.append(f"| {k} | {icon.get(v, '')} {v} |")
    return "\n".join(rows)


def english_subtitles_section(tag, limit=40):
    """English-only subtitle review table — so the owner can read the spoken
    lines without opening the video.

    Issue #24 §12: production is English-only. The legacy "Subtitles EN ⇄ FA"
    table implied an active Persian translation layer (translator input /
    Persian column / translation engine) that no longer exists; it is
    replaced by this single-language section. There is no runtime
    translation, no Persian subtitle generation, no RTL rendering — the
    spoken English lines ARE the subtitles.
    """
    ep = os.path.join(common.ROOT, "content", "episodes", f"auto-{tag}", "script.json")
    sc = common.load_json(ep, {})
    if not sc or not sc.get("chunks"):
        return []
    rows = []
    for ch in sc["chunks"]:
        for en in ch.get("en", []):
            t = en.get("t", "") if isinstance(en, dict) else str(en)
            if t.strip():
                rows.append(t)
    esc = lambda x: str(x).replace("|", "\\|")             # noqa: E731
    L = [f"<details><summary>English subtitles — synchronized, LTR, safe-zone "
         f"validated ({len(rows)} lines)</summary>\n",
         "| # | English (as spoken and shown) |",
         "|---|---|"]
    for i, t in enumerate(rows[:limit], 1):
        L.append(f"| {i} | {esc(t)} |")
    if len(rows) > limit:
        L.append(f"| … | {len(rows) - limit} more line(s) in `content/episodes/auto-{tag}/script.json` |")
    L += ["\n</details>", ""]
    return L


def render_body(tag, st, repo, run_url):
    status = st.get("status", "automation-error")
    topic = st.get("topic") or {}
    qa = st.get("qa") or {}
    script = st.get("script") or {}
    buf = st.get("buffer") or {}
    branch = st.get("branch") or f"drafts/{tag}"
    cid = st.get("content_id") or common.content_id(tag)
    mp4 = raw_url(repo, branch, f"output/auto-{tag}.mp4")
    prev_dir = f"output/drafts/auto-{tag}"
    L = []
    head = {"queued-in-buffer": "✅ Queued in Buffer", "auto-published": "✅ Queued in Buffer",
            "approved-dry-run": "🧪 Approved (dry run — nothing sent to Buffer)",
            "qa-failed": "⛔ Rejected by the Quality Supervisor — no post today",
            "queue-full": "⏸ Buffer queue is full — no post today",
            "duplicate-prevented": "🔁 Duplicate prevented — no post today"}.get(status, f"❌ Failed at stage `{st.get('stage', '?')}`")
    L.append(f"## {head}")
    L.append("")
    L.append(f"**Date:** {tag} · **content id:** `{cid}` · **run:** {run_url or 'n/a'}")
    if topic:
        disc = topic.get("discovery_source") or {}
        L.append(f"**Topic:** {topic.get('title', '?')}  \n**Pillar:** {topic.get('pillar', '?')} · "
                 f"**evidence mode:** {topic.get('evidence_mode', '?')} · **discovered via:** "
                 f"{disc.get('name', '?')}" + (f" ({disc.get('url')})" if disc.get("url") else ""))
    L.append("")
    if status in ("queued-in-buffer", "auto-published", "approved-dry-run"):
        # ---------- success body
        if script.get("summary"):
            L.append("### English summary")
            L.append(script["summary"])
            L.append("")
        if script.get("sources"):
            L.append("### Sources")
            for s in script["sources"]:
                role = "evidence" if s.get("role") == "evidence" else "discovery only"
                lab = s.get("label", "")
                L.append(f"- **{role}** (tier {s.get('tier', '?')}): {lab}" + (f" — {s['url']}" if s.get("url") else ""))
            if any(s.get("role") == "evidence" and not s.get("url") for s in script["sources"]):
                L.append("<sub>Evidence entries are bibliographic citations from the editorial calendar (curated by "
                         "the owner); they are not fetched or verified automatically. The narration is written in "
                         "limited-claims style — no figures, no 'studies show'.</sub>")
            L.append("")
        L.append("### Previews")
        L.append(" ".join(f'<img src="{blob_url(repo, branch, f"{prev_dir}/preview_{n}.jpg")}" width="180">'
                          for n in ("start", "middle", "end")))
        L.append("")
        L.append(f"🎬 **Reel (MP4, 9:16):** {mp4}  ")
        L.append(f"🖼 **Poster 9:16:** {blob_url(repo, branch, f'output/auto-{tag}_poster.jpg')} · "
                 f"**Poster 4:5:** {blob_url(repo, branch, f'output/auto-{tag}_poster_4x5.jpg')}")
        L.append(f'<img src="{blob_url(repo, branch, f"output/auto-{tag}_poster_4x5.jpg")}" width="220">')
        L.append("")
        if st.get("caption"):
            L.append("<details><summary>Caption (first comment: hashtags)</summary>\n")
            L.append("```text")
            L.append(st["caption"][:2300])
            L.append("```")
            L.append("</details>")
            L.append("")
        L += english_subtitles_section(tag)
        L.append(f"### Quality Supervisor — score {qa.get('score', '?')}/100 (min {qa.get('min_score', '?')}) · "
                 f"{'approved' if qa.get('approved') else 'rejected'}")
        L.append(checks_table(qa))
        L.append("")
        L += visual_provenance_section(qa)
        if qa.get("warnings"):
            L.append("")
            L.append(f"<details><summary>{len(qa['warnings'])} warning(s)</summary>\n")
            L += [f"- {w}" for w in qa["warnings"]]
            L.append("\n</details>")
        L.append("")
        L.append("### Buffer")
        if status == "approved-dry-run":
            L.append("Dry run: everything passed, **no createPost call was made**. "
                     "Set repo variable `AUTO_PUBLISH_ENABLED=true` and run with `dry_run=false` to enable queueing.")
        else:
            due = buf.get("due_at")
            L.append(f"Queued via official API → post id `{buf.get('buffer_post_id', '?')}` · status `{buf.get('status', '?')}`"
                     + (f" · due {due} UTC = {common.to_tehran(due)}" if due else " · due: next queue slot (19:30 Asia/Tehran)"))
            if buf.get("adopted"):
                L.append("(existing post adopted — no duplicate created)")
            if buf.get("first_comment_used") is False:
                L.append("⚠️ hashtags first-comment was rejected by the API; post queued without it.")
        L.append("")
        L.append(f"Stand-in logo in use (no official logo file yet). Retention: branch `{branch}` is kept until Buffer "
                 f"has sent the post.")
    else:
        # ---------- failure body
        L.append(f"**Stage:** `{st.get('stage', '?')}` · **retries:** script {st.get('retries', {}).get('script', 0)}, "
                 f"render {st.get('retries', {}).get('render', 0)}")
        if st.get("error"):
            L.append("")
            L.append("**Reason:**")
            L.append("```text")
            L.append(common.scrub_secrets(str(st["error"]))[:1500])
            L.append("```")
        if qa:
            L.append("")
            verdict = "rejected" if not qa.get("approved") else "approved, but the run did not complete"
            L.append(f"### Quality Supervisor — score {qa.get('score', '?')}/100 · {verdict}")
            if qa.get("blocking_errors"):
                L.append("**Blocking errors**")
                L += [f"- {b}" for b in qa["blocking_errors"]]
            L.append("")
            L.append(checks_table(qa))
            L.append("")
            L += visual_provenance_section(qa)
            if qa.get("warnings"):
                L.append("")
                L.append(f"<details><summary>{len(qa['warnings'])} warning(s)</summary>\n")
                L += [f"- {w}" for w in qa["warnings"]]
                L.append("\n</details>")
        if st.get("artifacts_note"):
            L.append("")
            L.append(st["artifacts_note"])
        if st.get("previews_pushed"):
            L.append("")
            L.append("Previews of the rejected render (for debugging only):")
            L.append(" ".join(f'<img src="{blob_url(repo, branch, f"{prev_dir}/preview_{n}.jpg")}" width="160">'
                              for n in ("start", "middle", "end")))
        L.append("")
        L.append("Nothing was sent to Buffer. The next scheduled run will try a fresh topic.")
    if st.get("notes"):
        L.append("")
        L.append("### Operator notes")
        L += [f"- {common.scrub_secrets(str(n))}" for n in st["notes"]]
    L.append("")
    L.append(f"<sub>content_id: `{cid}` · run_id: `{st.get('run_id', 'local')}` · generated {common.utc_now()} UTC · "
             f"Buffer token never leaves the Actions secret store.</sub>")
    return "\n".join(L)


def find_existing_issue(repo, cid, tag=None):
    """ONE issue per content id. Two lookups, both keyed on the body marker (never the title alone):
    1. a plain listing of recent `daily-reel` issues (GraphQL listing — real time, no search-index lag),
    2. the search API as a fallback for very old issues."""
    marker = f"content_id: `{cid}`"
    try:
        out = gh(["issue", "list", "-R", repo, "--state", "all", "--label", "daily-reel",
                  "--json", "number,title,state,labels,body", "--limit", "60"])
        for it in json.loads(out or "[]"):
            if marker in (it.get("body") or "") or (tag and it.get("title", "").startswith(f"Daily reel {tag} ")):
                it.pop("body", None)
                return it
    except Exception as e:                                    # noqa: BLE001
        print(f"[report] issue listing failed: {e}")
    try:
        out = gh(["issue", "list", "-R", repo, "--state", "all", "--search", f'"{marker}" in:body',
                  "--json", "number,title,state,labels", "--limit", "20"])
        for it in json.loads(out or "[]"):
            return it
    except Exception as e:                                    # noqa: BLE001
        print(f"[report] issue search failed: {e}")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Captain-Jorf/workspace"))
    ap.add_argument("--run-url", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    st = common.load_json(a.state, {}) or {"status": "automation-error", "stage": "unknown",
                                          "error": "state file missing"}
    if not a.run_url and os.environ.get("GITHUB_RUN_ID"):
        a.run_url = f"https://github.com/{a.repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    st.setdefault("run_id", os.environ.get("GITHUB_RUN_ID", "local"))
    body = render_body(a.tag, st, a.repo, a.run_url)
    status = st.get("status", "automation-error")
    label = LABEL_FOR_STATUS.get(status, "automation-error")
    topic_title = (st.get("topic") or {}).get("title") or "no topic"
    title = f"Daily reel {a.tag} — {status} — {topic_title[:70]}"
    out = a.out or os.path.join(common.ROOT, "output", f"auto-{a.tag}_issue.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[report] body → {out} ({len(body)} chars), label {label}")
    if a.dry_run:
        return
    ensure_labels(a.repo)
    cid = st.get("content_id") or common.content_id(a.tag)
    existing = find_existing_issue(a.repo, cid, a.tag)
    if existing:
        num = str(existing["number"])
        old_labels = [l["name"] for l in existing.get("labels", []) if l["name"] in ALL_LABELS and l["name"] != "daily-reel"]
        args = ["issue", "edit", num, "-R", a.repo, "--title", title, "--body-file", out, "--add-label", f"daily-reel,{label}"]
        rm = [l for l in old_labels if l != label]
        if rm:
            args += ["--remove-label", ",".join(rm)]
        gh(args)
        print(f"[report] updated issue #{num}")
    else:
        url = gh(["issue", "create", "-R", a.repo, "--title", title, "--body-file", out,
                  "--label", f"daily-reel,{label}"]).strip()
        print(f"[report] created {url}")


if __name__ == "__main__":
    main()
