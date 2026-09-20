"""Shared helpers for the @metacognition.hq reel factory — English-only production.

Policy loading, editorial memory, text normalisation, hashing, safe JSON I/O.
No network access, no secrets. Every agent imports from here.

New in this version:
- CONTENT_LANGUAGE=en explicit fail-closed (English-only)
- Quarantine for Issue #14 (reel-2026-09-15)
- English-only script_hash (no FA)
- Generation mode tracking (groq vs static-fallback)
"""
import datetime
import hashlib
import json
import os
import re
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTENT = os.path.join(ROOT, "content")
POLICY_PATH = os.path.join(CONTENT, "editorial_policy.json")
MEMORY_PATH = os.path.join(CONTENT, "editorial_memory.json")
CALENDAR_PATH = os.path.join(CONTENT, "calendar.json")
PERF_PATH = os.path.join(CONTENT, "performance_history.json")
QUARANTINE_PATH = os.path.join(CONTENT, "quarantine.json")

TEHRAN_OFFSET = datetime.timedelta(hours=3, minutes=30)

# Legacy Persian detection (kept for quarantine doc, not used in production)
PERSIAN_LETTER_RE = re.compile(
    r"[\u0621-\u063A\u0641-\u064A\u067E\u0686\u0698\u06A9\u06AF\u06CC\u06C0\u06BE\u0629]")
ARABIC_ONLY_RE = re.compile(r"[\u064A\u0643]")
LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’.\-]*")
ZWNJ = "\u200c"
PLACEHOLDER_RE = re.compile(
    r"\b(TODO|TBD|FIXME|SAMPLE|LOREM|PLACEHOLDER|XXX)\b|\{\{|\}\}|\[\[|\]\]|lorem ipsum",
    re.IGNORECASE)

PUBLISHED_LIKE = {"queued", "queued-in-buffer", "scheduled", "sent", "sending", "published-manual",
                  "approved-dry-run", "legacy-not-published", "translation-rejected", "qa-failed"}

# Content language — fail-closed English-only
CONTENT_LANGUAGE = os.environ.get("CONTENT_LANGUAGE", "en").strip().lower()
if CONTENT_LANGUAGE not in ("en", "english"):
    # In production, only en is allowed. Any other value fails closed.
    # For local tests, allow override via ALLOW_NON_EN=1
    if os.environ.get("ALLOW_NON_EN") != "1":
        raise RuntimeError(f"CONTENT_LANGUAGE must be 'en' in production, got '{CONTENT_LANGUAGE}' — fail-closed English-only")

def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        if default is not None:
            return default
        raise

def save_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)

def policy(path=None):
    p = load_json(path or POLICY_PATH)
    if not p:
        raise RuntimeError(f"editorial policy missing/invalid: {path or POLICY_PATH}")
    return p

def calendar(path=None):
    c = load_json(path or CALENDAR_PATH)
    if not c or not c.get("episodes"):
        raise RuntimeError("content calendar missing or empty")
    return c

def empty_memory():
    return {"version": 2, "entries": []}

def load_memory(path=None):
    m = load_json(path or MEMORY_PATH, default=None)
    if not m or "entries" not in m:
        return empty_memory()
    return m

def save_memory(mem, path=None):
    mem["updated_utc"] = utc_now()
    save_json(path or MEMORY_PATH, mem)

def upsert_memory(mem, entry):
    cid = entry["content_id"]
    for i, e in enumerate(mem["entries"]):
        if e.get("content_id") == cid:
            merged = dict(e)
            merged.update({k: v for k, v in entry.items() if v is not None})
            mem["entries"][i] = merged
            return merged
    mem["entries"].append(entry)
    return entry

def recent_entries(mem, n=30, statuses=None):
    ents = sorted(mem.get("entries", []), key=lambda e: e.get("content_date", ""), reverse=True)
    if statuses:
        ents = [e for e in ents if e.get("status") in statuses]
    return ents[:n]

