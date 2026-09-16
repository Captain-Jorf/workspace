"""Security review of the $0 external image downloader (build/asset_fetch.py).

Focused LOCAL STUB tests — NO real external network request is made:
  * SSRF: https-only, no embedded credentials, port 443 only; loopback /
    private / link-local (169.254.169.254) / multicast / reserved /
    unspecified IPv4 AND IPv6 rejected at the URL-string level and at the
    RESOLVED-ADDRESS level (a public name resolving to a private IP is
    rejected); the connected peer is re-checked;
  * redirects: urllib auto-follow is disabled; a public URL redirecting to
    a local/private address is rejected; a redirect loop terminates; a
    legitimate single public hop still works;
  * body: streamed with a hard cap (an oversized body stops the read early);
  * image: magic bytes and Content-Type must agree; JPEG/PNG/WebP only
    (HTML-with-image/jpg, SVG, zip, wrong-type images rejected);
    decompression-bomb / extreme-dimension / extreme-aspect limits;
    corrupt and truncated payloads rejected; the filename is never trusted;
  * filesystem: generated digest filename inside the dedicated cache dir,
    atomic write, partial writes cleaned up, a planted symlink is replaced
    (the target file stays untouched);
  * network/logging: one attempt per fetch (no retry storm), the bounded
    timeout is propagated, nothing is logged — the only diagnostic
    (LAST_ERROR) is the scrubbed exception class name (no URL, no query
    string, no remote text, no secret).
"""
import email.message
import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import unittest
import urllib.error
import zlib
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import common  # noqa: E402
import asset_fetch as af  # noqa: E402

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


# ---------------------------------------------------------------------------
# stubs (no real network)
# ---------------------------------------------------------------------------
def dns_stub(addresses):
    """A getaddrinfo replacement resolving to the given IP strings.
    `addresses` may be a list (every address validated) or None (NXDOMAIN)."""
    def fake(host, port, *a, **kw):
        if addresses is None:
            raise socket.gaierror("stub: no such host")
        out = []
        for addr in addresses:
            fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
            out.append((fam, socket.SOCK_STREAM, 6, "", (addr, port)))
        return out
    return fake


def _jpeg(w=480, h=640):
    from PIL import Image
    import numpy as np
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.stack([((xx * 0.5) % 255).astype(np.uint8),
                    ((yy * 0.4) % 255).astype(np.uint8),
                    np.full((h, w), 120, np.uint8)], axis=-1)
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _png(w, h, color=(200, 170, 120)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _webp(w=800, h=1200):
    """A textured webp large enough to clear the 4KB compressed-body floor."""
    from PIL import Image
    import numpy as np
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.stack([((xx * 0.3) % 255).astype(np.uint8),
                    ((yy * 0.3) % 255).astype(np.uint8),
                    ((xx ^ yy) % 255).astype(np.uint8)], axis=-1)
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="WEBP", quality=80)
    return buf.getvalue()


def _png_chunk(t, data):
    c = t + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)


def _png_with_dims(w, h):
    """A structurally valid PNG whose IHDR declares (w, h) — with a dummy
    IDAT/IEND — so image headers alone exercise the dimension/bomb checks."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 10)
    return sig + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


class FakeResp:
    """A canned HTTP response: 200-with-body for the streaming tests."""

    def __init__(self, status=200, headers=None, body=b"", chunk=af._CHUNK,
                 fail_after=None):
        self.status = status
        self.headers = headers if headers is not None else {}
        self._buf = body
        self._pos = 0
        self._chunk = chunk
        self._fail_after = fail_after
        self.read_calls = 0
        self.served = 0
        self.closed = False

    def read(self, n=None):
        self.read_calls += 1
        if self._fail_after is not None and self._pos >= self._fail_after:
            raise OSError("stub: connection reset by peer")
        if n is None or n < 0:
            out = self._buf[self._pos:]
            self._pos = len(self._buf)
        else:
            out = self._buf[self._pos:self._pos + n]
            self._pos = min(len(self._buf), self._pos + n)
        self.served += len(out)
        return bytes(out)

    def close(self):
        self.closed = True


class OpenRecorder:
    """Stubs af._open: URL-substring-matched script of canned responses or
    exceptions; records every (url, timeout) request it is asked to make."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, req, timeout):
        url = req.full_url
        self.requests.append((url, timeout))
        for key, outcome in self.script:
            if key in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"stub: unexpected URL {url}")


