from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_sentinel.config import ConfigError, load_suite
from qa_sentinel.environment import (
    EnvironmentError,
    load_environment_profile,
    resolve_profile_variables,
)
from qa_sentinel.models import SuiteResult
from qa_sentinel.redact import redact_text
from qa_sentinel.reporting import build_report, render_html, render_junit_xml


def suite_document() -> dict[str, object]:
    return {
        "name": "Profile suite",
        "variables": {"base_url": "http://suite.example", "api_token": "old-value"},
        "defaults": {"headers": {"Authorization": "Bearer {{api_token}}"}},
        "tests": [
            {
                "name": "Health",
                "url": "{{base_url}}/health",
                "assertions": [{"type": "status", "equals": 200}],
            }
        ],
    }


class EnvironmentTests(unittest.TestCase):
    def write_json(self, root: Path, name: str, value: object) -> Path:
        path = root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_profile_loads_sources_without_reading_secret_into_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"QA_API_TOKEN": "profile-secret"}, clear=True
        ):
            root = Path(directory)
            path = self.write_json(
                root,
                "staging.json",
                {
                    "name": "staging",
                    "variables": {"base_url": "https://staging.example"},
                    "secrets": {"api_token": {"from_env": "QA_API_TOKEN"}},
                },
            )
            profile = load_environment_profile(path, require_secrets=True)
            self.assertEqual(profile.name, "staging")
            self.assertEqual(profile.variables["base_url"], "https://staging.example")
            self.assertEqual(profile.secret_names, ("api_token",))
            self.assertEqual(profile.missing_secret_sources, ())
            self.assertEqual(profile.public_secret_sources()[0]["source"], "QA_API_TOKEN")
            self.assertNotIn("profile-secret", profile.config_hash)
            self.assertEqual(len(profile.config_hash), 16)

    def test_profile_hash_is_stable_when_secret_value_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write_json(
                root,
                "staging.json",
                {
                    "name": "staging",
                    "variables": {"base_url": "https://staging.example"},
                    "secrets": {"api_token": {"from_env": "QA_API_TOKEN"}},
                },
            )
            with patch.dict(os.environ, {"QA_API_TOKEN": "one"}, clear=True):
                first = load_environment_profile(path).config_hash
            with patch.dict(os.environ, {"QA_API_TOKEN": "two"}, clear=True):
                second = load_environment_profile(path).config_hash
            self.assertEqual(first, second)

    def test_resolution_precedence_keeps_secret_out_of_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"QA_API_TOKEN": "profile-secret"}, clear=True
        ):
            root = Path(directory)
            path = self.write_json(
                root,
                "local.json",
                {
                    "name": "local",
                    "variables": {"base_url": "http://profile.example", "region": "apac"},
                    "secrets": {"api_token": {"from_env": "QA_API_TOKEN"}},
                },
            )
            profile = load_environment_profile(path, require_secrets=True)
            resolved = resolve_profile_variables(
                profile,
                {"base_url": "http://suite.example", "region": "old"},
                {"region": "override"},
            )
            self.assertEqual(resolved["base_url"], "http://profile.example")
            self.assertEqual(resolved["region"], "override")
            self.assertEqual(resolved["api_token"], "profile-secret")
            with self.assertRaisesRegex(EnvironmentError, "cannot override declared secret"):
                resolve_profile_variables(profile, {}, {"api_token": "unsafe"})

    def test_missing_secret_is_reported_by_profile_and_suite_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            profile = load_environment_profile(
                self.write_json(
                    root,
                    "staging.json",
                    {
                        "name": "staging",
                        "variables": {"base_url": "https://staging.example"},
                        "secrets": {"api_token": {"from_env": "QA_API_TOKEN"}},
                    },
                )
            )
            self.assertEqual(profile.missing_secret_sources, ("QA_API_TOKEN",))
            suite_path = self.write_json(root, "suite.json", suite_document())
            with self.assertRaisesRegex(ConfigError, "QA_API_TOKEN"):
                load_suite(suite_path, environment_profile=profile)

    def test_profile_values_are_used_and_carried_into_report_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"QA_API_TOKEN": "profile-secret"}, clear=True
        ):
            root = Path(directory)
            profile = load_environment_profile(
                self.write_json(
                    root,
                    "staging.json",
                    {
                        "name": "staging",
                        "variables": {"base_url": "https://staging.example"},
                        "secrets": {"api_token": {"from_env": "QA_API_TOKEN"}},
                    },
                ),
                require_secrets=True,
            )
            suite = load_suite(
                self.write_json(root, "suite.json", suite_document()),
                {"region": "apac"},
                environment_profile=profile,
            )
            self.assertEqual(suite.environment, "staging")
            self.assertEqual(suite.tests[0].url, "https://staging.example/health")
            self.assertEqual(suite.tests[0].headers["Authorization"], "Bearer profile-secret")
            self.assertEqual(suite.environment_config_hash, profile.config_hash)
            self.assertEqual(suite.secret_sources[0].source, "QA_API_TOKEN")
            self.assertIn("profile-secret", suite.known_secrets)
            result = SuiteResult(
                suite.name,
                (),
                "2026-08-30T00:00:00+00:00",
                "2026-08-30T00:00:00+00:00",
                0,
                environment=suite.environment,
                environment_config_hash=suite.environment_config_hash,
                secret_sources=suite.secret_sources,
            )
            report = build_report(result, suite.known_secrets)
            self.assertEqual(report["environment"], "staging")
            self.assertEqual(report["environment_config_hash"], profile.config_hash)
            self.assertEqual(
                report["secret_sources"], [{"name": "api_token", "source": "QA_API_TOKEN"}]
            )
            serialized = json.dumps(report)
            self.assertNotIn("profile-secret", serialized)
            self.assertIn("Environment profile fingerprint", render_html(result, suite.known_secrets))
            self.assertIn("environment_config_hash", render_junit_xml(result, suite.known_secrets))

    def test_profile_rejects_unknown_fields_and_secret_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unknown = self.write_json(root, "unknown.json", {"name": "x", "secretts": {}})
            with self.assertRaisesRegex(EnvironmentError, "unsupported field.*secretts"):
                load_environment_profile(unknown)
            overlap = self.write_json(
                root,
                "overlap.json",
                {
                    "variables": {"api_token": "literal"},
                    "secrets": {"api_token": {"from_env": "QA_API_TOKEN"}},
                },
            )
            with self.assertRaisesRegex(EnvironmentError, "both a variable and a secret"):
                load_environment_profile(overlap)

    def test_profile_rejects_inline_secret_values_and_bad_source_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inline = self.write_json(
                root,
                "inline.json",
                {"secrets": {"api_token": {"value": "do-not-store"}}},
            )
            with self.assertRaisesRegex(EnvironmentError, "unsupported field.*value"):
                load_environment_profile(inline)
            inline_variable = self.write_json(
                root,
                "inline-variable.json",
                {"variables": {"api_token": "do-not-store"}},
            )
            with self.assertRaisesRegex(EnvironmentError, "looks sensitive"):
                load_environment_profile(inline_variable)
            bad_name = self.write_json(
                root,
                "bad-name.json",
                {"secrets": {"api-token": {"from_env": "QA_API_TOKEN"}}},
            )
            with self.assertRaisesRegex(EnvironmentError, "valid name"):
                load_environment_profile(bad_name)

    def test_redaction_covers_url_encoded_and_short_secret_boundaries(self) -> None:
        secret = "päss word/42"
        text = "url=p%C3%A4ss%20word%2F42 JSON=\"päss word/42\""
        self.assertNotIn("päss", redact_text(text, (secret,)))
        self.assertIn("[REDACTED]", redact_text(text, (secret,)))
        self.assertEqual(redact_text("id=abc", ("abc",)), "id=abc")


if __name__ == "__main__":
    unittest.main()
