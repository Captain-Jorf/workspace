"""Trend Scout Agent — picks today's topic for @metacognition.hq.

Sources (all free, HTTPS, no auth): Hacker News (Algolia), Google Trends daily
RSS, arXiv API, and content/calendar.json as the evergreen fallback.

Pipeline:  collect → negative-keyword filter → relevance scoring → duplicate
check against editorial memory (≥30 recent posts) → pillar rotation → pick.
A trend is only chosen when it clears the relevance floor AND has an evidence
plan (tier A/B source or "limited-claims" mode); otherwise the calendar wins.
Never forces an off-brand trend.

usage:
  python3 build/trend_scout.py --date 2026-09-16 --out content/episodes/auto-2026-09-16/topic.json
  python3 build/trend_scout.py --fixture fixtures/trends_sample.json --date ... --out ...
  python3 build/trend_scout.py --calendar-only --date ... --out ...
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (trend-scout; +metacognition.hq)"}

# relevance vocabulary → pillar. Weighted: strong terms 3, medium 2, weak 1.
PILLAR_TERMS = {
    "LEARN": {"learning": 2, "study": 2, "students": 1, "exam": 1, "revision": 1, "spaced": 3,
              "retrieval": 3, "repetition": 2, "practice testing": 3, "flashcard": 2, "interleav": 3, "note-taking": 2,
              "notes": 1, "tutor": 1, "education": 1, "textbook": 1, "homework": 1, "learn": 1,
              "cognitive load": 3, "onboarding": 1},
    "MEMORY": {"memory": 3, "forgetting": 3, "recall": 2, "remember": 2, "mnemonic": 3, "hippocamp": 2,
               "consolidation": 2, "sleep": 1, "amnesia": 1, "working memory": 3},
    "ATTENTION": {"attention": 3, "focus": 2, "distract": 3, "multitask": 3, "task switching": 3,
                  "deep work": 2, "notification": 1, "concentrat": 2, "mind wandering": 3, "flow state": 2},
    "BIAS": {"bias": 3, "overconfiden": 3, "dunning": 3, "kruger": 3, "illusion": 2, "fallacy": 3,
             "anchoring": 3, "confirmation": 2, "hindsight": 3, "sunk cost": 3, "availability": 1,
             "halo effect": 3, "framing": 2, "calibrat": 3, "heuristic": 2, "stereotype": 1},
    "DECIDE": {"decision": 3, "decide": 2, "choice": 2, "judgment": 2, "planning": 2, "forecast": 2,
               "pre-mortem": 3, "premortem": 3, "trade-off": 2, "regret": 1, "risk": 1, "strategy": 1},
    "THINK": {"critical thinking": 3, "reasoning": 3, "argument": 2, "logic": 2, "misinformation": 2,
              "skeptic": 2, "evidence": 1, "fact-check": 2, "rational": 2, "thinking": 1, "debate": 1,
              "motivated reasoning": 3, "hallucinat": 2, "believe": 1},
    "SOLVE": {"problem solving": 3, "problem-solving": 3, "insight": 2, "puzzle": 2, "incubation": 3,
              "creativity": 2, "brainstorm": 2, "debugging": 1, "first principles": 3},
    "SELF": {"metacognit": 4, "self-aware": 3, "self-monitor": 3, "reflection": 2, "journaling": 2,
             "mindfulness": 1, "introspect": 3, "self-explanation": 3, "know what you know": 3, "confidence": 2},
    "PROB": {"probabilit": 3, "bayes": 3, "base rate": 3, "uncertainty": 2, "forecasting": 3,
             "superforecast": 3, "expected value": 3, "statistics": 1, "odds": 1, "randomness": 2, "luck": 1},
    "MODELS": {"mental model": 4, "mental models": 4, "second-order": 2, "inversion": 2, "feedback loop": 2,
               "map is not the territory": 3, "occam": 2, "hanlon": 2, "circle of competence": 3,
               "systems thinking": 3, "first principles": 2},
}
PSYCH_CONTEXT = {"psycholog": 2, "cognitive": 2, "brain": 1, "neuroscien": 1, "behavio": 1, "mind": 1,
                 "habit": 1, "productivity": 1, "procrastinat": 2, "motivation": 1, "expert": 1,
                 "language model": 1, "llm": 1}
MIN_TREND_SCORE = 6          # relevance floor; below → calendar
MIN_CALENDAR_SCORE = 5


# ------------------------------------------------------------------ sources
def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def src_hn():
    out = []
    try:
        d = json.loads(get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"))
        for h in d.get("hits", []):
            out.append({"source": "hackernews", "tier": "C", "title": h.get("title") or "",
                        "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                        "traffic": h.get("points") or 0})
    except Exception as e:                                    # noqa: BLE001
        print(f"[scout] hn skip: {e}")
    return out


def src_gtrends():
    out = []
    for geo in ("US", "GB"):
        try:
            root = ET.fromstring(get(f"https://trends.google.com/trending/rss?geo={geo}"))
            for item in root.iter("item"):
                t = item.findtext("title") or ""
                tr = item.find("{https://trends.google.com/trending/rss}traffic")
                out.append({"source": f"google-trends-{geo}", "tier": "C", "title": t,
                            "url": item.findtext("link") or
                            "https://trends.google.com/trends/explore?q=" + urllib.parse.quote(t),
                            "traffic": int(re.sub(r"\D", "", tr.text or "0") or 0) if tr is not None else 0})
        except Exception as e:                                # noqa: BLE001
            print(f"[scout] gtrends {geo} skip: {e}")
    return out


def src_arxiv():
    q = ("(cat:q-bio.NC OR cat:cs.HC OR cat:cs.AI) AND (abs:metacognition OR abs:%22cognitive bias%22 "
         "OR abs:%22working memory%22 OR abs:overconfidence OR abs:%22decision making%22)")
    url = ("https://export.arxiv.org/api/query?search_query=" + urllib.parse.quote(q, safe="()%:")
           + "&sortBy=submittedDate&sortOrder=descending&max_results=15")
    out = []
    try:
        root = ET.fromstring(get(url))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns):
            out.append({"source": "arxiv", "tier": "A-preprint",
                        "title": " ".join((e.findtext("a:title", "", ns) or "").split()),
                        "url": (e.findtext("a:id", "", ns) or "").replace("http://", "https://"),
                        "summary": " ".join((e.findtext("a:summary", "", ns) or "").split())[:600],
                        "traffic": 0})
    except Exception as e:                                    # noqa: BLE001
        print(f"[scout] arxiv skip: {e}")
    return out


# ------------------------------------------------------------------ scoring
def negative_hits(title, pol):
    t = f" {title.lower()} "
    hits = []
    for kw in pol["negative_keywords"]:
        k = kw.lower()
        if len(k) <= 3:                     # short tokens must be whole words (vs, war, nfl…)
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t):
                hits.append(kw)
        elif k in t:
            hits.append(kw)
    return hits


def relevance(title, extra=""):
    text = f"{title} {extra}".lower()
    best_p, best_s, total = None, 0, 0
    for pillar, terms in PILLAR_TERMS.items():
        s = sum(w for term, w in terms.items() if term in text)
        total += s
        if s > best_s:
            best_p, best_s = pillar, s
    ctx = sum(w for term, w in PSYCH_CONTEXT.items() if term in text)
    return best_p, best_s, min(ctx, 3), total


def score_item(item, pol):
    title = item.get("title", "")
    neg = negative_hits(title, pol)
    pillar, ps, ctx, total = relevance(title, item.get("summary", ""))
    traffic = item.get("traffic") or 0
    pop = min(3, traffic // 300) if item["source"].startswith("hackernews") else min(2, traffic // 20000)
    score = ps + ctx + pop - 4 * len(neg)
    if ps == 0:
        score -= 6                                            # no pillar term at all → off-brand
    item.update(score=score, pillar=pillar or "THINK", neg=neg, pillar_score=ps)
    return item


def duplicate_state(title, memory, pol, tags=()):
    """→ (blocked: bool, reason: str, max_similarity: float)."""
    window = pol["duplicates"]["window_posts"]
    block = pol["duplicates"]["title_similarity_block"]
    cooldown = pol["duplicates"]["tag_cooldown_posts"]
    recent = common.recent_entries(memory, window,
                                   statuses=common.PUBLISHED_LIKE | {"queued", "sent"})
    worst = 0.0
    for i, e in enumerate(recent):
        sim = common.title_similarity(title, e.get("topic", ""))
        worst = max(worst, sim)
        if sim >= block:
            return True, f"too similar ({sim:.2f}) to {e.get('content_date')} '{e.get('topic')}'", worst
        if i < cooldown and tags and set(tags) & set(e.get("tags", [])):
            return True, f"tag {sorted(set(tags) & set(e.get('tags', [])))} used {i + 1} post(s) ago", worst
    return False, "", worst


def last_pillars(memory, n=2):
    return [e.get("pillar") for e in common.recent_entries(memory, n, statuses=common.PUBLISHED_LIKE)]


def calendar_candidates(pol, memory, date):
    """Calendar entries that have a production playbook, not recently used, rotated by
    day-of-year so consecutive fallbacks don't repeat the same entry."""
    from content_producer import CALENDAR_MAP, PLAYBOOKS      # single source of truth for playbooks
    cal = [c for c in common.calendar()["episodes"] if c.get("id") in CALENDAR_MAP]
    if not cal:
        return []
    doy = date.timetuple().tm_yday
    recent_p = last_pillars(memory, pol["duplicates"]["pillar_repeat_warn_consecutive"])
    out = []
    for k in range(len(cal)):
        c = cal[(doy + k) % len(cal)]
        title = c["title"]
        pb = PLAYBOOKS[CALENDAR_MAP[c["id"]]]
        neg = negative_hits(title, pol)
        blocked, why, sim = duplicate_state(title, memory, pol, pb.get("tags") or c.get("tags") or [])
        if blocked or neg:
            continue
        pillar = pb.get("pillar") or c.get("pillar") or "THINK"
        rot = 3 if pillar not in recent_p else 0
        out.append({"source": "content-calendar", "tier": "cal", "title": title, "url": "",
                    "score": MIN_CALENDAR_SCORE + rot + 2 * (k == 0), "pillar": pillar, "neg": [],
                    "calendar": c, "order": k, "max_similarity": sim})
    out.sort(key=lambda x: (-x["score"], x["order"]))
    return out


