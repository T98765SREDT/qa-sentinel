"""Deterministic loopback API for the order-lifecycle workflow example."""

from __future__ import annotations

import argparse
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


ACCESS_TOKEN = "order-demo-access-token"


class OrderRequestHandler(BaseHTTPRequestHandler):
    """Small stateful API with explicit auth and deterministic failure switches."""

    server_version = "QA-Sentinel-Order-Demo/1.0"

    def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {ACCESS_TOKEN}"

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/login":
            payload = self._read_json()
            if payload != {"username": "demo", "password": "demo"}:
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid credentials"})
                return
            self._json(
                HTTPStatus.OK,
                {"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
            return
        if parsed.path != "/orders":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "authorization required"})
            return
        if query.get("fail", [""])[0] == "create":
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "synthetic create failure"})
            return
        payload = self._read_json()
        if not payload or not isinstance(payload.get("item"), str):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "item is required"})
            return
        with self.server.state_lock:  # type: ignore[attr-defined]
            order_id = f"ord-{self.server.next_order_id}"  # type: ignore[attr-defined]
            self.server.next_order_id += 1  # type: ignore[attr-defined]
            order = {
                "id": order_id,
                "item": payload["item"],
                "quantity": payload.get("quantity", 1),
                "status": "pending",
            }
            self.server.orders[order_id] = order  # type: ignore[attr-defined]
        self._json(HTTPStatus.CREATED, {"data": order}, {"Location": f"/orders/{order_id}"})

    def _order(self, path: str) -> dict[str, Any] | None:
        order_id = path.rsplit("/", 1)[-1]
        with self.server.state_lock:  # type: ignore[attr-defined]
            order = self.server.orders.get(order_id)  # type: ignore[attr-defined]
            return dict(order) if order is not None else None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "authorization required"})
            return
        parsed = urlparse(self.path)
        order = self._order(parsed.path)
        if order is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "order not found"})
            return
        self._json(HTTPStatus.OK, {"data": order})

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "authorization required"})
            return
        parsed = urlparse(self.path)
        payload = self._read_json()
        status = payload.get("status") if payload else None
        if status not in {"processing", "shipped", "cancelled"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "unsupported status"})
            return
        order_id = parsed.path.rsplit("/", 1)[-1]
        with self.server.state_lock:  # type: ignore[attr-defined]
            order = self.server.orders.get(order_id)  # type: ignore[attr-defined]
            if order is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "order not found"})
                return
            order["status"] = status
            response = dict(order)
        self._json(HTTPStatus.OK, {"data": response})

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "authorization required"})
            return
        order_id = urlparse(self.path).path.rsplit("/", 1)[-1]
        with self.server.state_lock:  # type: ignore[attr-defined]
            existed = self.server.orders.pop(order_id, None) is not None  # type: ignore[attr-defined]
        self._empty(HTTPStatus.NO_CONTENT if existed else HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)


def create_order_server(
    host: str = "127.0.0.1", port: int = 8766, *, verbose: bool = False
) -> ThreadingHTTPServer:
    """Build the order demo server; port 0 asks the OS for an ephemeral port."""

    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("the order demo server only binds to loopback")
    server = ThreadingHTTPServer((host, port), OrderRequestHandler)
    server.orders = {}  # type: ignore[attr-defined]
    server.next_order_id = 1001  # type: ignore[attr-defined]
    server.state_lock = threading.Lock()  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def serve(host: str = "127.0.0.1", port: int = 8766, *, verbose: bool = False) -> None:
    server = create_order_server(host, port, verbose=verbose)
    print(f"QA Sentinel order demo listening on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping order demo API")
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run QA Sentinel's order lifecycle demo API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--verbose", action="store_true")
    options = parser.parse_args()
    serve(options.host, options.port, verbose=options.verbose)