def _redir(url, location):
    """What the hardened opener raises for a 302 with NoRedirect in effect
    (verified wiring: HTTPErrorProcessor + HTTPDefaultErrorHandler +
    _NoRedirectHandler -> HTTPError, never an automatic follow)."""
    hdrs = email.message.Message()
    hdrs["Location"] = location
    return urllib.error.HTTPError(url, 302, "Found", hdrs, None)


def _api_json(asset_url):
    """A canned Openverse API answer with ONE CC0 result (mocked _get)."""
    return json.dumps({"results": [
        {"id": 1, "url": asset_url,
         "foreign_landing_url": "https://example.org/photographer",
         "creator": "Public Domain", "license": "cc0",
         "title": "street interview", "width": 480, "height": 640},
    ]}).encode()


def _download_with(body, ctype, status=200, fail_after=None, chunk=af._CHUNK):
    """Run the real _download_image against a canned 200 (or failing)
    response on a public host — DNS stubbed public, no real network."""
    url = "https://cdn.example.org/photo/1.jpg"
    headers = {"Content-Type": ctype} if ctype is not None else {}
    rec = OpenRecorder([(url, FakeResp(status=status, headers=headers,
                                       body=body, chunk=chunk,
                                       fail_after=fail_after))])
    d = tempfile.mkdtemp(prefix="afsec_")
    cache = os.path.join(d, "ext_" + "f" * 16 + ".jpg")
    try:
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo",
                               dns_stub([PUBLIC_V4, PUBLIC_V6])):
            return af._download_image(url, cache)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. SSRF: URL-string policy
# ---------------------------------------------------------------------------
class UrlPolicy(unittest.TestCase):
    def test_https_only(self):
        for bad in ("http://cdn.example.org/x.jpg", "ftp://cdn.example.org/x.jpg",
                    "//cdn.example.org/x.jpg", "cdn.example.org/x.jpg", ""):
            self.assertFalse(af._validate_url(bad), f"{bad!r} must be rejected")
        self.assertTrue(af._validate_url("https://cdn.example.org/x.jpg"))

    def test_localhost_and_private_ipv4_literals(self):
        for host in ("127.0.0.1", "127.5.5.5", "10.0.0.5", "10.255.255.255",
                     "172.16.0.1", "172.31.255.254", "192.168.1.100",
                     "0.0.0.0", "255.255.255.255"):
            self.assertFalse(af._validate_url(f"https://{host}/x.jpg"), host)

    def test_link_local_and_cloud_metadata_host(self):
        for host in ("169.254.169.254", "169.254.1.1", "100.64.0.1",
                     "198.18.0.7"):
            self.assertFalse(af._validate_url(f"https://{host}/x.jpg"), host)
        self.assertFalse(af._validate_url(
            "https://169.254.169.254/latest/meta-data/iam/security-credentials/"),
            "the cloud-metadata host must never be downloadable")

    def test_multicast_reserved_unspecified(self):
        for host in ("224.0.0.1", "239.10.10.10", "240.0.0.1", "255.255.255.255",
                     "192.0.2.1", "198.51.100.1", "203.0.113.1"):
            self.assertFalse(af._validate_url(f"https://{host}/x.jpg"), host)

    def test_private_and_reserved_ipv6_literals(self):
        for host in ("::1", "fc00::42", "fd12:3456::1", "fe80::1",
                     "ff02::1", "::", "64:ff9b::1.2.3.4"):
            self.assertFalse(af._validate_url(f"https://[{host}]/x.jpg"), host)

    def test_public_ip_literals_pass_the_string_check(self):
        self.assertTrue(af._validate_url(f"https://{PUBLIC_V4}/x.jpg"))
        self.assertTrue(af._validate_url(f"https://[{PUBLIC_V6}]/x.jpg"))

    def test_embedded_credentials_are_rejected(self):
        for u in ("https://user:pass@cdn.example.org/x.jpg",
                  "https://user@cdn.example.org/x.jpg",
                  "https://:secret@cdn.example.org/x.jpg"):
            self.assertFalse(af._validate_url(u), u)

    def test_nonstandard_ports_are_rejected(self):
        self.assertFalse(af._validate_url("https://cdn.example.org:8443/x.jpg"))
        self.assertFalse(af._validate_url("https://cdn.example.org:80/x.jpg"))
        self.assertFalse(af._validate_url("https://cdn.example.org:99999999/x.jpg"))
        self.assertTrue(af._validate_url("https://cdn.example.org:443/x.jpg"),
                        "port 443 is the standard https port — allowed")
        self.assertTrue(af._validate_url("https://cdn.example.org/x.jpg"),
                        "the implicit default port is allowed")

    def test_localhost_and_reserved_names(self):
        for u in ("https://localhost/x.jpg", "https://sub.localhost/x.jpg",
                  "https://host.local/x.jpg", "https://svc.internal/x.jpg"):
            self.assertFalse(af._validate_url(u), u)

    def test_traversal_and_control_characters(self):
        for u in ("https://cdn.example.org/a/../b.jpg", "https://cdn.example.org/a b.jpg",
                  "https://cdn.example.org/a\nb.jpg"):
            self.assertFalse(af._validate_url(u), u)


