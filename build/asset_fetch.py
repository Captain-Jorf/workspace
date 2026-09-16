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

SECURITY (the image URL is remote, untrusted data — SSRF / redirect /
payload / filesystem hardening):
  * HTTPS only, no embedded URL credentials, port 443 (or the implicit
    default) only — nonstandard ports are not justified;
  * SSRF: the hostname must resolve (getaddrinfo) and EVERY resolved
    address — IPv4 AND IPv6 — must be globally routable. Loopback,
    private, link-local (incl. the 169.254.169.254 cloud-metadata range),
    multicast, reserved and unspecified addresses are rejected, for
    IP-literal hosts and DNS names alike (a public name resolving to a
    private IP is rejected). The CONNECTED peer address is re-checked
    after the TCP/TLS handshake (defense against DNS-rebinding);
  * redirects are NOT followed automatically: each 3xx is followed
    manually, at most MAX_REDIRECTS hops, and every target is re-validated
    from scratch (scheme, credentials, port, hostname, every resolved
    address, peer) — a public URL can never redirect to a private/local
    address, and a redirect loop terminates;
  * the response body is STREAMED with a hard byte cap (the read stops
    early on overflow — no unbounded download);
  * the image is validated BEFORE anything touches disk: magic bytes and
    Content-Type must AGREE and be JPEG/PNG/WebP only (SVG, HTML, XML,
    archives, GIF/BMP/TIFF and any other payload are rejected); decoded
    dimension, aspect-ratio and total-pixel limits with Pillow's
    decompression-bomb protection; a full integrity check. The server's
    filename/extension is never trusted;
  * the write is ATOMIC into a generated digest filename inside the
    dedicated cache directory (mkstemp + os.replace): a remote filename is
    never used, a planted symlink at the destination is replaced (never
    followed), a path-traversal name cannot reach the filesystem, and a
    partial write never survives a failure. The cache directory is
    git-ignored and is never an uploaded repository artifact;
  * logging: the module logs NOTHING — no query string, remote body,
    secret or binary content can reach a log. The only diagnostic kept
    (LAST_ERROR) is the exception CLASS name (never the message, so no
    URL/query/remote text survives) run through the central secret
    scrubber (common.scrub_secrets).
"""
import hashlib
import http.client
import ipaddress
import io
import json
import os
import re
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import common  # noqa: E402  (central secret scrubber for the one diagnostic)

API_URL = "https://api.openverse.org/v1/images/"
# Attribution-free public-domain licenses ONLY (see module docstring,
# LICENSE POLICY). CC-BY ("by") is intentionally absent: without a public
# attribution channel on Instagram, a CC-BY image can never be published.
ALLOWED_LICENSES = ("cc0", "pdm")
# Openverse license code → canonical manifest name
_OPENVERSE_LICENSE = {"cc0": "cc0", "pdm": "pdm"}
MAX_BYTES = 8 * 1024 * 1024          # never a large/untrusted blob
MIN_IMAGE_DIM = 400                  # too small to be a scene image
TIMEOUT = 25                         # strict budget, one attempt
USER_AGENT = "metacognition-hq-reel-factory/1.0 (+https://github.com/Captain-Jorf/workspace)"

_QUERY_RE = re.compile(r"[^a-z0-9 ]+")

# ---------------------------------------------------------------------------
# Network / payload hardening constants
# ---------------------------------------------------------------------------
MAX_REDIRECTS = 2                # bounded redirect budget (then give up)
MAX_DIMENSION = 8192             # decoded side limit, px
MAX_ASPECT = 4.0                 # max(w/h, h/w) — reject banner/strip blobs
MAX_TOTAL_PIXELS = 32_000_000    # decoded pixel budget (decompression-bomb guard)
MIN_BODY_BYTES = 4096            # compressed body floor: a "photo" under 4KB is not a photo
_CHUNK = 256 * 1024              # streaming read size
_MAGIC_TO_FORMAT = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
_MIME_TO_KIND = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp"}
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.?$")

# The ONLY diagnostic the fetcher keeps (see the logging note in the
# docstring): an exception CLASS name, scrubbed. Never a URL, query string,
# remote body, secret or binary content.
LAST_ERROR = ""


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


def _note_error(err):
    """Record the single diagnostic: the exception CLASS name (never the
    message — a message would carry URLs, query strings and possibly remote
    text), run through the central secret scrubber."""
    global LAST_ERROR
    name = type(err).__name__ if isinstance(err, BaseException) else "unknown"
    LAST_ERROR = common.scrub_secrets(f"asset-fetch: {name}")


# ---------------------------------------------------------------------------
# SSRF validation: URL string, resolved addresses, connected peer
# ---------------------------------------------------------------------------
def _is_public_ip(ip):
    """True only for globally routable addresses (IPv4 + IPv6). Rejects
    loopback, private, link-local (169.254.0.0/16 incl. the 169.254.169.254
    cloud-metadata host), multicast, reserved, unspecified and CGNAT/
    benchmark/test-net ranges — the `is_global` flag plus explicit checks."""
    if not isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return False
    return (ip.is_global
            and not ip.is_loopback
            and not ip.is_private
            and not ip.is_link_local
            and not ip.is_multicast
            and not ip.is_reserved
            and not ip.is_unspecified)


def _public_ip_literal(host):
    """The ip_address for an IP-literal host if it is public, else None
    (None also when the host is not an IP literal at all)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return ip if _is_public_ip(ip) else None


