"""Shared helpers for the @metacognition.hq reel factory.

Policy loading, editorial memory, text normalisation, hashing, safe JSON I/O.
No network access, no secrets. Every agent (trend_scout, content_producer,
qa_supervisor, buffer_publish, performance_analyst) imports from here.
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
FA_CATALOG_PATH = os.path.join(CONTENT, "fa_catalog.json")
FA_TREND_TEMPLATES_PATH = os.path.join(CONTENT, "fa_trend_templates.json")
FA_GLOSSARY_PATH = os.path.join(CONTENT, "fa_glossary.json")

TEHRAN_OFFSET = datetime.timedelta(hours=3, minutes=30)

PERSIAN_LETTER_RE = re.compile(
    r"[\u0621-\u063A\u0641-\u064A\u067E\u0686\u0698\u06A9\u06AF\u06CC\u06C0\u06BE\u0629]")
ARABIC_ONLY_RE = re.compile(r"[\u064A\u0643]")
LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’.\-]*")
ZWNJ = "\u200c"
PLACEHOLDER_RE = re.compile(
    r"\b(TODO|TBD|FIXME|SAMPLE|LOREM|PLACEHOLDER|XXX)\b|\{\{|\}\}|\[\[|\]\]|lorem ipsum",
    re.IGNORECASE)

PUBLISHED_LIKE = {"queued", "queued-in-buffer", "scheduled", "sent", "sending", "published-manual",
                  "approved-dry-run", "legacy-not-published"}

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
    core = [[(l["t"], f) for l, f in zip(ch["en"], ch["fa"])] for ch in script.get("chunks", [])]
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

def scrub_secrets(text):
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}", r"\1***", text or "")
    text = re.sub(r"\b[A-Za-z0-9_\-]{40,}\b", "***", text)
    return text

def env_flag_exact_true(name):
    return os.environ.get(name, "") == "true"

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

# ------------------------------------------------------------------ quarantine & FA catalog
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

def load_fa_catalog(path=None):
    return load_json(path or FA_CATALOG_PATH, default=None)

def load_fa_trend_templates(path=None):
    return load_json(path or FA_TREND_TEMPLATES_PATH, default=None)

def load_fa_glossary(path=None):
    return load_json(path or FA_GLOSSARY_PATH, default=None)

def en_hash(text):
    """Stable hash for English line -> used to bind curated FA translation."""
    norm = re.sub(r"\s+", " ", (text or "").strip())
    return sha256_text(norm)[:16]
