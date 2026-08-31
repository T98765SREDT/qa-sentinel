from __future__ import annotations

import io
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from unittest.mock import patch

from qa_sentinel.http_client import HttpClient, ResponseTooLargeError, SafeRedirectHandler, _read_bounded
from qa_sentinel.models import AssertionSpec, HttpResponse, TestCase


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._body = body
        self._position = 0

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._position
        start = self._position
        self._position = min(len(self._body), self._position + size)
        return self._body[start:self._position]


class FakeOpener:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.calls = 0

    def open(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        del request, timeout
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return FakeResponse(status)


def case(**changes: object) -> TestCase:
    base = TestCase(
        case_id="request",
        name="Request",
        method="GET",
        url="https://api.example.test/health",
        headers={},
        body=None,
        timeout_seconds=1.0,
        retries=1,
        retry_delay_seconds=0.0,
        retry_on_status=(503,),
        retry_non_idempotent=False,
        assertions=(AssertionSpec("status", {"equals": 200}),),
    )
    return replace(base, **changes)


class RedirectPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = SafeRedirectHandler()

    def redirect(
        self, request: urllib.request.Request, target: str
    ) -> urllib.request.Request | None:
        return self.handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_cross_origin_redirect_strips_sensitive_headers(self) -> None:
        request = urllib.request.Request(
            "https://api.example.test/start",
            headers={
                "Authorization": "Bearer secret",
                "Cookie": "session=secret",
                "X-Api-Key": "secret",
                "X-Trace": "trace-123",
            },
        )
        redirected = self.redirect(request, "https://other.example.test/next")
        self.assertIsNotNone(redirected)
        headers = {key.lower(): value for key, value in redirected.header_items()}  # type: ignore[union-attr]
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("x-api-key", headers)
        self.assertEqual(headers["x-trace"], "trace-123")

    def test_same_origin_redirect_keeps_headers(self) -> None:
        request = urllib.request.Request(
            "https://api.example.test/start",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = self.redirect(request, "/next")
        headers = {key.lower(): value for key, value in redirected.header_items()}  # type: ignore[union-attr]
        self.assertEqual(headers["authorization"], "Bearer secret")

    def test_https_downgrade_is_blocked(self) -> None:
        request = urllib.request.Request("https://api.example.test/start")
        with self.assertRaisesRegex(urllib.error.URLError, "HTTPS-to-HTTP"):
            self.redirect(request, "http://api.example.test/next")

    def test_redirect_user_information_is_blocked(self) -> None:
        request = urllib.request.Request("https://api.example.test/start")
        with self.assertRaisesRegex(urllib.error.URLError, "user information"):
            self.redirect(request, "https://user:pass@other.example.test/next")


class RetryPolicyTests(unittest.TestCase):
    def test_idempotent_method_retries_transient_status(self) -> None:
        opener = FakeOpener([503, 200])
        response = HttpClient(opener).execute(case())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.attempts, 2)
        self.assertEqual(opener.calls, 2)

    def test_non_idempotent_method_does_not_retry_by_default(self) -> None:
        opener = FakeOpener([503, 200])
        response = HttpClient(opener).execute(case(method="POST", body={"name": "Ada"}))
        self.assertEqual(response.status, 503)
        self.assertEqual(response.attempts, 1)
        self.assertEqual(opener.calls, 1)

    def test_non_idempotent_retry_requires_explicit_opt_in(self) -> None:
        opener = FakeOpener([503, 200])
        response = HttpClient(opener).execute(
            case(
                method="POST",
                body={"name": "Ada"},
                retry_non_idempotent=True,
            )
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.attempts, 2)
        self.assertEqual(opener.calls, 2)

    @patch("qa_sentinel.http_client.time.sleep")
    def test_exponential_backoff_is_capped(self, sleep: object) -> None:
        opener = FakeOpener([503])
        response = HttpClient(opener).execute(
            case(retries=5, retry_delay_seconds=30.0)
        )
        self.assertEqual(response.attempts, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30.0] * 5)  # type: ignore[attr-defined]


class ResponseDecodingTests(unittest.TestCase):
    def test_declared_charset_is_used(self) -> None:
        response = HttpResponse(
            status=200,
            headers={"Content-Type": "text/plain; charset=iso-8859-1"},
            body="café".encode("iso-8859-1"),
            elapsed_ms=1.0,
            attempts=1,
        )
        self.assertEqual(response.text, "café")

    def test_unknown_charset_falls_back_to_utf8(self) -> None:
        response = HttpResponse(
            status=200,
            headers={"content-type": "application/json; charset=made-up"},
            body='{"ok": true}'.encode("utf-8"),
            elapsed_ms=1.0,
            attempts=1,
        )
        self.assertEqual(response.text, '{"ok": true}')


class ResponseLimitTests(unittest.TestCase):
    def test_read_bounded_rejects_a_body_over_the_limit(self) -> None:
        with self.assertRaisesRegex(ResponseTooLargeError, "10 byte limit"):
            _read_bounded(io.BytesIO(b"01234567890"), 10)

    def test_oversized_http_response_is_not_retried(self) -> None:
        class OversizedOpener:
            calls = 0

            def open(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
                del request, timeout
                self.calls += 1
                return FakeResponse(503, b"x" * 11)

        opener = OversizedOpener()
        response = HttpClient(opener).execute(case(retries=3, max_response_bytes=10))
        self.assertEqual(opener.calls, 1)
        self.assertEqual(response.status, 503)
        self.assertIn("10 byte limit", response.error or "")


if __name__ == "__main__":
    unittest.main()