def _is_ip_literal(host):
    return ":" in host or bool(re.fullmatch(r"[0-9.]{1,15}", host))


def _validate_url(url):
    """Structural SSRF validation of a URL string (no network is touched):
    https only, no embedded credentials, port 443 (or the implicit default)
    only, sane hostname shape; IP-literal hosts must already be public."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if len(u) > 2000 or any(ord(ch) < 32 for ch in u) or ".." in u or " " in u:
        return False
    try:
        p = urllib.parse.urlparse(u)
        host = p.hostname or ""
        port = p.port  # raises ValueError on a malformed port
    except (ValueError, TypeError):
        return False
    if p.scheme != "https" or not host:
        return False
    if p.username is not None or p.password is not None:
        return False  # embedded credentials
    if port is not None and port != 443:
        return False  # nonstandard ports are not justified
    host = host.lower().rstrip(".")
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
        return False
    if _is_ip_literal(host):
        return _public_ip_literal(host) is not None
    return bool(_HOSTNAME_RE.match(host))


def _assert_public_host(host):
    """Resolve `host` via DNS and require EVERY resolved address (IPv4 and
    IPv6 alike) to be globally routable. A public name resolving to any
    private/loopback/link-local/multicast/reserved/unspecified address is
    rejected (SSRF via DNS / DNS-rebinding at resolution time)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]  # drop a v6 zone id
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if not _is_public_ip(ip):
            return False
    return True


def _peer_is_public(peername):
    """The CONNECTED peer must be a public address — checked AFTER the
    TCP/TLS handshake, so a DNS-rebinding swap between validation and
    connect cannot reach a private/local target."""
    try:
        ip = ipaddress.ip_address(str(peername[0]).split("%", 1)[0])
    except (ValueError, IndexError, TypeError):
        return False
    return _is_public_ip(ip)


# ---------------------------------------------------------------------------
# Hardened opener: peer-checked TLS, no automatic redirect following
# ---------------------------------------------------------------------------
class _PeerCheckedHTTPS(http.client.HTTPSConnection):
    """HTTPS connection that re-validates the CONNECTED peer address after
    the handshake (final defense against DNS rebinding). TLS certificate
    validation stays enabled via Python's default SSL context."""

    def connect(self):
        super().connect()
        peer = None
        try:
            peer = self.sock.getpeername()
        except OSError:
            pass
        if peer is None or not _peer_is_public(peer):
            try:
                self.sock.close()
            except OSError:
                pass
            raise OSError("refusing to talk to a non-public peer address")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Suppress urllib's automatic redirect following: a 3xx surfaces as an
    HTTPError, and _safe_get follows it manually — bounded, and with the
    target re-validated from scratch."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PeerHTTPSHandler(urllib.request.AbstractHTTPHandler):
    def https_open(self, req):
        return self.do_open(_PeerCheckedHTTPS, req)


_OPENER = None


