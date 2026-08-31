from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qa_sentinel.config import ConfigError, load_suite
from qa_sentinel.models import AssertionSpec, TestCase, TestSuite
from qa_sentinel.planner import PlanError, format_plan, plan_suite
from qa_sentinel.runner import SuiteRunner


def case(case_id: str, depends_on: tuple[str, ...] = (), *, run_if: str = "success") -> TestCase:
    return TestCase(
        case_id=case_id,
        name=case_id,
        method="GET",
        url="https://example.test/" + case_id,
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
    )


class PlannerTests(unittest.TestCase):
    def write(self, root: Path, document: object) -> Path:
        path = root / "suite.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def v2_document(self, tests: list[dict[str, object]]) -> dict[str, object]:
        return {"schemaVersion": 2, "name": "Workflow", "tests": tests}

    def test_legacy_suite_is_normalized_to_v2_with_independent_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                {
                    "name": "Legacy",
                    "tests": [
                        {
                            "name": "Health",
                            "url": "https://example.test/health",
                            "assertions": [{"type": "status", "equals": 200}],
                        }
                    ],
                },
            )
            suite = load_suite(path)
            plan = plan_suite(suite)
            self.assertEqual(suite.schema_version, 2)
            self.assertEqual(plan.layers[0].step_ids, ("health",))
            self.assertFalse(plan.has_dependencies)

    def test_v2_requires_stable_explicit_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                self.v2_document(
                    [
                        {
                            "name": "Health",
                            "url": "https://example.test/health",
                            "assertions": [{"type": "status", "equals": 200}],
                        }
                    ]
                ),
            )
            with self.assertRaisesRegex(ConfigError, "id is required"):
                load_suite(path)

    def test_v2_workflow_fields_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                self.v2_document(
                    [
                        {
                            "id": "login",
                            "name": "Login",
                            "url": "https://example.test/login",
                            "assertions": [{"type": "status", "equals": 200}],
                        },
                        {
                            "id": "cleanup",
                            "name": "Cleanup",
                            "url": "https://example.test/cleanup",
                            "depends_on": ["login"],
                            "cleanup": True,
                            "extract": {"id": {"from": "status"}},
                            "assertions": [{"type": "status", "equals": 200}],
                        },
                    ]
                ),
            )
            suite = load_suite(path)
            cleanup = suite.tests[1]
            self.assertEqual(cleanup.depends_on, ("login",))
            self.assertEqual(cleanup.run_if, "always")
            self.assertTrue(cleanup.cleanup)
            self.assertIn("id", cleanup.extract)

    def test_stable_layers_preserve_declaration_order_for_diamond(self) -> None:
        suite = TestSuite(
            "Diamond",
            (case("start"), case("right", ("start",)), case("left", ("start",)), case("finish", ("left", "right"))),
        )
        plan = plan_suite(suite)
        self.assertEqual([layer.step_ids for layer in plan.layers], [
            ("start",),
            ("right", "left"),
            ("finish",),
        ])
        self.assertEqual(plan.layer_for("left"), 1)

    def test_unknown_dependency_is_rejected(self) -> None:
        with self.assertRaisesRegex(PlanError, "unknown step"):
            plan_suite(TestSuite("Unknown", (case("health", ("missing",)),)))

    def test_self_dependency_is_rejected(self) -> None:
        with self.assertRaisesRegex(PlanError, "cannot depend on itself"):
            plan_suite(TestSuite("Self", (case("health", ("health",)),)))

    def test_cycle_is_rejected_with_stable_ids(self) -> None:
        with self.assertRaisesRegex(PlanError, "cycle detected.*first.*second"):
            plan_suite(TestSuite("Cycle", (case("first", ("second",)), case("second", ("first",)))))

    def test_always_run_policy_is_retained_for_scheduler(self) -> None:
        plan = plan_suite(TestSuite("Cleanup", (case("cleanup", run_if="always"),)))
        self.assertEqual(plan.steps[0].run_if, "always")
        self.assertFalse(plan.has_dependencies)

    def test_future_schema_is_rejected_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                {"schemaVersion": 3, "name": "Future", "tests": []},
            )
            with self.assertRaisesRegex(ConfigError, "Unsupported suite schemaVersion"):
                load_suite(path)

    def test_legacy_workflow_fields_require_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                {
                    "name": "Legacy workflow",
                    "tests": [
                        {
                            "id": "health",
                            "name": "Health",
                            "url": "https://example.test/health",
                            "depends_on": [],
                            "run_if": "always",
                            "assertions": [{"type": "status", "equals": 200}],
                        }
                    ],
                },
            )
            with self.assertRaisesRegex(ConfigError, "add schemaVersion: 2"):
                load_suite(path)

    def test_format_plan_is_compact_and_deterministic(self) -> None:
        output = format_plan(plan_suite(TestSuite("Format", (case("a"), case("b")))))
        self.assertEqual(
            output,
            "Format: schema v2; 2 step(s); 1 layer(s)\n  layer 0: a, b",
        )

    def test_runner_handles_workflow_fields_without_ignoring_dependencies(self) -> None:
        class NoRequestClient:
            def execute(self, case: TestCase, captures: object = None) -> object:
                raise RuntimeError("synthetic failure")

        suite = TestSuite("Workflow", (case("login"), case("read", ("login",))))
        result = SuiteRunner(NoRequestClient()).run(suite)
        self.assertEqual([item.status for item in result.tests], ["error", "blocked"])

    def test_step_reference_requires_declared_dependency_and_capture(self) -> None:
        suite = TestSuite(
            "References",
            (
                TestCase(
                    **{
                        **case("login").__dict__,
                        "extract": {"token": {"from": "status"}},
                    }
                ),
                TestCase(
                    **{
                        **case("read", ("login",)).__dict__,
                        "url": "https://example.test/{{steps.login.token}}",
                    }
                ),
            ),
        )
        plan = plan_suite(suite)
        self.assertEqual(plan.layers[1].step_ids, ("read",))

    def test_step_reference_without_dependency_is_rejected(self) -> None:
        with self.assertRaisesRegex(PlanError, "without declaring it"):
            plan_suite(
                TestSuite(
                    "References",
                    (
                        case("login"),
                        TestCase(
                            **{
                                **case("read").__dict__,
                                "url": "https://example.test/{{steps.login.token}}",
                            }
                        ),
                    ),
                )
            )

    def test_step_reference_to_unknown_capture_is_rejected(self) -> None:
        with self.assertRaisesRegex(PlanError, "unknown capture"):
            plan_suite(
                TestSuite(
                    "References",
                    (
                        TestCase(
                            **{
                                **case("login").__dict__,
                                "extract": {"other": {"from": "status"}},
                            }
                        ),
                        TestCase(
                            **{
                                **case("read", ("login",)).__dict__,
                                "url": "https://example.test/{{steps.login.token}}",
                            }
                        ),
                    ),
                )
            )


if __name__ == "__main__":
    unittest.main()
