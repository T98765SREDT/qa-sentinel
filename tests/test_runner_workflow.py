from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from qa_sentinel.capture import resolve_case_references
from qa_sentinel.cli import main
from qa_sentinel.models import AssertionSpec, HttpResponse, TestCase, TestSuite
from qa_sentinel.reporting import build_report, render_html, render_junit_xml
from qa_sentinel.runner import SuiteRunner


class FakeClient:
    """Deterministic transport double that still exercises capture resolution."""

    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, object]] = []

    def execute(self, case: TestCase, captures: object | None = None) -> HttpResponse:
        if captures is not None:
            case = resolve_case_references(case, captures)  # type: ignore[arg-type]
        self.calls.append((case.case_id, case.url, case.body))
        return self.responses.get(
            case.case_id,
            HttpResponse(200, {"Content-Type": "application/json"}, b"{}", 1.0, 1),
        )


def make_case(
    case_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    run_if: str = "success",
    extract: dict[str, object] | None = None,
    url: str | None = None,
    cleanup: bool = False,
) -> TestCase:
    return TestCase(
        case_id=case_id,
        name=case_id.replace("-", " ").title(),
        method="GET",
        url=url or f"https://example.test/{case_id}",
        headers={},
        body=None,
        timeout_seconds=1,
        retries=0,
        retry_delay_seconds=0,
        retry_on_status=(),
        retry_non_idempotent=False,
        assertions=(AssertionSpec("status", {"equals": 200}),),
        depends_on=depends_on,
        run_if=run_if,
        extract=extract or {},
        cleanup=cleanup,
    )