# ---------------------------------------------------------------------------
# 2. SSRF: resolved-address validation (DNS is stubbed)
# ---------------------------------------------------------------------------
class ResolvedAddresses(unittest.TestCase):
    URL = "https://evil.example.org/x.jpg"

    def test_name_resolving_to_loopback_is_rejected(self):
        rec = OpenRecorder([])
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo", dns_stub(["127.0.0.1"])):
            self.assertIsNone(af._request(self.URL))
        self.assertEqual(rec.requests, [], "no request may leave the machine")

    def test_name_resolving_to_private_ipv4_is_rejected(self):
        rec = OpenRecorder([])
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo", dns_stub(["10.0.0.9"])):
            self.assertIsNone(af._request(self.URL))
        self.assertEqual(rec.requests, [])

    def test_mixed_public_and_private_records_are_rejected(self):
        rec = OpenRecorder([])
        # one public A record AND one private AAAA record: EVERY resolved
        # address must be public, so the name is rejected
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo",
                               dns_stub([PUBLIC_V4, "10.9.8.7"])):
            self.assertIsNone(af._request(self.URL))
        self.assertEqual(rec.requests, [])

    def test_mixed_public_and_ula_v6_records_are_rejected(self):
        rec = OpenRecorder([])
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo",
                               dns_stub([PUBLIC_V4, "fd00::1"])):
            self.assertIsNone(af._request(self.URL))
        self.assertEqual(rec.requests, [])

    def test_all_public_records_are_accepted(self):
        body = _jpeg()
        rec = OpenRecorder([(self.URL, FakeResp(200, {"Content-Type": "image/jpeg"},
                                                body))])
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo",
                               dns_stub([PUBLIC_V4, PUBLIC_V6])):
            res = af._request(self.URL)
        self.assertEqual(res[0], 200)
        self.assertEqual(res[2], body)
        self.assertEqual(len(rec.requests), 1)

    def test_unresolvable_name_is_rejected(self):
        rec = OpenRecorder([])
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo", dns_stub(None)):
            self.assertIsNone(af._request(self.URL))
        self.assertEqual(rec.requests, [])

    def test_connected_peer_is_rechecked(self):
        # the post-handshake peer guard (DNS-rebinding defense)
        self.assertFalse(af._peer_is_public(("127.0.0.1", 443)))
        self.assertFalse(af._peer_is_public(("10.0.0.5", 443)))
        self.assertFalse(af._peer_is_public(("169.254.169.254", 80)))
        self.assertFalse(af._peer_is_public(("fe80::1", 443)))
        self.assertFalse(af._peer_is_public(("fc00::7", 443)))
        self.assertFalse(af._peer_is_public(("", 443)))
        self.assertTrue(af._peer_is_public((PUBLIC_V4, 443)))
        self.assertTrue(af._peer_is_public((PUBLIC_V6, 443)))

    def test_peer_checked_connection_refuses_a_private_peer(self):
        conn = af._PeerCheckedHTTPS("cdn.example.org")

        class Sock:
            def __init__(self, peer):
                self._p = peer
                self.closed = False

            def getpeername(self):
                return self._p

            def close(self):
                self.closed = True

        with mock.patch.object(http.client.HTTPSConnection, "connect"):
            conn.sock = Sock(("10.0.0.1", 443))
            with self.assertRaises(OSError):
                af._PeerCheckedHTTPS.connect(conn)
            self.assertTrue(conn.sock.closed, "the private peer must be disconnected")
            conn.sock = Sock((PUBLIC_V4, 443))
            af._PeerCheckedHTTPS.connect(conn)  # a public peer passes

    def test_opener_wiring(self):
        od = af._get_opener()
        self.assertEqual([type(h).__name__ for h in od.handle_open.get("https", [])],
                         ["_PeerHTTPSHandler"],
                         "only the peer-checked https handler may serve https")
        self.assertIn("_NoRedirectHandler",
                      [type(h).__name__ for h in
                       od.handle_error.get("http", {}).get(302, [])],
                      "redirects must not be followed automatically")


