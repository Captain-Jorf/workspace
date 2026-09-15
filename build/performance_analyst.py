"""Performance Analysis agent — official Buffer API only, read-only, never fails a run.

  python3 build/performance_analyst.py collect   # pull metrics for posts we queued (needs BUFFER_TOKEN)
  python3 build/performance_analyst.py report    # weekly Markdown report → output/performance_report.md

Data model (content/performance_history.json): one record per content id with
date, topic, pillar, duration, hook type, CTA type, publish time and the public
post metrics Buffer exposes (reach, plays/views, likes, comments, shares, saves).
No audience-level or personal data is stored — aggregate post metrics only.

Guard rails: < 10 posts → "insufficient data"; 10–29 → preliminary observations;
>= 30 → strategy suggestions. The analyst can only suggest topic/hook/CTA/duration
adjustments; visual identity and hard constraints are out of its scope.
"""
import argparse
import collections
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

PRELIM_MIN = 10
STRATEGY_MIN = 30
POST_QUERY = """
query Post($input: PostInput!) {
  post(input: $input) {
    id status dueAt sentAt
    metrics { type name value unit }
    metricsUpdatedAt
  }
}
"""
METRIC_KEYS = {"reach": ("reach", "impressions", "reach_total"), "plays": ("plays", "views", "video_views", "video_plays"),
               "likes": ("likes", "reactions"), "comments": ("comments",), "shares": ("shares", "reposts"),
               "saves": ("saves", "saved")}


def load_history():
    h = common.load_json(common.PERF_PATH, None)
    if not h or "posts" not in h:
        h = {"version": 1, "_comment": "aggregate Buffer post metrics only — no personal audience data", "posts": []}
    return h


def upsert(h, rec):
    for i, p in enumerate(h["posts"]):
        if p.get("content_id") == rec["content_id"]:
            p.update({k: v for k, v in rec.items() if v is not None})
            return
    h["posts"].append(rec)


def normalize_metrics(raw):
    out = {}
    for m in raw or []:
        name = (m.get("name") or m.get("type") or "").lower()
        for key, aliases in METRIC_KEYS.items():
            if name in aliases:
                try:
                    out[key] = int(float(m.get("value") or 0))
                except (TypeError, ValueError):
                    pass
    return out


def collect():
    try:
        import buffer_publish as bp
    except Exception as e:                                    # noqa: BLE001
        print(f"[perf] buffer client unavailable: {e}")
        return 0
    tok = os.environ.get("BUFFER_TOKEN", "").strip()
    if not tok:
        print("[perf] BUFFER_TOKEN not set — skipping metric collection (non-fatal)")
        return 0
    mem = common.load_memory()
    h = load_history()
    n = 0
    changed = False
    for e in mem.get("entries", []):
        pid = e.get("buffer_post_id")
        if not pid:
            continue
        try:
            data = bp._post({"query": POST_QUERY, "variables": {"input": {"id": pid}}}, tok)
        except Exception as ex:                               # noqa: BLE001
            print(f"[perf] {e.get('content_id')}: {bp._scrub(str(ex), tok)[:160]}")
            continue
        post = data.get("post") or {}
        rec = {"content_id": e.get("content_id"), "date": e.get("content_date"), "topic": e.get("topic"),
               "pillar": e.get("pillar"), "playbook": e.get("playbook"), "hook_type": e.get("hook_type"),
               "cta_type": e.get("cta_type"), "duration_s": e.get("duration_s"), "buffer_post_id": pid,
               "status": post.get("status"), "sent_at": post.get("sentAt") or post.get("dueAt"),
               "publish_hour_tehran": (common.to_tehran(post.get("sentAt") or post.get("dueAt") or "") or "")[11:16],
               "metrics": normalize_metrics(post.get("metrics")), "metrics_updated": post.get("metricsUpdatedAt"),
               "collected_utc": common.utc_now()}
        upsert(h, rec)
        n += 1
        # mirror Buffer's own status into editorial memory (queued-in-buffer → sent / error) so that
        # retention knows when the public file has definitely been fetched by Buffer.
        bstatus = (post.get("status") or "").lower()
        if bstatus in ("sent", "error") and e.get("status") != bstatus:
            e["status"] = bstatus
            e["buffer_status_utc"] = common.utc_now()
            changed = True
    common.save_json(common.PERF_PATH, h)
    if changed:
        common.save_memory(mem)
        print("[perf] editorial memory statuses synced from Buffer")
    print(f"[perf] updated {n} post record(s) → {os.path.relpath(common.PERF_PATH, common.ROOT)}")
    return 0


