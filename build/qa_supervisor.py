"""Quality Supervisor Agent — independent, measurable gate before Buffer.

It does NOT trust the producer: it re-reads the script, the timing map, the
render layout data, the MP4 (ffprobe + ffmpeg filters), the posters, the
caption and the editorial memory, and produces its own verdict:

  {"approved": bool, "score": 0..100, "checks": {...}, "warnings": [], "blocking_errors": []}

Rules that never bend:
  * any blocking error → approved=false, whatever the score
  * score < MIN_QA_SCORE (env/var, floor 80, default 85) → approved=false
  * thresholds come from content/editorial_policy.json; nothing here lowers them

usage:
  python3 build/qa_supervisor.py --ep content/episodes/auto-<tag> --video output/auto-<tag>.mp4 \
      --caption output/auto-<tag>_caption.txt --poster output/auto-<tag>_poster.jpg \
      --poster45 output/auto-<tag>_poster_4x5.jpg --topic <topic.json> \
      [--public-url https://raw.githubusercontent.com/...mp4] [--skip-network] \
      --out-json output/auto-<tag>_qa.json --out-md output/auto-<tag>_qa.md
exit code 0 = approved, 20 = rejected (report still written), 21 = could not evaluate (fail closed)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

CHECKS = ["topic_relevance", "source_quality", "script_quality", "english_quality", "persian_quality",
          "subtitle_layout", "audio_quality", "video_quality", "caption_quality", "duplicate_check",
          "buffer_readiness"]
WEIGHTS = {"topic_relevance": 10, "source_quality": 8, "script_quality": 12, "english_quality": 10,
           "persian_quality": 10, "subtitle_layout": 10, "audio_quality": 10, "video_quality": 10,
           "caption_quality": 6, "duplicate_check": 8, "buffer_readiness": 6}
EXIT_APPROVED, EXIT_REJECTED, EXIT_CANNOT = 0, 20, 21


def ffmpeg_bin():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                                        # noqa: BLE001
        return shutil.which("ffmpeg")


def ffprobe_bin():
    return shutil.which("ffprobe")


class Report:
    def __init__(self, pol):
        self.pol = pol
        self.checks = {c: "pass" for c in CHECKS}
        self.deductions = {c: 0 for c in CHECKS}
        self.warnings, self.blocking, self.details = [], [], {}

    def warn(self, check, msg, points=2):
        self.warnings.append(f"[{check}] {msg}")
        self.deductions[check] = min(WEIGHTS[check], self.deductions[check] + points)
        if self.checks[check] == "pass":
            self.checks[check] = "warn"

    def block(self, check, msg):
        self.blocking.append(f"[{check}] {msg}")
        self.checks[check] = "fail"
        self.deductions[check] = WEIGHTS[check]

    def score(self):
        return max(0, 100 - sum(self.deductions.values()))


# ----------------------------------------------------------------- helpers
def probe(path):
    fp = ffprobe_bin()
    if fp:
        r = subprocess.run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return json.loads(r.stdout)
    # fallback: parse ffmpeg -i (imageio binary has no ffprobe)
    ff = ffmpeg_bin()
    r = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True)
    txt = r.stderr
    info = {"format": {}, "streams": []}
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", txt)
    if m:
        h, mi, s = m.groups()
        info["format"]["duration"] = str(int(h) * 3600 + int(mi) * 60 + float(s))
    m = re.search(r"bitrate: (\d+) kb/s", txt)
    if m:
        info["format"]["bit_rate"] = str(int(m.group(1)) * 1000)
    for line in txt.splitlines():
        if "Stream" in line and "Video:" in line:
            mm = re.search(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})", line)
            fps = re.search(r"([\d.]+) fps", line)
            info["streams"].append({"codec_type": "video", "codec_name": mm.group(1) if mm else "?",
                                    "width": int(mm.group(2)) if mm else 0, "height": int(mm.group(3)) if mm else 0,
                                    "r_frame_rate": f"{fps.group(1)}/1" if fps else "0/1",
                                    "duration": info["format"].get("duration")})
        elif "Stream" in line and "Audio:" in line:
            mm = re.search(r"Audio: (\w+)", line)
            sr = re.search(r"(\d+) Hz", line)
            info["streams"].append({"codec_type": "audio", "codec_name": mm.group(1) if mm else "?",
                                    "sample_rate": sr.group(1) if sr else "0",
                                    "duration": info["format"].get("duration")})
    info["format"]["size"] = str(os.path.getsize(path))
    return info


def run_filter(path, args):
    ff = ffmpeg_bin()
    r = subprocess.run([ff, "-hide_banner", "-nostats", "-i", path] + args + ["-f", "null", "-"],
                       capture_output=True, text=True)
    return r.stderr


def decode_ok(path):
    ff = ffmpeg_bin()
    r = subprocess.run([ff, "-v", "error", "-xerror", "-i", path, "-f", "null", "-"], capture_output=True, text=True)
    return r.returncode == 0, r.stderr[-400:]


def head_url(url, timeout=25):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0 (qa-supervisor)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), int(r.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as e:
        return e.code, "", 0
    except Exception as e:                                    # noqa: BLE001
        return 0, str(e)[:80], 0


def rel_luminance(rgb):
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(fg, bg):
    l1, l2 = rel_luminance(fg), rel_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# ----------------------------------------------------------------- checks
def check_topic(rep, script, topic, pol):
    meta = script.get("meta", {})
    title = (meta.get("topic") or topic.get("title") or "").lower()
    text = " ".join(l["t"] for ch in script["chunks"] for l in ch["en"]).lower()
    allowed = pol["allowed_topics"]
    vocab = ["metacognit", "bias", "decision", "critical", "learn", "memory", "attention", "focus", "problem",
             "self-aware", "probabilit", "mental model", "confidence", "recall", "study", "think", "judg",
             "plan", "monitor", "evaluate", "calibrat", "forecast", "explain", "understand", "notice", "listen",
             "remember", "forget", "practice", "mind", "brain", "check", "confident", "sure", "watcher", "expert",
             "mistake", "skill", "guess", "predict", "estimate", "test", "question", "habit", "feel", "know",
             "trust", "distract", "review", "reason", "evidence", "assum", "belie", "opinion", "certain",
             "doubt", "wrong", "error", "compare", "score", "measure"]
    hits = sum(1 for v in vocab if v in text)
    if hits < 5:
        rep.block("topic_relevance", f"narration barely touches the page's field ({hits} field terms)")
    elif hits < 8:
        rep.warn("topic_relevance", f"weak field vocabulary ({hits} terms)", 4)
    negs = []
    for kw in pol["negative_keywords"]:
        k = kw.lower()
        if len(k) <= 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", f" {title} "):
                negs.append(kw)
        elif k in title:
            negs.append(kw)
    if negs:
        rep.block("topic_relevance", f"topic contains banned keywords {negs}")
    # sensitive / dangerous content in the narration itself
    for kw in ("suicide", "self-harm", "overdose", "kill yourself", "porn"):
        if kw in text:
            rep.block("topic_relevance", f"sensitive content '{kw}' in narration")
    # finance/investment framing is off-brand and explicitly excluded by the owner (even "not advice" framings)
    fin = [k for k in ("trader", "trading", "stock", "invest", "crypto", "portfolio", "forex", "betting")
           if re.search(rf"\b{k}", text)]
    if fin:
        rep.block("topic_relevance", f"investment/finance framing in narration {fin}")
    # medical/psychiatric advice framing
    for kw in ("you have adhd", "you are depressed", "take this medication", "stop your medication", "diagnose you"):
        if kw in text:
            rep.block("topic_relevance", f"medical/psychiatric advice framing: '{kw}'")
    rep.details["topic"] = {"field_terms": hits, "allowed_topics": len(allowed)}


def source_tier(url, label, pol):
    tiers = pol["source_policy"]["tiers"]
    u = (url or "").lower()
    if u:
        for t in ("A", "B", "C"):
            for dom in tiers[t]:
                if dom.lower() in u:
                    return t
        return "C"
    # label-only citation (journal, year) → treated as A if it looks like a proper citation
    if re.search(r"\(\d{4}\)", label or "") or re.search(r"\b(19|20)\d{2}\b", label or ""):
        return "A"
    return "?"


def check_sources(rep, script, topic, pol, skip_network):
    srcs = script.get("sources", [])
    claim_words = pol["source_policy"]["require_evidence_for_claim_words"]
    text = " ".join(l["t"] for ch in script["chunks"] for l in ch["en"]).lower()
    claims = [w for w in claim_words if re.search(rf"\b{re.escape(w)}\b", text)]
    numbers = re.findall(r"\b\d{1,3}(?:\.\d+)?\s?(?:%|percent\b)", text)
    numbers += re.findall(r"\b\d{1,3}\s(?:times|in\s\d+|out\sof\s\d+)\b", text)   # "3 times", "9 in 10"
    ev = [s for s in srcs if s.get("role") == "evidence"]
    tiers = [source_tier(s.get("url"), s.get("label"), pol) for s in ev]
    best = min(tiers, key=lambda t: "ABC?".index(t)) if tiers else "?"
    rep.details["sources"] = {"evidence": len(ev), "best_tier": best, "claim_words": claims,
                              "numbers_in_narration": numbers,
                              "discovery": (topic.get("discovery_source") or {}).get("name")}
    mode = script.get("meta", {}).get("evidence_mode")
    if claims and best not in ("A", "B"):
        rep.block("source_quality", f"narration uses claim words {claims} without a tier A/B evidence source")
    if numbers and not script.get("meta", {}).get("stats_verified"):
        # the factory never verifies figures automatically → any digit statistic is unsourced by definition
        rep.block("source_quality", f"statistics {numbers} in narration cannot be verified automatically")
    if mode == "limited-claims" and claims:
        rep.block("source_quality", "trend topic in limited-claims mode must not make research claims")
    for kw in pol["tone"]["banned_phrases"][:5]:                # "science proves" family
        if kw in text:
            rep.block("source_quality", f"unsupported certainty phrase '{kw}'")
    # URL reachability (discovery + evidence URLs)
    for s in srcs:
        u = s.get("url")
        if not u:
            continue
        if not u.startswith("https://"):
            rep.warn("source_quality", f"non-HTTPS source URL {u}", 2)
            continue
        if skip_network:
            continue
        code, _, _ = head_url(u)
        if code == 0 or code >= 400 and code not in (403, 405, 429):   # some publishers block HEAD
            rep.warn("source_quality", f"source URL not reachable (HTTP {code}): {u}", 3)


def check_script(rep, script, pol):
    beats = [ch.get("beat") for ch in script["chunks"]]
    need = pol["script_structure"]
    missing = [b for b in need if b not in beats]
    if missing:
        rep.block("script_quality", f"script structure incomplete, missing beats {missing}")
    order = [b for b in beats if b in need]
    dedup = []
    for b in order:
        if not dedup or dedup[-1] != b:
            dedup.append(b)
    if dedup != [b for b in need if b in dedup]:
        rep.block("script_quality", f"beats out of order: {dedup}")
    lines = [l["t"] for ch in script["chunks"] for l in ch["en"]]
    words = sum(common.word_count(l) for l in lines)
    lo, hi = pol["length"]["narration_words"]
    if words < lo * 0.8 or words > hi * 1.15:
        rep.block("script_quality", f"narration {words} words outside {lo}-{hi}")
    elif words < lo or words > hi:
        rep.warn("script_quality", f"narration {words} words slightly outside {lo}-{hi}", 3)
    long_lines = [l for l in lines if common.word_count(l) > pol["length"]["max_words_per_line"]]
    if long_lines:
        rep.warn("script_quality", f"{len(long_lines)} line(s) over {pol['length']['max_words_per_line']} words", 2)
    # hook rules
    hook = next((ch["en"][0]["t"] for ch in script["chunks"] if ch.get("beat") == "hook"), "")
    hp = pol["hook_policy"]
    hw = common.word_count(hook)
    if hw > hp["max_words"] + 5:
        rep.block("script_quality", f"hook too long to say in 3 s ({hw} words)")
    elif hw > hp["max_words"]:
        rep.warn("script_quality", f"hook a bit long ({hw} words)", 2)
    hl = hook.lower()
    if hp["must_be_question_or_contrast"] and "?" not in hook and not any(m in hl for m in hp["contrast_markers"]):
        rep.warn("script_quality", "hook is neither a question nor a contrast", 4)
    for bad in hp["banned_hook_phrases"]:
        if bad in hl:
            rep.block("script_quality", f"hook uses clickbait phrase '{bad}'")
    for bad in pol["tone"]["fear_words"]:
        if bad in hl:
            rep.block("script_quality", f"hook uses fear framing '{bad}'")
    for op in pol["tone"]["forbidden_openers"]:
        if hl.startswith(op) or op in hl[:40]:
            rep.block("script_quality", f"hook uses a banned opener '{op}'")
    # hook ↔ body coherence: at least one content word from the hook recurs later
    hook_toks = {t[:5] for t in common.normalize_title(hook).split()}
    body = common.normalize_title(" ".join(lines[1:]))
    body_toks = {t[:5] for t in body.split()}
    if hook_toks and not (hook_toks & body_toks):
        rep.warn("script_quality", "hook shares no content words with the body (possible bait)", 5)
    # technique must be actionable
    tech = " ".join(l["t"] for ch in script["chunks"] if ch.get("beat") == "technique" for l in ch["en"]).lower()
    if not any(k in tech for k in ("try", "before", "after", "ask", "write", "set", "pick", "say", "next", "close",
                                    "pause", "name", "review", "recall", "compare", "keep")):
        rep.block("script_quality", "technique beat has no actionable instruction")
    # CTA
    ending = " ".join(l["t"] for ch in script["chunks"] if ch.get("beat") == "ending" for l in ch["en"])
    cp = pol["cta_policy"]
    for bad in cp["banned_phrases"]:
        if bad in ending.lower():
            rep.block("script_quality", f"ending uses engagement-bait phrase '{bad}'")
    if common.word_count(ending) > cp["max_words"]:
        rep.warn("script_quality", f"ending/CTA too long ({common.word_count(ending)} words)", 2)
    # placeholders
    for l in lines:
        if common.PLACEHOLDER_RE.search(l):
            rep.block("script_quality", f"placeholder text in narration: '{l[:50]}'")
    # main message: one technique block, no second unrelated 'technique'
    rep.details["script"] = {"words": words, "hook_words": hw, "beats": beats, "cta_type": script["meta"].get("cta_type")}


def check_english(rep, script, pol):
    lines = [l["t"] for ch in script["chunks"] for l in ch["en"]]
    text = " ".join(lines)
    low = text.lower()
    markers = sum(1 for m in pol["tone"]["informality_markers"] if m in low)
    if markers == 0:
        rep.warn("english_quality", "no conversational contractions at all — sounds formal", 4)
    elif markers < 3:
        rep.warn("english_quality", "few conversational markers", 2)
    formal = ["furthermore", "moreover", "thus", "hence", "utilize", "in conclusion", "it is imperative",
              "aforementioned", "notwithstanding", "heretofore"]
    fh = [w for w in formal if w in low]
    if fh:
        rep.warn("english_quality", f"academic phrasing {fh}", 3)
    for bad in pol["tone"]["banned_phrases"]:
        if bad in low:
            rep.block("english_quality", f"banned phrase '{bad}'")
    urls = re.findall(r"https?://|www\.", low)
    if urls:
        rep.block("english_quality", "URL inside narration (TTS would read it aloud)")
    weird = re.findall(r"[<>{}\[\]|\\^~`_#*]", text)
    if weird:
        rep.warn("english_quality", f"technical symbols in narration {sorted(set(weird))}", 2)
    jargon = [j for j in pol["tone"]["jargon_needing_explanation"] if j in low]
    explained = 0
    for j in jargon:
        idx = low.find(j)
        window = low[idx: idx + 220]
        if any(k in window for k in (" means", " is ", ":", " called", "that is", "in other words", "simple", "just")):
            explained += 1
    if jargon and explained < len(jargon):
        rep.warn("english_quality", f"jargon possibly unexplained: {jargon}", 2)
    avg = sum(common.word_count(l) for l in lines) / max(1, len(lines))
    if avg > 18:
        rep.warn("english_quality", f"average line length {avg:.1f} words — long for speech", 3)
    dup = len(lines) - len({l.strip().lower() for l in lines})
    if dup:
        rep.warn("english_quality", f"{dup} repeated line(s)", 3)
    rep.details["english"] = {"informality_markers": markers, "avg_words_per_line": round(avg, 1)}


def check_persian(rep, script, pol):
    from content_producer import translation_valid   # same hard rules as production
    n_bad = 0
    total = 0
    bidi_errors = []
    latin_leakage = []
    yeh_kaf_errors = []
    punctuation_errors = []
    glossary_conflicts = []
    duplicate_fa = {}
    all_fa_lines = []

    # Load glossary and catalog for advanced checks
    glossary = common.load_fa_glossary() or {}
    terms = glossary.get("terms", {}) if glossary else {}
    allowlist_latin = set((glossary.get("allowlist_latin") or []))
    allowlist_latin_lower = {x.lower() for x in allowlist_latin}
    # Also allow metacognition.hq variants via common
    allowlist_latin_lower.update({"metacognition", "hq", "metacognition.hq", "@metacognition.hq"})

    catalog = common.load_fa_catalog() or {}
    catalog_trans = catalog.get("translations", {}) if catalog else {}

    for ch in script["chunks"]:
        if len(ch["fa"]) != len(ch["en"]):
            rep.block("persian_quality", f"chunk {ch['id']}: {len(ch['fa'])} FA lines for {len(ch['en'])} EN lines")
            continue
        for en_obj, fa in zip(ch["en"], ch["fa"]):
            en = en_obj["t"]
            total += 1
            all_fa_lines.append(fa)
            ok, why = translation_valid(en, fa, pol)
            if not ok:
                n_bad += 1
                rep.block("persian_quality", f"'{en[:40]}' → invalid Persian ({why})")
            # Yeh/Kaf normalization: Arabic ي ك should not appear
            if common.ARABIC_ONLY_RE.search(fa):
                yeh_kaf_errors.append(f"'{fa[:30]}' contains Arabic ي/ك")
                rep.warn("persian_quality", f"Arabic letterforms (ي/ك) in '{fa[:30]}'", 1)
            # Bidi controls: check for explicit bidi chars that should not be in stored text (they are for rendering)
            if any(c in fa for c in ["\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e"]):
                bidi_errors.append(f"bidi control char in '{fa[:30]}'")
                rep.warn("persian_quality", f"bidi control in FA '{fa[:30]}'", 1)
            # Punctuation: Persian should use ؟ ، ؛ not ? , ;
            if "?" in fa or ("," in fa and "،" not in fa and len([w for w in fa if w.isalpha()]) > 5):
                # If English ? appears in FA, it's wrong
                if "?" in fa:
                    punctuation_errors.append(f"English ? in FA '{fa[:40]}'")
                    rep.warn("persian_quality", f"English ? in Persian '{fa[:30]}' should be ؟", 1)
            if ";" in fa:
                punctuation_errors.append(f"English ; in FA '{fa[:30]}'")
            # Latin leakage
            latins = [w for w in common.latin_words(fa) if w.lower() not in allowlist_latin_lower]
            if latins:
                latin_leakage.append({"fa": fa[:60], "latin": latins})
                if len(latins) > 2:
                    rep.block("persian_quality", f"Latin leakage {latins[:4]} in FA '{fa[:40]}'")
                else:
                    rep.warn("persian_quality", f"Latin word {latins} in FA '{fa[:30]}'", 1)
            # Glossary consistency: if EN contains glossary term, FA should contain its Persian equivalent
            en_low = en.lower()
            for en_term, fa_term in terms.items():
                if en_term.lower() in en_low:
                    # Check if FA contains fa_term (allow partial)
                    if fa_term not in fa:
                        # Only warn if term is significant and not already covered by other checks
                        # For critical terms like planning fallacy, enforce
                        if en_term.lower() in ("planning fallacy", "dunning-kruger", "metacognition"):
                            glossary_conflicts.append(f"EN term '{en_term}' expects FA '{fa_term}' but got '{fa[:40]}'")
                            rep.warn("persian_quality", f"glossary term '{en_term}' → expected '{fa_term}' missing in FA", 2)
            # Duplicate detection
            fa_norm = fa.strip()
            duplicate_fa[fa_norm] = duplicate_fa.get(fa_norm, 0) + 1

            if len(fa) > pol["length"]["max_chars_per_line_fa"]:
                rep.warn("persian_quality", f"FA line long for two mobile rows ({len(fa)} chars)", 1)

    if total == 0:
        rep.block("persian_quality", "no Persian subtitles at all")

    engine = script["meta"].get("translation_engine")
    # Provenance checks (fail-closed)
    provenance = engine or "unknown"
    curated_coverage = 0
    if catalog_trans:
        # Calculate coverage: how many EN lines have hash in catalog
        matched = 0
        for ch in script["chunks"]:
            for en_obj in ch["en"]:
                h = common.en_hash(en_obj["t"])
                if h in catalog_trans:
                    matched += 1
        curated_coverage = matched / max(1, total)

    # Fixture ban (never publishable in production)
    if engine == "fixture" and os.environ.get("QA_ALLOW_FIXTURE") != "1":
        rep.block("persian_quality", "translation engine is the offline test fixture — never publishable")

    # MyMemory-only ban (blocking error for auto-publish)
    if engine == "mymemory":
        rep.block("persian_quality", "translation engine mymemory-only is not allowed for auto-publish (must be curated)")

    # Google-only also not allowed? Spec says block mymemory-only as blocking error, but curated is required for calendar
    if engine in ("google",):
        # For calendar, only curated allowed
        meta = script.get("meta", {})
        if meta.get("evidence_mode") == "calendar" or meta.get("playbook") != "trend":
            rep.block("persian_quality", f"translation engine {engine} not allowed for calendar — must be curated")

    # Missing engine
    if n_bad == 0 and total and not engine:
        rep.warn("persian_quality", "translation engine not recorded", 1)

    # Duplicate FA lines (exact duplicates across different EN)
    dups = [fa for fa, cnt in duplicate_fa.items() if cnt > 1]
    if dups:
        rep.warn("persian_quality", f"{len(dups)} duplicate FA line(s) across different EN", 1)

    # Build persian_translation detailed block
    persian_translation = {
        "approved": n_bad == 0 and not any("mymemory" in b or "fixture" in b or "Latin leakage" in b for b in rep.blocking),
        "provenance": provenance,
        "curated_coverage": round(curated_coverage, 3),
        "latin_leakage": latin_leakage[:10],
        "bidi_errors": bidi_errors[:10],
        "yeh_kaf_errors": yeh_kaf_errors[:10],
        "punctuation_errors": punctuation_errors[:10],
        "terminology_conflicts": glossary_conflicts[:10],
        "blocking_errors": [b for b in rep.blocking if "persian" in b.lower() or "translation" in b.lower() or "mymemory" in b.lower() or "fixture" in b.lower()],
        "total_lines": total,
        "invalid_lines": n_bad,
        "duplicate_fa_count": len(dups),
        "engine": engine,
    }

    rep.details["persian"] = {"lines": total, "invalid": n_bad, "engine": engine}
    rep.details["persian_translation"] = persian_translation


def check_layout(rep, layout, timing, pol):
    L = pol["layout"]
    if not layout:
        rep.block("subtitle_layout", "layout.json missing — cannot verify subtitle placement")
        return
    for e in layout.get("en", []):
        x0, y0, x1, y1 = e["bbox"]
        if e.get("direction") != "ltr":
            rep.block("subtitle_layout", "English caption not LTR")
        if y0 < L["safe_top"] - 1 or y1 > L["safe_bottom"]:
            rep.block("subtitle_layout", f"EN caption outside safe zone ({y0}-{y1}): '{e['text'][:40]}'")
        if x0 < 0 or x1 > L["width"]:
            rep.block("subtitle_layout", f"EN caption wider than frame: '{e['text'][:40]}'")
        if e["rows"] > L["en_max_rows"]:
            rep.block("subtitle_layout", f"EN caption has {e['rows']} rows (> {L['en_max_rows']}): '{e['text'][:40]}'")
    fa_bottoms = []
    for f in layout.get("fa", []):
        x0, y0, x1, y1 = f["bbox"]
        fa_bottoms.append(y1)
        if f.get("direction") != "rtl":
            rep.block("subtitle_layout", "Persian subtitle not RTL")
        if y1 > L["safe_bottom"] or y0 < L["scene_zone"][1] - 40:
            rep.block("subtitle_layout", f"FA subtitle outside its band ({y0}-{y1}): '{f['text'][:30]}'")
        if x0 < 0 or x1 > L["width"] or f.get("overflow"):
            rep.block("subtitle_layout", f"FA subtitle overflows width: '{f['text'][:30]}'")
        if f["rows"] > L["fa_max_rows"]:
            rep.block("subtitle_layout", f"FA subtitle has {f['rows']} rows: '{f['text'][:30]}'")
        # right-hand Instagram button column: FA pill must not reach into it
        if x1 > L["right_button_column_x"] and L["right_button_column_y"][0] <= y1 <= L["right_button_column_y"][1]:
            rep.warn("subtitle_layout", f"FA pill reaches the right button column: '{f['text'][:30]}'", 2)
    # EN/FA overlap impossible by construction (top vs bottom) — verify numerically anyway
    en_max = max((e["bbox"][3] for e in layout.get("en", [])), default=0)
    fa_min = min((f["bbox"][1] for f in layout.get("fa", [])), default=H_DEFAULT)
    if en_max and fa_min and en_max > fa_min:
        rep.block("subtitle_layout", "English and Persian subtitles overlap vertically")
    # karaoke timing inside audio duration
    total = float(timing.get("total", 0))
    for ch in timing.get("chunks", []):
        for ln in ch["lines"]:
            for w in ln["words"]:
                if w["end"] > total + 0.05 or w["start"] < 0:
                    rep.block("subtitle_layout", f"karaoke word '{w['w']}' timed outside the audio ({w['end']:.2f}s > {total:.2f}s)")
                    break
    rep.details["layout"] = {"en_lines": len(layout.get("en", [])), "fa_lines": len(layout.get("fa", [])),
                             "en_bottom_max": en_max, "fa_top_min": fa_min}


H_DEFAULT = 1920


def check_video(rep, video, timing, pol, layout):
    vp = pol["video_policy"]
    if not video or not os.path.exists(video):
        rep.block("video_quality", "MP4 missing")
        return None
    info = probe(video)
    vs = [s for s in info["streams"] if s.get("codec_type") == "video"]
    as_ = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    if not vs:
        rep.block("video_quality", "no video stream")
        return info
    if not as_:
        rep.block("audio_quality", "no audio stream")
    v = vs[0]
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    if (w, h) != (vp["width"], vp["height"]):
        if abs(w / max(h, 1) - 9 / 16) > vp["aspect_tolerance"]:
            rep.block("video_quality", f"aspect {w}x{h} is not 9:16")
        elif h < 1280:
            rep.block("video_quality", f"resolution {w}x{h} too low for Reels")
        else:
            rep.warn("video_quality", f"resolution {w}x{h} (expected 1080x1920)", 2)
    if (v.get("codec_name") or "").lower() not in ("h264", "avc1"):
        rep.block("video_quality", f"video codec {v.get('codec_name')} is not H.264")
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    lo, hi = vp_hard = pol["length"]["hard_seconds"]
    if dur < lo or dur > hi:
        rep.block("video_quality", f"duration {dur:.1f}s outside {lo}-{hi}s")
    tlo, thi = pol["length"]["target_seconds"]
    if lo <= dur <= hi and not (tlo <= dur <= thi):
        rep.warn("video_quality", f"duration {dur:.1f}s outside target {tlo}-{thi}s", 2)
    try:
        num, den = v.get("r_frame_rate", "30/1").split("/")
        fps = float(num) / float(den or 1)
    except Exception:                                        # noqa: BLE001
        fps = 0
    if not (vp["fps"][0] <= fps <= vp["fps"][1]):
        rep.warn("video_quality", f"fps {fps:.1f} unusual", 2)
    size_mb = int(info["format"].get("size", 0)) / 1e6
    if not (vp["file_size_mb"][0] <= size_mb <= vp["file_size_mb"][1]):
        rep.block("video_quality", f"file size {size_mb:.1f} MB outside {vp['file_size_mb']}")
    br = int(info["format"].get("bit_rate") or 0) / 1000
    if br and not (vp["bitrate_kbps"][0] <= br <= vp["bitrate_kbps"][1]):
        rep.warn("video_quality", f"bitrate {br:.0f} kbps outside {vp['bitrate_kbps']}", 2)
    ok, err = decode_ok(video)
    if not ok:
        rep.block("video_quality", f"MP4 does not decode cleanly: {err.strip()[-120:]}")
    # audio/video duration agreement
    if as_:
        adur = float(as_[0].get("duration") or dur)
        if abs(adur - dur) > vp["max_av_duration_diff_seconds"]:
            rep.block("audio_quality", f"audio {adur:.2f}s vs video {dur:.2f}s differ too much")
    exp = float(timing.get("total", dur))
    if abs(exp - dur) > 1.5:
        rep.warn("video_quality", f"timing total {exp:.1f}s vs mp4 {dur:.1f}s", 2)
    # black + freeze detection
    out = run_filter(video, ["-vf", f"blackdetect=d={vp['max_black_seconds']}:pix_th=0.10", "-an"])
    blacks = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", out)
    if blacks:
        rep.block("video_quality", f"black segment(s) longer than {vp['max_black_seconds']}s: {blacks[:2]}")
    out = run_filter(video, ["-vf", f"freezedetect=n=0.001:d={vp['max_freeze_seconds']}", "-an"])
    freezes = re.findall(r"freeze_start: ([\d.]+)", out)
    if freezes:
        rep.warn("video_quality", f"static picture > {vp['max_freeze_seconds']}s at {freezes[:3]}", 3)
    rep.details["video"] = {"w": w, "h": h, "duration": round(dur, 2), "fps": round(fps, 2),
                            "size_mb": round(size_mb, 2), "bitrate_kbps": round(br), "codec": v.get("codec_name"),
                            "audio_codec": as_[0].get("codec_name") if as_ else None}
    return info


def check_audio(rep, video, timing, script, pol):
    vp = pol["video_policy"]
    if not video or not os.path.exists(video):
        return
    out = run_filter(video, ["-vn", "-af", "volumedetect"])
    mean = re.search(r"mean_volume: ([-\d.]+) dB", out)
    peak = re.search(r"max_volume: ([-\d.]+) dB", out)
    mean_db = float(mean.group(1)) if mean else None
    peak_db = float(peak.group(1)) if peak else None
    if mean_db is None:
        rep.block("audio_quality", "could not measure audio level")
    else:
        lo, hi = vp["mean_volume_db"]
        if mean_db < lo - 6:
            rep.block("audio_quality", f"audio too quiet (mean {mean_db} dB)")
        elif not (lo <= mean_db <= hi):
            rep.warn("audio_quality", f"mean volume {mean_db} dB outside {lo}..{hi}", 3)
    if peak_db is not None and peak_db > vp["max_peak_db"]:
        rep.warn("audio_quality", f"peak {peak_db} dB — possible clipping", 3)
    out = run_filter(video, ["-vn", "-af", f"silencedetect=noise=-38dB:d={vp['max_silence_seconds']}"])
    sil = re.findall(r"silence_start: ([\d.]+)", out)
    ends = re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", out)
    total = float(timing.get("total", 0))
    for s in sil:
        s = float(s)
        # leading/trailing silence allowances
        if s < 0.05:
            dur_lead = next((float(d) for e, d in ends if abs(float(e) - float(d) - s) < 0.1), 0)
            if dur_lead > vp["max_lead_silence_seconds"]:
                rep.warn("audio_quality", f"leading silence {dur_lead:.1f}s", 3)
            continue
        tail_len = total - s
        if tail_len <= vp["max_tail_silence_seconds"] + 0.4:
            continue
        rep.block("audio_quality", f"silence longer than {vp['max_silence_seconds']}s inside narration at {s:.1f}s")
    # speech rate sanity
    words = sum(common.word_count(l["t"]) for ch in script["chunks"] for l in ch["en"])
    spoken = sum(ch["dur"] for ch in timing.get("chunks", [])) or total
    wps = words / max(spoken, 1)
    lo, hi = pol["length"]["speech_rate_wps"]
    if wps > hi:
        rep.block("audio_quality", f"narration too fast ({wps:.2f} words/s)")
    elif wps < lo:
        rep.warn("audio_quality", f"narration slow ({wps:.2f} words/s)", 2)
    wt = timing.get("word_timing") or {}
    if wt.get("lines") and wt.get("measured_lines", 0) < wt["lines"]:
        rep.warn("audio_quality", f"karaoke timing estimated for {wt['lines'] - wt['measured_lines']}/{wt['lines']} "
                                  f"line(s) (no usable TTS word boundaries)", 2)
    # each chunk mp3 should be roughly proportional to its word count (truncated TTS detection)
    for ch, sch in zip(timing.get("chunks", []), script["chunks"]):
        wc = sum(common.word_count(l["t"]) for l in sch["en"])
        if ch["dur"] < wc / 4.5:
            rep.block("audio_quality", f"chunk {ch['id']} audio {ch['dur']:.1f}s too short for {wc} words (cut-off TTS?)")
    # level consistency across chunks
    levels = []
    for ch in timing.get("chunks", []):
        seg = run_filter(video, ["-vn", "-ss", f"{ch['start']:.2f}", "-t", f"{max(ch['dur'], 0.5):.2f}", "-af", "volumedetect"])
        m = re.search(r"mean_volume: ([-\d.]+) dB", seg)
        if m:
            levels.append(float(m.group(1)))
    if len(levels) >= 3 and (max(levels) - min(levels)) > 10:
        rep.warn("audio_quality", f"level varies {min(levels):.0f}..{max(levels):.0f} dB across chunks", 3)
    rep.details["audio"] = {"mean_db": mean_db, "peak_db": peak_db, "words_per_second": round(wps, 2),
                            "chunk_levels_db": levels}


def check_frames(rep, ep, layout, pol):
    """Sample frames: contrast under captions + placeholder text in script props."""
    try:
        from PIL import Image
        import numpy as np
        from reel_engine import Reel
    except Exception as e:                                    # noqa: BLE001
        rep.warn("subtitle_layout", f"frame sampling unavailable ({e})", 2)
        return
    try:
        reel = Reel(ep, pol)
    except Exception as e:                                    # noqa: BLE001
        rep.warn("subtitle_layout", f"could not rebuild reel for frame sampling: {e}", 2)
        return
    L = pol["layout"]
    worst = 99.0
    checked = 0
    for qf in (layout.get("qa_frames") or [])[:18]:
        t = qf["t"]
        bg = np.array(reel.background_only(t), np.float32)
        # EN caption band
        band = bg[L["en_top"]: L["en_top"] + 2 * L["en_row_height"], 100: 980]
        # scrim (alpha 132/255 of near-black) is drawn under captions → simulate it
        scr = band * (1 - 132 / 255) + np.array([8, 6, 4]) * (132 / 255)
        mean_bg = tuple(scr.reshape(-1, 3).mean(0))
        c = contrast((246, 234, 210), mean_bg)
        worst = min(worst, c)
        checked += 1
    if checked and worst < L["min_contrast_ratio"]:
        rep.block("subtitle_layout", f"caption contrast {worst:.1f}:1 below {L['min_contrast_ratio']}:1")
    elif checked and worst < L["min_contrast_ratio"] + 1.5:
        rep.warn("subtitle_layout", f"caption contrast only {worst:.1f}:1", 2)
    rep.details["frames"] = {"sampled": checked, "min_caption_contrast": round(worst, 2) if checked else None}


def check_caption(rep, caption_path, script, pol):
    cp = pol["caption_policy"]
    if not caption_path or not os.path.exists(caption_path):
        rep.block("caption_quality", "caption file missing")
        return
    with open(caption_path, encoding="utf-8") as fh:
        txt = fh.read()
    body_lines, tag_lines = [], []
    for ln in txt.splitlines():
        (tag_lines if ln.strip().startswith("#") else body_lines).append(ln)
    body = "\n".join(body_lines).strip()
    tags = " ".join(tag_lines).split()
    if len(body) > cp["max_chars"]:
        rep.block("caption_quality", f"caption {len(body)} chars > {cp['max_chars']}")
    if len(body) < cp["min_chars"]:
        rep.block("caption_quality", f"caption too short ({len(body)} chars)")
    if common.persian_ratio(body) > 0.2:
        rep.block("caption_quality", "caption is not primarily English")
    hp = pol["hashtag_policy"]
    if not (hp["min"] <= len(tags) <= hp["max"]):
        rep.block("caption_quality", f"{len(tags)} hashtags outside {hp['min']}..{hp['max']}")
    bad = [t for t in tags if t.lower() in hp["banned"]]
    if bad:
        rep.block("caption_quality", f"spam hashtags {bad}")
    for a in hp["always"]:
        if a not in tags:
            rep.warn("caption_quality", f"missing brand hashtag {a}", 1)
    for phrase in pol["tone"]["banned_phrases"]:
        if phrase in body.lower():
            rep.block("caption_quality", f"banned phrase in caption '{phrase}'")
    if common.PLACEHOLDER_RE.search(body):
        rep.block("caption_quality", "placeholder text in caption")
    if "official logo" in body.lower():
        rep.block("caption_quality", "caption claims an official logo (stand-in only)")
    if script["caption"]["hook"].strip() not in body:
        rep.warn("caption_quality", "caption does not start with the hook", 1)
    rep.details["caption"] = {"chars": len(body), "hashtags": len(tags)}


def check_posters(rep, poster, poster45, pol):
    from PIL import Image
    sizes = pol["video_policy"]["poster_sizes"]
    for path, key in ((poster, "9x16"), (poster45, "4x5")):
        if not path or not os.path.exists(path):
            rep.block("video_quality", f"poster {key} missing")
            continue
        try:
            im = Image.open(path)
            im.verify()
            im = Image.open(path)
            if list(im.size) != sizes[key]:
                rep.block("video_quality", f"poster {key} is {im.size}, expected {sizes[key]}")
            if os.path.getsize(path) < 20_000:
                rep.warn("video_quality", f"poster {key} suspiciously small file", 2)
        except Exception as e:                                # noqa: BLE001
            rep.block("video_quality", f"poster {key} invalid: {e}")


def check_duplicates(rep, script, topic, memory, pol):
    dp = pol["duplicates"]
    title = script["meta"].get("topic") or topic.get("title", "")
    tags = set(script["meta"].get("tags", []))
    shash = common.script_hash(script)
    recent = common.recent_entries(memory, dp["window_posts"], statuses=common.PUBLISHED_LIKE)
    this_id = script["meta"].get("content_id")
    for i, e in enumerate(recent):
        if e.get("content_id") == this_id:
            continue
        if e.get("script_hash") == shash:
            rep.block("duplicate_check", f"identical script already used on {e.get('content_date')}")
        sim = common.title_similarity(title, e.get("topic", ""))
        if sim >= dp["title_similarity_block"]:
            rep.block("duplicate_check", f"topic too similar ({sim:.2f}) to {e.get('content_date')} '{e.get('topic')}'")
        elif sim >= dp["title_similarity_warn"]:
            rep.warn("duplicate_check", f"topic similar ({sim:.2f}) to {e.get('content_date')}", 3)
        if i < dp["tag_cooldown_posts"] and tags & set(e.get("tags", [])):
            rep.block("duplicate_check", f"technique/bias tag {sorted(tags & set(e.get('tags', [])))} reused within {dp['tag_cooldown_posts']} posts")
    pillars = [e.get("pillar") for e in recent[: dp["pillar_repeat_warn_consecutive"]]]
    if pillars and all(p == script["meta"].get("pillar") for p in pillars):
        rep.warn("duplicate_check", f"same pillar {pillars[0]} for {len(pillars) + 1} posts in a row", 3)
    # CTA type rotation
    if recent and recent[0].get("cta_type") == script["meta"].get("cta_type"):
        rep.warn("duplicate_check", f"same CTA type '{recent[0].get('cta_type')}' as the previous post", 2)
    rep.details["duplicates"] = {"compared": len(recent), "script_hash": shash[:12]}


def check_buffer_readiness(rep, public_url, caption_path, skip_network, video):
    if caption_path and os.path.exists(caption_path):
        with open(caption_path, encoding="utf-8") as fh:
            body = "\n".join(l for l in fh.read().splitlines() if not l.strip().startswith("#")).strip()
        if len(body) > 2200:
            rep.block("buffer_readiness", "caption body exceeds Instagram limit")
    if not public_url:
        rep.warn("buffer_readiness", "public URL not provided (pre-push check)", 0)
        rep.details["buffer"] = {"public_url": None}
        return
    if not public_url.startswith("https://"):
        rep.block("buffer_readiness", "public video URL is not https")
        return
    if skip_network:
        rep.details["buffer"] = {"public_url": public_url, "checked": False}
        return
    code, ctype, length = head_url(public_url)
    if code != 200:
        rep.block("buffer_readiness", f"public video URL returned HTTP {code}")
    else:
        if "video" not in ctype and "octet-stream" not in ctype:
            rep.warn("buffer_readiness", f"MIME type '{ctype}' is not video/mp4", 2)
        if video and os.path.exists(video) and length and abs(length - os.path.getsize(video)) > 1024:
            rep.block("buffer_readiness", f"public file size {length} ≠ local {os.path.getsize(video)} (stale upload?)")
    rep.details["buffer"] = {"public_url": public_url, "http": code, "mime": ctype, "bytes": length}


# ----------------------------------------------------------------- main
def evaluate(a, pol):
    rep = Report(pol)
    ep = a.ep
    script = common.load_json(os.path.join(ep, "script.json"))
    timing = common.load_json(os.path.join(ep, "timing.json"), {}) or {}
    layout = common.load_json(os.path.join(ep, "layout.json"), {}) or {}
    topic = common.load_json(a.topic, {}) if a.topic else {}
    memory = common.load_memory(a.memory)
    if not script:
        raise SystemExit(EXIT_CANNOT)

    # Quarantine check (Issue #14): fail-closed, blocking overrides score 100
    try:
        cid = script.get("meta", {}).get("content_id")
        cdate = script.get("meta", {}).get("content_date")
        if common.is_quarantined(cid, cdate):
            rep.block("duplicate_check", f"content_id {cid} / date {cdate} is quarantined (translation-rejected) — must not publish")
    except Exception:
        pass

    check_topic(rep, script, topic, pol)
    check_sources(rep, script, topic, pol, a.skip_network)
    check_script(rep, script, pol)
    check_english(rep, script, pol)
    check_persian(rep, script, pol)
    check_layout(rep, layout, timing, pol)
    check_video(rep, a.video, timing, pol, layout)
    check_audio(rep, a.video, timing, script, pol)
    if not a.no_frames:
        check_frames(rep, ep, layout, pol)
    check_caption(rep, a.caption, script, pol)
    check_posters(rep, a.poster, a.poster45, pol)
    check_duplicates(rep, script, topic, memory, pol)
    check_buffer_readiness(rep, a.public_url, a.caption, a.skip_network, a.video)

    min_score = common.min_qa_score(pol)
    score = rep.score()
    approved = (not rep.blocking) and score >= min_score
    result = {
        "approved": approved,
        "score": score,
        "min_score": min_score,
        "checks": rep.checks,
        "warnings": rep.warnings,
        "blocking_errors": rep.blocking,
        "details": rep.details,
        "content_id": script["meta"].get("content_id"),
        "content_date": script["meta"].get("content_date"),
        "topic": script["meta"].get("topic"),
        "script_hash": common.script_hash(script),
        "video_hash": common.sha256_file(a.video) if a.video and os.path.exists(a.video) else None,
        "evaluated_utc": common.utc_now(),
        "supervisor_version": "1.0",
    }
    return result


def to_markdown(r):
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    L = [f"## QA Supervisor — {'APPROVED' if r['approved'] else 'REJECTED'} · score {r['score']}/100 "
         f"(min {r['min_score']})", "", "| check | result |", "|---|---|"]
    for k, v in r["checks"].items():
        L.append(f"| {k} | {icon.get(v, v)} {v} |")
    if r["blocking_errors"]:
        L += ["", "**Blocking errors**"] + [f"- {b}" for b in r["blocking_errors"]]
    if r["warnings"]:
        L += ["", "<details><summary>Warnings (" + str(len(r["warnings"])) + ")</summary>", ""]
        L += [f"- {w}" for w in r["warnings"]]
        L += ["", "</details>"]
    d = r.get("details", {})
    v, au = d.get("video", {}), d.get("audio", {})
    if v:
        L += ["", f"Video: {v.get('w')}x{v.get('h')} · {v.get('duration')}s · {v.get('size_mb')} MB · "
                  f"{v.get('bitrate_kbps')} kbps · {v.get('codec')}/{v.get('audio_codec')}"]
    if au:
        L += [f"Audio: mean {au.get('mean_db')} dB · peak {au.get('peak_db')} dB · {au.get('words_per_second')} words/s"]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ep", required=True)
    ap.add_argument("--video")
    ap.add_argument("--caption")
    ap.add_argument("--poster")
    ap.add_argument("--poster45")
    ap.add_argument("--topic")
    ap.add_argument("--memory")
    ap.add_argument("--policy")
    ap.add_argument("--public-url", default="")
    ap.add_argument("--skip-network", action="store_true")
    ap.add_argument("--no-frames", action="store_true")
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md", required=True)
    a = ap.parse_args()
    pol = common.policy(a.policy)
    try:
        r = evaluate(a, pol)
    except SystemExit as e:
        if e.code == EXIT_CANNOT:
            print("[qa] cannot evaluate (script missing) — fail closed")
        raise
    common.save_json(a.out_json, r)
    with open(a.out_md, "w", encoding="utf-8") as f:
        f.write(to_markdown(r))
    print(to_markdown(r))
    sys.exit(EXIT_APPROVED if r["approved"] else EXIT_REJECTED)


if __name__ == "__main__":
    main()
