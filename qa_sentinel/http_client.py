"""Small, retry-aware HTTP transport built on urllib."""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from .capture import CaptureStore, resolve_case_references
from .models import (
    IDEMPOTENT_METHODS,
    MAX_RETRY_DELAY_SECONDS,
    HttpResponse,
    TestCase,
)


_SENSITIVE_HEADER = re.compile(
    r"authorization|proxy-authorization|cookie|token|api[-_]?key|secret|password|credential",
    re.IGNORECASE,
)
_RESPONSE_READ_CHUNK_BYTES = 64 * 1024


class ResponseTooLargeError(ValueError):
    """Raised when a response exceeds the configured memory safety limit."""


def _read_bounded(stream: Any, limit: int) -> bytes:
    """Read at most *limit* bytes and fail before buffering a larger body."""

    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = stream.read(min(_RESPONSE_READ_CHUNK_BYTES, limit - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ResponseTooLargeError(
                f"response exceeded the {limit} byte limit"
            )
    return b"".join(chunks)


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise urllib.error.URLError("redirect target has an invalid port") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise urllib.error.URLError("redirect target must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise urllib.error.URLError("redirect target must not contain user information")
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname.lower(), port or default_port


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep redirects inside explicit transport security boundaries."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urljoin(req.full_url, newurl)
        source_origin = _origin(req.full_url)
        target_origin = _origin(target)
        if source_origin[0] == "https" and target_origin[0] != "https":
            raise urllib.error.URLError("blocked HTTPS-to-HTTP redirect downgrade")

        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected is not None and source_origin != target_origin:
            for name, _ in tuple(redirected.header_items()):
                if _SENSITIVE_HEADER.search(name):
                    redirected.remove_header(name)
        return redirected


class HttpClient:
    """Execute HTTP test cases with deterministic retry behavior."""

    user_agent = "QA-Sentinel/1.0"

    def __init__(self, opener: Any | None = None) -> None:
        self._opener = opener or urllib.request.build_opener(SafeRedirectHandler())

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

    def execute(
        self, case: TestCase, captures: CaptureStore | None = None
    ) -> HttpResponse:
        """Execute *case*, retrying transient statuses and transport errors."""

        if captures is not None:
            case = resolve_case_references(case, captures)
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
                with self._opener.open(request, timeout=case.timeout_seconds) as response:
                    body = _read_bounded(response, case.max_response_bytes)
                    elapsed = (time.perf_counter() - start) * 1000
                    last_response = HttpResponse(
                        status=response.status,
                        headers=dict(response.headers.items()),
                        body=body,
                        elapsed_ms=elapsed,
                        attempts=attempt,
                    )
            except ResponseTooLargeError as exc:
                elapsed = (time.perf_counter() - start) * 1000
                last_response = HttpResponse(
                    status=getattr(response, "status", None),
                    headers=dict(getattr(response, "headers", {}) or {}),
                    body=b"",
                    elapsed_ms=elapsed,
                    attempts=attempt,
                    error=str(exc),
                )
            except urllib.error.HTTPError as exc:
                elapsed = (time.perf_counter() - start) * 1000
                try:
                    body = _read_bounded(exc, case.max_response_bytes)
                    error = None
                except ResponseTooLargeError as read_error:
                    body = b""
                    error = str(read_error)
                last_response = HttpResponse(
                    status=exc.code,
                    headers=dict(exc.headers.items()) if exc.headers else {},
                    body=body,
                    elapsed_ms=elapsed,
                    attempts=attempt,
                    error=error,
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
                    retryable_error=True,
                )

            method_allows_retry = (
                case.method in IDEMPOTENT_METHODS or case.retry_non_idempotent
            )
            retryable_response = last_response.retryable_error or (
                last_response.error is None
                and last_response.status in case.retry_on_status
            )
            should_retry = (
                attempt <= case.retries
                and method_allows_retry
                and retryable_response
            )
            if not should_retry:
                return last_response
            if case.retry_delay_seconds:
                delay = min(
                    case.retry_delay_seconds * (2 ** (attempt - 1)),
                    MAX_RETRY_DELAY_SECONDS,
                )
                time.sleep(delay)

        assert last_response is not None  # The loop always executes at least once.
        return last_response
