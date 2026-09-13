"""Turn a trend-radar entry into a draft episode (script.json + caption), fully free.
usage: python3 build/episode_from_trend.py [--top N | --index I] [--date TAG]
Narration uses an honest template (no invented numbers); FA lines via free translation
with graceful fallback (draft is human-approved before publish anyway).
"""
import datetime, json, os, re, sys, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wiki_summary(topic):
    try:
        t = urllib.parse.quote(topic.replace(" ", "_")) if hasattr(urllib, "parse") else topic
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{t}"
        with urllib.request.urlopen(url, timeout=15) as r:
            d = json.load(r)
        return (d.get("extract") or "").split(". ")
    except Exception:
        return []


def translate(lines):
    try:
        from deep_translator import GoogleTranslator
        tr = GoogleTranslator(source="en", target="fa")
        return [tr.translate(x) or x for x in lines]
    except Exception as e:
        print(f"[ep] translation unavailable ({e}) — FA left as EN for human polish")
        return list(lines)


def split2(title, maxlen=26):
    w = title.split()
    if len(w) == 1:
        return [title]
    mid = len(w) // 2
    return [" ".join(w[:mid]), " ".join(w[mid:])]


def main():
    a = sys.argv[1:]
    idx = 0
    if "--index" in a:
        idx = int(a[a.index("--index") + 1])
    if "--top" in a:
        idx = int(a[a.index("--top") + 1]) - 1
    tag = a[a.index("--date") + 1] if "--date" in a else datetime.date.today().isoformat()
    trends = json.load(open(f"{ROOT}/content/trends.json"))["trends"]
    t = trends[idx]
    topic = t["title"].rstrip(".?!")
    sentences = [s.strip() + "." for s in wiki_summary(topic) if len(s.strip()) > 40][:2]
    s1 = sentences[0] if sentences else f"Today '{topic}' is everywhere online."
    s2 = sentences[1] if len(sentences) > 1 else "Most posts repeat it; almost nobody checks it."
    en = [
        [{"t": t["hook"], "scene": "hook"}],
        [{"t": f"Today it is everywhere: {topic}.", "scene": "article"},
         {"t": s1, "scene": "article"}],
        [{"t": s2, "scene": "article"},
         {"t": "Here is the part most posts skip.", "scene": "article"}],
        [{"t": "Same questions as always: how do we know it is true?", "scene": "loop"},
         {"t": "Plan your sources. Monitor your confidence. Evaluate the evidence.", "scene": "loop"}],
        [{"t": "That is the metacognition way: watch the thinking, not just the topic.", "scene": "hq"}],
        [{"t": "Follow @metacognition.hq — we watch the watchers, daily.", "scene": "cta"}],
    ]
    flat = [l["t"] for ch in en for l in ch]
    fa_groups = [[0], [1, 2], [3, 4], [5, 6], [7], [8]]
    fa_all = translate(flat)
    fa = [" ".join(fa_all[i] for i in g) for g in fa_groups]
    epdir = f"{ROOT}/content/episodes/auto-{tag}"
    os.makedirs(epdir, exist_ok=True)
    script = {
        "meta": {"title": f"Auto draft {tag} — {topic}", "handle": "@metacognition.hq",
                 "fps": 30, "w": 1080, "h": 1920, "gap": 0.34, "lead": 0.55, "tail": 1.1},
        "scene_tags": {"hook": "01 · THE QUESTION", "article": "02 · THE TREND",
                       "loop": "03 · THE LENS", "hq": "04 · THE HQ", "cta": "04 · THE HQ"},
        "props": {"paper": {"journal": t["source"].upper().replace("-", " "),
                            "year": str(datetime.date.today().year),
                            "title": split2(topic), "subtitle": s1[:90],
                            "author": "TREND RADAR", "affil": "daily free scan",
                            "seal1": "THE TREND", "seal2": tag}},
        "chunks": [{"id": f"c{i}", "en": ch, "fa": [fa[i - 1]]} for i, ch in enumerate(en, 1)],
        "caption": {
            "hook": t["hook"],
            "intro": s1,
            "sections": [{"icon": "✦", "title": "WATCH IT LIKE A METACOGNITIVIST",
                          "lines": [s2, "Plan your sources → monitor your confidence → evaluate the evidence"]}],
            "sources": [t["url"]] + ([f"Wikipedia: {topic}"] if sentences else []),
            "ctas": ["↗ SHARE this before the trend shares misinformation.",
                     "💬 What should we watch next? Comment it.",
                     "Follow @metacognition.hq — the watcher trains here."],
            "hashtags": ["#metacognition", "#trendwatch", f"#{re.sub(r'[^a-z0-9]', '', topic.lower())[:24]}",
                         "#cognitivescience", "#criticalthinking", "#metacognitionhq"],
        },
    }
    json.dump(script, open(f"{epdir}/script.json", "w"), indent=1, ensure_ascii=False)
    print(f"[ep] draft episode → {epdir}/script.json  (trend: {topic} | pillar {t['pillar']})")


if __name__ == "__main__":
    main()
