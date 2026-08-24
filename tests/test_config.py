from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_sentinel.config import ConfigError, load_suite


def valid_document() -> dict[str, object]:
    return {
        "name": "Config test",
        "variables": {"base_url": "http://localhost:8000", "api_token": "top-secret-value"},
        "defaults": {
            "headers": {"Authorization": "Bearer {{api_token}}"},
            "timeout_seconds": 2,
        },
        "tests": [
            {
                "name": "Health",
                "url": "{{base_url}}/health",
                "assertions": [{"type": "status", "equals": 200}],
            }
        ],
    }


class ConfigTests(unittest.TestCase):
    def write(self, document: object) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "suite.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return temporary, path

    def test_loads_and_interpolates_suite(self) -> None:
        temporary, path = self.write(valid_document())
        self.addCleanup(temporary.cleanup)
        suite = load_suite(path)
        self.assertEqual(suite.tests[0].url, "http://localhost:8000/health")
        self.assertEqual(suite.tests[0].headers["Authorization"], "Bearer top-secret-value")
        self.assertIn("top-secret-value", suite.known_secrets)

    def test_cli_override_replaces_variable(self) -> None:
        temporary, path = self.write(valid_document())
        self.addCleanup(temporary.cleanup)
        suite = load_suite(path, {"base_url": "https://example.test"})
        self.assertEqual(suite.tests[0].url, "https://example.test/health")

    def test_environment_interpolation(self) -> None:
        document = valid_document()
        document["tests"][0]["headers"] = {"X-Build": "${BUILD_NUMBER}"}  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {"BUILD_NUMBER": "42"}):
            suite = load_suite(path)
        self.assertEqual(suite.tests[0].headers["X-Build"], "42")

    def test_duplicate_ids_are_rejected(self) -> None:
        document = valid_document()
        document["tests"] = [document["tests"][0], document["tests"][0]]  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "Duplicate test id"):
            load_suite(path)

    def test_unknown_variable_is_rejected(self) -> None:
        document = valid_document()
        document["tests"][0]["url"] = "{{missing}}/health"  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "Unknown variable"):
            load_suite(path)

    def test_invalid_http_url_is_rejected(self) -> None:
        document = valid_document()
        document["tests"][0]["url"] = "file:///etc/passwd"  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "absolute HTTP"):
            load_suite(path)


if __name__ == "__main__":
    unittest.main()

