"""Trend Scout — English-only, Technology × Metacognition.

Sources (free, HTTPS, no auth): Hacker News Algolia, GitHub Search/Releases,
arXiv AI/HCI, Google Trends RSS, company engineering blogs, university/research RSS,
open-access originals. Calendar as evergreen fallback.

Policy:
- Only if related to AI, software, coding, product, digital behavior, future of work
- Extract real metacognitive lesson
- Have discovery+evidence source
- Not pure promo/rumor/hype
- No scientific claim without evidence
- Not similar to recent
- HN/Google Trends only discovery, not evidence
- If no valid angle, evergreen tech topic

Hybrid rolling 10/20 window: ~60% evergreen, ~30% trend analysis, ~10% quiz/experiment/prediction
controlled in editorial memory, not here, but we prefer calendar when recent trends dominate.

usage:
  python3 build/trend_scout.py --date 2026-09-16 --out content/episodes/auto-2026-09-16/topic.json
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
import common

UA = {"User-Agent": "Mozilla/5.0 (trend-scout-tech; +metacognition.hq)"}

# New pillars: AI_JUDGMENT, CODING, LEARNING_TECH, PRODUCT, ATTENTION, HUMAN_AI
PILLAR_TERMS = {
    "AI_JUDGMENT": {"automation bias": 4, "hallucination": 3, "confidence calibration": 4, "AI judgment": 3, "AI assistant": 3,
                    "llm": 2, "language model": 2, "AI": 1, "trust AI": 3, "overreliance": 3, "AI error": 2, "calibration": 2},
    "CODING": {"debugging": 4, "coding": 3, "software engineering": 3, "code review": 3, "autocomplete": 3, "copilot": 3,
               "program": 2, "developer": 2, "bug": 2, "stack overflow": 2, "refactor": 2, "tech debt": 3},
    "LEARNING_TECH": {"tutorial hell": 4, "learning to code": 4, "learn programming": 3, "illusion of competence": 3,
                      "desirable difficulties": 3, "cognitive offloading": 3, "learning science": 2, "study": 1, "bootcamp": 2},
    "PRODUCT": {"product management": 3, "startup": 2, "planning fallacy": 4, "estimation": 3, "goodhart": 4, "metrics": 2,
                "sunk cost": 3, "architecture": 2, "user research": 3, "confirmation bias": 3, "product": 1},
    "ATTENTION": {"context switching": 4, "notifications": 3, "attention": 3, "focus": 2, "distraction": 3, "deep work": 3,
                  "multitasking": 3, "digital behavior": 3, "screen time": 2},
    "HUMAN_AI": {"human-AI collaboration": 4, "human AI": 3, "AI pair programming": 3, "future of work": 3, "deskilling": 3,
                 "AI tools": 2, "collaboration": 1, "augmentation": 2},
}

TECH_DOMAIN_KEYWORDS = ["AI", "software", "coding", "program", "product", "digital", "tech", "developer", "engineer", "startup",
                        "automation", "algorithm", "app", "API", "GitHub", "LLM", "machine learning", "future of work", "attention",
                        "notification", "code", "debugging", "tutorial", "learning to code"]

# For negative filtering
PROMO_RUMOR_KEYWORDS = ["rumor", "leak", "price", "buy now", "discount", "sale", "giveaway", "crypto", "memecoin", "airdrop"]

MIN_TREND_SCORE = 6
MIN_CALENDAR_SCORE = 5

def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def src_hn():
    out = []
    try:
        d = json.loads(get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"))
        for h in d.get("hits", []):
            title = h.get("title") or ""
            # Tech filter early
            if not any(kw.lower() in title.lower() for kw in TECH_DOMAIN_KEYWORDS):
                # Still collect but will be filtered later, but we keep to allow scoring
                pass
            out.append({"source": "hackernews", "tier": "C", "title": title,
                        "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                        "traffic": h.get("points") or 0})
    except Exception as e:
        print(f"[scout] hn skip: {e}")
    return out

def src_github_search():
    out = []
    try:
        # Free GitHub Search API (no auth, rate limited) — trending repos
        # Use search for metacognition + AI topics
        q = "AI+assistant+productivity+language:python"
        url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page=10"
        data = json.loads(get(url))
        for repo in data.get("items", [])[:10]:
            out.append({"source": "github-search", "tier": "C", "title": f"{repo.get('full_name')}: {repo.get('description') or ''}",
                        "url": repo.get("html_url"), "traffic": repo.get("stargazers_count", 0)})
    except Exception as e:
        print(f"[scout] github search skip: {e}")
    return out

def src_gtrends():
    out = []
    for geo in ("US", "GB"):
        try:
            root = ET.fromstring(get(f"https://trends.google.com/trending/rss?geo={geo}"))
            for item in root.iter("item"):
                t = item.findtext("title") or ""
                # Tech filter: only keep if tech-related
                if not any(kw.lower() in t.lower() for kw in TECH_DOMAIN_KEYWORDS):
                    continue
                tr = item.find("{https://trends.google.com/trending/rss}traffic")
                out.append({"source": f"google-trends-{geo}", "tier": "C", "title": t,
                            "url": item.findtext("link") or "https://trends.google.com/trends/explore?q=" + urllib.parse.quote(t),
                            "traffic": int(re.sub(r"\D", "", tr.text or "0") or 0) if tr is not None else 0})
        except Exception as e:
            print(f"[scout] gtrends {geo} skip: {e}")
    return out

def src_arxiv():
    # AI/HCI focused
    q = "(cat:cs.AI OR cat:cs.HC OR cat:cs.SE) AND (abs:metacognition OR abs:automation bias OR abs:calibration OR abs:human-AI OR abs:coding)"
    url = ("https://export.arxiv.org/api/query?search_query=" + urllib.parse.quote(q, safe="()%:") +
           "&sortBy=submittedDate&sortOrder=descending&max_results=15")
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
    except Exception as e:
        print(f"[scout] arxiv skip: {e}")
    return out

def negative_hits(title, pol):
    t = f" {title.lower()} "
    hits = []
    for kw in pol.get("negative_keywords", []):
        k = kw.lower()
        if len(k) <= 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t):
                hits.append(kw)
        elif k in t:
            hits.append(kw)
    # Promo/rumor
    for kw in PROMO_RUMOR_KEYWORDS:
        if kw in t:
            hits.append(f"promo/rumor:{kw}")
    return hits

def relevance(title, extra=""):
    text = f"{title} {extra}".lower()
    best_p, best_s, total = None, 0, 0
    for pillar, terms in PILLAR_TERMS.items():
        s = sum(w for term, w in terms.items() if term.lower() in text)
        total += s
        if s > best_s:
            best_p, best_s = pillar, s
    # Tech domain bonus
    tech_hits = sum(1 for kw in TECH_DOMAIN_KEYWORDS if kw.lower() in text)
    return best_p, best_s, tech_hits, total

def score_item(item, pol):
    title = item.get("title", "")
    neg = negative_hits(title, pol)
    pillar, ps, tech_hits, total = relevance(title, item.get("summary", ""))
    traffic = item.get("traffic") or 0
    pop = min(3, traffic // 300) if item["source"].startswith("hackernews") else min(2, traffic // 20000) if "trends" in item["source"] else min(2, traffic // 500)
    score = ps + tech_hits + pop - 4 * len(neg)
    if ps == 0 and tech_hits == 0:
        score -= 6
    # Must be tech-related
    if tech_hits == 0:
        score -= 5
    item.update(score=score, pillar=pillar or "AI_JUDGMENT", neg=neg, pillar_score=ps, tech_hits=tech_hits)
    return item

def duplicate_state(title, memory, pol, tags=()):
    window = pol["duplicates"]["window_posts"]
    block = pol["duplicates"]["title_similarity_block"]
    cooldown = pol["duplicates"]["tag_cooldown_posts"]
    recent = common.recent_entries(memory, window, statuses=common.PUBLISHED_LIKE | {"queued", "sent"})
    worst = 0.0
    for i, e in enumerate(recent):
        sim = common.title_similarity(title, e.get("topic", ""))
        worst = max(worst, sim)
        if sim >= block:
            return True, f"too similar ({sim:.2f}) to {e.get('content_date')} '{e.get('topic')}'", worst
        if i < cooldown and tags and set(tags) & set(e.get("tags", [])):
            return True, f"tag {sorted(set(tags) & set(e.get('tags', [])))} used {i+1} posts ago", worst
    return False, "", worst

def last_pillars(memory, n=2):
    return [e.get("pillar") for e in common.recent_entries(memory, n, statuses=common.PUBLISHED_LIKE)]

def choose(items, pol, memory, date, allow_trends=True):
    """Compatibility wrapper for tests: returns pick, ranked, rejected."""
    # Simplified version of main's logic
    try:
        from content_producer import short_title
        def speakable_topic(title):
            st = short_title(title)
            n = len(st.split())
            if n < 1 or n > 6:
                return False, st, f"{n} words"
            if not any(kw.lower() in st.lower() for kw in TECH_DOMAIN_KEYWORDS):
                if not any(kw.lower() in title.lower() for kw in TECH_DOMAIN_KEYWORDS):
                    return False, st, "not tech-relevant"
            return True, st, ""
    except Exception:
        def speakable_topic(title):
            short = " ".join(title.split()[:6])
            if len(short) < 4:
                return False, short, "too short"
            if not any(kw.lower() in title.lower() for kw in TECH_DOMAIN_KEYWORDS):
                return False, short, "not tech-relevant"
            return True, short, ""

    rejected = []
    ranked = []
    if allow_trends:
        for it in items:
            if not it.get("title"):
                continue
            score_item(it, pol)
            if it["neg"]:
                rejected.append({"title": it["title"], "why": f"negative {it['neg']}"})
                continue
            if it["score"] < MIN_TREND_SCORE:
                rejected.append({"title": it["title"], "why": f"relevance {it['score']} < {MIN_TREND_SCORE}"})
                continue
            if it.get("tech_hits", 0) == 0:
                rejected.append({"title": it["title"], "why": "not tech-related"})
                continue
            blocked, why, sim = duplicate_state(it["title"], memory, pol)
            if blocked:
                rejected.append({"title": it["title"], "why": f"duplicate {why}"})
                continue
            ok, short, why = speakable_topic(it["title"])
            if not ok:
                rejected.append({"title": it["title"], "why": f"not speakable ({short}: {why})"})
                continue
            low = it["title"].lower()
            if any(k in low for k in PROMO_RUMOR_KEYWORDS):
                rejected.append({"title": it["title"], "why": "promo/rumor/hype"})
                continue
            it["short"] = short
            it["max_similarity"] = sim
            ranked.append(it)
        ranked.sort(key=lambda x: -x["score"])
    pick = None
    for it in ranked[:5]:
        it["evidence_mode"] = "limited-claims"
        it["technology_angle"] = f"{it['pillar']} — {it.get('short','tech')}"
        pick = it
        break
    if pick is None:
        cands = calendar_candidates(pol, memory, date)
        if not cands:
            raise SystemExit("[scout] no calendar candidate")
        pick = cands[0]
        pick["evidence_mode"] = "calendar"
        pick["fallback_reason"] = "no tech trend cleared gates" if allow_trends else "calendar-only"
    return pick, ranked, rejected

def calendar_candidates(pol, memory, date):
    from content_producer import CALENDAR_MAP, PLAYBOOKS
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
        pillar = pb.get("pillar") or c.get("pillar") or "AI_JUDGMENT"
        rot = 3 if pillar not in recent_p else 0
        out.append({"source": "content-calendar", "tier": "cal", "title": title, "url": "",
                    "score": MIN_CALENDAR_SCORE + rot + 2 * (k == 0), "pillar": pillar, "neg": [],
                    "calendar": c, "order": k, "max_similarity": sim, "tech_hits": 5})
    out.sort(key=lambda x: (-x["score"], x["order"]))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--out", required=True)
    ap.add_argument("--fixture", help="JSON list of items instead of live sources")
    ap.add_argument("--calendar-only", action="store_true")
    ap.add_argument("--memory", default=None)
    ap.add_argument("--policy", default=None)
    a = ap.parse_args()

    common.assert_content_language_en()
    pol = common.policy(a.policy)
    memory = common.load_memory(a.memory)
    date = datetime.date.fromisoformat(a.date)

    items = []
    if a.fixture:
        items = common.load_json(a.fixture, []) or []
        if isinstance(items, dict):
            items = items.get("trends") or items.get("items") or []
        for it in items:
            it.setdefault("tier", "C")
    elif not a.calendar_only:
        for fn in (src_hn, src_github_search, src_gtrends, src_arxiv):
            items += fn()
        print(f"[scout] collected {len(items)} raw items (tech-filtered)")

    # Hybrid rolling window: check recent ratio
    recent = common.recent_entries(memory, 20, statuses=common.PUBLISHED_LIKE)
    trend_count = sum(1 for e in recent if e.get("playbook") == "trend" or "trend" in (e.get("topic","").lower()))
    evergreen_count = len(recent) - trend_count
    # If trend ratio >30% in last 10, prefer calendar
    prefer_calendar = False
    if len(recent) >= 10:
        last10 = recent[:10]
        t10 = sum(1 for e in last10 if e.get("playbook") == "trend" or e.get("pillar") in ("AI_JUDGMENT",) and "trend" in str(e.get("topic","")).lower())
        if t10 >= 4:  # >30%
            prefer_calendar = True
            print(f"[scout] rolling window: {t10}/10 trends in last 10, preferring calendar to keep 60/30/10")

    # Fallback speakable check
    def speakable_topic_local(title):
        # Simple: must be tech-relevant and 1-6 words short phrase possible
        short = title.split()[:6]
        short_phrase = " ".join(short)
        if len(short_phrase) < 4:
            return False, short_phrase, "too short"
        if not any(kw.lower() in title.lower() for kw in TECH_DOMAIN_KEYWORDS):
            return False, short_phrase, "not tech-relevant"
        return True, short_phrase, ""

    try:
        from content_producer import short_title
        def speakable_topic(title):
            st = short_title(title)
            n = len(st.split())
            if n < 1 or n > 6:
                return False, st, f"{n} words"
            if not any(kw.lower() in st.lower() for kw in TECH_DOMAIN_KEYWORDS):
                # Check original title
                if not any(kw.lower() in title.lower() for kw in TECH_DOMAIN_KEYWORDS):
                    return False, st, "not tech-relevant"
            return True, st, ""
    except Exception:
        speakable_topic = speakable_topic_local

    rejected = []
    ranked = []
    if not a.calendar_only and not prefer_calendar:
        for it in items:
            if not it.get("title"):
                continue
            score_item(it, pol)
            if it["neg"]:
                rejected.append({"title": it["title"], "why": f"negative {it['neg']}"})
                continue
            if it["score"] < MIN_TREND_SCORE:
                rejected.append({"title": it["title"], "why": f"relevance {it['score']} < {MIN_TREND_SCORE} tech_hits={it.get('tech_hits',0)}"})
                continue
            if it.get("tech_hits", 0) == 0:
                rejected.append({"title": it["title"], "why": "not tech-related (must be AI/software/coding/product/digital behavior/future of work)"})
                continue
            blocked, why, sim = duplicate_state(it["title"], memory, pol)
            if blocked:
                rejected.append({"title": it["title"], "why": f"duplicate {why}"})
                continue
            ok, short, why = speakable_topic(it["title"])
            if not ok:
                rejected.append({"title": it["title"], "why": f"not speakable ({short}: {why})"})
                continue
            # Check promo/rumor/hype
            low = it["title"].lower()
            if any(k in low for k in PROMO_RUMOR_KEYWORDS):
                rejected.append({"title": it["title"], "why": "pure promo/rumor/hype"})
                continue
            it["short"] = short
            it["max_similarity"] = sim
            ranked.append(it)
        ranked.sort(key=lambda x: -x["score"])

    pick = None
    for it in ranked[:5]:
        it["evidence_mode"] = "limited-claims"
        # Technology angle extraction
        it["technology_angle"] = f"{it['pillar']} — {it['short']}"
        pick = it
        break

    if pick is None:
        cands = calendar_candidates(pol, memory, date)
        if not cands:
            raise SystemExit("[scout] no calendar candidate (all duplicates) — trend-error")
        pick = cands[0]
        pick["evidence_mode"] = "calendar"
        pick["fallback_reason"] = "no tech trend cleared gates" if not a.calendar_only else "calendar-only mode"
        if prefer_calendar:
            pick["fallback_reason"] += " | rolling window prefers evergreen to keep 60/30/10"

    # Build topic.json
    topic = {
        "content_date": a.date,
        "content_id": common.content_id(a.date),
        "title": pick["title"],
        "normalized_topic": common.normalize_title(pick["title"]),
        "pillar": pick.get("pillar", "AI_JUDGMENT"),
        "technology_angle": pick.get("technology_angle", f"{pick.get('pillar','AI_JUDGMENT')} — tech"),
        "discovery_source": {"name": pick["source"], "url": pick.get("url",""), "tier": pick.get("tier","C")},
        "evidence_source": {"label": pick.get("calendar", {}).get("sources", [""])[0] if pick.get("calendar") else "HN discovery only (not evidence)", "tier": "B" if pick.get("calendar") else "C"},
        "evidence_mode": pick["evidence_mode"],
        "score": pick.get("score"),
        "max_similarity_recent": round(pick.get("max_similarity",0.0),3),
        "fallback_reason": pick.get("fallback_reason",""),
        "calendar": pick.get("calendar"),
        "ranked_trends": [{"title": r["title"], "score": r["score"], "source": r["source"], "tech_hits": r.get("tech_hits",0)} for r in ranked[:8]],
        "rejected": rejected[:25],
        "recent_pillars": last_pillars(memory, 3),
        "generated_utc": common.utc_now(),
        "content_language": "en",
        "language": "en",
        "trend_policy": {
            "allowed_domains": ["AI", "software", "coding", "product", "digital behavior", "future of work"],
            "discovery_is_not_evidence": True,
        }
    }
    common.save_json(a.out, topic)
    print(f"[scout] topic → {topic['title']} [{topic['pillar']}] tech_angle={topic['technology_angle']} via {pick['source']} (evidence: {pick['evidence_mode']})")
    if pick.get("fallback_reason"):
        print(f"[scout] fallback: {pick['fallback_reason']}")
    for r in rejected[:6]:
        print(f"[scout]   rejected: {r['title'][:60]} — {r['why']}")

if __name__ == "__main__":
    main()