# ------------------------------------------------------------------ main
def choose(items, pol, memory, date, allow_trends=True):
    from content_producer import speakable_topic                 # noun-phrase gate shared with the producer
    rejected = []
    ranked = []
    if allow_trends:
        for it in items:
            if not it.get("title"):
                continue
            score_item(it, pol)
            if it["neg"]:
                rejected.append({"title": it["title"], "why": f"negative keywords {it['neg']}"})
                continue
            if it["score"] < MIN_TREND_SCORE:
                rejected.append({"title": it["title"], "why": f"relevance {it['score']} < {MIN_TREND_SCORE}"})
                continue
            blocked, why, sim = duplicate_state(it["title"], memory, pol)
            if blocked:
                rejected.append({"title": it["title"], "why": f"duplicate: {why}"})
                continue
            ok, short, why = speakable_topic(it["title"])
            if not ok:
                rejected.append({"title": it["title"], "why": f"not speakable as a noun phrase ('{short}': {why})"})
                continue
            it["short"] = short
            it["max_similarity"] = sim
            ranked.append(it)
        ranked.sort(key=lambda x: -x["score"])
    pick = None
    for it in ranked[:5]:
        # evidence plan: the producer has no paper-specific generator yet, so every live trend
        # (HN, Google Trends, arXiv) runs in limited-claims mode: the trend is the hook, the body
        # is an evergreen technique with no research claims. Calendar topics carry tier-A sources.
        it["evidence_mode"] = "limited-claims"
        pick = it
        break
    if pick is None:
        cands = calendar_candidates(pol, memory, date)
        if not cands:
            raise SystemExit("[scout] no calendar candidate left (all duplicates) — trend-error")
        pick = cands[0]
        pick["evidence_mode"] = "calendar"
        pick["fallback_reason"] = ("no trend cleared the relevance/duplicate gates"
                                   if allow_trends else "calendar-only mode")
    return pick, ranked, rejected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--out", required=True)
    ap.add_argument("--fixture", help="JSON list of items instead of live sources")
    ap.add_argument("--calendar-only", action="store_true")
    ap.add_argument("--memory", default=None)
    ap.add_argument("--policy", default=None)
    a = ap.parse_args()

    pol = common.policy(a.policy)
    memory = common.load_memory(a.memory)
    date = datetime.date.fromisoformat(a.date)

    items = []
    if a.fixture:
        items = common.load_json(a.fixture, [])
        if isinstance(items, dict):
            items = items.get("trends") or items.get("items") or []
        for it in items:
            it.setdefault("tier", "C")
    elif not a.calendar_only:
        for fn in (src_hn, src_gtrends, src_arxiv):
            items += fn()
        print(f"[scout] collected {len(items)} raw items")

    pick, ranked, rejected = choose(items, pol, memory, date, allow_trends=not a.calendar_only)
    topic = {
        "content_date": a.date,
        "content_id": common.content_id(a.date),
        "title": pick["title"],
        "normalized_topic": common.normalize_title(pick["title"]),
        "pillar": pick.get("pillar", "THINK"),
        "discovery_source": {"name": pick["source"], "url": pick.get("url", ""), "tier": pick.get("tier", "C")},
        "evidence_mode": pick["evidence_mode"],
        "score": pick.get("score"),
        "max_similarity_recent": round(pick.get("max_similarity", 0.0), 3),
        "fallback_reason": pick.get("fallback_reason", ""),
        "calendar": pick.get("calendar"),
        "ranked_trends": [{"title": r["title"], "score": r["score"], "source": r["source"]} for r in ranked[:8]],
        "rejected": rejected[:25],
        "recent_pillars": last_pillars(memory, 3),
        "generated_utc": common.utc_now(),
    }
    common.save_json(a.out, topic)
    print(f"[scout] topic → {topic['title']}  [{topic['pillar']}] via {pick['source']} "
          f"(evidence mode: {pick['evidence_mode']})")
    if pick.get("fallback_reason"):
        print(f"[scout] fallback: {pick['fallback_reason']}")
    for r in rejected[:6]:
        print(f"[scout]   rejected: {r['title'][:60]} — {r['why']}")


if __name__ == "__main__":
    main()
