from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_sentinel.models import AssertionResult, AssertionSpec, HttpResponse, SuiteResult, TestCase, TestResult, TestSuite
from qa_sentinel.reporting import build_report, render_html, render_junit_xml, write_json_report
from qa_sentinel.reproduce import curl_command
from qa_sentinel.runner import SuiteRunner


def failed_result() -> SuiteResult:
    case = TestCase(
        case_id="create",
        name="Create account",
        method="POST",
        url="https://api.example.test/users?token=super-secret-token",
        headers={"Authorization": "Bearer super-secret-token", "Accept": "application/json"},
        body={"email": "person@example.test", "password": "super-secret-token"},
        timeout_seconds=3,
        retries=2,
        retry_delay_seconds=0.1,
        retry_on_status=(429, 500),
        retry_non_idempotent=True,
        assertions=(AssertionSpec("status", {"equals": 201}),),
        tags=("contract", "write"),
    )
    test = TestResult(
        case=case,
        passed=False,
        response=HttpResponse(
            500,
            {"Content-Type": "application/json; charset=utf-8"},
            b'{"error":"super-secret-token","reason":"upstream"}',
            42.5,
            3,
        ),
        assertions=(AssertionResult("status", False, "status did not match", 201, 500),),
        started_at="2026-08-30T00:00:00+00:00",
        finished_at="2026-08-30T00:00:01+00:00",
    )
    return SuiteResult(
        suite_name="Report v2",
        tests=(test,),
        started_at="2026-08-30T00:00:00+00:00",
        finished_at="2026-08-30T00:00:01+00:00",
        duration_ms=1000,
        environment="staging",
        environment_config_hash="env1234567890abcd",
        suite_config_hash="suite1234567890",
        run_id="run-123",
        tool_version="1.2.0",
        git_sha="abc123",
        git_branch="main",
        ci_url="https://ci.example.test/job/7",
        selected_tags=("contract",),
        worker_count=4,
        retry_settings={
            "max_retries": 2,
            "retry_on_status": [429, 500],
            "retry_non_idempotent": True,
        },
        known_secrets=("super-secret-token",),
    )


class ReportingV2Tests(unittest.TestCase):
    def test_runner_records_explicit_ci_provenance(self) -> None:
        class PassingClient:
            def execute(self, case: TestCase) -> HttpResponse:
                return HttpResponse(200, {}, b"{}", 1, 1)

        with patch.dict(
            "os.environ",
            {
                "GITHUB_SHA": "abc123",
                "GITHUB_REF_NAME": "main",
                "GITHUB_RUN_URL": "https://github.example.test/run/7",
            },
        ):
            result = SuiteRunner(PassingClient()).run(
                TestSuite(
                    "ci",
                    (TestCase(
                        case_id="health", name="Health", method="GET", url="https://example.test/health",
                        headers={}, body=None, timeout_seconds=1, retries=0, retry_delay_seconds=0,
                        retry_on_status=(), retry_non_idempotent=False,
                        assertions=(AssertionSpec("status", {"equals": 200}),),
                    ),),
                )
            )
        self.assertEqual(result.git_sha, "abc123")
        self.assertEqual(result.git_branch, "main")
        self.assertEqual(result.ci_url, "https://github.example.test/run/7")

    def test_report_contains_provenance_paths_and_response_metadata(self) -> None:
        result = failed_result()
        report = build_report(result, result.known_secrets)

        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["run_id"], "run-123")
        self.assertEqual(report["suite_config_hash"], "suite1234567890")
        self.assertEqual(report["provenance"]["git_sha"], "abc123")
        self.assertEqual(report["execution"]["workers"], 4)
        test = report["tests"][0]
        self.assertEqual(test["request_id"], "create")
        self.assertEqual(test["response_size_bytes"], len(result.tests[0].response.body))
        self.assertEqual(test["response_content_type"], "application/json")
        self.assertEqual(test["assertions"][0]["path"], "tests[0].assertions[0]")
        self.assertNotIn("super-secret-token", json.dumps(report))
        self.assertIn("reproduction", test)

    def test_curl_reproduction_is_shell_quoted_and_redacted(self) -> None:
        command = curl_command(failed_result().tests[0], ("super-secret-token",))

        self.assertTrue(command.startswith("curl --request POST"))
        self.assertIn("--header", command)
        self.assertIn("[REDACTED]", command)
        self.assertNotIn("super-secret-token", command)
        self.assertIn("--data-raw", command)

    def test_junit_and_html_expose_same_run_provenance(self) -> None:
        result = failed_result()
        junit = render_junit_xml(result, result.known_secrets)
        html = render_html(result, result.known_secrets)

        self.assertIn('name="run_id" value="run-123"', junit)
        self.assertIn('name="suite_config_hash" value="suite1234567890"', junit)
        self.assertIn("run-123", html)
        self.assertIn("curl", html)
        self.assertNotIn("super-secret-token", junit)
        self.assertNotIn("super-secret-token", html)

    def test_report_writes_are_atomic_and_preserve_previous_file_on_failure(self) -> None:
        result = failed_result()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("previous report", encoding="utf-8")
            with patch("qa_sentinel.reporting.os.replace", side_effect=OSError("simulated interruption")):
                with self.assertRaises(OSError):
                    write_json_report(result, path, result.known_secrets)
            self.assertEqual(path.read_text(encoding="utf-8"), "previous report")
            self.assertEqual(list(Path(directory).glob(".report.json.*.tmp")), [])

    def test_report_schema_declares_v2_required_contract(self) -> None:
        schema_path = Path(__file__).parents[1] / "schemas" / "report-v2.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 2)
        required = set(schema["required"])
        self.assertTrue({"run_id", "tool_version", "provenance", "execution", "summary", "tests"} <= required)
        report = build_report(failed_result(), ("super-secret-token",))
        self.assertTrue(set(report) >= required)


if __name__ == "__main__":
    unittest.main()