# ---------------------------------------------------------------------------
# 3. Redirects: bounded, re-validated, loop-safe
# ---------------------------------------------------------------------------
class RedirectPolicy(unittest.TestCase):
    A = "https://a.example.org/p.jpg"
    B = "https://b.example.org/p.jpg"

    def _run(self, script):
        rec = OpenRecorder(script)
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo",
                               dns_stub([PUBLIC_V4, PUBLIC_V6])):
            res = af._safe_get(self.A)
        return res, rec

    def test_public_url_redirecting_to_localhost_is_rejected(self):
        res, rec = self._run([(self.A, _redir(self.A, "http://127.0.0.1/steal"))])
        self.assertIsNone(res, "a public URL must never reach localhost")
        self.assertEqual(len(rec.requests), 1, "the local target is never requested")

    def test_public_url_redirecting_to_private_ipv6_is_rejected(self):
        res, rec = self._run([(self.A, _redir(self.A, "https://[::1]/steal"))])
        self.assertIsNone(res)
        self.assertEqual(len(rec.requests), 1)

    def test_redirect_to_nonstandard_port_is_rejected(self):
        res, rec = self._run([(self.A, _redir(self.A, "https://a.example.org:8443/p.jpg"))])
        self.assertIsNone(res)
        self.assertEqual(len(rec.requests), 1)

    def test_redirect_to_http_scheme_is_rejected(self):
        res, rec = self._run([(self.A, _redir(self.A, "http://b.example.org/p.jpg"))])
        self.assertIsNone(res)
        self.assertEqual(len(rec.requests), 1)

    def test_redirect_loop_terminates_with_a_bounded_budget(self):
        res, rec = self._run([(self.A, _redir(self.A, self.B)),
                              (self.B, _redir(self.B, self.A))])
        self.assertIsNone(res)
        self.assertEqual(len(rec.requests), af.MAX_REDIRECTS + 1,
                         "the loop must stop after the bounded budget")

    def test_legitimate_single_public_redirect_is_followed(self):
        body = _jpeg()
        res, rec = self._run([(self.A, _redir(self.A, self.B)),
                              (self.B, FakeResp(200, {"Content-Type": "image/jpeg"},
                                               body))])
        self.assertEqual(res[0], 200)
        self.assertEqual(res[2], body)
        self.assertEqual([u for u, _ in rec.requests], [self.A, self.B])