STOP = {"the", "a", "an", "of", "to", "in", "on", "and", "or", "for", "is", "are", "why", "how",
        "what", "your", "you", "it", "its", "that", "this", "with", "vs", "from", "by", "at", "be",
        "do", "does", "did", "can", "cant", "not", "so", "but", "when", "than", "then"}

def normalize_title(s):
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    toks = [t for t in s.split() if t not in STOP and len(t) > 2]
    return " ".join(toks)

def title_similarity(a, b):
    ta = {t[:6] for t in normalize_title(a).split()}
    tb = {t[:6] for t in normalize_title(b).split()}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def persian_ratio(s):
    # Legacy, kept for quarantine doc but not used in English-only production
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if PERSIAN_LETTER_RE.match(c)) / len(letters)

def latin_words(s):
    return LATIN_WORD_RE.findall(s or "")

def word_count(s):
    return len((s or "").split())

# ------------------------------------------------------------------ pre-render script gate
# Single-sourced word/duration counting shared by the deterministic PRE-RENDER gate
# (build/content_producer.py + build/pipeline.py) and the QA supervisor's own checks.
# All of them call word_count()/narration_lines()/spoken_word_count() below, so the
# gate and QA can never diverge. It counts ACTUAL narration tokens — a model-reported
# word count is never trusted — and it never "fixes" length by padding (silence,
# slowed speech or a repeated CTA are not the remedy; real content is).
DEFAULT_TTS_LEAD_SECONDS = 0.55
DEFAULT_TTS_TAIL_SECONDS = 1.2
DEFAULT_TTS_GAP_SECONDS = 0.34

def parse_speech_rate(rate):
    """edge-tts style rate string ('+5%', '-10%', None) as a multiplier."""
    if rate is None:
        return 1.0
    m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*%\s*$", str(rate))
    if not m:
        return 1.0
    return max(0.5, min(2.0, 1.0 + float(m.group(1)) / 100.0))

def narration_lines(script):
    """Every spoken-English narration line of a script, in order.

    This IS the QA extraction (script_quality / english / audio all read the same
    thing); the pre-render gate reuses it so both always count the same text.
    """
    out = []
    for ch in (script or {}).get("chunks", []) or []:
        for en in ch.get("en", []) or []:
            out.append(en.get("t", "") if isinstance(en, dict) else str(en))
    return out

def spoken_word_count(script):
    """Actual spoken English word count — sum of word_count() over narration_lines().

    Never a model-claimed number: exactly what the QA supervisor counts.
    """
    return sum(word_count(l) for l in narration_lines(script))

def pre_render_params(pol=None):
    """Effective pre-render gate parameters (policy-driven, with safe defaults).

    The required word range is the UNCHANGED QA policy range length.narration_words
    (150-260) and the duration safety band is derived from the UNCHANGED final
    requirement length.hard_seconds (60-120) minus length.pre_render.duration_margin_seconds.
    word_target (175-210) is guidance for prompts/revision instructions, never a gate.
    """
    pol = pol or policy()
    length = pol.get("length", {}) or {}
    pr = length.get("pre_render", {}) or {}
    lo, hi = (length.get("narration_words") or [150, 260])[:2]
    dlo, dhi = (length.get("hard_seconds") or [60, 120])[:2]
    margin = float(pr.get("duration_margin_seconds", 4.0))
    tlo, thi = (pr.get("word_target") or [175, 210])[:2]
    return {"words_min": int(lo), "words_max": int(hi),
            "target_min": int(tlo), "target_max": int(thi),
            "dur_min": float(dlo) + margin, "dur_max": float(dhi) - margin,
            "wpm_base": float(pr.get("narration_wpm_base", 150.0)),
            "chunk_pad": float(pr.get("chunk_pad_seconds", 0.28))}