class WorkflowRunnerTests(unittest.TestCase):
    def test_dependency_layers_resolve_typed_captures_and_retain_order(self) -> None:
        login = make_case(
            "login",
            extract={"token": {"from": "json", "path": "token", "secret": True}},
        )
        create = make_case(
            "create",
            depends_on=("login",),
            url="https://example.test/orders",
        )
        create = TestCase(**{**create.__dict__, "headers": {"Authorization": "Bearer {{steps.login.token}}"}})
        read = make_case(
            "read",
            depends_on=("create",),
            url="https://example.test{{steps.create.location}}",
            extract={"unused": {"from": "status"}},
        )
        responses = {
            "login": HttpResponse(
                200,
                {"Content-Type": "application/json"},
                b'{"token":"workflow-secret"}',
                1,
                1,
            ),
            "create": HttpResponse(201, {"Location": "/orders/42"}, b"{}", 1, 1),
            "read": HttpResponse(200, {"Content-Type": "application/json"}, b"{}", 1, 1),
        }
        # The create response is intentionally 201, so its assertion needs to
        # describe the real contract while the workflow still remains green.
        create = TestCase(**{**create.__dict__, "assertions": (AssertionSpec("status", {"equals": 201}),), "extract": {"location": {"from": "header", "name": "Location"}}})
        client = FakeClient(responses)
        suite = TestSuite("workflow", (read, login, create), workers=2)

        result = SuiteRunner(client).run(suite)

        self.assertTrue(result.is_successful)
        self.assertEqual([test.case.case_id for test in result.tests], ["read", "login", "create"])
        self.assertEqual([call[0] for call in client.calls], ["login", "create", "read"])
        self.assertEqual(client.calls[1][1], "https://example.test/orders")
        self.assertEqual(client.calls[2][1], "https://example.test/orders/42")
        self.assertEqual(client.calls[1][2], None)
        self.assertIn("workflow-secret", result.known_secrets)
        self.assertEqual(
            [(item["step"], item["name"]) for item in result.capture_metadata],
            [("login", "token"), ("create", "location"), ("read", "unused")],
        )

    def test_failed_dependency_blocks_child_without_sending_request(self) -> None:
        parent = make_case("parent")
        child = make_case("child", depends_on=("parent",))
        client = FakeClient(
            {
                "parent": HttpResponse(500, {}, b"", 1, 1),
            }
        )

        result = SuiteRunner(client).run(TestSuite("blocked", (parent, child)))

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.blocked, 1)
        self.assertEqual([call[0] for call in client.calls], ["parent"])
        self.assertIn("unsuccessful dependency", result.tests[1].response.error or "")

    def test_always_cleanup_runs_after_failed_dependency(self) -> None:
        parent = make_case("parent")
        cleanup = make_case(
            "cleanup", depends_on=("parent",), run_if="always", cleanup=True
        )
        client = FakeClient({"parent": HttpResponse(500, {}, b"", 1, 1)})

        result = SuiteRunner(client).run(TestSuite("cleanup", (parent, cleanup)))

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.passed, 1)
        self.assertEqual([call[0] for call in client.calls], ["parent", "cleanup"])

    def test_missing_capture_is_an_error_and_dependent_is_blocked(self) -> None:
        parent = make_case(
            "parent", extract={"token": {"from": "header", "name": "X-Token"}}
        )
        child = make_case(
            "child",
            depends_on=("parent",),
            url="https://example.test/{{steps.parent.token}}",
        )
        client = FakeClient({"parent": HttpResponse(200, {}, b"{}", 1, 1)})

        result = SuiteRunner(client).run(TestSuite("capture failure", (parent, child)))

        self.assertEqual(result.errors, 1)
        self.assertEqual(result.blocked, 1)
        self.assertEqual([call[0] for call in client.calls], ["parent"])
        self.assertIn("capture", result.tests[0].response.error or "")

    def test_fail_fast_skips_ordinary_steps_but_keeps_cleanup(self) -> None:
        first = make_case("first")
        later = make_case("later", depends_on=("first",))
        cleanup = make_case("cleanup", depends_on=("first",), run_if="always", cleanup=True)
        client = FakeClient({"first": HttpResponse(500, {}, b"", 1, 1)})

        result = SuiteRunner(client).run(
            TestSuite("fail fast", (first, later, cleanup)), fail_fast=True
        )

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.blocked, 1)
        self.assertEqual(result.passed, 1)
        self.assertEqual([call[0] for call in client.calls], ["first", "cleanup"])

    def test_max_failures_counts_failures_and_keeps_always_cleanup(self) -> None:
        first = make_case("first")
        cleanup = make_case("cleanup", depends_on=("first",), run_if="always", cleanup=True)
        later = make_case("later", depends_on=("cleanup",))
        client = FakeClient({"first": HttpResponse(500, {}, b"", 1, 1)})

        result = SuiteRunner(client).run(
            TestSuite("failure limit", (first, cleanup, later)), max_failures=1
        )

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.passed, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual([call[0] for call in client.calls], ["first", "cleanup"])
        self.assertIn("failure limit", result.tests[2].response.error or "")

    def test_keyboard_interrupt_returns_safe_interrupted_result(self) -> None:
        class InterruptingClient(FakeClient):
            def execute(self, case: TestCase, captures: object | None = None) -> HttpResponse:
                self.calls.append((case.case_id, case.url, case.body))
                raise KeyboardInterrupt()

        suite = TestSuite("interrupted", (make_case("first"), make_case("second")))
        result = SuiteRunner(InterruptingClient({})).run(suite)

        self.assertTrue(result.interrupted)
        self.assertEqual(result.skipped, 2)
        self.assertFalse(result.is_successful)
        self.assertTrue(all(test.status == "skipped" for test in result.tests))

    def test_report_exposes_workflow_status_separately_from_http_status(self) -> None:
        parent = make_case("parent")
        child = make_case("child", depends_on=("parent",))
        client = FakeClient({"parent": HttpResponse(500, {}, b"", 1, 1)})
        result = SuiteRunner(client).run(TestSuite("report states", (parent, child)))

        report = build_report(result)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["summary"]["blocked"], 1)
        self.assertEqual(report["tests"][0]["status"], "failed")
        self.assertEqual(report["tests"][0]["response_status"], 500)
        self.assertEqual(report["tests"][1]["status"], "blocked")
        self.assertIn('class="test-card blocked"', render_html(result))
        junit = render_junit_xml(result)
        self.assertIn('failures="1"', junit)
        self.assertIn("<skipped>", junit)

    def test_cli_dry_run_prints_plan_without_writing_reports(self) -> None:
        document = {
            "schemaVersion": 2,
            "name": "dry-run",
            "tests": [
                {
                    "id": "health",
                    "name": "Health",
                    "url": "https://unreachable.invalid/health",
                    "assertions": [{"type": "status", "equals": 200}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite_path = root / "suite.json"
            suite_path.write_text(json.dumps(document), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["run", str(suite_path), "--dry-run", "--json", str(root / "report.json")])

            self.assertEqual(exit_code, 0)
            self.assertIn("dry-run: schema v2; 1 step(s); 1 layer(s)", output.getvalue())
            self.assertIn("No requests sent (dry run).", output.getvalue())
            self.assertFalse((root / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
