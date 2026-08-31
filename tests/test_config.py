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

    def test_optional_environment_label_is_normalized(self) -> None:
        document = valid_document()
        document["environment"] = "  staging  "
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        suite = load_suite(path)
        self.assertEqual(suite.environment, "staging")

    def test_environment_label_is_bounded(self) -> None:
        document = valid_document()
        document["environment"] = "x" * 81
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "environment"):
            load_suite(path)

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

    def test_resolved_environment_secret_is_collected_for_redaction(self) -> None:
        document = valid_document()
        document["variables"]["api_token"] = "${RUNTIME_VALUE}"  # type: ignore[index]
        document["tests"][0]["url"] = (  # type: ignore[index]
            "http://localhost:8000/health?credential={{api_token}}"
        )
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {"RUNTIME_VALUE": "resolved-env-secret"}):
            suite = load_suite(path)
        self.assertIn("resolved-env-secret", suite.known_secrets)

    def test_suite_config_hash_excludes_secret_values(self) -> None:
        document = valid_document()
        document["variables"]["api_token"] = "${HASHED_TOKEN}"  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {"HASHED_TOKEN": "first-secret-value"}):
            first = load_suite(path)
        with patch.dict(os.environ, {"HASHED_TOKEN": "second-secret-value"}):
            second = load_suite(path)
        self.assertEqual(first.config_hash, second.config_hash)
        self.assertNotEqual(first.known_secrets, second.known_secrets)

        changed = dict(document)
        changed["description"] = "changed"
        other_temporary, other_path = self.write(changed)
        self.addCleanup(other_temporary.cleanup)
        with patch.dict(os.environ, {"HASHED_TOKEN": "first-secret-value"}):
            self.assertNotEqual(first.config_hash, load_suite(other_path).config_hash)

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

    def test_url_without_hostname_is_rejected(self) -> None:
        document = valid_document()
        document["tests"][0]["url"] = "http:///health"  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "hostname"):
            load_suite(path)

    def test_url_user_information_is_rejected(self) -> None:
        document = valid_document()
        document["tests"][0]["url"] = "https://user:pass@example.test/health"  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "user information"):
            load_suite(path)

    def test_unknown_assertion_is_rejected_during_loading(self) -> None:
        document = valid_document()
        document["tests"][0]["assertions"] = [{"type": "mystery"}]  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "unsupported assertion"):
            load_suite(path)

    def test_malformed_assertion_is_rejected_during_loading(self) -> None:
        document = valid_document()
        document["tests"][0]["assertions"] = [  # type: ignore[index]
            {"type": "status", "equals": "200"}
        ]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "HTTP integers"):
            load_suite(path)

    def test_retry_settings_have_safety_caps(self) -> None:
        document = valid_document()
        document["defaults"]["retries"] = 6  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "must not exceed 5"):
            load_suite(path)

        document["defaults"]["retries"] = 0  # type: ignore[index]
        document["defaults"]["retry_delay_seconds"] = 31  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "must not exceed 30"):
            load_suite(path)

    def test_response_limit_defaults_and_per_test_override(self) -> None:
        document = valid_document()
        document["defaults"]["max_response_bytes"] = 4096  # type: ignore[index]
        document["tests"][0]["max_response_bytes"] = 1024  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        suite = load_suite(path)
        self.assertEqual(suite.tests[0].max_response_bytes, 1024)

    def test_response_limit_must_be_within_safety_cap(self) -> None:
        document = valid_document()
        document["defaults"]["max_response_bytes"] = 0  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "positive integer"):
            load_suite(path)

        document["defaults"]["max_response_bytes"] = 2 * 1024 * 1024 + 1  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "must not exceed"):
            load_suite(path)

    def test_unknown_suite_field_is_rejected_with_its_path(self) -> None:
        document = valid_document()
        document["retrys"] = 1
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "Suite contains unsupported field.*retrys"):
            load_suite(path)

    def test_unknown_defaults_field_is_rejected(self) -> None:
        document = valid_document()
        document["defaults"]["timeuot_seconds"] = 2  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "defaults contains unsupported field.*timeuot_seconds"):
            load_suite(path)

    def test_unknown_test_field_is_rejected(self) -> None:
        document = valid_document()
        document["tests"][0]["asertions"] = []  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, r"tests\[0\] contains unsupported field.*asertions"):
            load_suite(path)

    def test_unknown_assertion_field_is_rejected(self) -> None:
        document = valid_document()
        document["tests"][0]["assertions"][0]["equlas"] = 200  # type: ignore[index]
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, r"tests\[0\]\.assertions\[0\] contains unsupported field.*equlas"):
            load_suite(path)

    def test_ambiguous_json_predicates_are_rejected(self) -> None:
        document = valid_document()
        document["tests"][0]["assertions"][0] = {  # type: ignore[index]
            "type": "json_path",
            "path": "status",
            "exists": True,
            "equals": "ok",
        }
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ConfigError, "at most one JSON predicate"):
            load_suite(path)

    def test_slow_threshold_is_carried_from_suite(self) -> None:
        document = valid_document()
        document["slow_threshold_ms"] = 250
        temporary, path = self.write(document)
        self.addCleanup(temporary.cleanup)
        suite = load_suite(path)
        self.assertEqual(suite.slow_threshold_ms, 250.0)


if __name__ == "__main__":
    unittest.main()