def estimate_spoken_seconds(script, pol=None):
    """Deterministic spoken-duration estimate BEFORE TTS, from the CONFIGURED
    narration rate: words / (tts.rate applied to a base words-per-minute) plus the
    master-track overhead timing.py will add (lead, tail, per-chunk gaps and the
    silence padding around each chunk).

    Calibration: the failing run 35050738918 rendered a 106-word script at 45.3 s;
    this model predicts ~45.5 s. It is an ESTIMATE ONLY — the actual rendered
    duration remains authoritative in final QA (video_quality, 60-120 s).
    """
    pol = pol or policy()
    p = pre_render_params(pol)
    n = len((script or {}).get("chunks", []) or [])
    words = spoken_word_count(script)
    if not n or not words:
        return 0.0
    meta = (script or {}).get("meta", {}) or {}
    lead = float(meta.get("lead", DEFAULT_TTS_LEAD_SECONDS))
    tail = float(meta.get("tail", DEFAULT_TTS_TAIL_SECONDS))
    gap = float(meta.get("gap", DEFAULT_TTS_GAP_SECONDS))
    wps = p["wpm_base"] * parse_speech_rate((pol.get("tts", {}) or {}).get("rate")) / 60.0
    return round(words / wps + lead + tail + (n - 1) * gap + n * p["chunk_pad"], 2)

def pre_render_issues(script, pol=None):
    """Hard deterministic gate that must pass BEFORE TTS, subtitle generation or
    video rendering. Empty list = valid.

      1. actual spoken words (same single-sourced count as QA) inside the policy
         range 150-260 — required, thresholds untouched;
      2. estimated spoken duration (configured narration rate) inside a safe band
         around the final 60-120 s render requirement.

    A failing script is never padded into passing; the caller routes it through the
    one allowed Revision, then the validated Static English Fallback, then skip.
    """
    p = pre_render_params(pol)
    words = spoken_word_count(script)
    est = estimate_spoken_seconds(script, pol)
    issues = []
    if words < p["words_min"]:
        issues.append(f"spoken words {words} below the required {p['words_min']}-{p['words_max']}")
    elif words > p["words_max"]:
        issues.append(f"spoken words {words} above the required {p['words_min']}-{p['words_max']}")
    if not (p["dur_min"] <= est <= p["dur_max"]):
        issues.append(f"estimated spoken duration {est:.1f}s outside the safe "
                      f"{p['dur_min']:.0f}-{p['dur_max']:.0f}s band around the 60-120s render limit")
    return issues

