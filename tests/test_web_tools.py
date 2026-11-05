"""J07 M4b: the DeepSeek Search/Fetch client.

TW-01..03 hit the live API a few times on purpose: the point is that the real
response shape is what the handler must accept.  TW-04/05 stay offline and pin
the retry and validation rules.  No model is called here.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from quintessence_pcs.foundation.jsonutil import sha256_hex  # noqa: E402
from quintessence_pcs.runtime.web_tools import (  # noqa: E402
    MAX_CAPTURE_BYTES,
    WEB_BAD_REQUEST,
    WEB_SERVER_ERROR,
    WEB_TIMEOUT,
    DeepSeekWebClient,
    WebFetchPolicy,
    WebToolError,
)

def _load_key() -> str | None:
    """The key comes from the environment, never from a path in source."""

    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return value or None


class LiveWebTests(unittest.TestCase):
    """TW-01..03: real calls, a handful per run."""

    def setUp(self) -> None:
        key = _load_key()
        if not key:
            self.skipTest("no DEEPSEEK_API_KEY configured; live web cases skipped")
        self.client = DeepSeekWebClient(api_key=key)

    def test_tw_01_search_returns_the_documented_fields(self) -> None:
        payload = self.client.search(query="DeepSeek API documentation", count=2)
        results = payload.get("results")
        self.assertIsInstance(results, list)
        self.assertTrue(results)
        first = results[0]
        for field in ("title", "url", "summary"):
            self.assertIn(field, first)
        self.assertTrue(str(first["url"]).startswith("http"))

    def test_tw_02_fetch_metadata_keeps_no_body(self) -> None:
        found = self.client.search(query="DeepSeek API documentation", count=1)
        url = found["results"][0]["url"]
        summary, payload = self.client.fetch(url=url, capture="metadata")
        self.assertFalse(summary["captured"])
        self.assertNotIn("text", payload)
        self.assertEqual(64, len(summary["content_sha256"]))
        # The length is kept as metadata; the body is not.
        self.assertGreater(summary["text_len"], 0)

    def test_tw_03_fetch_evidence_payload_hashes_its_text(self) -> None:
        found = self.client.search(query="DeepSeek API documentation", count=1)
        url = found["results"][0]["url"]
        summary, payload = self.client.fetch(url=url, capture="evidence")
        self.assertTrue(summary["captured"])
        self.assertIn("text", payload)
        self.assertEqual(sha256_hex(payload["text"]), payload["content_sha256"])
        self.assertLessEqual(len(payload["text"].encode("utf-8")), MAX_CAPTURE_BYTES)


class OfflineWebTests(unittest.TestCase):
    """TW-04/05: deterministic transport, no network."""

    def test_tw_04_error_mapping_does_not_depend_on_error_code(self) -> None:
        def transport_400(request, timeout):
            return 400, json.dumps(
                {
                    "error": {
                        "message": "bad",
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                    }
                }
            ).encode()

        client = DeepSeekWebClient(api_key="synthetic-key", transport=transport_400)
        with self.assertRaises(WebToolError) as caught:
            client.search(query="x")
        self.assertEqual(WEB_BAD_REQUEST, caught.exception.code)

        attempts: list[int] = []

        def transport_500(request, timeout):
            attempts.append(1)
            return 500, b"{}"

        client = DeepSeekWebClient(
            api_key="synthetic-key", transport=transport_500, sleep=lambda _s: None
        )
        with self.assertRaises(WebToolError) as caught:
            client.search(query="x")
        self.assertEqual(WEB_SERVER_ERROR, caught.exception.code)
        self.assertEqual(3, len(attempts), "retries must stop at three attempts")

    def test_tw_05_empty_query_is_refused_before_any_request(self) -> None:
        client = DeepSeekWebClient(
            api_key="synthetic-key",
            transport=lambda request, timeout: self.fail("must not reach the network"),
        )
        with self.assertRaises(WebToolError) as caught:
            client.search(query="   ")
        self.assertEqual(WEB_BAD_REQUEST, caught.exception.code)

    def test_tw_06_evidence_capture_is_bounded(self) -> None:
        body = "x" * (MAX_CAPTURE_BYTES + 100)

        def transport_ok(request, timeout):
            return 200, json.dumps({"title": "t", "content": body}).encode()

        client = DeepSeekWebClient(api_key="synthetic-key", transport=transport_ok)
        summary, payload = client.fetch(url="https://example.com", capture="evidence")
        self.assertTrue(summary["truncated"])
        self.assertLessEqual(len(payload["text"].encode("utf-8")), MAX_CAPTURE_BYTES)
        self.assertEqual(sha256_hex(payload["text"]), payload["content_sha256"])

    # ------------------------------------------------- RG08 fetch egress policy

    def test_tw_07_fetch_policy_refuses_unsafe_targets_before_any_request(self) -> None:
        policy = WebFetchPolicy()
        refused = (
            "http://example.com/",
            "file:///etc/passwd",
            "https://127.0.0.1/",
            "https://[::1]/",
            "https://localhost/",
            "https://intranet.local/x",
            "https://metadata.google.internal/computeMetadata/v1/",
            "https://user:pw@example.com/",
            "https://example.com:8443/",
            "https://bare-host/",
            "https://example.test/",
        )
        for url in refused:
            with self.subTest(url=url):
                with self.assertRaises(PermissionError):
                    policy.check(url)
        self.assertEqual(
            "https://example.com/a?b=1",
            policy.check("https://example.com/a?b=1"),
        )
        self.assertEqual(
            "https://Example.COM/x", policy.check("https://Example.COM/x")
        )

    def test_tw_08_allowlist_narrows_fetch_to_named_hosts(self) -> None:
        policy = WebFetchPolicy(allowed_hosts=(".wikipedia.org", "example.com"))
        self.assertEqual(
            "https://wikipedia.org/x", policy.check("https://wikipedia.org/x")
        )
        self.assertEqual(
            "https://en.wikipedia.org/x",
            policy.check("https://en.wikipedia.org/x"),
        )
        with self.assertRaises(PermissionError):
            policy.check("https://example.org/")

    def test_tw_09_total_timeout_bounds_retries_and_reports_timeout(self) -> None:
        values = iter([0.0, 0.0, 0.1, 0.2, 0.3, 0.6])
        attempts: list[float] = []

        def transport_timeout(request, timeout):
            attempts.append(timeout)
            raise TimeoutError()

        client = DeepSeekWebClient(
            api_key="synthetic-key",
            timeout=10.0,
            total_timeout=0.5,
            transport=transport_timeout,
            clock=lambda: next(values, 9.9),
            sleep=lambda _seconds: None,
        )
        with self.assertRaises(WebToolError) as caught:
            client.search(query="x")
        self.assertEqual(WEB_TIMEOUT, caught.exception.code)
        self.assertEqual(2, len(attempts), "the deadline stops the third attempt")
        self.assertTrue(
            all(value <= 0.6 for value in attempts),
            "each attempt is capped by the remaining whole-call budget",
        )

    def test_tw_10_error_messages_never_echo_the_key(self) -> None:
        key = "synthetic-web-key-0123456789"

        def transport_401(request, timeout):
            return 401, b'{"error":{"code":"invalid_request_error"}}'

        client = DeepSeekWebClient(api_key=key, transport=transport_401)
        with self.assertRaises(WebToolError) as caught:
            client.search(query="x")
        self.assertEqual("web_unauthorized", caught.exception.code)
        self.assertNotIn(key, str(caught.exception))
        self.assertNotIn(key, caught.exception.message)
        self.assertNotIn("Bearer", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
