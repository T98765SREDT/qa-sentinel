from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qa_sentinel.config import load_suite
from qa_sentinel.openapi import OpenAPIImportError, import_openapi, write_import


def openapi_document() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Example API", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.test/{version}", "variables": {"version": {"default": "v1"}}}],
        "components": {
            "parameters": {
                "UserId": {"name": "user_id", "in": "path", "required": True, "example": "u/7"},
            },
            "schemas": {"Create": {"type": "object", "example": {"name": "Ada", "password": "secret-from-spec"}}},
            "responses": {"Created": {"description": "created", "content": {"application/json": {"example": {"id": 7}}}}},
        },
        "paths": {
            "/health": {
                "get": {
                    "operationId": "healthCheck",
                    "summary": "Health check",
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/users/{user_id}": {
                "get": {
                    "operationId": "getUser",
                    "parameters": [{"$ref": "#/components/parameters/UserId"}],
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/users": {
                "post": {
                    "operationId": "createUser",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Create"}
                            }
                        },
                    },
                    "responses": {"201": {"$ref": "#/components/responses/Created"}},
                }
            },
            "/search": {
                "get": {
                    "operationId": "search",
                    "parameters": [{"name": "q", "in": "query", "required": True}],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }


class OpenAPIImportTests(unittest.TestCase):
    def write_spec(self, directory: str, value: dict | str) -> Path:
        path = Path(directory) / "openapi.json"
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return path

    def test_safe_methods_import_and_write_methods_are_skipped_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = import_openapi(self.write_spec(directory, openapi_document()))
        self.assertEqual([item["id"] for item in result.imported], ["healthcheck", "getuser"])
        reasons = {item["path"]: item["reason"] for item in result.skipped}
        self.assertIn("requires --allow-method POST", reasons["paths./users.post"])
        self.assertIn("required parameter has no example/default", reasons["paths./search.get"])

    def test_explicit_write_method_uses_body_example_and_redacts_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = import_openapi(self.write_spec(directory, openapi_document()), ("POST",))
        ids = [item["id"] for item in result.imported]
        self.assertEqual(ids, ["healthcheck", "createuser", "getuser"])
        create = next(test for test in result.suite["tests"] if test["id"] == "createuser")
        self.assertEqual(create["method"], "POST")
        self.assertEqual(create["json"]["name"], "Ada")
        self.assertEqual(create["json"]["password"], "[REDACTED]")
        self.assertTrue(any("redacted" in warning.lower() for warning in result.warnings))

    def test_generated_suite_is_valid_and_write_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = import_openapi(self.write_spec(directory, openapi_document()))
            output = Path(directory) / "generated.json"
            write_import(result, output)
            load_suite(output)
            with self.assertRaisesRegex(OpenAPIImportError, "already exists"):
                write_import(result, output)

    def test_import_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec = self.write_spec(directory, openapi_document())
            first = import_openapi(spec)
            second = import_openapi(spec)
        self.assertEqual(first.suite, second.suite)
        self.assertEqual(first.coverage, second.coverage)

    def test_remote_and_traversal_refs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            remote = openapi_document()
            remote["paths"]["/health"]["get"]["responses"] = {"200": {"$ref": "https://example.test/response.json"}}
            with self.assertRaisesRegex(OpenAPIImportError, "Only local"):
                import_openapi(self.write_spec(directory, remote))
            traversal = openapi_document()
            traversal["paths"]["/health"]["get"]["responses"] = {"200": {"$ref": "../response.json"}}
            with self.assertRaisesRegex(OpenAPIImportError, "Only local"):
                import_openapi(self.write_spec(directory, traversal))

    def test_missing_server_uses_explicit_placeholder_warning(self) -> None:
        document = openapi_document()
        document.pop("servers")
        with tempfile.TemporaryDirectory() as directory:
            result = import_openapi(self.write_spec(directory, document))
        self.assertEqual(result.suite["variables"]["base_url"], "https://example.invalid")
        self.assertTrue(any("no servers" in warning.lower() for warning in result.warnings))

    def test_required_server_variable_without_default_is_rejected(self) -> None:
        document = openapi_document()
        document["servers"][0]["variables"]["version"].pop("default")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(OpenAPIImportError, "needs a default"):
                import_openapi(self.write_spec(directory, document))

    def test_all_skipped_error_includes_the_safe_reason(self) -> None:
        document = {
            "openapi": "3.0.3",
            "paths": {"/search": {"get": {"responses": {"200": {"description": "ok"}}, "parameters": [{"name": "q", "in": "query", "required": True}]}}},
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(OpenAPIImportError, "required parameter has no example/default"):
                import_openapi(self.write_spec(directory, document))

    def test_colliding_operation_ids_get_stable_unique_ids(self) -> None:
        document = openapi_document()
        document["paths"]["/health"]["get"]["operationId"] = "same"
        document["paths"]["/users/{user_id}"]["get"]["operationId"] = "same"
        with tempfile.TemporaryDirectory() as directory:
            result = import_openapi(self.write_spec(directory, document), ("POST",))
        self.assertEqual([test["id"] for test in result.suite["tests"]], ["same", "createuser", "same-2"])

    def test_cli_import_openapi_reports_coverage(self) -> None:
        from qa_sentinel.cli import main

        with tempfile.TemporaryDirectory() as directory:
            spec = self.write_spec(directory, openapi_document())
            output = Path(directory) / "suite.json"
            self.assertEqual(main(["import-openapi", str(spec), "--out", str(output)]), 0)
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
