"""Local deterministic API used by the quickstart and integration tests."""

from __future__ import annotations

import argparse
import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class DemoRequestHandler(BaseHTTPRequestHandler):
    """Small API with healthy, slow, echo, and intentionally unstable routes."""

    server_version = "QA-Sentinel-Demo/1.0"

    def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Demo-API", "qa-sentinel")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok", "service": "demo-api", "version": "1.0.0"})
            return
        if parsed.path == "/users/1":
            self._json(
                HTTPStatus.OK,
                {"data": {"id": 1, "name": "Ada Lovelace", "roles": ["admin", "engineer"]}},
            )
            return
        if parsed.path == "/slow":
            milliseconds = min(max(int(query.get("ms", ["25"])[0]), 0), 1000)
            time.sleep(milliseconds / 1000)
            self._json(HTTPStatus.OK, {"waited_ms": milliseconds})
            return
        if parsed.path == "/unstable":
            key = query.get("key", ["default"])[0]
            failures = max(int(query.get("failures", ["1"])[0]), 0)
            with self.server.state_lock:  # type: ignore[attr-defined]
                count = self.server.attempts.get(key, 0) + 1  # type: ignore[attr-defined]
                self.server.attempts[key] = count  # type: ignore[attr-defined]
            if count <= failures:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "retry", "attempt": count})
            else:
                self._json(HTTPStatus.OK, {"status": "recovered", "attempt": count})
            return
        if parsed.path.startswith("/status/"):
            try:
                status = int(parsed.path.rsplit("/", 1)[1])
                if not 100 <= status <= 599:
                    raise ValueError
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid status"})
                return
            self._json(status, {"status": status})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlparse(self.path).path != "/echo":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        self._json(HTTPStatus.CREATED, {"received": payload})

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)


def create_demo_server(
    host: str = "127.0.0.1", port: int = 8765, *, verbose: bool = False
) -> ThreadingHTTPServer:
    """Build a demo server. Port 0 requests an available ephemeral port."""

    server = ThreadingHTTPServer((host, port), DemoRequestHandler)
    server.attempts = {}  # type: ignore[attr-defined]
    server.state_lock = threading.Lock()  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def serve(host: str = "127.0.0.1", port: int = 8765, *, verbose: bool = False) -> None:
    server = create_demo_server(host, port, verbose=verbose)
    print(f"QA Sentinel demo API listening on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping demo API")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run QA Sentinel's local demo API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--verbose", action="store_true", help="log incoming demo requests")
    args = parser.parse_args()
    serve(args.host, args.port, verbose=args.verbose)


if __name__ == "__main__":
    main()