def _get_opener():
    """The hardened opener. The handler set is built explicitly (not via
    build_opener) so the ONLY https handler is the peer-checked one and the
    ONLY redirect behavior is _NoRedirectHandler."""
    global _OPENER
    if _OPENER is None:
        od = urllib.request.OpenerDirector()
        od.add_handler(urllib.request.ProxyHandler())
        od.add_handler(urllib.request.UnknownHandler())
        od.add_handler(_PeerHTTPSHandler())
        od.add_handler(urllib.request.HTTPErrorProcessor())
        od.add_handler(urllib.request.HTTPDefaultErrorHandler())
        od.add_handler(_NoRedirectHandler())
        _OPENER = od
    return _OPENER


def _open(req, timeout):
    """One network open through the hardened opener (the test seam)."""
    return _get_opener().open(req, timeout=timeout)


def _read_capped(resp, max_bytes):
    """Stream the response body with a hard cap. Returns the bytes, or None
    when the body exceeds max_bytes — the read STOPS EARLY (no unbounded
    download before the size check)."""
    out = bytearray()
    while True:
        chunk = resp.read(_CHUNK)
        if not chunk:
            return bytes(out)
        out += chunk
        if len(out) > max_bytes:
            return None


def _request(url, timeout=TIMEOUT, max_bytes=MAX_BYTES, accept="*/*"):
    """One SSRF-validated HTTPS request (redirects are NOT followed here).
    Returns (status, headers, data) — data is None for redirect/error
    responses or a capped body — or None when the URL itself fails
    validation. Transport failures raise; _safe_get converts them to None
    (fail-soft)."""
    if not _validate_url(url):
        return None
    host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    if not _is_ip_literal(host) and not _assert_public_host(host):
        return None
    req = urllib.request.Request(
        url.strip(), headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        resp = _open(req, timeout)
    except urllib.error.HTTPError as e:
        return e.code, e.headers, None
    try:
        status = getattr(resp, "status", None)
        if status is None:
            status = getattr(resp, "code", 0)
        data = _read_capped(resp, max_bytes)
        return status, resp.headers, data
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _safe_get(url, timeout=TIMEOUT, max_bytes=MAX_BYTES, max_redirects=MAX_REDIRECTS,
              accept="*/*"):
    """GET with bounded, re-validated redirect following.

    Every hop — the original URL and each redirect target — is re-validated
    from scratch (scheme, credentials, port, hostname, EVERY resolved
    address, connected peer), so a public URL can never redirect to a
    private/local address; a redirect loop or more than max_redirects hops
    terminates with None. Returns (200, headers, body) or None for any
    violation or transport failure (fail-soft; LAST_ERROR records the
    scrubbed exception class name)."""
    current = url
    try:
        for _ in range(max_redirects + 1):
            res = _request(current, timeout=timeout, max_bytes=max_bytes, accept=accept)
            if res is None:
                return None
            status, headers, data = res
            if 300 <= status < 400:
                loc = (headers.get("Location") or "").strip()
                if not loc:
                    return None
                current = urllib.parse.urljoin(current, loc)
                continue
            if status != 200 or data is None:
                return None
            return 200, headers, data
        return None  # redirect budget exhausted (loop or too many hops)
    except Exception as e:
        _note_error(e)
        return None


def _get(url, timeout=TIMEOUT):
    """Bounded, SSRF-validated GET of the Openverse API (one attempt, no
    retries, no unbounded network dependency). Returns the JSON body bytes
    (capped at MAX_BYTES) or None."""
    res = _safe_get(url, timeout=timeout, accept="application/json")
    if res is None:
        return None
    return res[2]


# ---------------------------------------------------------------------------
# Image payload validation (before anything touches disk)
# ---------------------------------------------------------------------------
def _magic_kind(data):
    """Raster kind from the decoded MAGIC bytes (the server's filename and
    extension are never trusted). Anything that is not JPEG/PNG/WebP magic
    — SVG/XML/HTML, ZIP/archives, GIF/BMP/TIFF, scripts, polyglots — is
    rejected."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _pixel_safe(data, kind):
    """Decoded-image validation: PIL must agree with the magic on the
    format; min/max dimension, total-pixel (decompression-bomb) and
    aspect-ratio limits; zero-size, corrupt or truncated payloads fail the
    integrity check."""
    try:
        from PIL import Image
    except Exception:
        return False
    Image.MAX_IMAGE_PIXELS = MAX_TOTAL_PIXELS  # Pillow's own bomb guard
    try:
        im = Image.open(io.BytesIO(data))
        if im.format != _MAGIC_TO_FORMAT[kind]:
            return False  # magic said JPEG/PNG/WebP, PIL disagrees
        w, h = im.size
        if w <= 0 or h <= 0:
            return False
        if min(w, h) < MIN_IMAGE_DIM:
            return False
        if max(w, h) > MAX_DIMENSION:
            return False
        if w * h > MAX_TOTAL_PIXELS:
            return False
        if max(w, h) / float(min(w, h)) > MAX_ASPECT:
            return False
        im.verify()
        im = Image.open(io.BytesIO(data))
        im.load()  # full decode: catches truncation/corruption verify() misses
        return im.size == (w, h)
    except Exception:
        return False


def _atomic_write(path, data):
    """Write `data` to `path` atomically: a fresh 0600 temp file in the SAME
    directory (mkstemp — O_EXCL, generated name, never follows a symlink) is
    written and fsynced, then os.replace() moves it over the destination. A
    planted symlink at the destination is replaced AS A LINK (never
    followed), a path-traversal name cannot reach the filesystem (the temp
    file lives in the destination's own directory), and any failure removes
    the partial temp file."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".part_", dir=directory)
    try:
        f = os.fdopen(fd, "wb")
    except Exception:
        os.close(fd)
        raise
    try:
        with f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _cached_image_ok(path):
    """A cache entry is trusted only if it is a REGULAR file (never a
    symlink) holding a decodable allowed raster (magic + dimensions +
    integrity)."""
    try:
        if not os.path.isfile(path) or os.path.islink(path):
            return False
        with open(path, "rb") as fh:
            blob = fh.read(MAX_BYTES + 1)
        if len(blob) > MAX_BYTES:
            return False
        kind = _magic_kind(blob)
        return kind is not None and _pixel_safe(blob, kind)
    except Exception:
        return False


def _download_image(url, cache_path, timeout=TIMEOUT):
    """Download and validate ONE image into a generated cache path.

    Order matters: SSRF-validated https (one attempt + bounded, re-validated
    redirects) → streamed body with a hard cap → content validation
    (magic + Content-Type must agree and be JPEG/PNG/WebP; dimensions,
    aspect, pixel budget, integrity) BEFORE anything touches disk → ATOMIC
    write into the dedicated cache directory under the generated digest
    filename. A remote filename is never used, a planted symlink at the
    destination is replaced (not followed), and a partial write never
    survives a failure. Returns the cache path or None (fail-soft — the
    caller falls back to the procedural visual)."""
    res = _safe_get(url, timeout=timeout, accept="image/*")
    if res is None:
        return None
    _, headers, data = res
    if len(data) < MIN_BODY_BYTES:
        return None
    kind = _magic_kind(data)
    if kind is None:
        return None  # SVG/HTML/XML/archives/GIF/BMP/TIFF/... — not allowed
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if _MIME_TO_KIND.get(ctype) != kind:
        return None  # declared type missing, foreign, or mismatched with magic
    if not _pixel_safe(data, kind):
        return None
    try:
        _atomic_write(cache_path, data)
    except OSError:
        return None
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
            if not _cached_image_ok(cache_path):
                if os.path.islink(cache_path):
                    # never trust a planted symlink in the cache directory
                    try:
                        os.unlink(cache_path)
                    except OSError:
                        continue
                if _download_image(asset_url, cache_path, timeout) is None:
                    continue
                if not _cached_image_ok(cache_path):
                    continue
            try:
                with open(cache_path, "rb") as fh:
                    blob = fh.read(MAX_BYTES + 1)
                if len(blob) > MAX_BYTES or _magic_kind(blob) is None:
                    continue
                sha = hashlib.sha256(blob).hexdigest()
            except Exception:
                continue
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
    except Exception as e:
        _note_error(e)
        return None
    return None