def sha256_text(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def script_hash(script):
    """Hash of English narration only (English-only production)."""
    core = [l["t"] for ch in script.get("chunks", []) for l in ch.get("en", [])]
    return sha256_text(json.dumps(core, ensure_ascii=False, sort_keys=True))

def reviewer_candidate_hash(script, stage, attempt_id):
    """Deterministic canonical reviewer-candidate SHA-256 digest.

    Hashes all fields the Reviewer's decision applies to:
    content_id, content_date, generation_mode, pillar, technology_angle,
    metacognition_concept, complete English narration/chunks, hook, ending,
    actionable_technique, claims, sources using safe canonical fields,
    caption content, stage (initial or revision), attempt_id / variant.
    """
    if not isinstance(script, dict):
        return ""
    meta = script.get("meta", {}) if isinstance(script.get("meta"), dict) else {}

    stage_str = str(stage or "")
    attempt_str = str(attempt_id if attempt_id is not None else meta.get("variant", 0))
    if not attempt_str.startswith("variant-") and attempt_str.isdigit():
        attempt_str = f"variant-{attempt_str}"

    raw_sources = script.get("sources", [])
    canonical_sources = []
    if isinstance(raw_sources, list):
        for s in raw_sources:
            if isinstance(s, dict):
                canonical_sources.append({
                    "label": str(s.get("label", "") or ""),
                    "role": str(s.get("role", "") or ""),
                    "tier": str(s.get("tier", "") or ""),
                    "url": str(s.get("url", "") or ""),
                })
            elif isinstance(s, str):
                canonical_sources.append({"label": s, "role": "", "tier": "", "url": ""})

    raw_claims = script.get("claims", [])
    canonical_claims = []
    if isinstance(raw_claims, list):
        canonical_claims = [str(c) for c in raw_claims]

    raw_chunks = script.get("chunks", [])
    canonical_chunks = []
    if isinstance(raw_chunks, list):
        for ch in raw_chunks:
            if isinstance(ch, dict):
                en_lines = []
                for l in ch.get("en", []):
                    if isinstance(l, dict):
                        en_lines.append({
                            "beat": str(l.get("beat", "") or ""),
                            "scene": str(l.get("scene", "") or ""),
                            "t": str(l.get("t", "") or ""),
                        })
                    elif isinstance(l, str):
                        en_lines.append({"beat": "", "scene": "", "t": l})
                canonical_chunks.append({
                    "beat": str(ch.get("beat", "") or ""),
                    "en": en_lines,
                    "id": str(ch.get("id", "") or ""),
                    "tts_text": str(ch.get("tts_text", "") or ""),
                })

    caption = script.get("caption", {})
    if isinstance(caption, dict):
        canonical_caption = {}
        for k, v in caption.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                canonical_caption[k] = v
            elif isinstance(v, list):
                canonical_caption[k] = [str(x) if not isinstance(x, (dict, list)) else x for x in v]
            elif isinstance(v, dict):
                canonical_caption[k] = v
    else:
        canonical_caption = {}

    hook = str(canonical_caption.get("hook", "") or "")
    ctas = canonical_caption.get("ctas", [])
    ending = str(ctas[0] if isinstance(ctas, list) and ctas else "")
    actionable_tech = str(
        script.get("actionable_technique") or
        meta.get("actionable_technique") or
        ""
    )

    candidate_obj = {
        "actionable_technique": actionable_tech,
        "attempt_id": attempt_str,
        "caption": canonical_caption,
        "claims": canonical_claims,
        "content_date": str(meta.get("content_date", "") or ""),
        "content_id": str(meta.get("content_id", "") or ""),
        "ending": ending,
        "generation_mode": str(meta.get("generation_mode", "groq") or ""),
        "hook": hook,
        "metacognition_concept": str(meta.get("metacognition_concept", "") or ""),
        "pillar": str(meta.get("pillar", "") or ""),
        "sources": canonical_sources,
        "stage": stage_str,
        "technology_angle": str(meta.get("technology_angle", "") or ""),
        "chunks": canonical_chunks,
    }

    canonical_bytes = json.dumps(
        candidate_obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(',', ':')
    ).encode('utf-8')

    return hashlib.sha256(canonical_bytes).hexdigest()

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def to_tehran(iso_utc):
    if not iso_utc:
        return ""
    s = str(iso_utc).replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return str(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    local = dt.astimezone(datetime.timezone(TEHRAN_OFFSET))
    return local.strftime("%Y-%m-%d %H:%M") + " Asia/Tehran"

def content_id(tag):
    return f"reel-{tag}"

def episode_dir(tag):
    return os.path.join(CONTENT, "episodes", f"auto-{tag}")

def output_paths(tag):
    out = os.path.join(ROOT, "output")
    return {
        "mp4": os.path.join(out, f"auto-{tag}.mp4"),
        "caption": os.path.join(out, f"auto-{tag}_caption.txt"),
        "poster": os.path.join(out, f"auto-{tag}_poster.jpg"),
        "poster_4x5": os.path.join(out, f"auto-{tag}_poster_4x5.jpg"),
        "previews_dir": os.path.join(out, "drafts", f"auto-{tag}"),
        "qa_json": os.path.join(out, f"auto-{tag}_qa.json"),
        "qa_md": os.path.join(out, f"auto-{tag}_qa.md"),
        "manifest": os.path.join(out, f"auto-{tag}_manifest.json"),
        "marker": os.path.join(out, f"auto-{tag}.buffer.json"),
    }

# Env vars whose VALUES must never survive in a log, error, traceback,
# GitHub annotation, issue body, artifact or test failure diff.
SECRET_ENV_NAMES = ("GROQ_API_KEY", "BUFFER_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
                    "GROQ_KEY", "OPENAI_API_KEY")
# sha256(value) -> compiled fragment matcher (the value itself is never cached)
_FRAGMENT_CACHE = {}


def _fragment_pattern(value):
    """Compiled matcher for identifiable FRAGMENTS of a secret value.

    A rotated/partially-copied key can still show up in a log as a substring, so
    redacting only the exact value is not enough. Windows are long enough to
    avoid colliding with ordinary prose. Compiled patterns are cached by the
    SHA-256 of the value (the value itself is never stored).
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    cached = _FRAGMENT_CACHE.get(digest)
    if cached is not None:
        return cached
    width = max(12, min(16, len(value) // 4))
    windows = sorted({value[i:i + width] for i in range(0, len(value) - width + 1)})
    pattern = re.compile("|".join(re.escape(w) for w in windows)) if windows else None
    _FRAGMENT_CACHE[digest] = pattern
    return pattern


def scrub_secrets(text):
    """Central secret scrubber for EVERY log/error/artifact/issue path.

    Redacts, in order:
      1. exact values of credential-bearing env vars (GROQ_API_KEY,
         BUFFER_TOKEN, GITHUB_TOKEN, GH_TOKEN, ...) — and identifiable
         fragments of them, so a partially copied key cannot survive;
      2. `Bearer <token>` / `token=<value>` / `api_key: <value>` shapes;
      3. provider key shapes (`gsk_...`);
      4. long opaque runs (>= 40 chars).

    Only the NAME of the redacted variable is kept. Never the value, never its
    length, never a real prefix/suffix.
    """
    if text is None:
        return ""
    s = str(text)
    for name in SECRET_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if len(value) < 8:
            continue
        if value in s:
            s = s.replace(value, f"[redacted:{name}]")
        if len(value) >= 20:
            frag = _fragment_pattern(value)
            if frag is not None:
                s = frag.sub(f"[redacted:{name}:fragment]", s)
    s = re.sub(r"(?i)(bearer\s+)[^\s'\"]+", r"\1***", s)
    s = re.sub(r"(?i)\bgsk_[A-Za-z0-9_\-]{4,}", "[redacted-key]", s)
    s = re.sub(r"(?i)((?:api|access|auth|secret|client|refresh)[_-]?(?:key|token)|password)"
               r"([\"']?\s*[:=]\s*)([^\s,;\"'}\]]{6,})",
               r"\1\2***", s)
    s = re.sub(r"\b[A-Za-z0-9_\-]{40,}\b", "***", s)
    return s

def env_flag_exact_true(name):
    return os.environ.get(name, "") == "true"

# ------------------------------------------------------------------ evidence grounding
# Single-sourced tier derivation and claim-word matching (issue #22).
#
# The QA supervisor's source_quality blocker and the deterministic PRE-RENDER
# text gate use THESE functions — there is no second implementation and no
# second keyword list anywhere in the pipeline. The claim-word patterns are
# exactly policy source_policy.require_evidence_for_claim_words; the tier of a
# source is derived (never taken from a stored "tier" field the model could
# claim for itself) with source_tier below. Nothing here can invent a citation,
# URL, author, statistic or tier: grounding is only ever established from what
# the sanitized packet itself provides.
def source_tier(url, label, pol):
    """Tier of one source by URL domain / dated-label pattern, per policy tiers.

    Moved verbatim from qa_supervisor (issue #22) so the pre-render text gate,
    the evidence packet and final QA share ONE matcher. A stored "tier" field is
    never consulted — only what the URL/label actually proves.
    """
    tiers = pol["source_policy"]["tiers"]
    u = (url or "").lower()
    if u:
        for t in ("A", "B", "C"):
            for dom in tiers.get(t, []):
                if dom.lower() in u:
                    return t
        # Tech discovery tier
        for dom in pol["source_policy"]["tiers"].get("TECH_DISCOVERY", []):
            if dom.lower() in u:
                return "C"
        return "C"
    if re.search(r"\(\d{4}\)", label or "") or re.search(r"\b(19|20)\d{2}\b", label or ""):
        return "A"
    return "?"

def claim_word_matches(text, pol):
    """The source_quality claim-word matcher — the exact patterns QA blocks.

    Returns the policy claim-word patterns present in `text` as whole
    (case-insensitive, word-boundary) matches. Single implementation shared by
    final QA (qa_supervisor.check_sources), the pre-render text gate and the
    producer/revision guidance: prompts never carry their own keyword list.
    """
    low = (text or "").lower()
    return [w for w in pol["source_policy"]["require_evidence_for_claim_words"]
            if re.search(rf"\b{re.escape(w)}\b", low)]

def best_tier(tiers):
    """Best of a list of tiers ('A' beats 'B' beats 'C' beats '?'); '' when empty.

    Unknown tier strings rank last — they can never look better than a real tier.
    """
    return min(tiers, key=lambda t: "ABC?".index(t) if t in ("A", "B", "C", "?") else 9) if tiers else "?"

def packet_evidence_urls(packet):
    """The URLs a sanitized evidence packet itself provides. The ONLY source
    URLs an LLM output may carry; anything else is an invented citation and is
    rejected deterministically (producer citation guard)."""
    urls = set()
    for key in ("discovery_source", "evidence_source"):
        u = str(((packet or {}).get(key) or {}).get("url") or "").strip()
        if u:
            urls.add(u)
    return urls

def ungrounded_source_urls(sources, packet):
    """Script source URLs the evidence packet does NOT provide (invented citations)."""
    allowed = packet_evidence_urls(packet)
    out, seen = [], set()
    for s in sources or []:
        u = str((s or {}).get("url") or "").strip()
        if u and u not in allowed and u not in seen:
            seen.add(u)
            out.append(u)
    return out

def packet_evidence_tier(packet, pol=None):
    """Best evidence tier the PACKET itself carries, derived with source_tier —
    same matcher as final QA, never a stored/claimed tier."""
    pol = pol or policy()
    tiers = []
    e = (packet or {}).get("evidence_source") or {}
    if e.get("label") or e.get("url"):
        tiers.append(source_tier(e.get("url", ""), e.get("label", ""), pol))
    d = (packet or {}).get("discovery_source") or {}
    if d.get("url"):
        tiers.append(source_tier(d["url"], d.get("name", ""), pol))
    return best_tier(tiers) if tiers else "?"

def packet_has_tier_ab_evidence(packet, pol=None):
    """True when the sanitized packet carries a Tier A/B evidence source — the
    same bar the source_quality blocker applies to claim words."""
    return packet_evidence_tier(packet, pol) in ("A", "B")

# ------------------------------------------------------------------ numeric-claim grounding
# Single source of truth for "what counts as a numeric claim" in narration.
# The QA supervisor (check_sources → source_quality blocker) and the producer's
# deterministic pre-gate MUST use the same matcher, so the gate can never be
# looser (or stricter in a surprising way) than the QA threshold it mirrors.
NUMERIC_CLAIM_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?\s?(?:%|percent\b)")

def find_numeric_claims(text):
    """Percentages / 'N percent' in `text`, in order of appearance."""
    return NUMERIC_CLAIM_RE.findall(text or "")

def normalize_numeric_claim(claim):
    """'100 %' / '100%' → '100%'; '92 percent' → '92%'. For allowlist comparison."""
    c = str(claim).strip().lower()
    c = re.sub(r"\s+", " ", c)
    m = re.match(r"^(\d{1,3}(?:\.\d+)?)\s?(?:%|percent)$", c)
    return f"{m.group(1)}%" if m else c

def packet_numeric_evidence(packet):
    """Set of numeric claims EXPLICITLY present in the sanitized evidence packet.

    The packet is the only trusted fact source for the producer; calendar /
    evergreen packets deliberately contain no statistics, so this is normally
    empty — which is exactly what the hard rule in the prompts encodes.
    """
    blob = " ".join([
        str((packet or {}).get("trusted_excerpt", "")),
        str(((packet or {}).get("evidence_source") or {}).get("label", "")),
        str(((packet or {}).get("discovery_source") or {}).get("name", "")),
    ])
    return {normalize_numeric_claim(c) for c in find_numeric_claims(blob)}

def unsupported_numeric_claims(narration_text, packet):
    """Numeric claims in `narration_text` that the evidence packet does NOT support.

    Mirrors the QA source_quality rule ("statistics cannot be verified
    automatically") so the producer can fail closed BEFORE tts/render/QA, and
    so calendar/evergreen generation can never introduce percentages,
    statistics, study results or precise numeric claims on its own.
    """
    allowed = packet_numeric_evidence(packet)
    out, seen = [], set()
    for c in find_numeric_claims(narration_text):
        n = normalize_numeric_claim(c)
        if n in allowed:
            continue
        if n not in seen:
            seen.add(n)
            out.append(c)
    return out

def recent_cta_types(memory, n=5):
    """CTA types of the most recent `n` editorial-memory entries (newest first).

    Used for rolling CTA diversity. Entries without a cta_type are skipped;
    an unavailable/empty memory yields [] — callers fall back deterministically.
    """
    out = []
    for e in recent_entries(memory or empty_memory(), max(n, 1)):
        ct = e.get("cta_type")
        if ct and ct not in out:
            out.append(ct)
        if len(out) >= n:
            break
    return out

# ------------------------------------------------------------------ hashtags
def normalize_hashtags(tags, pol):
    """Limited, non-spam hashtag set with the brand tags guaranteed present.

    Rules (from hashtag_policy):
      * every tag in `always` (brand, e.g. #metacognitionhq) is included, first;
      * banned (spam) tags are dropped;
      * duplicates (case-insensitive) are collapsed;
      * the set is capped at `max` — brand tags are the ones that survive the cap.
    """
    hp = pol["hashtag_policy"]
    banned = {t.lower() for t in hp.get("banned", [])}
    out, seen = [], set()
    for t in list(hp.get("always", [])) + list(tags or []):
        t = str(t).strip()
        if not t:
            continue
        if t[0] != "#":
            t = "#" + t
        if t.lower() in banned or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
    return out[: hp.get("max", 8)]

def min_qa_score(pol=None):
    pol = pol or policy()
    floor = int(pol["qa_thresholds"].get("min_score_floor", 80))
    default = int(pol["qa_thresholds"].get("min_score", 85))
    raw = os.environ.get("MIN_QA_SCORE", "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(val, floor)

# ------------------------------------------------------------------ quarantine (preserved from PR #15)
def load_quarantine(path=None):
    return load_json(path or QUARANTINE_PATH, default={}) or {}

def is_quarantined(content_id, date_tag=None):
    q = load_quarantine()
    ids = set(q.get("quarantined_content_ids", []))
    dates = set(q.get("quarantined_dates", []))
    if content_id in ids:
        return True
    if date_tag and date_tag in dates:
        return True
    if date_tag and content_id == f"reel-{date_tag}" and date_tag in dates:
        return True
    return False

def assert_content_language_en():
    """Fail-closed English-only check, callable from agents."""
    import os
    lang = os.environ.get("CONTENT_LANGUAGE", "en").strip().lower()
    if lang not in ("en", "english"):
        if os.environ.get("ALLOW_NON_EN") != "1":
            raise SystemExit(f"CONTENT_LANGUAGE must be 'en' in production, got '{lang}' — fail-closed")

def en_hash(text):
    """Stable hash for English line."""
    norm = re.sub(r"\s+", " ", (text or "").strip())
    return sha256_text(norm)[:16]
