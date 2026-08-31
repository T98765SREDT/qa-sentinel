from __future__ import annotations

import unittest

from qa_sentinel.capture import (
    CaptureError,
    CaptureStore,
    MissingCaptureError,
    capture_response,
    resolve_case_references,
)
from qa_sentinel.models import AssertionSpec, HttpResponse, TestCase


def make_case(
    extract: dict[str, object],
    *,
    url: str = "https://example.test/items",
    body: object = None,
) -> TestCase:
    return TestCase(
        case_id="create",
        name="Create",
        method="POST",
        url=url,
        headers={"X-Request": "1"},
        body=body,
        timeout_seconds=1,
        retries=0,
        retry_delay_seconds=0,
        retry_on_status=(),
        retry_non_idempotent=False,
        assertions=(AssertionSpec("status", {"equals": 200}),),
        extract=extract,
    )


class CaptureTests(unittest.TestCase):
    def test_extracts_json_header_cookie_and_status_values(self) -> None:
        case = make_case(
            {
                "access_token": {"from": "json", "path": "data.token", "secret": True},
                "item_id": {"from": "header", "name": "Location"},
                "session": {"from": "cookie", "name": "sid"},
                "code": {"from": "status"},
            }
        )
        response = HttpResponse(
            201,
            {"Content-Type": "application/json", "Location": "/items/42", "Set-Cookie": "sid=abc123; Path=/"},
            b'{"data":{"token":"secret-value"}}',
            4,
            1,
        )
        store = capture_response(case, response)
        self.assertEqual(store.get("create", "access_token").value, "secret-value")
        self.assertTrue(store.get("create", "access_token").secret)
        self.assertEqual(store.get("create", "item_id").value, "/items/42")
        self.assertEqual(store.get("create", "session").value, "abc123")
        self.assertEqual(store.get("create", "code").value, 201)

    def test_store_is_immutable_and_duplicate_step_capture_is_rejected(self) -> None:
        empty = CaptureStore()
        first = empty.add("login", ())
        second = first.add("read", ())
        self.assertEqual(empty.entries, ())
        self.assertEqual(first.entries, (("login", ()),))
        self.assertEqual(len(second.entries), 2)
        with self.assertRaisesRegex(CaptureError, "already exist"):
            first.add("login", ())

    def test_missing_capture_is_typed_and_never_becomes_empty(self) -> None:
        with self.assertRaisesRegex(MissingCaptureError, "not available"):
            CaptureStore().get("login", "token")
        case = make_case({}, url="https://example.test/{{steps.login.token}}")
        with self.assertRaisesRegex(MissingCaptureError, "not available"):
            resolve_case_references(case, CaptureStore())

    def test_substitution_preserves_full_json_value_and_embeds_scalars(self) -> None:
        source = make_case(
            {},
            url="https://example.test/items/{{steps.create.item_id}}",
            body={"id": "{{steps.create.item_id}}", "label": "item-{{steps.create.item_id}}"},
        )
        response = HttpResponse(200, {}, b"", 1, 1)
        store = capture_response(
            make_case({"item_id": {"from": "status"}}), response
        )
        resolved = resolve_case_references(source, store)
        self.assertEqual(resolved.url, "https://example.test/items/200")
        self.assertEqual(resolved.body, {"id": 200, "label": "item-200"})

    def test_structured_capture_must_occupy_an_entire_json_value(self) -> None:
        source = make_case({}, body={"payload": "prefix-{{steps.create.payload}}"})
        response = HttpResponse(200, {"Content-Type": "application/json"}, b'{"payload":{"id":1}}', 1, 1)
        store = capture_response(
            make_case({"payload": {"from": "json", "path": "payload"}}), response
        )
        with self.assertRaisesRegex(CaptureError, "structured capture"):
            resolve_case_references(source, store)

    def test_public_metadata_never_contains_secret_value(self) -> None:
        case = make_case({"token": {"from": "json", "path": "token", "secret": True}})
        store = capture_response(
            case,
            HttpResponse(200, {"Content-Type": "application/json"}, b'{"token":"very-secret"}', 1, 1),
        )
        metadata = store.public_metadata()
        self.assertEqual(metadata[0], {"step": "create", "name": "token", "source": "json", "present": True, "secret": True})
        self.assertNotIn("very-secret", repr(metadata))
        self.assertEqual(store.secret_values, ("very-secret",))

    def test_invalid_definitions_are_rejected_before_extraction(self) -> None:
        response = HttpResponse(200, {}, b"{}", 1, 1)
        with self.assertRaisesRegex(CaptureError, "unsupported field"):
            capture_response(make_case({"id": {"from": "status", "regex": "x"}}), response)
        with self.assertRaisesRegex(CaptureError, "must be one of"):
            capture_response(make_case({"id": {"from": "regex"}}), response)
        with self.assertRaisesRegex(CaptureError, "must be a boolean"):
            capture_response(make_case({"id": {"from": "status", "secret": "yes"}}), response)

    def test_missing_json_path_and_invalid_json_are_actionable(self) -> None:
        with self.assertRaisesRegex(MissingCaptureError, "unavailable"):
            capture_response(
                make_case({"id": {"from": "json", "path": "data.id"}}),
                HttpResponse(200, {"Content-Type": "application/json"}, b'{"data":{}}', 1, 1),
            )
        with self.assertRaisesRegex(CaptureError, "invalid JSON"):
            capture_response(
                make_case({"id": {"from": "json", "path": "data.id"}}),
                HttpResponse(200, {"Content-Type": "application/json"}, b"not-json", 1, 1),
            )

    def test_failed_response_cannot_publish_captures(self) -> None:
        with self.assertRaisesRegex(CaptureError, "failed step"):
            capture_response(
                make_case({"id": {"from": "status"}}),
                HttpResponse(None, {}, b"", 1, 1, "connection failed"),
            )

    def test_cookie_and_header_lookup_is_case_insensitive(self) -> None:
        case = make_case(
            {
                "location": {"from": "header", "name": "location"},
                "sid": {"from": "cookie", "name": "sid"},
            }
        )
        store = capture_response(
            case,
            HttpResponse(200, {"lOcAtIoN": "/x", "cOoKiE": "sid=xyz"}, b"", 1, 1),
        )
        self.assertEqual(store.get("create", "location").value, "/x")
        self.assertEqual(store.get("create", "sid").value, "xyz")


if __name__ == "__main__":
    unittest.main()