def engagement(p):
    m = p.get("metrics") or {}
    return m.get("likes", 0) + 2 * m.get("comments", 0) + 3 * m.get("shares", 0) + 3 * m.get("saves", 0)


def group_stats(posts, key):
    g = collections.defaultdict(list)
    for p in posts:
        g[p.get(key) or "?"].append(engagement(p))
    return sorted(((k, round(statistics.mean(v), 1), len(v)) for k, v in g.items()), key=lambda x: -x[1])


def report():
    h = load_history()
    posts = [p for p in h["posts"] if p.get("metrics")]
    mem = common.load_memory()
    L = ["# Weekly performance report", "", f"Generated {common.utc_now()} UTC · posts with metrics: {len(posts)} · "
         f"memory entries: {len(mem.get('entries', []))}", ""]
    if len(posts) < PRELIM_MIN:
        L += [f"**Insufficient data** ({len(posts)} < {PRELIM_MIN} posts with metrics). No conclusions yet — the factory keeps "
              "rotating pillars and CTA types as planned.", ""]
    else:
        level = "STRATEGY" if len(posts) >= STRATEGY_MIN else "PRELIMINARY"
        L += [f"Confidence level: **{level}** ({len(posts)} posts)", ""]
        for key, label in (("pillar", "Best categories"), ("hook_type", "Hook types"), ("cta_type", "CTA types")):
            L += [f"## {label}", "", "| value | mean engagement | posts |", "|---|---|---|"]
            L += [f"| {k} | {m} | {n} |" for k, m, n in group_stats(posts, key)]
            L.append("")
        durs = [(p.get("duration_s") or 0, engagement(p)) for p in posts if p.get("duration_s")]
        if durs:
            short = [e for d, e in durs if d < 75]
            long_ = [e for d, e in durs if d >= 75]
            L += ["## Duration", "",
                  f"- < 75 s: {round(statistics.mean(short), 1) if short else 'n/a'} mean engagement ({len(short)} posts)",
                  f"- ≥ 75 s: {round(statistics.mean(long_), 1) if long_ else 'n/a'} mean engagement ({len(long_)} posts)", ""]
        weak = sorted(posts, key=engagement)[:3]
        L += ["## Weakest topics", ""] + [f"- {p.get('date')} · {p.get('topic')} ({engagement(p)})" for p in weak] + [""]
        pil = [e.get("pillar") for e in common.recent_entries(mem, 14, statuses=common.PUBLISHED_LIKE)]
        rep = [k for k, c in collections.Counter(pil).items() if c >= 4]
        L += ["## Repetition check", "", (f"pillars used ≥ 4× in the last 14 posts: {rep}" if rep else "no pillar over-used in the last 14 posts"), ""]
        best = group_stats(posts, "pillar")[:2]
        L += ["## Suggestions for next week (topic/hook/CTA only — visual identity unchanged)", ""]
        L += [f"- lean slightly towards {', '.join(k for k, _, _ in best)} while keeping the rotation rule" if best else "- keep rotating"]
        L += ["- keep one actionable technique per reel; the analyst cannot change hard constraints", ""]
    out = os.path.join(common.ROOT, "output", "performance_report.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("\n".join(L))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "report"])
    a = ap.parse_args()
    try:
        sys.exit(collect() if a.cmd == "collect" else report())
    except Exception as e:                                    # noqa: BLE001  never fail the workflow
        print(f"[perf] non-fatal error: {common.scrub_secrets(str(e))[:200]}")
        sys.exit(0)


if __name__ == "__main__":
    main()
