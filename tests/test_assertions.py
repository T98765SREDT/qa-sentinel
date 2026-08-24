from __future__ import annotations

import json
import unittest

from qa_sentinel.assertions import JsonPathError, evaluate_assertions, resolve_json_path
from qa_sentinel.models import AssertionSpec, HttpResponse


class JsonPathTests(unittest.TestCase):
    def test_resolves_nested_objects_and_arrays(self) -> None:
        document = {"users": [{"profile": {"name": "Ada"}}]}
        self.assertEqual(resolve_json_path(document, "users[0].profile.name"), "Ada")
        self.assertEqual(resolve_json_path(document, "$.users[0].profile.name"), "Ada")

    def test_root_path_returns_document(self) -> None:
        document = {"ok": True}
        self.assertIs(resolve_json_path(document, "$"), document)

    def test_missing_path_raises_descriptive_error(self) -> None:
        with self.assertRaisesRegex(JsonPathError, "missing key 'email'"):
            resolve_json_path({"user": {}}, "user.email")

    def test_out_of_range_index_raises_descriptive_error(self) -> None:
        with self.assertRaisesRegex(JsonPathError, "missing index 3"):
            resolve_json_path({"items": [1]}, "items[3]")

    def test_malformed_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(JsonPathError, "Invalid JSON path"):
            resolve_json_path({"user": {"name": "Ada"}}, "user..name")


class AssertionEngineTests(unittest.TestCase):
    @staticmethod
    def response(
        payload: object = None,
        *,
        status: int = 200,
        elapsed_ms: float = 18.0,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        body = json.dumps(payload).encode() if payload is not None else b""
        return HttpResponse(status, headers or {}, body, elapsed_ms, 1)

    def test_status_json_header_and_latency_pass(self) -> None:
        specs = (
            AssertionSpec("status", {"equals": 200}),
            AssertionSpec("json_path", {"path": "data.id", "equals": 7}),
            AssertionSpec("header", {"name": "X-Trace", "equals": "abc"}),
            AssertionSpec("latency", {"max_ms": 100}),
        )
        results = evaluate_assertions(
            specs,
            self.response({"data": {"id": 7}}, headers={"x-trace": "abc"}),
        )
        self.assertTrue(all(result.passed for result in results))

    def test_json_absence_can_be_asserted(self) -> None:
        specs = (AssertionSpec("json_path", {"path": "data.email", "exists": False}),)
        result = evaluate_assertions(specs, self.response({"data": {}}))[0]
        self.assertTrue(result.passed)

    def test_invalid_json_fails_without_crashing(self) -> None:
        response = HttpResponse(200, {}, b"not-json", 1.0, 1)
        result = evaluate_assertions(
            (AssertionSpec("json_path", {"path": "ok", "equals": True}),), response
        )[0]
        self.assertFalse(result.passed)
        self.assertIn("not valid JSON", result.message)

    def test_network_error_becomes_request_failure(self) -> None:
        response = HttpResponse(None, {}, b"", 2.0, 3, "connection refused")
        results = evaluate_assertions((AssertionSpec("status", {"equals": 200}),), response)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "request")
        self.assertIn("3 attempt", results[0].message)

    def test_unknown_assertion_fails_clearly(self) -> None:
        result = evaluate_assertions(
            (AssertionSpec("mystery", {}),), self.response({"ok": True})
        )[0]
        self.assertFalse(result.passed)
        self.assertIn("unknown assertion", result.message)


if __name__ == "__main__":
    unittest.main()
