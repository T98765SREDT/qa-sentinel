from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qa_sentinel.doctor as doctor_module
from qa_sentinel.doctor import diagnose, format_report


class FakeVersionInfo(tuple):
    @property
    def major(self) -> int:
        return self[0]

    @property
    def minor(self) -> int:
        return self[1]

    @property
    def micro(self) -> int:
        return self[2]


def write_suite(root: Path, *, url: str = "http://127.0.0.1:8765/health") -> Path:
    path = root / "suite.json"
    path.write_text(
        json.dumps(
            {
                "name": "Doctor suite",
                "tests": [
                    {
                        "name": "Health",
                        "url": url,
                        "assertions": [{"type": "status", "equals": 200}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class DoctorTests(unittest.TestCase):
    def test_valid_suite_is_ready_without_sending_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            doctor_module.sys, "version_info", FakeVersionInfo((3, 11, 0))
        ):
            root = Path(directory)
            suite = write_suite(root)
            output = root / "reports/report.json"
            report = diagnose(suite, output_paths=(output,))
            self.assertTrue(report.passed)
            self.assertIn("suite", {check.name for check in report.checks})
            self.assertIn("can be written", format_report(report))
            self.assertFalse(output.exists())

    def test_missing_environment_reports_name_but_not_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=True
        ):
            suite = write_suite(Path(directory), url="http://example.test/${QA_API_TOKEN}")
            report = diagnose(suite)
            self.assertFalse(report.passed)
            text = format_report(report)
            self.assertIn("QA_API_TOKEN", text)
            self.assertNotIn("missing-secret-value", text)

    def test_profile_variables_are_used_for_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            doctor_module.sys, "version_info", FakeVersionInfo((3, 11, 0))
        ):
            root = Path(directory)
            suite = write_suite(root, url="{{base_url}}/health")
            profile = root / "local.json"
            profile.write_text(
                json.dumps({"name": "local", "variables": {"base_url": "http://127.0.0.1:8765"}}),
                encoding="utf-8",
            )
            report = diagnose(suite, environment_path=profile)
            self.assertTrue(report.passed)
            self.assertIn("local is valid", format_report(report))

    def test_unwritable_output_parent_is_reported_without_probe_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = write_suite(root)
            blocked = root / "not-a-directory"
            blocked.write_text("file", encoding="utf-8")
            output = blocked / "report.json"
            report = diagnose(suite, output_paths=(output,))
            self.assertFalse(report.passed)
            self.assertIn("not a directory", format_report(report))
            self.assertFalse((root / "report.json").exists())

    def test_invalid_profile_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = write_suite(root)
            profile = root / "broken.json"
            profile.write_text(json.dumps({"variables": []}), encoding="utf-8")
            report = diagnose(suite, environment_path=profile)
            self.assertFalse(report.passed)
            self.assertIn("environment profile", format_report(report))
            self.assertIn("variables must map", format_report(report))


if __name__ == "__main__":
    unittest.main()
