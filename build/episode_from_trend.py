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
    """Chain free engines: Google (scrape) → MyMemory (official free API) → give up (EN)."""
    out = list(lines)
    try:
        from deep_translator import GoogleTranslator
        tr = GoogleTranslator(source="en", target="fa")
        res = [tr.translate(x) for x in lines]
        if all(res) and any("\u0600" <= ch <= "\u06FF" for ch in "".join(res)):
            return [r or l for r, l in zip(res, lines)]
    except Exception as e:
        print(f"[ep] google translate failed: {e}")
    try:
        from deep_translator import MyMemoryTranslator
        tr = MyMemoryTranslator(source="en-US", target="fa-IR")
        res = [tr.translate(x) for x in lines]
        if all(res) and any("\u0600" <= ch <= "\u06FF" for ch in "".join(res)):
            print("[ep] translated via MyMemory")
            return [r or l for r, l in zip(res, lines)]
    except Exception as e:
        print(f"[ep] mymemory failed: {e}")
    print("[ep] FA left as EN for human polish")
    return out


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
    t = trends[idx] if idx < len(trends) else trends[0]
    fallback = ""
    MIN_SCORE = 2
    if t.get("score", 0) < MIN_SCORE:
        cal = json.load(open(f"{ROOT}/content/calendar.json"))["episodes"]
        doy = datetime.date.today().timetuple().tm_yday
        c = cal[doy % len(cal)]
        fallback = f"today's trends scored below relevance floor ({t.get('score')} < {MIN_SCORE}); using evergreen calendar topic #{c['id']}"
        t = {"source": "CONTENT CALENDAR (evergreen)", "title": c["title"].split(":")[0],
             "url": "", "pillar": c["pillar"], "score": 9,
             "hook": c["hook"], "cal_sources": c.get("sources", [])[:2]}
        print(f"[ep] FALLBACK: {fallback}")
    topic = t["title"].rstrip(".?!")
    sentences = [s.strip() + "." for s in wiki_summary(topic) if len(s.strip()) > 40][:4]
    s1 = sentences[0] if sentences else f"Today '{topic}' is everywhere online."
    s2 = sentences[1] if len(sentences) > 1 else "Most posts repeat it; almost nobody checks it."
    s3 = sentences[2] if len(sentences) > 2 else "The story spreads faster than the evidence behind it."
    s4 = sentences[3] if len(sentences) > 3 else "By the time people argue about it, nobody remembers the source."
    # ~15 narration lines ≈ 75–95 s reel (target: 1–2 minutes, 9:16)
    en = [
        [{"t": t["hook"], "scene": "hook"}],
        [{"t": f"Today it is everywhere: {topic}.", "scene": "article"},
         {"t": s1, "scene": "article"}],
        [{"t": s2, "scene": "article"},
         {"t": s3, "scene": "article"}],
        [{"t": s4, "scene": "article"},
         {"t": "Here is the part most posts skip.", "scene": "article"}],
        [{"t": "So slow down and ask the basic question: how do we know it is true?", "scene": "loop"},
         {"t": "Who measured it, who paid for it, and what would change their mind?", "scene": "loop"}],
        [{"t": "Plan your sources before you form an opinion.", "scene": "loop"},
         {"t": "Monitor your confidence while you read.", "scene": "loop"},
         {"t": "Evaluate the evidence after the argument, not during it.", "scene": "loop"}],
        [{"t": "That is the metacognition way: watch the thinking, not just the topic.", "scene": "hq"},
         {"t": "Every trend is a chance to train the watcher inside your head.", "scene": "hq"}],
        [{"t": "Follow @metacognition.hq — we watch the watchers, daily.", "scene": "cta"}],
    ]
    # one Persian subtitle line per English line (ep02 convention)
    fa_chunks = [translate([l["t"] for l in ch]) for ch in en]
    epdir = f"{ROOT}/content/episodes/auto-{tag}"
    os.makedirs(epdir, exist_ok=True)
    script = {
        "meta": {"title": f"Auto draft {tag} — {topic}", "note": fallback, "handle": "@metacognition.hq",
                 "fps": 30, "w": 1080, "h": 1920, "gap": 0.34, "lead": 0.55, "tail": 1.1},
        "scene_tags": {"hook": "01 · THE QUESTION", "article": "02 · THE TREND",
                       "loop": "03 · THE LENS", "hq": "04 · THE HQ", "cta": "04 · THE HQ"},
        "props": {"paper": {"journal": t["source"].upper().replace("-", " "),
                            "year": str(datetime.date.today().year),
                            "title": split2(topic), "subtitle": s1[:90],
                            "author": "TREND RADAR", "affil": "daily free scan",
                            "seal1": "THE TREND", "seal2": tag}},
        "chunks": [{"id": f"c{i}", "en": ch, "fa": fa}
                   for i, (ch, fa) in enumerate(zip(en, fa_chunks), 1)],
        "caption": {
            "hook": t["hook"],
            "intro": s1,
            "sections": [{"icon": "✦", "title": "WATCH IT LIKE A METACOGNITIVIST",
                          "lines": [s2, "Plan your sources → monitor your confidence → evaluate the evidence"]}],
            "sources": ([t["url"]] if t.get("url") else t.get("cal_sources", [])) + ([f"Wikipedia: {topic}"] if sentences else []),
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