# ---------------------------------------------------------------------------
# 4. Body: streamed with a hard cap
# ---------------------------------------------------------------------------
class BodyLimits(unittest.TestCase):
    URL = "https://cdn.example.org/big.jpg"

    def test_oversized_compressed_body_stops_early(self):
        huge = b"\x42" * (af.MAX_BYTES + 10)
        resp = FakeResp(200, {"Content-Type": "image/jpeg"}, body=huge)
        rec = OpenRecorder([(self.URL, resp)])
        d = tempfile.mkdtemp(prefix="afsec_big_")
        try:
            with mock.patch.object(af, "_open", rec), \
                 mock.patch.object(socket, "getaddrinfo",
                                   dns_stub([PUBLIC_V4])):
                self.assertIsNone(af._download_image(
                    self.URL, os.path.join(d, "ext_b.jpg")))
            self.assertLessEqual(resp.served, af.MAX_BYTES + af._CHUNK,
                                 "the read must stop early, not drain the body")
            self.assertEqual(os.listdir(d), [], "no oversized blob on disk")
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. Image payload: magic + content-type + format allowlist + dimensions
# ---------------------------------------------------------------------------
class ImagePayload(unittest.TestCase):
    def test_html_returned_with_image_jpeg_is_rejected(self):
        html = b"<!DOCTYPE html><html><body><script>steal()</script></body></html>"
        self.assertIsNone(_download_with(html, "image/jpeg"))
        self.assertIsNone(af._magic_kind(html), "no image magic at all")

    def test_valid_image_with_wrong_or_unsafe_type_is_rejected(self):
        jpeg = _jpeg()
        self.assertIsNone(_download_with(jpeg, "text/html"))
        self.assertIsNone(_download_with(jpeg, "image/svg+xml"),
                          "magic and declared type must agree")
        self.assertIsNone(_download_with(_png(480, 640), "image/jpeg"),
                          "PNG bytes declared as JPEG")
        self.assertIsNone(_download_with(jpeg, None),
                          "a missing Content-Type is fail-closed")
        self.assertIsNotNone(_download_with(jpeg, "image/jpeg; charset=binary"),
                             "a charset suffix on the correct type is legal MIME")
        self.assertIsNotNone(_download_with(jpeg, "image/jpeg"))

    def test_svg_is_rejected(self):
        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg">'
        self.assertIsNone(_download_with(svg, "image/svg+xml"))
        self.assertIsNone(af._magic_kind(svg))

    def test_archives_and_other_rasters_are_rejected(self):
        self.assertIsNone(_download_with(b"PK\x03\x04" + b"\x00" * 5000, "application/zip"))
        self.assertIsNone(_download_with(b"GIF89a" + b"\x00" * 5000, "image/gif"),
                          "GIF is not an allowed scene raster")
        self.assertIsNone(_download_with(b"II*\x00" + b"\x00" * 5000, "image/tiff"))

    def test_webp_is_accepted(self):
        self.assertIsNotNone(_download_with(_webp(), "image/webp"))

    def test_decompression_bomb_dimensions_are_rejected(self):
        bomb = _png_with_dims(50000, 50000)  # 2.5e9 decoded pixels
        self.assertIsNone(_download_with(bomb, "image/png"))
        self.assertFalse(af._pixel_safe(bomb, "png"))

    def test_extreme_aspect_ratio_is_rejected(self):
        self.assertIsNone(_download_with(_png(4096, 400), "image/png"),
                          "10:1 strip is not a scene image")

    def test_over_max_dimension_is_rejected(self):
        self.assertIsNone(_download_with(_png(9000, 500), "image/png"))

    def test_under_min_dimension_is_rejected(self):
        self.assertIsNone(_download_with(_png(300, 450), "image/png"),
                          "a tiny thumbnail is not a scene image")

    def test_corrupt_and_truncated_payloads_are_rejected(self):
        jpeg = _jpeg()
        self.assertIsNone(_download_with(jpeg[: int(len(jpeg) * 0.4)], "image/jpeg"))
        png = _png(480, 640)
        self.assertIsNone(_download_with(png[: int(len(png) * 0.5)], "image/png"))
        self.assertIsNone(_download_with(b"\xff\xd8\xff" + b"\x00" * 6000, "image/jpeg"))


