"""Free daily trend radar for @metacognition.hq (no paid APIs, no scraping of Instagram).
Sources: Google Trends daily RSS, Reddit public JSON, Hacker News API, arXiv API.
Output: content/trends.json  (ranked, pillar-tagged, hook suggested)
"""
import json, os, re, sys, datetime
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (trend-radar; +metacognition.hq)"}

PILLAR_KW = {
    "LEARN": ["study", "exam", "memory", "learning", "revision", "students", "focus",
              "attention", "spacing", "testing", "school", "college", "homework"],
    "MIND": ["confidence", "bias", "overconfid", "dunning", "illusion", "decision",
             "judgment", "habit", "mindset", "awareness", "psychology", "brain"],
    "AI": ["ai ", "llm", "chatgpt", "model", "agent", "hallucination", "gpt",
           "reasoning", "machine learning", "neural"],
    "LIFE": ["work", "productivity", "meeting", "conversation", "money", "trading",
             "sleep", "stress", "parenting", "talk"],
}


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def src_gtrends():
    out = []
    for geo in ("US", "GB"):
        try:
            root = ET.fromstring(get(f"https://trends.google.com/trending/rss?geo={geo}"))
            for item in root.iter("item"):
                t = item.findtext("title") or ""
                tr = item.find("{https://trends.google.com/trending/rss}traffic")
                out.append({"source": f"google-trends-{geo}", "title": t,
                            "url": item.findtext("link") or f"https://trends.google.com/trends/explore?q={t}",
                            "traffic": int(tr.text) if tr is not None and tr.text else 0})
        except Exception as e:
            print(f"[radar] gtrends {geo} skip: {e}")
    return out


def src_reddit():
    out = []
    for sub in ("psychology", "MachineLearning", "GetStudying", "science", "ArtificialInteligence"):
        try:
            d = json.load(get(f"https://www.reddit.com/r/{sub}/top.json?limit=8&t=day"))
            for c in d["data"]["children"]:
                p = c["data"]
                out.append({"source": f"reddit/r/{sub}", "title": p["title"],
                            "url": "https://reddit.com" + p["permalink"],
                            "traffic": p.get("score", 0)})
        except Exception as e:
            print(f"[radar] reddit {sub} skip: {e}")
    return out


def src_hn():
    try:
        ids = json.load(get("https://hacker-news.firebaseio.com/v0/topstories.json"))[:12]
        out = []
        for i in ids:
            try:
                s = json.load(get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json"))
                out.append({"source": "hackernews", "title": s.get("title", ""),
                            "url": s.get("url", f"https://news.ycombinator.com/item?id={i}"),
                            "traffic": s.get("score", 0)})
            except Exception:
                pass
        return out
    except Exception as e:
        print(f"[radar] hn skip: {e}")
        return []


def src_arxiv():
    try:
        root = ET.fromstring(get("http://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:q-bio.NC&sortBy=submittedDate&sortOrder=descending&max_results=8"))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out = []
        for e in root.findall("a:entry", ns):
            out.append({"source": "arxiv", "title": " ".join((e.findtext("a:title", "", ns) or "").split()),
                        "url": e.findtext("a:id", "", ns), "traffic": 5})
        return out
    except Exception as e:
        print(f"[radar] arxiv skip: {e}")
        return []


def score(item):
    t = item["title"].lower()
    best, bp = 0, "LIFE"
    for pil, kws in PILLAR_KW.items():
        s = sum(2 for k in kws if k in t)
        if s > best:
            best, bp = s, pil
    traffic = min(6, max(0, (item.get("traffic") or 0) // 500 if item["source"].startswith("google") else (item.get("traffic") or 0) // 800))
    item.update(score=best + traffic + (1 if best else -3), pillar=bp)
    item["hook"] = f"Why is everyone talking about {item['title'].rstrip('.?!').lower()} — and should you?"
    return item


def main():
    a = sys.argv[1:]
    if "--fixture" in a:
        items = json.load(open(a[a.index("--fixture") + 1]))
    else:
        items = []
        for fn in (src_gtrends, src_reddit, src_hn, src_arxiv):
            items += fn()
        if not items:
            sys.exit("[radar] all sources unreachable (run where internet is open, e.g. GitHub Actions)")
    ranked = sorted((score(i) for i in items if i.get("title")), key=lambda x: -x["score"])[:15]
    out = a[a.index("--out") + 1] if "--out" in a else f"{ROOT}/content/trends.json"
    json.dump({"fetched": datetime.datetime.utcnow().isoformat() + "Z", "trends": ranked},
              open(out, "w"), indent=1, ensure_ascii=False)
    print(f"[radar] {len(ranked)} trends → {out}")
    for t in ranked[:5]:
        print(f"  {t['score']:>3}  [{t['pillar']:<5}] {t['title'][:70]}")


if __name__ == "__main__":
    main()
