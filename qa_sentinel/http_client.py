"""Small, retry-aware HTTP transport built on urllib."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from .models import HttpResponse, TestCase


class HttpClient:
    """Execute HTTP test cases with deterministic retry behavior."""

    user_agent = "QA-Sentinel/1.0"

    @staticmethod
    def _encode_body(body: Any, headers: dict[str, str]) -> bytes | None:
        if body is None:
            return None
        if isinstance(body, (dict, list, int, float, bool)):
            headers.setdefault("Content-Type", "application/json")
            return json.dumps(body, ensure_ascii=False).encode("utf-8")
        if isinstance(body, str):
            return body.encode("utf-8")
        raise TypeError(f"Unsupported request body type: {type(body).__name__}")

    def execute(self, case: TestCase) -> HttpResponse:
        """Execute *case*, retrying transient statuses and transport errors."""

        last_response: HttpResponse | None = None
        for attempt in range(1, case.retries + 2):
            headers = dict(case.headers)
            headers.setdefault("User-Agent", self.user_agent)
            try:
                data = self._encode_body(case.body, headers)
            except (TypeError, ValueError) as exc:
                return HttpResponse(None, {}, b"", 0.0, attempt, str(exc))

            request = urllib.request.Request(
                url=case.url,
                data=data,
                headers=headers,
                method=case.method,
            )
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=case.timeout_seconds) as response:
                    body = response.read()
                    elapsed = (time.perf_counter() - start) * 1000
                    last_response = HttpResponse(
                        status=response.status,
                        headers=dict(response.headers.items()),
                        body=body,
                        elapsed_ms=elapsed,
                        attempts=attempt,
                    )
            except urllib.error.HTTPError as exc:
                elapsed = (time.perf_counter() - start) * 1000
                last_response = HttpResponse(
                    status=exc.code,
                    headers=dict(exc.headers.items()) if exc.headers else {},
                    body=exc.read(),
                    elapsed_ms=elapsed,
                    attempts=attempt,
                )
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                elapsed = (time.perf_counter() - start) * 1000
                reason = getattr(exc, "reason", exc)
                last_response = HttpResponse(
                    status=None,
                    headers={},
                    body=b"",
                    elapsed_ms=elapsed,
                    attempts=attempt,
                    error=f"{type(reason).__name__}: {reason}",
                )

            should_retry = (
                attempt <= case.retries
                and (last_response.error is not None or last_response.status in case.retry_on_status)
            )
            if not should_retry:
                return last_response
            if case.retry_delay_seconds:
                time.sleep(case.retry_delay_seconds * (2 ** (attempt - 1)))

        assert last_response is not None  # The loop always executes at least once.
        return last_response