# ---------------------------------------------------------------------------
# 6. Filesystem: generated name, atomic write, symlink safety, cleanup
# ---------------------------------------------------------------------------
class FilesystemSafety(unittest.TestCase):
    URL = "https://cdn.example.org/photo/1.jpg"

    def _download(self, d, name="ext_" + "e" * 16 + ".jpg", body=None, ctype="image/jpeg"):
        body = body if body is not None else _jpeg()
        headers = {"Content-Type": ctype} if ctype is not None else {}
        rec = OpenRecorder([(self.URL, FakeResp(200, headers, body))])
        cache = os.path.join(d, name)
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo", dns_stub([PUBLIC_V4])):
            return af._download_image(self.URL, cache), cache, rec

    def test_valid_public_image_fixture(self):
        d = tempfile.mkdtemp(prefix="afsec_ok_")
        try:
            body = _jpeg()
            result, cache, rec = self._download(d)
            self.assertEqual(result, cache)
            self.assertEqual(rec.requests[0][1], af.TIMEOUT, "bounded timeout propagated")
            self.assertTrue(os.path.isfile(cache))
            self.assertRegex(os.path.basename(cache), r"^ext_[0-9a-f]{16}\.jpg$",
                             "generated digest filename only")
            self.assertTrue(os.path.realpath(cache).startswith(os.path.realpath(d)),
                            "the file must stay inside the cache directory")
            with open(cache, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                                 hashlib.sha256(body).hexdigest())
            from PIL import Image
            im = Image.open(cache)
            try:
                self.assertEqual(im.format, "JPEG")
                self.assertEqual(im.size, (480, 640))
            finally:
                im.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_path_traversal_url_is_rejected(self):
        for u in ("https://cdn.example.org/../../etc/passwd",
                  "https://cdn.example.org/..%2f..%2fx.jpg"):
            if ".." in u:
                self.assertFalse(af._validate_url(u))
        d = tempfile.mkdtemp(prefix="afsec_trav_")
        try:
            self.assertFalse(af._validate_url("https://cdn.example.org/a/../b.jpg"))
            # and a traversal-style cache path cannot escape its directory
            import os.path as op
            evil = os.path.join(d, "sub", "..", "..", "escape.jpg")
            try:
                af._atomic_write(evil, b"x" * 100)
            except OSError:
                pass
            self.assertFalse(os.path.exists(op.normpath(op.join(d, "..", "escape.jpg"))),
                             "nothing may be written outside the cache directory")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_planted_symlink_is_replaced_not_followed(self):
        d = tempfile.mkdtemp(prefix="afsec_sym_")
        try:
            target = os.path.join(d, "target.bin")
            with open(target, "wb") as f:
                f.write(b"SENTINEL-DO-NOT-TOUCH")
            cache = os.path.join(d, "ext_" + "a" * 16 + ".jpg")
            os.symlink(target, cache)
            body = _jpeg()
            result, _, _ = self._download(d, os.path.basename(cache), body=body)
            self.assertEqual(result, cache)
            self.assertFalse(os.path.islink(cache),
                             "the destination must be a regular file after the write")
            with open(target, "rb") as f:
                self.assertEqual(f.read(), b"SENTINEL-DO-NOT-TOUCH",
                                 "the symlink target must be untouched")
            with open(cache, "rb") as f:
                self.assertEqual(f.read(), body)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_partial_write_is_cleaned_up_on_failure(self):
        d = tempfile.mkdtemp(prefix="afsec_part_")
        try:
            name = "ext_" + "c" * 16 + ".jpg"
            os.makedirs(os.path.join(d, name))  # destination is a DIRECTORY
            result, _, _ = self._download(d, name=name)
            self.assertIsNone(result)
            self.assertEqual([n for n in os.listdir(d) if n.startswith(".part_")], [],
                             "no partial temp file may survive a failed write")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_midstream_reset_leaves_no_file(self):
        d = tempfile.mkdtemp(prefix="afsec_mid_")
        try:
            body = _jpeg()
            headers = {"Content-Type": "image/jpeg"}
            resp = FakeResp(200, headers, body, fail_after=1024)
            rec = OpenRecorder([(self.URL, resp)])
            cache = os.path.join(d, "ext_" + "d" * 16 + ".jpg")
            with mock.patch.object(af, "_open", rec), \
                 mock.patch.object(socket, "getaddrinfo", dns_stub([PUBLIC_V4])):
                self.assertIsNone(af._download_image(self.URL, cache))
            self.assertEqual(os.listdir(d), [], "a reset download leaves nothing behind")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_cache_name_never_uses_the_remote_filename(self):
        """fetch_image end-to-end (API + image stubbed): the cache file is
        named ONLY from sha256(query|asset_url) — a remote filename with
        traversal or an exotic extension cannot influence it."""
        asset_url = "https://cdn.example.org/photos/report%3bexec.php.jpg"
        d = tempfile.mkdtemp(prefix="afsec_name_")
        try:
            with mock.patch.object(af, "_cache_dir", lambda: d):
                with mock.patch.object(af, "enabled", lambda: True), \
                     mock.patch.object(af, "_get",
                                       lambda url, timeout=af.TIMEOUT: _api_json(asset_url)), \
                     mock.patch.object(af, "_open",
                                       OpenRecorder([(
                                           asset_url,
                                           FakeResp(200, {"Content-Type": "image/jpeg"},
                                                     _jpeg()))])), \
                     mock.patch.object(socket, "getaddrinfo",
                                       dns_stub([PUBLIC_V4])):
                    manifest = af.fetch_image("user interviews")
            self.assertIsNotNone(manifest)
            self.assertEqual(os.path.dirname(manifest["path"]), d)
            expected = hashlib.sha256(
                f"{'user interviews'}|{asset_url}".encode()).hexdigest()[:16]
            self.assertTrue(manifest["path"].endswith(f"ext_{expected}.jpg"),
                            f"{manifest['path']}")
            self.assertNotIn(".php", os.path.basename(manifest["path"]))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_valid_cache_hit_is_reused_without_network(self):
        asset_url = "https://cdn.example.org/cache-hit.jpg"
        d = tempfile.mkdtemp(prefix="afsec_hit_")
        try:
            body = _jpeg()
            expected = hashlib.sha256(
                f"{'user interviews'}|{asset_url}".encode()).hexdigest()[:16]
            cache = os.path.join(d, f"ext_{expected}.jpg")
            with open(cache, "wb") as f:
                f.write(body)
            boom = OpenRecorder([])  # any network attempt fails the test
            with mock.patch.object(af, "_cache_dir", lambda: d):
                with mock.patch.object(af, "enabled", lambda: True), \
                     mock.patch.object(af, "_get",
                                       lambda url, timeout=af.TIMEOUT: _api_json(asset_url)), \
                     mock.patch.object(af, "_open", boom):
                    manifest = af.fetch_image("user interviews")
            self.assertIsNotNone(manifest, "a valid cache entry must be reused")
            self.assertEqual(manifest["path"], cache)
            self.assertEqual(boom.requests, [], "no network on a valid cache hit")
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. Network & logging: bounded, silent, scrubbed
# ---------------------------------------------------------------------------
class NetworkAndLogging(unittest.TestCase):
    URL = "https://cdn.example.org/p.jpg?q=user+interviews&license_type=cc0,pdm"

    def test_single_attempt_no_retry_storm(self):
        rec = OpenRecorder([(self.URL, urllib.error.URLError("stub: down"))])
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo", dns_stub([PUBLIC_V4])):
            self.assertIsNone(af._safe_get(self.URL))
        self.assertEqual(len(rec.requests), 1, "exactly ONE attempt — no retries")

    def test_timeout_is_bounded_and_propagated(self):
        body = _jpeg()
        rec = OpenRecorder([(self.URL, FakeResp(200, {"Content-Type": "image/jpeg"},
                                                body))])
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo", dns_stub([PUBLIC_V4])):
            self.assertIsNotNone(af._safe_get(self.URL, timeout=af.TIMEOUT))
        self.assertEqual([t for _, t in rec.requests], [af.TIMEOUT])

    def test_no_query_string_or_remote_text_reaches_the_diagnostic(self):
        rec = OpenRecorder([(self.URL, urllib.error.URLError("boom " + self.URL))])
        with mock.patch.object(af, "_open", rec), \
             mock.patch.object(socket, "getaddrinfo", dns_stub([PUBLIC_V4])):
            self.assertIsNone(af._safe_get(self.URL))
        self.assertTrue(af.LAST_ERROR, "a diagnostic is recorded for debugging")
        self.assertIn("URLError", af.LAST_ERROR, "the exception CLASS name is kept")
        self.assertNotIn("boom", af.LAST_ERROR, "the message is never stored")
        self.assertNotIn("q=user", af.LAST_ERROR, "no query string survives")
        self.assertNotIn("cdn.example.org", af.LAST_ERROR, "no URL survives")

    def test_exception_text_is_sane_to_the_secret_scrubber(self):
        secret = "sk-test-1234567890abcdef1234567890abcdef"
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": secret}):
            scrubbed = common.scrub_secrets(f"asset-fetch: URLError {secret}")
            self.assertNotIn(secret, scrubbed)
            self.assertIn("[redacted:GROQ_API_KEY]", scrubbed)
            # the fetcher's own path: even a message that carries the secret
            # cannot survive into LAST_ERROR
            af._note_error(RuntimeError(secret))
            self.assertNotIn(secret, af.LAST_ERROR)

    def test_module_logs_nothing(self):
        with open(os.path.join(ROOT, "build", "asset_fetch.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("import logging", src)
        self.assertNotIn("logging.", src)
        self.assertNotIn("print(", src)


if __name__ == "__main__":
    unittest.main()
