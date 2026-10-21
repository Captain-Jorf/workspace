"""Quality Supervisor — English-only, Technology × Metacognition.

Independent gate before Buffer. Checks:
- CONTENT_LANGUAGE=en fail-closed
- Only English script/narration/karaoke, no Persian layer
- Metadata language=en
- Technology relevance required, metacognition relevance required
- No translator network call (no mymemory/google in production path)
- No Persian subtitle layer in layout.json
- No fake citation/URL, no unsupported claims
- Quarantine block (reel-2026-09-15)
- Caption <=2200, valid audio/video 9:16, 60-120s, karaoke safe-zone
- Reviewer report if present: score>=85, no blocking, tech+metacog true
- Duplicate, Buffer readiness, etc.

This module also exposes the deterministic PRE-RENDER TEXT QA gate
(pre_render_text_gate) — the exact blocker functions above, run on script text
BEFORE TTS/render by build/pipeline.py and build/content_producer.py. Final QA
stays mandatory and authoritative for everything about rendered media.

Exit 0 approved, 20 rejected, 21 cannot evaluate.
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
import common
import cursor_qa
import layout_gate
import palette_qa
import text_norm
import visual_plan

CHECKS = ["content_language", "english_only", "technology_relevance", "metacognition_relevance",
          "topic_relevance", "source_quality", "script_quality", "english_quality",
          "subtitle_layout", "audio_quality", "video_quality", "caption_quality",
          "duplicate_check", "buffer_readiness", "reviewer_check", "visual_semantics"]
WEIGHTS = {"content_language": 15, "english_only": 15, "technology_relevance": 10, "metacognition_relevance": 10,
           "topic_relevance": 5, "source_quality": 8, "script_quality": 10, "english_quality": 8,
           "subtitle_layout": 8, "audio_quality": 5, "video_quality": 5, "caption_quality": 4,
           "duplicate_check": 6, "buffer_readiness": 4, "reviewer_check": 12,
           "visual_semantics": 10, "minimal_contract": 10}
EXIT_APPROVED, EXIT_REJECTED, EXIT_CANNOT = 0, 20, 21

# --- Reviewer lifecycle fix (issue #30, run 35484782317) ---
# Candidate-bound Reviewer reports: every report must be bound to the exact
# candidate it reviewed. Malformed reports are classified as malformed, not
# score-0. Stale/mismatched reports are never silently applied to another
# script. Static Fallback is not applicable for review.
REVIEWER_REQUIRED_FIELDS = {
    "approved": bool,
    "score": int,
    "technology_relevance": bool,
    "metacognition_relevance": bool,
    "blocking_errors": list,
    "unsupported_claims": list,
}

def _validate_reviewer_output(out):
    if not isinstance(out, dict):
        return False, "reviewer output not a dict"
    for field, typ in REVIEWER_REQUIRED_FIELDS.items():
        if field not in out:
            return False, f"missing required field: {field}"
        val = out[field]
        if typ is bool:
            if not isinstance(val, bool):
                return False, f"field {field} not bool"
        elif typ is int:
            if not isinstance(val, int) or isinstance(val, bool):
                return False, f"field {field} not int"
            if field == "score" and not (0 <= val <= 100):
                return False, f"score out of range: {val}"
        elif typ is list:
            if not isinstance(val, list):
                return False, f"field {field} not list"
        else:
            if not isinstance(val, typ):
                return False, f"field {field} not {typ.__name__}"
    return True, ""


def ffmpeg_bin():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
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

def probe(path):
    fp = ffprobe_bin()
    if fp:
        r = subprocess.run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return json.loads(r.stdout)
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
    r = subprocess.run([ff, "-hide_banner", "-nostats", "-i", path] + args + ["-f", "null", "-"], capture_output=True, text=True)
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
    except Exception as e:
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

# --- checks

def check_content_language(rep, script, pol):
    try:
        common.assert_content_language_en()
    except SystemExit as e:
        rep.block("content_language", str(e))
        return
    # Check metadata
    lang = script.get("meta", {}).get("language") or script.get("meta", {}).get("content_language") or ""
    if lang and lang != "en":
        rep.block("content_language", f"metadata language={lang} must be en")
    # Check policy
    cl = pol.get("content_language") or pol.get("page", {}).get("content_language") or ""
    if cl and cl != "en":
        rep.block("content_language", f"policy content_language={cl} must be en")
    rep.details["content_language"] = {"policy": cl, "meta": lang, "env": os.environ.get("CONTENT_LANGUAGE","en")}

def check_english_only(rep, script, pol):
    # No Persian ratio in narration — check overall and per-line
    text = " ".join(l["t"] for ch in script["chunks"] for l in ch["en"])
    if common.persian_ratio(text) > 0.05:
        rep.block("english_only", f"narration contains Persian characters (ratio {common.persian_ratio(text):.2f})")
    # Per-line check: any line with high Persian ratio
    for ch in script["chunks"]:
        for en_obj in ch.get("en", []):
            t = en_obj.get("t","") if isinstance(en_obj, dict) else str(en_obj)
            if common.persian_ratio(t) > 0.3:
                rep.block("english_only", f"EN line contains Persian: '{t[:40]}' ratio {common.persian_ratio(t):.2f}")
                break
    # No FA lines should have content
    fa_count = sum(len(ch.get("fa", [])) for ch in script["chunks"])
    if fa_count > 0:
        fa_texts = [fa for ch in script["chunks"] for fa in ch.get("fa", []) if fa]
        if fa_texts:
            rep.block("english_only", f"script contains FA lines ({len(fa_texts)} non-empty) — English-only required")
    # No translation engine that is Persian
    engine = script.get("meta", {}).get("translation_engine", "")
    if engine in ("mymemory", "google", "fixture", "curated", "curated-trend"):
        rep.block("english_only", f"translation engine {engine} indicates Persian path — not allowed in English-only production")
    # Check no Persian in caption
    cap_text = json.dumps(script.get("caption", {}), ensure_ascii=False)
    if common.persian_ratio(cap_text) > 0.05:
        rep.block("english_only", "caption contains Persian characters")
    # Also check caption per-line
    cap_hook = script.get("caption", {}).get("hook","")
    if common.persian_ratio(cap_hook) > 0.3:
        rep.block("english_only", f"caption hook contains Persian")
    rep.details["english_only"] = {"fa_lines": fa_count, "persian_ratio": round(common.persian_ratio(text),3), "engine": engine}

def check_technology_relevance(rep, script, topic, pol):
    tech_angle = (script.get("meta", {}).get("technology_angle") or "").lower()
    text = " ".join(l["t"] for ch in script["chunks"] for l in ch["en"]).lower()
    meta_concept = (script.get("meta", {}).get("metacognition_concept") or "").lower()

    tech_keywords = ["ai", "software", "code", "coding", "program", "debugg", "product", "startup", "digital", "automation", "autocomplete", "llm", "model", "algorithm", "app", "tech", "computer", "api", "github", "stack", "engineer", "metric", "user research", "attention", "notification"]
    tech_hits = sum(1 for kw in tech_keywords if kw in text or kw in tech_angle)
    if not tech_angle:
        rep.block("technology_relevance", "technology_angle missing in meta")
    elif tech_hits < 2:
        rep.block("technology_relevance", f"technology relevance weak (hits={tech_hits}) text does not contain tech domain")
    elif tech_hits < 4:
        rep.warn("technology_relevance", f"technology relevance moderate (hits={tech_hits})", 3)

    # Trend policy: if trend, must be tech-related
    if topic.get("evidence_mode") == "trend" or "trend" in script.get("meta", {}).get("playbook",""):
        # Check if discovery source is tech
        disc = topic.get("discovery_source") or {}
        disc_name = (disc.get("name","") + " " + topic.get("title","")).lower()
        allowed_domains = pol.get("trend_policy", {}).get("allowed_domains", ["AI", "software", "coding", "product", "digital behavior", "future of work"])
        # Simple check: title should contain tech
        if tech_hits < 2:
            rep.block("technology_relevance", f"trend topic '{topic.get('title','')}' not tech-relevant per trend_policy")
    rep.details["technology"] = {"technology_angle": tech_angle, "hits": tech_hits, "concept": meta_concept}

def check_metacognition_relevance(rep, script, pol):
    text = " ".join(l["t"] for ch in script["chunks"] for l in ch["en"]).lower()
    concept = (script.get("meta", {}).get("metacognition_concept") or "").lower()
    metacog_keywords = ["metacognit", "bias", "calibration", "confidence", "judg", "thinking about thinking", "monitor", "offload", "illusion", "planning fallacy", "goodhart", "sunk cost", "automation bias", "attention", "focus", "learning", "memory", "recall", "explain", "understand", "know", "watcher", "de-bug", "debug"]
    hits = sum(1 for kw in metacog_keywords if kw in text or kw in concept)
    if not concept:
        rep.block("metacognition_relevance", "metacognition_concept missing")
    elif hits < 2:
        rep.block("metacognition_relevance", f"metacognition relevance weak (hits={hits})")
    elif hits < 4:
        rep.warn("metacognition_relevance", f"metacognition relevance moderate (hits={hits})", 2)
    rep.details["metacognition"] = {"concept": concept, "hits": hits}

def check_topic(rep, script, topic, pol):
    meta = script.get("meta", {})
    title = (meta.get("topic") or topic.get("title") or "").lower()
    text = " ".join(l["t"] for ch in script["chunks"] for l in ch["en"]).lower()
    allowed = pol.get("allowed_topics", [])
    vocab = ["metacognit", "bias", "decision", "critical", "learn", "memory", "attention", "focus", "problem", "self-aware", "probabilit", "mental model", "confidence", "recall", "study", "think", "judg", "plan", "monitor", "evaluate", "calibrat", "forecast", "explain", "understand", "notice", "listen", "remember", "forget", "practice", "mind", "brain", "check", "confident", "sure", "watcher", "expert", "mistake", "skill", "guess", "predict", "estimate", "test", "question", "habit", "feel", "know", "trust", "distract", "review", "reason", "evidence", "assum", "belie", "opinion", "certain", "doubt", "wrong", "error", "compare", "score", "measure", "code", "ai", "software", "product", "tech"]
    hits = sum(1 for v in vocab if v in text)
    if hits < 5:
        rep.block("topic_relevance", f"narration barely touches field ({hits} terms)")
    elif hits < 8:
        rep.warn("topic_relevance", f"weak field vocabulary ({hits})", 2)
    negs = []
    for kw in pol.get("negative_keywords", []):
        k = kw.lower()
        if len(k) <= 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", f" {title} "):
                negs.append(kw)
        elif k in title:
            negs.append(kw)
    if negs:
        rep.block("topic_relevance", f"banned keywords {negs}")
    for kw in ("suicide", "self-harm", "overdose", "kill yourself", "porn"):
        if kw in text:
            rep.block("topic_relevance", f"sensitive '{kw}'")
    rep.details["topic"] = {"field_terms": hits}

def source_tier(url, label, pol):
    """DELEGATED single source (issue #22): tier derivation now lives in
    common.source_tier so the pre-render text gate, the evidence packet and
    final QA share ONE matcher. This alias keeps the QA-supervisor API intact."""
    return common.source_tier(url, label, pol)

def check_sources(rep, script, topic, pol, skip_network):
    srcs = script.get("sources", [])
    text = " ".join(l["t"] for ch in script["chunks"] for l in ch["en"]).lower()
    # Shared matcher (common.claim_word_matches) built on the policy pattern list
    # itself — the producer's pre-gate, the pre-render text gate and this QA
    # blocker read the SAME implementation and can never diverge, and no second
    # keyword list exists anywhere.
    claims = common.claim_word_matches(text, pol)
    # Shared matcher (common.find_numeric_claims) so the producer's pre-gate and
    # this QA blocker can never diverge. Threshold unchanged: any statistic that
    # is not explicitly backed by verified evidence blocks the reel.
    numbers = common.find_numeric_claims(text)
    ev = [s for s in srcs if s.get("role") == "evidence"] or srcs
    tiers = [source_tier(s.get("url"), s.get("label"), pol) for s in ev]
    best = common.best_tier(tiers)
    rep.details["sources"] = {"evidence": len(ev), "best_tier": best, "claim_words": claims, "numbers": numbers}
    mode = script.get("meta", {}).get("evidence_mode")
    if claims and best not in ("A", "B"):
        rep.block("source_quality", f"claim words {claims} without tier A/B")
    if numbers and not script.get("meta", {}).get("stats_verified"):
        rep.block("source_quality", f"statistics {numbers} cannot be verified automatically")
    if mode == "limited-claims" and claims:
        rep.block("source_quality", "limited-claims mode must not make research claims")
    # Fake citation / URL check
    for s in srcs:
        url = s.get("url","")
        if url and not url.startswith("https://"):
            rep.warn("source_quality", f"non-HTTPS {url}", 2)
        if url and "example.com" in url:
            rep.block("source_quality", f"fake URL {url}")
        label = s.get("label","")
        if "fabricated" in label.lower() or "fake" in label.lower():
            rep.block("source_quality", f"fake citation {label}")
    # Unsupported claims from script
    unsupported = script.get("claims", []) or []
    # Also check reviewer report
    # If claims mention numbers without source
    for kw in pol["tone"]["banned_phrases"][:5]:
        if kw in text:
            rep.block("source_quality", f"unsupported certainty '{kw}'")

def check_script(rep, script, pol):
    beats = [ch.get("beat") for ch in script["chunks"]]
    need = pol["script_structure"]
    missing = [b for b in need if b not in beats]
    if missing:
        rep.block("script_quality", f"missing beats {missing}")
    order = [b for b in beats if b in need]
    dedup = []
    for b in order:
        if not dedup or dedup[-1] != b:
            dedup.append(b)
    if dedup != [b for b in need if b in dedup]:
        rep.block("script_quality", f"beats out of order {dedup}")
    # Single-sourced counting (common.narration_lines/spoken_word_count) — the
    # deterministic PRE-RENDER gate and this QA check therefore count identically.
    lines = common.narration_lines(script)
    words = common.spoken_word_count(script)
    lo, hi = pol["length"]["narration_words"]
    if words < lo * 0.8 or words > hi * 1.15:
        rep.block("script_quality", f"words {words} outside {lo}-{hi}")
    elif words < lo or words > hi:
        rep.warn("script_quality", f"words {words} slightly outside", 2)
    long_lines = [l for l in lines if common.word_count(l) > pol["length"]["max_words_per_line"]]
    if long_lines:
        rep.warn("script_quality", f"{len(long_lines)} long lines", 2)
    hook = next((ch["en"][0]["t"] for ch in script["chunks"] if ch.get("beat") == "hook"), "")
    hp = pol["hook_policy"]
    hw = common.word_count(hook)
    if hw > hp["max_words"] + 5:
        rep.block("script_quality", f"hook too long {hw} words")
    elif hw > hp["max_words"]:
        rep.warn("script_quality", f"hook a bit long {hw}", 1)
    hl = hook.lower()
    if hp["must_be_question_or_contrast"] and "?" not in hook and not any(m in hl for m in hp["contrast_markers"]):
        rep.warn("script_quality", "hook not question nor contrast", 3)
    for bad in hp["banned_hook_phrases"]:
        if bad in hl:
            rep.block("script_quality", f"clickbait '{bad}'")
    for bad in pol["tone"]["fear_words"]:
        if bad in hl:
            rep.block("script_quality", f"fear '{bad}'")
    for op in pol["tone"]["forbidden_openers"]:
        if hl.startswith(op) or op in hl[:40]:
            rep.block("script_quality", f"banned opener '{op}'")
    # hook ↔ body coherence
    hook_toks = {t[:5] for t in common.normalize_title(hook).split()}
    body = common.normalize_title(" ".join(lines[1:]))
    body_toks = {t[:5] for t in body.split()}
    if hook_toks and not (hook_toks & body_toks):
        rep.warn("script_quality", "hook shares no content words with body", 4)
    tech = " ".join(l["t"] for ch in script["chunks"] if ch.get("beat") == "technique" for l in ch["en"]).lower()
    if not any(k in tech for k in ("try", "before", "after", "ask", "write", "set", "pick", "say", "next", "close", "pause", "name", "review", "recall", "compare", "keep", "explain", "test", "build", "run")):
        rep.block("script_quality", "technique has no actionable instruction")
    ending = " ".join(l["t"] for ch in script["chunks"] if ch.get("beat") == "ending" for l in ch["en"])
    cp = pol["cta_policy"]
    for bad in cp["banned_phrases"]:
        if bad in ending.lower():
            rep.block("script_quality", f"engagement-bait '{bad}'")
    if common.word_count(ending) > cp["max_words"]:
        rep.warn("script_quality", f"ending too long {common.word_count(ending)}", 1)
    for l in lines:
        if common.PLACEHOLDER_RE.search(l):
            rep.block("script_quality", f"placeholder '{l[:50]}'")
    rep.details["script"] = {"words": words, "hook_words": hw, "beats": beats}

def check_english(rep, script, pol):
    lines = common.narration_lines(script)
    text = " ".join(lines)
    low = text.lower()
    # Issue #32: the informality markers use STRAIGHT apostrophes while Groq
    # narration carried curly ones (You're/brain's/you've with U+2019), so a
    # reel full of contractions was flagged "no contractions — formal".
    # Normalize first — the same single-sourced text_norm the renderer uses —
    # so the detector sees the same representation as TTS/timing/display.
    low = text_norm.normalize_text(low)
    markers = sum(1 for m in pol["tone"]["informality_markers"] if m in low)
    if markers == 0:
        rep.warn("english_quality", "no contractions — formal", 3)
    elif markers < 2:
        rep.warn("english_quality", "few contractions", 1)
    formal = ["furthermore", "moreover", "thus", "hence", "utilize", "in conclusion", "it is imperative", "aforementioned", "notwithstanding", "heretofore"]
    fh = [w for w in formal if w in low]
    if fh:
        rep.warn("english_quality", f"academic {fh}", 2)
    for bad in pol["tone"]["banned_phrases"]:
        if bad in low:
            rep.block("english_quality", f"banned '{bad}'")
    if re.search(r"https?://|www\.", low):
        rep.block("english_quality", "URL in narration")
    avg = sum(common.word_count(l) for l in lines) / max(1, len(lines))
    if avg > 18:
        rep.warn("english_quality", f"avg line {avg:.1f} words long", 2)
    rep.details["english"] = {"informality": markers, "avg": round(avg,1)}


def check_text_renderability(rep, script, pol, timing=None):
    """Issue #32 §1/§2/§11 — HARD BLOCKERS regardless of score:
    * U+FFFD replacement character anywhere in visible/spoken text;
    * any character without a glyph in the ACTUAL production font cmap
      (the intersection of en-400..en-800 — never a guessed allowlist,
      never OS font fallback);
    * TTS/timing/display source mismatch after normalization (a stage that
      normalized on its own would desynchronize the karaoke).
    The renderer applies the identical gate before frame 0; QA re-checks it
    here so a regression in ANY stage is rejected at final QA."""
    blockers = text_norm.glyph_gate_issues(script)
    for b in blockers:
        rep.block("english_quality", f"unsupported glyph: {b}")
    for t in text_norm.script_display_texts(script):
        if text_norm.contains_replacement_char(t):
            rep.block("english_quality",
                      f"U+FFFD replacement character in display text {(t or '')[:60]!r} — "
                      "corrupted text must never reach TTS/render")
    if timing is not None and timing.get("chunks"):
        for p in text_norm.parity_issues(script, timing):
            rep.block("subtitle_layout",
                      f"source/display mismatch after normalization: {p}")
    try:
        n_cov = len(text_norm.supported_codepoints())
    except FileNotFoundError:
        n_cov = 0
    rep.details["text_renderability"] = {
        "glyph_blockers": len(blockers),
        "parity_checked": bool(timing and timing.get("chunks")),
        "font_weights": list(text_norm.PRODUCTION_FONT_WEIGHTS),
        "supported_codepoints": n_cov,
    }

def check_layout(rep, layout, timing, pol):
    L = pol["layout"]
    if not layout:
        rep.block("subtitle_layout", "layout.json missing")
        return
    # English-only: fa must be empty
    if layout.get("fa"):
        if len(layout["fa"]) > 0:
            rep.block("english_only", f"layout contains FA layer ({len(layout['fa'])} entries) — English-only required")
    for e in layout.get("en", []):
        x0, y0, x1, y1 = e["bbox"]
        if e.get("direction") != "ltr":
            rep.block("subtitle_layout", "EN not LTR")
        if y0 < L["safe_top"] - 10 or y1 > L["safe_bottom"]:
            rep.block("subtitle_layout", f"EN outside safe zone {y0}-{y1}")
        if x0 < 0 or x1 > L["width"]:
            rep.block("subtitle_layout", f"EN wider than frame")
        if e["rows"] > L["en_max_rows"]:
            rep.block("subtitle_layout", f"EN {e['rows']} rows > {L['en_max_rows']}")
        if x1 > L["right_button_column_x"] and L["right_button_column_y"][0] <= y1 <= L["right_button_column_y"][1]:
            rep.warn("subtitle_layout", f"EN reaches button column", 1)
    total = float(timing.get("total", 0))
    for ch in timing.get("chunks", []):
        for ln in ch["lines"]:
            for w in ln["words"]:
                if w["end"] > total + 0.05 or w["start"] < 0:
                    rep.block("subtitle_layout", f"karaoke '{w['w']}' outside audio")
                    break
    en_max = max((e["bbox"][3] for e in layout.get("en", [])), default=0)
    rep.details["layout"] = {"en_lines": len(layout.get("en", [])), "fa_lines": len(layout.get("fa", [])), "en_bottom_max": en_max}

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
        if abs(w / max(h, 1) - 9/16) > vp["aspect_tolerance"]:
            rep.block("video_quality", f"aspect {w}x{h} not 9:16")
        elif h < 1280:
            rep.block("video_quality", f"resolution {w}x{h} too low")
        else:
            rep.warn("video_quality", f"resolution {w}x{h}", 1)
    if (v.get("codec_name") or "").lower() not in ("h264", "avc1"):
        rep.block("video_quality", f"codec {v.get('codec_name')} not H264")
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    lo, hi = pol["length"]["hard_seconds"]
    if dur < lo or dur > hi:
        rep.block("video_quality", f"duration {dur:.1f}s outside {lo}-{hi}")
    tlo, thi = pol["length"]["target_seconds"]
    if lo <= dur <= hi and not (tlo <= dur <= thi):
        rep.warn("video_quality", f"duration {dur:.1f}s outside target {tlo}-{thi}", 1)
    try:
        num, den = v.get("r_frame_rate", "30/1").split("/")
        fps = float(num) / float(den or 1)
    except Exception:
        fps = 0
    if not (vp["fps"][0] <= fps <= vp["fps"][1]):
        rep.warn("video_quality", f"fps {fps:.1f}", 1)
    size_mb = int(info["format"].get("size", 0)) / 1e6
    if not (vp["file_size_mb"][0] <= size_mb <= vp["file_size_mb"][1]):
        rep.block("video_quality", f"size {size_mb:.1f} MB outside")
    br = int(info["format"].get("bit_rate") or 0) / 1000
    if br and not (vp["bitrate_kbps"][0] <= br <= vp["bitrate_kbps"][1]):
        rep.warn("video_quality", f"bitrate {br:.0f}", 1)
    ok, err = decode_ok(video)
    if not ok:
        rep.block("video_quality", f"decode error {err[-120:]}")
    if as_:
        adur = float(as_[0].get("duration") or dur)
        if abs(adur - dur) > vp["max_av_duration_diff_seconds"]:
            rep.block("audio_quality", f"audio {adur:.2f}s vs video {dur:.2f}s")
    exp = float(timing.get("total", dur))
    if abs(exp - dur) > 1.5:
        rep.warn("video_quality", f"timing {exp:.1f}s vs mp4 {dur:.1f}s", 1)
    out = run_filter(video, ["-vf", f"blackdetect=d={vp['max_black_seconds']}:pix_th=0.10", "-an"])
    blacks = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", out)
    if blacks:
        rep.block("video_quality", f"black > {vp['max_black_seconds']}s {blacks[:2]}")
    out = run_filter(video, ["-vf", f"freezedetect=n=0.001:d={vp['max_freeze_seconds']}", "-an"])
    freezes = re.findall(r"freeze_start: ([\d.]+)", out)
    if freezes:
        rep.warn("video_quality", f"freeze > {vp['max_freeze_seconds']}s at {freezes[:3]}", 2)
    rep.details["video"] = {"w": w, "h": h, "duration": round(dur,2), "fps": round(fps,2), "size_mb": round(size_mb,2), "codec": v.get("codec_name")}

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
        rep.block("audio_quality", "could not measure audio")
    else:
        lo, hi = vp["mean_volume_db"]
        if mean_db < lo - 6:
            rep.block("audio_quality", f"quiet {mean_db} dB")
        elif not (lo <= mean_db <= hi):
            rep.warn("audio_quality", f"mean {mean_db} dB outside {lo}..{hi}", 2)
    if peak_db is not None and peak_db > vp["max_peak_db"]:
        rep.warn("audio_quality", f"peak {peak_db} dB clipping", 2)
    total = float(timing.get("total", 0))
    words = common.spoken_word_count(script)
    spoken = sum(ch["dur"] for ch in timing.get("chunks", [])) or total
    wps = words / max(spoken, 1)
    lo, hi = pol["length"]["speech_rate_wps"]
    if wps > hi:
        rep.block("audio_quality", f"fast {wps:.2f} w/s")
    elif wps < lo:
        rep.warn("audio_quality", f"slow {wps:.2f}", 1)
    rep.details["audio"] = {"mean_db": mean_db, "peak_db": peak_db, "wps": round(wps,2)}

def check_frames(rep, ep, layout, pol):
    try:
        from PIL import Image
        import numpy as np
        from reel_engine import Reel
    except Exception as e:
        rep.warn("subtitle_layout", f"frame sampling unavailable {e}", 1)
        return
    try:
        reel = Reel(ep, pol)
    except Exception as e:
        rep.warn("subtitle_layout", f"could not rebuild reel {e}", 1)
        return
    L = pol["layout"]
    worst = 99.0
    checked = 0
    for qf in (layout.get("qa_frames") or [])[:18]:
        t = qf["t"]
        bg = np.array(reel.background_only(t), np.float32)
        band = bg[L["en_top"]: L["en_top"] + 2 * L["en_row_height"], 100: 980]
        scr = band * (1 - 132/255) + np.array([8,6,4]) * (132/255)
        mean_bg = tuple(scr.reshape(-1,3).mean(0))
        c = contrast((246,234,210), mean_bg)
        worst = min(worst, c)
        checked += 1
    if checked and worst < L["min_contrast_ratio"]:
        rep.block("subtitle_layout", f"contrast {worst:.1f}:1 below {L['min_contrast_ratio']}:1")
    elif checked and worst < L["min_contrast_ratio"] + 1.5:
        rep.warn("subtitle_layout", f"contrast only {worst:.1f}:1", 1)
    rep.details["frames"] = {"sampled": checked, "min_contrast": round(worst,2) if checked else None}

# ---------------------------------------------------------------------------
# FINAL RENDERED VISUAL QA (issue #24)
#
# A metadata-only claim of visual variety is not enough: the RENDERED frames
# of the actual MP4 are sampled across the whole Reel and verified.
# Deterministic pixel-level checks (no OCR, no network):
#   * the legacy cold code/terminal-card signature must not appear anywhere;
#   * an unjustified typing cursor bar must not appear;
#   * no blue/navy/cyan/purple (cold) hue may appear in the brand treatment;
#   * the subtitle band must not be obstructed by bright overlay content;
#   * adjacent scenes must not be perceptual near-duplicates;
#   * the reel must contain enough DISTINCT rendered scenes (a repeated
#     underlying image presented as several scenes is caught here too —
#     the same crop/zoom of one image hashes the same);
#   * the code scenes actually rendered must equal the plan's justified set.
# ---------------------------------------------------------------------------

def _decode_frame(video, t, tmp):
    """Decode one frame of the REAL rendered MP4 at time t → numpy RGB array."""
    ff = ffmpeg_bin()
    if not ff:
        return None
    png = os.path.join(tmp, f"qa_frame_{t:.2f}.png")
    r = subprocess.run([ff, "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", video,
                        "-frames:v", "1", "-y", png],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not os.path.exists(png):
        return None
    try:
        from PIL import Image
        im = Image.open(png).convert("RGB")
        import numpy as np
        return np.array(im, dtype=np.int16)
    except Exception:
        return None


COLD_CARD_PX = 20000  # the legacy card was ~135k solid cold px; H.264
# chroma artifacts on warm frames stay in the low thousands


def detect_code_card(arr):
    """Legacy generic code/terminal card signature: a large COLD dark region
    (the old card was (18,20,24) — blue-dominant, unlike any warm brand
    color). Counts qualifying pixels in the central scene band; the legacy
    card registered ~135k px, compression noise on warm frames stays well
    under COLD_CARD_PX. The cold-dominance formula itself lives ONLY in
    palette_qa (single source); this detector adds the legacy card's
    darkness conditions."""
    if arr is None or arr.shape[0] < 1000 or arr.shape[1] < 600:
        return 0
    h = arr.shape[0]
    band = arr[int(h * 0.30):int(h * 0.80), int(arr.shape[1] * 0.08):int(arr.shape[1] * 0.92)]
    r, g, b = band[..., 0], band[..., 1], band[..., 2]
    mask = palette_qa.cold_dominance_mask(band, 2) & (r < 70) & (g < 70)
    return int(mask.sum())


def detect_cursor(arr, pol=None):
    """The typing cursor detector: a SOLID vertical gold bar with connected-component
    shape ownership (issue #28). Candidate vertical bars that belong to large shapes
    (ellipses, rings, cards, diagrams, network lines, logos) are rejected."""
    return cursor_qa.detect_cursor(arr, pol)


def check_visuals(rep, ep, script, video, pol, no_frames):
    """visual_semantics: plan-level deterministic gate + rendered-frame proof."""
    plan = common.load_json(os.path.join(ep, "visual_plan.json"))
    has_chunks = bool(script.get("chunks"))
    if not plan or not plan.get("scenes"):
        if has_chunks:
            plan = visual_plan.build_visual_plan(script, pol)
        else:
            rep.details["visuals"] = {"present": False,
                                      "note": "script without chunks (pre-plan test fixture)"}
            return
    issues = visual_plan.visual_semantic_issues(plan, script, pol)
    for i in issues:
        rep.block("visual_semantics", i)
    try:
        prov = visual_plan.photo_provenance_summary(plan)
    except Exception:
        prov = {}
    try:
        rep.details["visuals"] = {"present": True, **visual_plan.plan_summary(plan),
                                  "photo_provenance": prov}
    except Exception:
        rep.details["visuals"] = {"present": True}

    # Issue #32 §9: a zero-photo reel is NOT "fully visually complete". The
    # photo service stays fail-soft (never blocks the pipeline), but QA must
    # say WHY retrieval failed and flag the degraded mix for human review.
    if prov.get("degraded"):
        outcomes = prov.get("retrieval_outcomes") or {}
        rep.warn("visual_semantics",
                 "photo_mix_degraded — photo-designated scenes fell back to "
                 f"procedural visuals (designated={prov.get('photo_designated', 0)}, "
                 f"retrieved={prov.get('photos_retrieved', 0)}, reasons={outcomes}); "
                 "the reel must not be described as photographic variety", 2)
    elif prov.get("retrieval_outcomes"):
        soft = {"search_http_error", "search_timeout", "no_results",
                "no_allowed_license", "disabled", "cache_hit"}
        soft_hits = {k: v for k, v in (prov.get("retrieval_outcomes") or {}).items()
                     if k in soft and v}
        if soft_hits and prov.get("photos_retrieved", 0) > 0:
            rep.warn("visual_semantics",
                     f"Openverse partially unavailable ({soft_hits}) — clean "
                     "procedural fallback applied", 1)

    # Issue #32 §5/§11: re-run the DETERMINISTIC layout gate on the declared
    # items recorded in layout.json (the authoritative collision/density
    # source — glyph/layout verdicts never come from OCR alone). Overlapping
    # semantic text, hidden text, unreadable labels, lines through text and
    # safe-zone intrusion block regardless of score.
    layout = common.load_json(os.path.join(ep, "layout.json"), {}) or {}
    scene_items = layout.get("scene_items") or {}
    zone = layout.get("scene_zone") or {}
    zy = int(zone.get("y", 0))
    if scene_items:
        Lz = pol["layout"]
        band_bottom = int(Lz.get("en_top", 200)) + 3 * int(Lz.get("en_row_height", 78)) + 10
        safe_boxes = [
            ("subtitle", (0, 0, int(Lz.get("width", 1080)), band_bottom)),
            ("bottom-reserved", (0, int(Lz.get("safe_bottom", 1450)),
                                 int(Lz.get("width", 1080)), int(Lz.get("height", 1920)))),
        ]
        declared_issues = 0
        for sid, items in scene_items.items():
            shifted = []
            for it in items or []:
                it2 = dict(it)
                b = it2.get("bbox") or [0, 0, 0, 0]
                it2["bbox"] = [b[0], b[1] + zy, b[2], b[3] + zy]
                shifted.append(it2)
            for issue in layout_gate.check_items(shifted, safe_boxes=safe_boxes):
                rep.block("visual_semantics", f"declared layout of scene {sid}: {issue}")
                declared_issues += 1
        rep.details["visuals"]["declared_layout_gate"] = {
            "scenes_checked": len(scene_items), "issues": declared_issues}
    if not (video and os.path.exists(video)) or no_frames:
        rep.details["visuals"]["rendered_frames_checked"] = False
        return

    # ---- rendered-frame verification on the actual MP4 -------------------
    import numpy as np
    timing = common.load_json(os.path.join(ep, "timing.json"), {}) or {}
    layout = common.load_json(os.path.join(ep, "layout.json"), {}) or {}
    scenes = plan.get("scenes") or []
    total = float(timing.get("total", 0) or 0)
    cfg = palette_qa.load_policy(pol)
    windows, timing_source = scene_windows(layout, timing, scenes, total)
    rep.details["visuals"]["scene_timing_source"] = timing_source
    if not windows:
        rep.block("visual_semantics",
                  "no scene window could be determined for the rendered-frame QA "
                  "(layout.json carries no scene_windows and the timing fallback is empty) — "
                  "final rendered-media validation is mandatory")
        return
    import tempfile
    tmp = tempfile.mkdtemp(prefix="qa_visuals_")
    frames = []          # [(scene_id, beat, t_mid, [decoded arrays], [sample times])]
    decoded = 0
    budget = palette_qa.frame_budget(cfg, len(windows))
    try:
        for sid, beat, a, b in windows:
            if decoded >= budget:
                break
            times = palette_qa.sample_times(a, b, cfg, total=total)
            arrs = []
            for t in times:
                if decoded >= budget:
                    break
                arr = _decode_frame(video, t, tmp)
                if arr is None:
                    rep.block("visual_semantics",
                              f"could not decode a rendered frame for scene {sid} — "
                              "final rendered-media validation is mandatory")
                    return                      # finally: cleans the temp dir
                decoded += 1
                arrs.append(arr)
            if arrs:
                frames.append((sid, beat, times[len(arrs) // 2], arrs, times))
        sc_by_id = {sc.get("scene_id"): sc for sc in scenes}
        palette_summary = {"frames_sampled": decoded, "scenes_checked": len(frames),
                           "method": cfg.get("method"), "blocked": False,
                           "persistent_cold_regions": 0, "noise_only_px": 0,
                           "raw_cold_px": 0, "max_meaningful_area_fraction": 0.0,
                           "max_meaningful_core_px": 0, "scene_timing_source": timing_source,
                           "cold_family_px": {"blue": 0, "cyan": 0, "purple": 0},
                           "policy_notes": list(cfg.get("policy_notes") or [])}
        for sid, beat, t_mid, arrs, times in frames:
            sc = sc_by_id.get(sid) or {"scene_id": sid, "beat": beat}
            is_code = bool(sc.get("code_justified"))
            # (a0) blank/degenerate frames are a hard render failure
            if max(float(np.std(arr)) for arr in arrs) < 2.0:
                rep.block("visual_semantics",
                          f"rendered frame of scene {sid} is blank (near-zero pixel variance)")
            # (a) the legacy cold code/terminal card must not appear anywhere
            cold_card = max(detect_code_card(arr) for arr in arrs)
            if cold_card > COLD_CARD_PX and not is_code:
                rep.block("visual_semantics",
                          f"rendered frame of scene {sid} contains a cold code/terminal card "
                          f"({cold_card}px) — the generic code card is not allowed for {sc.get('visual_category')!r}")
            elif cold_card > COLD_CARD_PX:
                rep.block("visual_semantics",
                          f"rendered frame of scene {sid} shows a cold code card that violates "
                          "the warm profile palette")
            # (b) cursor only in a justified code-entry scene — multi-frame,
            #     shape-ownership and expected-region verified (issue #28)
            cursor_res = cursor_qa.analyze_scene_cursors(arrs, times, sc=sc, layout=layout, pol=pol)
            if not cursor_res["ok"]:
                rep.block("visual_semantics", cursor_res["reason"])
            # (c) no blue/navy/cyan/purple (cold) element — perceptually gated,
            #     multi-frame, single-sourced in palette_qa (issue #26)
            scene_palette = palette_qa.analyze_scene(arrs, cfg)
            palette_summary["blocked"] = palette_summary["blocked"] or scene_palette["blocked"]
            palette_summary["persistent_cold_regions"] += scene_palette["persistent_cold_regions"]
            palette_summary["noise_only_px"] += scene_palette["noise_only_px_total"]
            palette_summary["raw_cold_px"] += scene_palette["raw_cold_px_total"]
            for _fam, _n in (scene_palette.get("family_px_total") or {}).items():
                palette_summary["cold_family_px"][_fam] = \
                    palette_summary["cold_family_px"].get(_fam, 0) + _n
            palette_summary["max_meaningful_area_fraction"] = max(
                palette_summary["max_meaningful_area_fraction"],
                scene_palette["max_meaningful_area_fraction"])
            palette_summary["max_meaningful_core_px"] = max(
                palette_summary["max_meaningful_core_px"],
                scene_palette["max_meaningful_core_px"])
            ok, reason = palette_qa.scene_verdict(scene_palette)
            if not ok:
                rep.block("visual_semantics",
                          f"rendered frames of scene {sid}: {reason}")
            # (d) subtitle band must stay unobstructed (dark scrim zone)
            cues = layout.get("en") or []
            active = [c for c in cues if c.get("start", 1e9) <= t_mid <= c.get("end", -1)]
            if active:
                L = pol["layout"]
                top = int(L["en_top"]) - 10
                bot = int(L["en_top"]) + 3 * int(L["en_row_height"]) + 10
                band2 = arrs[len(arrs) // 2][top:bot, 60:arrs[0].shape[1] - 60]
                lum = (0.2126 * band2[..., 0] + 0.7152 * band2[..., 1] + 0.0722 * band2[..., 2])
                if float(np.percentile(lum, 25)) > 96:
                    rep.block("visual_semantics",
                              f"rendered frame of scene {sid}: the subtitle band is obstructed "
                              "by bright overlay content")
        rep.details["visuals"]["palette_qa"] = palette_summary
        # (e) adjacent scenes must not be perceptual near-duplicates
        hashes = [(sid, visual_plan.perceptual_hash(
            _frame_pil(arrs[len(arrs) // 2])))
            for sid, beat, t_mid, arrs, times in frames]
        for i in range(len(hashes) - 1):
            d = visual_plan.hamming(hashes[i][1], hashes[i + 1][1])
            if d <= 14:
                rep.block("visual_semantics",
                          f"adjacent scenes {hashes[i][0]} and {hashes[i + 1][0]} are near-duplicates "
                          "in the rendered frames (aHash distance "
                          f"{d}/256) — a repeated/zoomed image is not a new scene")
        # (f) the rendered reel must actually contain distinct scenes
        clusters = 0
        for i, (sid, hh) in enumerate(hashes):
            if all(visual_plan.hamming(hh, hh2) > 24 for _, hh2 in hashes[:i]):
                clusters += 1
        nonbrand = [sc for sc in scenes if sc.get("visual_category") not in visual_plan.BRAND_CATEGORIES]
        need = max(4, min(len(nonbrand), 9) - 2)
        if clusters < need:
            rep.block("visual_semantics",
                      f"rendered frames collapse to {clusters} distinct visual(s) "
                      f"(need >= {need}) — the Reel repeats one underlying image")
        # (g) rendered code scenes must equal the plan's justified set
        code_rendered = {sid for sid, beat, t_mid, arrs, times in frames
                         if (sc_by_id.get(sid) or {}).get("visual_category") in visual_plan.CODE_CATEGORIES
                         or max(detect_code_card(arr) for arr in arrs) > COLD_CARD_PX}
        code_planned = {sc["scene_id"] for sc in scenes
                        if sc.get("visual_category") in visual_plan.CODE_CATEGORIES}
        if code_rendered != code_planned:
            rep.block("visual_semantics",
                      f"rendered code scenes {sorted(code_rendered) or 'none'} do not match the "
                      f"plan's justified set {sorted(code_planned) or 'none'}")
        rep.details["visuals"]["rendered_frames_checked"] = True
        rep.details["visuals"]["rendered_frames"] = len(frames)
        rep.details["visuals"]["distinct_rendered"] = clusters
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _frame_pil(arr):
    import numpy as np
    from PIL import Image
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


# ---------------------------------------------------------------------------
# ISSUE #33 — MINIMAL FORMAT CONTRACT (hard blockers, veto regardless of
# score). Applies ONLY when the rendered format is minimal (the plan's own
# style marker, or the explicit PIPELINE_STYLE handoff).
#
# Declared boxes are the renderer's measured textbbox output (layout.json →
# "minimal"); decoded H.264 frames are the pixel proof. Metadata alone is
# never enough — both must agree.
# ---------------------------------------------------------------------------

MN_DEFAULT_SAFE = (220, 1700, 50)   # top, bottom, side at 1080x1920
MN_DEFAULT_FLOOR_MAIN = 44          # px, readable floor for kinetic text
MN_DEFAULT_FLOOR_LABEL = 30         # px, readable floor for scene labels
MN_SCENE_ZONE = (470, 1320)         # body motif zone (clear of subtitle band)


def _mn_ink_fraction(arr, box):
    """Fraction of a box's pixels that are TEXT-bright (luminance > 100).

    The warm marble background peaks at ~67 luminance, H.264 noise on the
    dark field stays far below 100, while ivory (~230) and gold (~200)
    glyphs sit far above — so this is a clean text-present/absent probe on
    the DECODED frame, not a metadata claim.
    """
    import numpy as np
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(arr.shape[1], x1), min(arr.shape[0], y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    box_arr = arr[y0:y1, x0:x1]
    lum = 0.2126 * box_arr[..., 0] + 0.7152 * box_arr[..., 1] + 0.0722 * box_arr[..., 2]
    return float((lum > 100).mean())


def _mn_undecclared_bright(arr, scene_items, zone, margin=12):
    """Fraction of the scene zone that is text-bright OUTSIDE every declared
    item box of the scene (cards, dial, labels, emblem, glow, connectors).

    The minimal format allows at most ONE dominant visual + <= 3 labels, so
    bright content outside the declared boxes is clutter / a second
    dominant visual / undrawn text — a hard blocker.
    """
    import numpy as np
    zy0, zy1 = zone
    zone_arr = arr[zy0:zy1, :, :]
    lum = 0.2126 * zone_arr[..., 0] + 0.7152 * zone_arr[..., 1] + 0.0722 * zone_arr[..., 2]
    bright = lum > 100
    for it in scene_items:
        x0, y0, x1, y1 = (int(v) for v in it.get("bbox", (0, 0, 0, 0)))
        rx0 = max(0, x0 - margin - 0)
        rx1 = min(zone_arr.shape[1], x1 + margin)
        ry0 = max(0, min(y0, zy1) - margin - zy0)
        ry1 = min(zone_arr.shape[0], max(y1, zy0) + margin - zy0)
        if ry1 > ry0 and rx1 > rx0:
            bright[ry0:ry1, rx0:rx1] = False
    return float(bright.mean())


def check_minimal_contract(rep, ep, script, video, pol, no_frames):
    """issue #33 §12-13: minimal-format hard blockers + decoded-frame proof."""
    plan = common.load_json(os.path.join(ep, "visual_plan.json"))
    style = (plan or {}).get("style") or os.environ.get("PIPELINE_STYLE")
    if style != "minimal":
        rep.details["minimal_contract"] = {"applied": False}
        return
    rep.details["minimal_contract"] = {"applied": True}
    if not plan or not plan.get("scenes"):
        rep.block("minimal_contract",
                  "minimal render without a gated minimal plan — fail closed")
        return
    # 1. the SAME deterministic gate the renderer ran, re-run on the record
    for issue in visual_plan.visual_semantic_issues(plan, script, pol):
        rep.block("minimal_contract", issue)
    scenes = plan["scenes"]

    # 2. zero Openverse / zero photos / zero code / zero cursor (issue §11)
    for sc in scenes:
        sid = sc.get("scene_id") or "?"
        if (sc.get("asset") or {}).get("kind") not in ("repo", "procedural"):
            rep.block("minimal_contract",
                      f"{sid}: external asset in minimal — Openverse/external "
                      f"images are forbidden in the default format")
        if sc.get("photo_designated"):
            rep.block("minimal_contract", f"{sid}: photo slot in minimal")
        if sc.get("code_justified") or sc.get("cursor_justified") or sc.get("code_lines"):
            rep.block("minimal_contract",
                      f"{sid}: code/terminal/cursor in minimal (explicit "
                      f"experimental mode required)")
    pp = plan.get("photo_policy") or {}
    if pp.get("photos_retrieved") or pp.get("designated"):
        rep.block("minimal_contract",
                  f"minimal plan records photo activity "
                  f"(retrieved={pp.get('photos_retrieved')}, designated={pp.get('designated')}) — must be zero")

    # 3. banner + CTA content contract (issue §4, §10)
    handle = (script.get("meta") or {}).get("handle") or "@metacognition.hq"
    banner, cta = scenes[0], scenes[-1]
    bb = banner.get("banner") or {}
    cc = cta.get("cta") or {}
    if not bb.get("brand_line"):
        rep.block("minimal_contract", "opening banner: brand line missing")
    if not (bb.get("hook_text") or "").strip():
        rep.block("minimal_contract", "opening banner: hook text missing")
    if bb.get("handle") != handle:
        rep.block("minimal_contract",
                  f"opening banner handle {bb.get('handle')!r} != page handle {handle!r}")
    if not bb.get("gold_keyword"):
        rep.block("minimal_contract", "opening banner: no single gold-highlighted keyword")
    if "follow" not in cc.get("follow_line", "").lower():
        rep.block("minimal_contract", "final CTA: no explicit follow ask")
    if handle not in cc.get("follow_line", ""):
        rep.block("minimal_contract", "final CTA must include @metacognition.hq")
    if cc.get("handle") != handle:
        rep.block("minimal_contract", f"final CTA handle {cc.get('handle')!r} != {handle!r}")
    # duration targets: hard window = blocker, soft target = warning
    b_c = plan.get("banner_contract", {}) or {}
    c_c = plan.get("cta_contract", {}) or {}
    lo, hi = b_c.get("duration_hard_s", (1.5, 5.8))
    tlo, thi = b_c.get("duration_target_s", (2.5, 4.0))
    bd = banner.get("expected_duration") or 0
    if not (lo <= bd <= hi):
        rep.block("minimal_contract", f"banner duration {bd:.1f}s outside hard window")
    elif not (tlo <= bd <= thi):
        rep.warn("minimal_contract", f"banner duration {bd:.1f}s outside target {tlo}-{thi}s", 1)
    lo, hi = c_c.get("duration_hard_s", (2.2, 6.0))
    tlo, thi = c_c.get("duration_target_s", (3.0, 5.0))
    cd = cta.get("expected_duration") or 0
    if not (lo <= cd <= hi):
        rep.block("minimal_contract", f"final CTA duration {cd:.1f}s outside hard window")
    elif not (tlo <= cd <= thi):
        rep.warn("minimal_contract", f"final CTA duration {cd:.1f}s outside target {tlo}-{thi}s", 1)

    # 4. declared-text geometry (measured textbbox boxes from layout.json)
    layout = common.load_json(os.path.join(ep, "layout.json"), {}) or {}
    items = layout.get("minimal") or []
    mp = pol.get("minimal") or {}
    safe_cfg = mp.get("safe") or {}
    top, bottom, side = (int(safe_cfg.get("top", MN_DEFAULT_SAFE[0])),
                         int(safe_cfg.get("bottom", MN_DEFAULT_SAFE[1])),
                         int(safe_cfg.get("side", MN_DEFAULT_SAFE[2])))
    floor_main = int(mp.get("floor_main", MN_DEFAULT_FLOOR_MAIN))
    floor_label = int(mp.get("floor_label", MN_DEFAULT_FLOOR_LABEL))
    floor_brand = int(mp.get("floor_brand", 28))
    Wf, Hf = 1080, 1920
    text_items = [it for it in items
                  if it.get("kind") in ("hook", "cta", "brand", "handle", "label")]
    for it in text_items:
        x0, y0, x1, y1 = it.get("bbox", (0, 0, 0, 0))
        if x0 < side or y0 < top or x1 > Wf - side or y1 > bottom:
            rep.block("minimal_contract",
                      f"{it.get('scene')}: text {it.get('text', '')[:24]!r} outside the safe "
                      f"bounds ({side},{top})-({Wf - side},{bottom})")
        fnt = it.get("font") or 0
        kind = it.get("kind")
        # documented floors: kinetic text (hook/cta) floor_main, scene
        # labels floor_label, the fixed brand lockup (brand line + handle)
        # floor_brand
        floor = (floor_label if kind == "label"
                 else floor_brand if kind in ("brand", "handle")
                 else floor_main)
        if fnt and fnt < floor:
            rep.block("minimal_contract",
                      f"{it.get('scene')}: text {it.get('text', '')[:24]!r} at {fnt}px is "
                      f"below the {floor}px readable floor")
    from collections import defaultdict
    blocks = defaultdict(list)
    for it in text_items:
        if it.get("kind") in ("hook", "cta"):
            blocks[(it["scene"], it["kind"])].append(it)
    for (sid, kind), lines in blocks.items():
        if len(lines) > 3:
            rep.block("minimal_contract",
                      f"{sid}: {kind} block has {len(lines)} lines (max 3 — split into "
                      f"another cue/scene, never shrink below the floor)")
    # overlapping semantic text (pairwise, same scene, 6px tolerance)
    for i in range(len(text_items)):
        for j in range(i + 1, len(text_items)):
            a, b = text_items[i], text_items[j]
            if a.get("scene") != b.get("scene"):
                continue
            ax0, ay0, ax1, ay1 = a["bbox"]
            bx0, by0, bx1, by1 = b["bbox"]
            ox = min(ax1, bx1) - max(ax0, bx0)
            oy = min(ay1, by1) - max(ay0, by0)
            if ox > 6 and oy > 6:
                rep.block("minimal_contract",
                          f"{a.get('scene')}: overlapping semantic text "
                          f"{a.get('text', '')[:16]!r} / {b.get('text', '')[:16]!r}")
    # glow must stay inside the frame (it is generated inside the measured
    # text bounds + blur margin; this is the frame-level proof)
    for it in [x for x in items if x.get("kind") == "glow"]:
        x0, y0, x1, y1 = it.get("bbox", (0, 0, 0, 0))
        if x0 < 0 or y0 < 0 or x1 > Wf or y1 > Hf:
            rep.block("minimal_contract",
                      f"{it.get('scene')}: glow outside measured bounds/frame")
    rep.details["minimal_contract"]["declared_text_items"] = len(text_items)

    # 5. HUMAN-STYLE DECODED-FRAME VALIDATION (issue §13): sample the REAL
    #    rendered H.264 frames at the opening banner (FIRST frame), each
    #    body scene and the final CTA; verify declared text is actually
    #    visible, centered content is in-frame, and nothing bright is
    #    hiding outside the declared composition.
    if not (video and os.path.exists(video)) or no_frames:
        rep.details["minimal_contract"]["frames"] = "skipped (no_frames or no video)"
        return
    import shutil
    import tempfile
    timing = common.load_json(os.path.join(ep, "timing.json"), {}) or {}
    total = float(timing.get("total", 0) or 0)
    windows, _ = scene_windows(layout, timing, scenes, total)
    win_by_scene = {w[0]: (w[2], w[3]) for w in windows}
    tmp = tempfile.mkdtemp(prefix="qa_minimal_")
    checked = 0
    try:
        # (a) the FIRST rendered frame: hook + brand + handle must be visible
        arr = _decode_frame(video, 0.08, tmp)
        if arr is None:
            rep.block("minimal_contract",
                      "could not decode the first rendered frame (unreadable)")
        else:
            checked += 1
            b_items = [it for it in items
                       if it.get("scene") == banner.get("scene_id")
                       and it.get("kind") in ("hook", "brand", "handle")]
            for it in b_items:
                ink = _mn_ink_fraction(arr, [v - 6 for v in it["bbox"][:2]] +
                                       [v + 6 for v in it["bbox"][2:]])
                if ink < 0.05:
                    what = "hook" if it["kind"] == "hook" else f"{it['kind']} text"
                    rep.block("minimal_contract",
                              f"first frame: {what} {it.get('text', '')[:20]!r} not visible "
                              f"at its declared position (ink {ink:.2f}) — the hook must "
                              f"appear in the FIRST frame")
        # (b) each body scene + the CTA at its window midpoint
        for sc in scenes[1:]:
            sid = sc.get("scene_id")
            if sid not in win_by_scene:
                rep.block("minimal_contract",
                          f"scene {sid}: no rendered window in layout/timing — cannot "
                          f"validate its decoded frames")
                continue
            a, b = win_by_scene[sid]
            t_mid = (a + b) / 2
            arr = _decode_frame(video, t_mid, tmp)
            if arr is None:
                rep.block("minimal_contract",
                          f"could not decode a rendered frame for scene {sid} (unreadable)")
                continue
            checked += 1
            scene_items = [it for it in items if it.get("scene") == sid]
            for it in [x for x in scene_items
                       if x.get("kind") in ("hook", "cta", "brand", "handle", "label")]:
                ink = _mn_ink_fraction(arr, [v - 6 for v in it["bbox"][:2]] +
                                       [v + 6 for v in it["bbox"][2:]])
                if ink < 0.04:
                    rep.block("minimal_contract",
                              f"scene {sid}: declared text {it.get('text', '')[:20]!r} not "
                              f"visible in the decoded frame (clipped/missing, ink {ink:.2f})")
            bright = _mn_undecclared_bright(arr, scene_items, MN_SCENE_ZONE)
            if bright > 0.004:
                rep.block("minimal_contract",
                          f"scene {sid}: {bright:.2%} undecclared bright content in the "
                          f"scene zone — clutter / a second dominant visual")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rep.details["minimal_contract"]["frames"] = checked


def _estimate_scene_windows(timing, scenes):
    """LEGACY FALLBACK ONLY — reconstruct scene windows from word-level timing.

    The renderer writes the authoritative per-scene windows it actually used
    (``layout.json`` → ``scene_windows``, produced by ``Reel._build_scene_times``
    through build/render_auto.py); QA consumes those exact windows. This second
    approximation exists only for historical fixtures whose layout.json predates
    the recorded windows, and it is reported as
    ``scene_timing_source = "estimated-fallback"`` in the QA details.
    """
    from collections import OrderedDict
    groups = OrderedDict()
    for sc in scenes:
        groups.setdefault(sc.get("beat"), []).append(sc)
    chunks = timing.get("chunks", []) or []
    out = []
    for sc in scenes:
        beat = sc.get("beat")
        beat_chunks = [c for c in chunks if c.get("beat") == beat]
        if not beat_chunks:
            continue
        t0 = min((ln.get("start", 0) for c in beat_chunks for ln in c.get("lines", [])), default=0)
        t1 = max((ln.get("end", 0) for c in beat_chunks for ln in c.get("lines", [])), default=0)
        idx = [i for i, s2 in enumerate(groups.get(beat, [])) if s2 is sc]
        idx = idx[0] if idx else 0
        n = len(groups.get(beat, []))
        start = t0 + (t1 - t0) * idx / max(1, n)
        end = t0 + (t1 - t0) * (idx + 1) / max(1, n)
        sid = sc.get("scene_id")
        if end > start:
            out.append((sid, beat, float(start), float(end)))
    return out


def scene_windows(layout, timing, scenes, total):
    """Authoritative per-scene rendered windows + their source.

    Preferred: ``layout["scene_windows"]`` — the EXACT start/end timestamps the
    renderer used for every scene (single source of truth, no second
    approximation, issue #26 §4). It is accepted only when it covers every plan
    scene, is well ordered and lies inside the render duration.

    Fallback: ``_estimate_scene_windows`` (historical fixtures only), clearly
    flagged by the returned source string.

    Returns ``([(scene_id, beat, start, end), ...], source)``.
    """
    plan_ids = [sc.get("scene_id") for sc in scenes]
    raw = (layout or {}).get("scene_windows")
    windows = []
    ok = bool(raw)
    if ok:
        for w in raw:
            try:
                windows.append((w["scene_id"], w.get("beat"), float(w["start"]), float(w["end"])))
            except (TypeError, KeyError, ValueError):
                ok = False
                break
    if ok and windows:
        ids = [w[0] for w in windows]
        ok = (len(ids) == len(set(ids))
              and set(plan_ids) <= set(ids)
              and all(b > a for _, _, a, b in windows)
              and all(a >= -0.01 and (not total or b <= total + 0.5) for _, _, a, b in windows)
              and windows == sorted(windows, key=lambda w: w[2]))
    else:
        ok = False
    if ok:
        return windows, "layout"
    return _estimate_scene_windows(timing, scenes), "estimated-fallback"

def check_caption(rep, caption_path, script, pol):
    if not caption_path or not os.path.exists(caption_path):
        rep.block("caption_quality", "caption missing")
        return
    with open(caption_path, encoding="utf-8") as fh:
        check_caption_text(rep, fh.read(), script, pol)

def check_caption_text(rep, caption_full_text, script, pol):
    """Caption/hashtag blockers on the caption TEXT itself (issue #22).

    Extracted verbatim from check_caption so the PRE-RENDER text gate can run
    the EXACT same blockers on the caption the deterministic assembler would
    produce, before any media exists. check_caption (final QA) delegates here —
    there is only one implementation. `caption_full_text` uses the file shape
    written by build/caption.py: body, blank line, one hashtag line.
    """
    cp = pol["caption_policy"]
    if not caption_full_text:
        rep.block("caption_quality", "caption missing")
        return
    body_lines, tag_lines = [], []
    for ln in caption_full_text.splitlines():
        (tag_lines if ln.strip().startswith("#") else body_lines).append(ln)
    body = "\n".join(body_lines).strip()
    tags = " ".join(tag_lines).split()
    if len(body) > cp["max_chars"]:
        rep.block("caption_quality", f"{len(body)} chars > {cp['max_chars']}")
    if len(body) > 2200:
        rep.block("caption_quality", "caption body exceeds Instagram limit 2200")
    if len(body) < cp["min_chars"]:
        rep.block("caption_quality", f"too short {len(body)}")
    if common.persian_ratio(body) > 0.2:
        rep.block("caption_quality", "caption not primarily English")
    hp = pol["hashtag_policy"]
    if not (hp["min"] <= len(tags) <= hp["max"]):
        rep.block("caption_quality", f"{len(tags)} hashtags outside {hp['min']}..{hp['max']}")
    bad = [t for t in tags if t.lower() in hp["banned"]]
    if bad:
        rep.block("caption_quality", f"spam hashtags {bad}")
    for a in hp["always"]:
        if a not in tags:
            rep.warn("caption_quality", f"missing brand {a}", 1)
    for phrase in pol["tone"]["banned_phrases"]:
        if phrase in body.lower():
            rep.block("caption_quality", f"banned phrase '{phrase}'")
    if common.PLACEHOLDER_RE.search(body):
        rep.block("caption_quality", "placeholder in caption")
    if script["caption"]["hook"].strip() not in body:
        rep.warn("caption_quality", "caption does not start with hook", 1)
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
            if os.path.getsize(path) < 20000:
                rep.warn("video_quality", f"poster {key} small file", 1)
        except Exception as e:
            rep.block("video_quality", f"poster {key} invalid {e}")

def check_duplicates(rep, script, topic, memory, pol):
    dp = pol["duplicates"]
    title = script["meta"].get("topic") or topic.get("title","")
    tags = set(script["meta"].get("tags", []))
    shash = common.script_hash(script)
    recent = common.recent_entries(memory, dp["window_posts"], statuses=common.PUBLISHED_LIKE)
    this_id = script["meta"].get("content_id")
    for i, e in enumerate(recent):
        if e.get("content_id") == this_id:
            continue
        if e.get("script_hash") == shash:
            rep.block("duplicate_check", f"identical script already used on {e.get('content_date')}")
        sim = common.title_similarity(title, e.get("topic",""))
        if sim >= dp["title_similarity_block"]:
            rep.block("duplicate_check", f"topic similar {sim:.2f} to {e.get('content_date')}")
        elif sim >= dp["title_similarity_warn"]:
            rep.warn("duplicate_check", f"topic similar {sim:.2f}", 2)
        if i < dp["tag_cooldown_posts"] and tags & set(e.get("tags", [])):
            rep.block("duplicate_check", f"tag {sorted(tags & set(e.get('tags', [])))} reused within {dp['tag_cooldown_posts']}")
    pillars = [e.get("pillar") for e in recent[: dp["pillar_repeat_warn_consecutive"]]]
    if pillars and all(p == script["meta"].get("pillar") for p in pillars):
        rep.warn("duplicate_check", f"same pillar {pillars[0]} for {len(pillars)+1} posts", 2)
    if recent and recent[0].get("cta_type") == script["meta"].get("cta_type"):
        rep.warn("duplicate_check", f"same CTA type", 1)
    rep.details["duplicates"] = {"compared": len(recent), "script_hash": shash[:12]}

def check_buffer_readiness(rep, public_url, caption_path, skip_network, video):
    if caption_path and os.path.exists(caption_path):
        with open(caption_path, encoding="utf-8") as fh:
            body = "\n".join(l for l in fh.read().splitlines() if not l.strip().startswith("#")).strip()
        if len(body) > 2200:
            rep.block("buffer_readiness", "caption exceeds Instagram limit")
    if not public_url:
        rep.warn("buffer_readiness", "public URL not provided (pre-push)", 0)
        rep.details["buffer"] = {"public_url": None}
        return
    if not public_url.startswith("https://"):
        rep.block("buffer_readiness", "public URL not https")
        return
    if skip_network:
        rep.details["buffer"] = {"public_url": public_url, "checked": False}
        return
    code, ctype, length = head_url(public_url)
    if code != 200:
        rep.block("buffer_readiness", f"public URL HTTP {code}")
    else:
        if "video" not in ctype and "octet-stream" not in ctype:
            rep.warn("buffer_readiness", f"MIME '{ctype}' not video/mp4", 1)
        if video and os.path.exists(video) and length and abs(length - os.path.getsize(video)) > 1024:
            rep.block("buffer_readiness", f"public size {length} != local {os.path.getsize(video)}")
    rep.details["buffer"] = {"public_url": public_url, "http": code, "mime": ctype, "bytes": length}

def check_reviewer(rep, script, topic, ep_dir, pol):
    # Candidate-bound Reviewer lifecycle (issue #30): Static Fallback never
    # carries a Groq Reviewer report; a stale report is not applicable.
    rev_path = os.path.join(ep_dir, "reviewer_report.json")
    mode = (script.get("meta", {}) or {}).get("generation_mode", "")
    if mode == "static-fallback":
        if os.path.exists(rev_path):
            # Stale Groq report beside fallback — must not be silently applied.
            # Do not block the validated fallback; report as not applicable and ignore stale file.
            # The lifecycle fix ensures this file is not written; if present, it is a leftover.
            rep.details["reviewer"] = {"present": True, "stale": True, "not_applicable": True, "reason": "stale reviewer report ignored for static fallback"}
            rep.details["reviewer_check"] = "not applicable — validated static fallback (stale report ignored)"
            rep.checks["reviewer_check"] = "pass"
            # Optionally, the file could be removed by the caller (pipeline/producer), but QA does not delete.
            return
        rep.details["reviewer"] = {"present": False, "not_applicable": True, "reason": "validated static fallback — reviewer not applicable"}
        rep.details["reviewer_check"] = "not applicable — validated static fallback"
        rep.checks["reviewer_check"] = "pass"
        return
    if not os.path.exists(rev_path):
        # For Groq, a missing report is tolerated at QA level (content_producer
        # already enforces strict threshold and fallback); keep legacy pass.
        rep.details["reviewer"] = {"present": False}
        return
    try:
        data = common.load_json(rev_path, {})
        out = data.get("output") or data
        binding = data.get("binding")
        if isinstance(binding, dict):
            meta = script.get("meta", {}) if isinstance(script.get("meta"), dict) else {}
            expected_stage = str(meta.get("stage") or "initial")
            expected_variant = meta.get("variant", 0)
            expected_attempt = f"variant-{expected_variant}"
            expected_content_id = str(meta.get("content_id", "") or "")
            expected_cand_hash = common.reviewer_candidate_hash(script, expected_stage, expected_attempt)

            cand_hash = binding.get("candidate_hash")
            if cand_hash:
                if cand_hash != expected_cand_hash:
                    rep.block("reviewer_check", f"reviewer report candidate hash mismatch (report {str(cand_hash)[:8]} != candidate {expected_cand_hash[:8]}) — stale/mismatched review must not be applied")
                    rep.details["reviewer"] = {"present": True, "mismatched": True, "expected": expected_cand_hash, "found": cand_hash}
                    return
            else:
                expected_shash = common.script_hash(script)
                if binding.get("script_hash") != expected_shash:
                    rep.block("reviewer_check", f"reviewer report hash mismatch (report {str(binding.get('script_hash','?'))[:8]} != script {expected_shash[:8]}) — stale/mismatched review must not be applied")
                    rep.details["reviewer"] = {"present": True, "mismatched": True, "expected": expected_shash, "found": binding.get("script_hash")}
                    return

            if binding.get("generation_mode") != mode:
                rep.block("reviewer_check", f"reviewer report generation_mode mismatch ({binding.get('generation_mode')} != {mode}) — mismatched report")
                rep.details["reviewer"] = {"present": True, "mismatched": True}
                return

            if binding.get("content_id") and binding.get("content_id") != expected_content_id:
                rep.block("reviewer_check", f"reviewer report content_id mismatch ({binding.get('content_id')} != {expected_content_id}) — mismatched report")
                rep.details["reviewer"] = {"present": True, "mismatched": True}
                return

            if binding.get("attempt_id") and binding.get("attempt_id") != expected_attempt:
                rep.block("reviewer_check", f"reviewer report attempt_id mismatch ({binding.get('attempt_id')} != {expected_attempt}) — mismatched report")
                rep.details["reviewer"] = {"present": True, "mismatched": True}
                return

            if binding.get("stage") != expected_stage:
                rep.block("reviewer_check", f"reviewer report stage mismatch ({binding.get('stage')} != {expected_stage}) — mismatched report")
                rep.details["reviewer"] = {"present": True, "mismatched": True}
                return
        valid, reason = _validate_reviewer_output(out)
        if not valid:
            rep.block("reviewer_check", f"reviewer output malformed ({reason}) — rejected groq candidate")
            rep.details["reviewer"] = {"present": True, "malformed": True, "reason": reason}
            return
    except Exception as e:
        rep.warn("reviewer_check", f"could not parse reviewer report {e}", 1)
        return
    try:
        check_reviewer_output(rep, script, topic, out, pol)
    except Exception as e:
        rep.warn("reviewer_check", f"could not parse reviewer report {e}", 1)

def check_reviewer_output(rep, script, topic, out, pol):
    """Structured-Reviewer blockers on an already-parsed reviewer output dict.

    A structured Reviewer approval can NEVER override a deterministic check:
    the word-range contradiction rule below is enforced identically by final QA
    and by the pre-render text gate (one implementation, shared by both)."""
    # Validate required fields first — missing => malformed, not score 0
    valid, reason = _validate_reviewer_output(out)
    if not valid:
        rep.block("reviewer_check", f"reviewer output malformed ({reason}) — rejected groq candidate")
        rep.details["reviewer"] = {"present": True, "malformed": True, "reason": reason}
        # Still record details for audit
        rep.details["reviewer_words"] = {"spoken_words": common.spoken_word_count(script), "policy_range": list(pol["length"]["narration_words"])}
        return
    approved = out.get("approved")
    score = out.get("score", 0)
    tech_rel = out.get("technology_relevance")
    meta_rel = out.get("metacognition_relevance")
    blocking = out.get("blocking_errors", [])
    unsupported = out.get("unsupported_claims", [])

    # Boundary fix (run 35050738918 / reel-2026-09-17): reviewer_check used to
    # mirror ONLY the reviewer's structured fields and never looked at the
    # script, so a reviewer-approved 106-word script passed this check and
    # reached rendering. Structured reviewer output cannot override a
    # deterministic check: an approval of a script that violates the QA word
    # range (counted with the same single-sourced logic the gate uses) is
    # itself a blocking error.
    words = common.spoken_word_count(script)
    lo, hi = pol["length"]["narration_words"]
    if approved and not (lo <= words <= hi):
        rep.block("reviewer_check", f"reviewer approved a script with {words} spoken words "
                  f"outside policy {lo}-{hi} — structured approval cannot override "
                  f"deterministic checks (the script must not reach rendering this way)")
    rep.details["reviewer_words"] = {"spoken_words": words, "policy_range": [lo, hi]}

    if not approved:
        rep.block("reviewer_check", f"reviewer not approved (score {score})")
    if score < 85:
        rep.block("reviewer_check", f"reviewer score {score} < 85")
    if tech_rel is False:
        rep.block("reviewer_check", "reviewer says technology_relevance false")
    if meta_rel is False:
        rep.block("reviewer_check", "reviewer says metacognition_relevance false")
    if blocking:
        rep.block("reviewer_check", f"reviewer blocking_errors {blocking}")
    if unsupported:
        rep.block("reviewer_check", f"reviewer unsupported_claims {unsupported}")

    rep.details["reviewer"] = {"present": True, "approved": approved, "score": score, "tech": tech_rel,
                               "metacog": meta_rel, "blocking": blocking}

# ------------------------------------------------------------------ PRE-RENDER TEXT QA gate
# Issue #22 (run 35054292820, reel-2026-09-18): the pipeline only pre-checked
# word count/duration before media, so a [source_quality] claim-word blocker
# ("researchers" in the SPOKEN script with no Tier A/B evidence in the calendar
# packet) was discovered by the final supervisor only AFTER TTS, timing,
# subtitles, FFmpeg and poster rendering. Every deterministic blocker that can
# be decided from TEXT alone now also runs BEFORE any media exists — through
# the exact check functions below, not through copies or approximations of them.
#
# Media-only checks stay in final QA and remain mandatory and authoritative:
# subtitle_layout (rendered layout.json geometry), audio_quality, video_quality
# (actual MP4 properties + posters), frame contrast sampling, buffer_readiness
# (public URL), duplicate_check / quarantine (editorial-memory state at publish
# time). Warnings stay warnings: only rep.blocking entries are hard here.

TEXT_QA_GATE_CHECKS = ["content_language", "english_only", "technology_relevance",
                       "metacognition_relevance", "topic_relevance", "source_quality",
                       "script_quality", "english_quality", "caption_quality",
                       "reviewer_check"]

# Final-QA checks that CANNOT be evaluated before media exists — they keep
# running exclusively in the full supervisor. (duplicate_check is deliberately
# here too: it reads editorial memory as it stands at PUBLISH time, not at
# script time, so it is evaluated by final QA, not by the pre-render gate.)
TEXT_QA_MEDIA_ONLY_CHECKS = ["subtitle_layout", "audio_quality", "video_quality",
                             "buffer_readiness", "duplicate_check"]

def build_caption_text(script, pol=None):
    """The EXACT caption text build/caption.py will write for this script
    (deterministic assembler — no media), so the pre-render gate evaluates the
    same caption bytes final QA will read from output/auto-<tag>_caption.txt."""
    try:
        import caption as caption_mod
        cap, tag = caption_mod.build(script)
        cap = caption_mod.fit(cap, tag)
        return cap + "\n\n" + tag + "\n"
    except Exception:
        return ""

def pre_render_text_gate(script, topic=None, pol=None, *, ep_dir=None,
                         reviewer_output=None, caption_text=None):
    """Deterministic PRE-RENDER text gate — runs the exact final-QA blocker
    functions from TEXT_QA_GATE_CHECKS on a script before TTS, timing,
    subtitle rendering, FFmpeg or media upload. Returns the supervisor-shaped
    dict {"blocking": [...], "warnings": [...], "checks": {...}, "words": n,
    "estimated_seconds": s}; an empty "blocking" list means text-clean.

      * every check is the SAME function object final QA uses (the gate calls
        the module-level names, verified by the parity tests);
      * the QA thresholds and the reviewer ≥85 bar are untouched;
      * warnings are reported but NEVER promoted to blockers;
      * media-only checks are NOT run here — final QA stays mandatory and
        authoritative for everything about rendered media;
      * reviewer contradictions: if `reviewer_output` is given (producer, the
        structured review in hand) or `ep_dir` holds reviewer_report.json
        (pipeline, the artifact the producer wrote), check_reviewer_output
        enforces "a structured approval never overrides deterministic QA" —
        including the reviewer's own blocking_errors / unsupported_claims lists.

    Word range (150-260) and the duration preflight are additionally enforced
    by the caller-side fast gate (common.pre_render_issues) — the strict band
    is TIGHTER than QA's hard-fail band on purpose; check_script runs here with
    its own unchanged thresholds.
    """
    pol = pol or common.policy()
    topic = topic or {}
    rep = Report(pol)
    check_content_language(rep, script, pol)
    check_english_only(rep, script, pol)
    check_technology_relevance(rep, script, topic, pol)
    check_metacognition_relevance(rep, script, pol)
    check_topic(rep, script, topic, pol)
    check_sources(rep, script, topic, pol, skip_network=True)
    check_script(rep, script, pol)
    check_english(rep, script, pol)
    # Issue #32 §1: glyph coverage + U+FFFD block BEFORE TTS/render — same
    # blockers final QA re-checks on the rendered episode.
    check_text_renderability(rep, script, pol)
    check_caption_text(rep, caption_text if caption_text is not None else build_caption_text(script, pol),
                       script, pol)
    # Reviewer lifecycle: explicit not applicable for static fallback
    mode = (script.get("meta", {}) or {}).get("generation_mode", "")
    if reviewer_output is not None:
        if mode == "static-fallback":
            rep.block("reviewer_check", "reviewer output supplied for static fallback — not applicable (must be none)")
            rep.details["reviewer"] = {"present": True, "error": "reviewer supplied for fallback"}
            # still report not applicable but block to surface bug
        else:
            try:
                check_reviewer_output(rep, script, topic, reviewer_output, pol)
            except Exception as e:
                rep.warn("reviewer_check", f"could not parse reviewer report {e}", 1)
    elif ep_dir:
        check_reviewer(rep, script, topic, ep_dir, pol)
    else:
        if mode == "static-fallback":
            rep.details["reviewer"] = {"present": False, "not_applicable": True, "reason": "validated static fallback — reviewer not applicable"}
            rep.details["reviewer_check"] = "not applicable — validated static fallback"
            rep.checks["reviewer_check"] = "pass"
        else:
            rep.details["reviewer"] = {"present": False}
    return {"blocking": list(rep.blocking), "warnings": list(rep.warnings),
            "checks": dict(rep.checks),
            "words": common.spoken_word_count(script),
            "estimated_seconds": common.estimate_spoken_seconds(script, pol)}

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

    # Quarantine check
    try:
        cid = script.get("meta", {}).get("content_id")
        cdate = script.get("meta", {}).get("content_date")
        if common.is_quarantined(cid, cdate):
            rep.block("duplicate_check", f"content_id {cid} / date {cdate} is quarantined (translation-rejected) — must not publish")
    except Exception:
        pass

    check_content_language(rep, script, pol)
    check_english_only(rep, script, pol)
    check_technology_relevance(rep, script, topic, pol)
    check_metacognition_relevance(rep, script, pol)
    check_topic(rep, script, topic, pol)
    check_sources(rep, script, topic, pol, a.skip_network)
    check_script(rep, script, pol)
    check_english(rep, script, pol)
    check_text_renderability(rep, script, pol, timing)
    check_layout(rep, layout, timing, pol)
    check_video(rep, a.video, timing, pol, layout)
    check_audio(rep, a.video, timing, script, pol)
    if not a.no_frames:
        check_frames(rep, ep, layout, pol)
    check_visuals(rep, ep, script, a.video, pol, a.no_frames)
    # issue #33: the minimal-format contract (hard blockers + decoded-frame
    # proof) — a no-op for non-minimal formats
    check_minimal_contract(rep, ep, script, a.video, pol, a.no_frames)
    check_caption(rep, a.caption, script, pol)
    check_posters(rep, a.poster, a.poster45, pol)
    check_duplicates(rep, script, topic, memory, pol)
    check_buffer_readiness(rep, a.public_url, a.caption, a.skip_network, a.video)
    check_reviewer(rep, script, topic, ep, pol)

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
        "supervisor_version": "2.0-english-only",
        "language": "en",
    }
    return result

def to_markdown(r):
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    L = [f"## QA Supervisor — {'APPROVED' if r['approved'] else 'REJECTED'} · score {r['score']}/100 (min {r['min_score']}) lang=en", "", "| check | result |", "|---|---|"]
    for k, v in r["checks"].items():
        L.append(f"| {k} | {icon.get(v, v)} {v} |")
    if r["blocking_errors"]:
        L += ["", "**Blocking errors**"] + [f"- {b}" for b in r["blocking_errors"]]
    if r["warnings"]:
        L += ["", f"<details><summary>Warnings ({len(r['warnings'])})</summary>", ""] + [f"- {w}" for w in r["warnings"]] + ["", "</details>"]
    d = r.get("details", {})
    v, au = d.get("video", {}), d.get("audio", {})
    if v:
        L += ["", f"Video: {v.get('w')}x{v.get('h')} · {v.get('duration')}s · {v.get('size_mb')} MB · {v.get('codec')}"]
    if au:
        L += [f"Audio: mean {au.get('mean_db')} dB · {au.get('wps')} w/s"]
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
