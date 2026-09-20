"""Openverse request-contract regression + safe failure categories.

Issue #32 root cause (verified against the official endpoint documentation at
https://api.openverse.org/v1/ — getimages_search): license CODES belong in the
`license` parameter ("comma separated list of licenses; available licenses
include: by, by-nc, ..., cc0, ..., pdm, ..."), while `license_type` is the
USAGE filter ("all, all-cc, commercial, modification"). The production code
sent `license_type=cc0,pdm`, so the API was asked a usage question with
license codes — and reel-2026-09-24 retrieved 0 of 2 designated photos with
NO reported reason.

The corrected request keeps EVERY post-response enforcement layer (cc0/pdm
only, HTTPS only, SSRF-guarded URL validation, redirect validation, image
format validation, size limits, one attempt, bounded timeout, no paid API)
and records one of the safe failure categories so the daily Issue can say
WHY retrieval failed — counts only, never URLs or remote bodies.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import asset_fetch as af  # noqa: E402
from PIL import Image  # noqa: E402


def _jpg(path, size=(800, 600), color=(120, 90, 60)):
    Image.new("RGB", size, color).save(path, quality=80)


class RequestContract(unittest.TestCase):
    def setUp(self):
        af.outcome_counts(reset=True)

    def test_outgoing_request_uses_license_parameter_not_license_type(self):
        seen = []

        def fake_get(url, timeout=25):
            seen.append(url)
            return json.dumps({"results": []}).encode()

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get):
            self.assertIsNone(af.fetch_image("stand up meetings"))
        self.assertEqual(len(seen), 1, "exactly ONE API attempt — no retries")
        url = seen[0]
        self.assertTrue(url.startswith(af.API_URL + "?"))
        # the fix: license codes in `license`
        self.assertIn("license=cc0%2Cpdm", url)
        # the bug must never come back: codes in the usage filter
        self.assertNotIn("license_type=cc0", url)
        self.assertNotIn("license_type=pdm", url)
        self.assertEqual(af.last_outcome(), af.OUTCOME_NO_RESULTS)

    def test_query_is_sanitized_and_urlencoded(self):
        seen = []

        def fake_get(url, timeout=25):
            seen.append(url)
            return json.dumps({"results": []}).encode()

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get):
            af.fetch_image("  AI-generated   agenda! <script> ")
        self.assertEqual(len(seen), 1)
        # sanitization keeps lowercase alphanumerics + spaces only: no raw
        # markup characters and no double spaces can reach the query string
        self.assertNotIn("<", seen[0])
        self.assertNotIn(">", seen[0])
        self.assertNotIn("!", seen[0])
        self.assertIn("q=", seen[0])

    def test_failure_categories_are_safe_counts_only(self):
        self.assertEqual(set(af.OUTCOME_CATEGORIES), {
            "retrieved", "cache_hit", "search_http_error", "search_timeout",
            "no_results", "no_allowed_license", "unsafe_asset_url",
            "image_http_error", "invalid_content_type", "invalid_image",
            "dimension_rejected", "security_rejected", "duplicate_asset"})

        def fake_get(url, timeout=25):
            return json.dumps({"results": [
                {"id": 1, "url": "http://insecure.example.org/x.jpg",
                 "license": "cc0", "title": "t"},
                {"id": 2, "url": "https://cdn.example.org/y.jpg",
                 "license": "by", "title": "ccby"},
            ]}).encode()

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get):
            self.assertIsNone(af.fetch_image("x"))
        # the report surface is aggregate categories only:
        counts = af.outcome_counts(reset=True)
        for k in counts:
            self.assertIn(k, af.OUTCOME_CATEGORIES)
            self.assertIsInstance(counts[k], int)
        for cat, n in counts.items():
            self.assertNotIn("http", cat)


class EnforcementPreserved(unittest.TestCase):
    """Every post-response hardening layer from issue #26 stays intact."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ovc_")
        self.cache = os.path.join(self.d, "cache")
        af.outcome_counts(reset=True)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _result(self, **over):
        base = {"id": 1, "url": "https://cdn.example.org/img/1.jpg",
                "foreign_landing_url": "https://example.org/p",
                "creator": "PD", "license": "cc0", "title": "t",
                "width": 800, "height": 600}
        base.update(over)
        return base

    def test_http_asset_url_is_rejected_as_unsafe(self):
        def fake_get(url, timeout=25):
            return json.dumps({"results": [self._result(url="http://cdn.example.org/x.jpg")]}).encode()

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get):
            self.assertIsNone(af.fetch_image("x"))
        self.assertEqual(af.last_outcome(), af.OUTCOME_UNSAFE_ASSET_URL)

    def test_non_image_download_is_rejected(self):
        def fake_get(url, timeout=25):
            return json.dumps({"results": [self._result()]}).encode()

        def fake_download(url, cache_path, timeout=25):
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as fh:
                fh.write(b"<!doctype html><html>not an image</html>")
            return cache_path, af.OUTCOME_INVALID_CONTENT_TYPE

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get), \
             mock.patch.object(af, "_download_image_with_reason", fake_download), \
             mock.patch.object(af, "_cache_dir", lambda: self.cache):
            self.assertIsNone(af.fetch_image("x"))
        self.assertIn(af.last_outcome(), (af.OUTCOME_INVALID_CONTENT_TYPE,
                                          af.OUTCOME_INVALID_IMAGE))

    def test_corrupt_image_bytes_are_rejected(self):
        def fake_get(url, timeout=25):
            return json.dumps({"results": [self._result()]}).encode()

        def fake_download(url, cache_path, timeout=25):
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as fh:
                fh.write(b"\x00\x01\x02not really jpeg bytes")
            return cache_path, None

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get), \
             mock.patch.object(af, "_download_image_with_reason", fake_download), \
             mock.patch.object(af, "_cache_dir", lambda: self.cache):
            self.assertIsNone(af.fetch_image("x"))
        self.assertEqual(af.last_outcome(), af.OUTCOME_INVALID_IMAGE)

    def test_clean_cc0_retrieval_succeeds_with_full_manifest(self):
        img = os.path.join(self.d, "img.jpg")
        _jpg(img)

        def fake_get(url, timeout=25):
            return json.dumps({"results": [self._result()]}).encode()

        def fake_download(url, cache_path, timeout=25):
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            shutil.copy(img, cache_path)
            return cache_path, None

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get), \
             mock.patch.object(af, "_download_image_with_reason", fake_download), \
             mock.patch.object(af, "_cache_dir", lambda: self.cache):
            r = af.fetch_image("stand up meetings")
        self.assertIsNotNone(r)
        self.assertEqual(r["license"], "cc0")
        self.assertEqual(r["origin"], "openverse-api")
        self.assertEqual(len(r["sha256"]), 64)
        self.assertEqual(af.last_outcome(), af.OUTCOME_RETRIEVED)
        self.assertTrue(os.path.exists(r["path"]))

    def test_search_error_is_a_safe_category_not_an_exception(self):
        def fake_get(url, timeout=25):
            af._outcome(af.OUTCOME_SEARCH_HTTP_ERROR)  # as the real _get does
            return None

        with mock.patch.object(af, "enabled", lambda: True), \
             mock.patch.object(af, "_get", fake_get):
            self.assertIsNone(af.fetch_image("x"))
        self.assertEqual(af.last_outcome(), af.OUTCOME_SEARCH_HTTP_ERROR)


if __name__ == "__main__":
    unittest.main()
