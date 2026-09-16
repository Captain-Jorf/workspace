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
