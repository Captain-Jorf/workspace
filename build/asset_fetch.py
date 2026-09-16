"""$0, license-aware external imagery for topic-aware Reel scenes.

Issue #24 §7: the Reel needs multiple distinct, topic-relevant images while
staying $0 and legally safe. This module implements the ONLY external image
source: the official Openverse API (openverse.org) — a free, no-key,
license-aware index of openly licensed images (production draws ONLY
from the attribution-free public-domain set, CC0 / PDM). It is explicitly
NOT a paid image-generation API, does not enable billing, does not scrape
Google Images, does not use unofficial endpoints or browser automation, and
never downloads executable content.

Fail-soft by design: ANY problem (network, timeout, malformed response,
license not allowed, unsafe URL, unreadable image) returns None and the caller
falls back to a deterministic topic-specific procedural visual. The pipeline
never fails because a stock image was unavailable, and never pays for one.

Untrusted-input handling: everything the API returns is remote, untrusted data.
Only the structured fields the manifest needs are read, sanitized (length-capped,
control characters stripped) and validated against allowlists. Remote text can
never alter prompts, policy, commands, workflow behavior or file paths — it is
copied into the plan manifest verbatim-sans-control-chars for attribution only.

Enabled by default in the production GitHub Actions path (the daily workflow
sets ASSET_FETCH=1 on the produce step) and disabled otherwise — local runs
stay fully deterministic. It needs NO API key, NO billing, NO scraping, NO
browser automation: one GET to the official public Openverse API, one GET for
the image, each with a strict 25 s timeout and exactly ONE attempt (no
retries, no unbounded network dependency). Any failure → None → the caller
falls back to a deterministic procedural visual.

LICENSE POLICY (Instagram attribution safety): production uses ONLY
attribution-free public-domain imagery — CC0 and PDM. CC-BY is deliberately
DISABLED (it is not in ALLOWED_LICENSES): a CC-BY credit stored only in an
internal QA manifest is NOT public attribution to Instagram viewers, and the
reel pipeline has no public attribution channel yet. Re-enabling CC-BY
requires implementing public credit (caption or explicitly linked credits
page) plus a test proving a CC-BY asset can never reach publication without
it.
"""
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request

API_URL = "https://api.openverse.org/v1/images/"
# Attribution-free public-domain licenses ONLY (see module docstring,
# LICENSE POLICY). CC-BY ("by") is intentionally absent: without a public
# attribution channel on Instagram, a CC-BY image can never be published.
ALLOWED_LICENSES = ("cc0", "pdm")
# Openverse license code → canonical manifest name
_OPENVERSE_LICENSE = {"cc0": "cc0", "pdm": "pdm"}
MAX_BYTES = 8 * 1024 * 1024          # never a large/untrusted blob
MIN_IMAGE_DIM = 400                  # too small to be a scene image
TIMEOUT = 25                          # strict budget, one attempt
USER_AGENT = "metacognition-hq-reel-factory/1.0 (+https://github.com/Captain-Jorf/workspace)"

_QUERY_RE = re.compile(r"[^a-z0-9 ]+")


def enabled():
    return os.environ.get("ASSET_FETCH", "0") == "1"


def sanitize_query(text, max_words=8):
    """Derive a sanitized query from OUR OWN deterministic topic keywords
    (never from remote text): lowercase alphanumerics + spaces, capped."""
    s = (text or "").lower()
    s = _QUERY_RE.sub(" ", s)
    words = [w for w in s.split() if w][:max_words]
    return " ".join(words)[:60]


def _clean(text, limit=120):
    """Sanitize a remote string field for the manifest (attribution only)."""
    s = str(text or "")
    s = "".join(ch for ch in s if ord(ch) >= 32)
    return s.strip()[:limit]


def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            return None
        return r.read(MAX_BYTES + 1)


def _download_image(url, cache_path, timeout=TIMEOUT):
    """Download an https image with a hard size cap; returns the saved path
    or None. Only image content types, only https."""
    if not url or not url.strip().startswith("https://"):
        return None
    if any(ord(ch) < 32 for ch in url) or ".." in url or " " in url:
        return None
    req = urllib.request.Request(url.strip(), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith("image/"):
                return None
            data = r.read(MAX_BYTES + 1)
    except Exception:
        return None
    if len(data) > MAX_BYTES or len(data) < 4096:
        return None
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(data)
    return cache_path


def _cache_dir():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "output", ".asset_cache")


def fetch_image(query, timeout=TIMEOUT, skip_urls=()):
    """Fetch ONE attribution-free (CC0/PDM) image for a sanitized query.

    Exactly ONE API attempt, one download attempt — no retries. `skip_urls`
    carries asset URLs already used by other scenes of this reel so a reel
    can never silently repeat one photograph. Returns a provenance manifest
    dict (recorded verbatim in the scene plan, validated by the pre-render
    visual gate) or None → deterministic procedural fallback.
    """
    if not enabled():
        return None
    q = sanitize_query(query)
    if not q:
        return None
    try:
        import datetime
        params = urllib.parse.urlencode({
            "q": q, "license_type": ",".join(ALLOWED_LICENSES),
            "size": "large", "page_size": "5",
        })
        raw = _get(API_URL + "?" + params, timeout)
        if raw is None or len(raw) > MAX_BYTES:
            return None
        data = json.loads(raw.decode("utf-8", "replace"))
        results = data.get("results") or []
        for res in results:
            if not isinstance(res, dict):
                continue
            license_ = _OPENVERSE_LICENSE.get(str(res.get("license") or "").lower())
            if license_ not in ALLOWED_LICENSES:
                continue  # incl. every CC-BY variant — no public attribution
            asset_url = str(res.get("url") or "").strip()
            if not asset_url.startswith("https://"):
                continue
            if any(ord(ch) < 32 for ch in asset_url) or ".." in asset_url:
                continue
            if skip_urls and asset_url in set(skip_urls):
                continue  # already used by another scene of this reel
            source_url = str(res.get("foreign_landing_url") or res.get("source") or "").strip()
            if source_url and not source_url.startswith("https://"):
                source_url = ""
            creator = _clean(res.get("creator") or res.get("by"))
            title = _clean(res.get("title"))
            # Defensive safety net: if the allowlist is ever loosened to a
            # license that REQUIRES attribution, never emit it without a
            # named creator. Unreachable while ALLOWED_LICENSES is CC0/PDM.
            if license_ == "cc-by" and not creator:
                continue
            digest = hashlib.sha256(f"{q}|{asset_url}".encode("utf-8")).hexdigest()[:16]
            cache_path = os.path.join(_cache_dir(), f"ext_{digest}.jpg")
            if not os.path.exists(cache_path):
                if _download_image(asset_url, cache_path, timeout) is None:
                    continue
            # validate it is a real image (PIL decode + min size)
            try:
                from PIL import Image
                im = Image.open(cache_path)
                im.verify()
                im = Image.open(cache_path)
                w, h = im.size
                if min(w, h) < MIN_IMAGE_DIM:
                    continue
            except Exception:
                try:
                    os.remove(cache_path)
                except OSError:
                    pass
                continue
            sha = hashlib.sha256(open(cache_path, "rb").read()).hexdigest()
            return {
                "kind": "external",
                "id": f"ext-{sha[:12]}",
                "url": source_url or asset_url,
                "asset_url": asset_url,
                "creator": creator,
                "title": title,
                "license": license_,
                "retrieved_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "query_sanitized": q,
                "sha256": sha,
                "path": cache_path,
                "origin": "openverse-api",
            }
    except Exception:
        # fail-soft: procedural fallback, never a pipeline failure, never paid
        return None
    return None
