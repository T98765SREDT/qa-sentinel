from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qa_sentinel.cli import main
from qa_sentinel.compare import CompareError, compare_reports, format_comparison
from qa_sentinel.trend import TrendError, build_trend, format_trend, trend_directory


def report(
    run_id: str,
    statuses: dict[str, str],
    *,
    suite_hash: str | None = "suite-a",
    environment: str = "local",
    environment_hash: str | None = "env-a",
    finished_at: str = "2026-08-30T00:00:00+00:00",
    latency_base: float = 100,
    attempts: int = 1,
) -> dict:
    tests = []
    for index, (case_id, status) in enumerate(statuses.items()):
        tests.append(
            {
                "id": case_id,
                "name": case_id.title(),
                "status": status,
                "passed": status == "passed",
                "latency_ms": latency_base + index * 100,
                "attempts": attempts,
                "finished_at": finished_at,
            }
        )
    return {
        "schema_version": 2,
        "run_id": run_id,
        "suite_schema_version": 2,
        "suite": "Example suite",
        "suite_config_hash": suite_hash,
        "environment": environment,
        "environment_config_hash": environment_hash,
        "started_at": finished_at,
        "finished_at": finished_at,
        "tests": tests,
    }


class CompareTests(unittest.TestCase):
    def test_classifies_changes_by_stable_id(self) -> None:
        baseline = report("old", {"new": "passed", "fixed": "failed", "kept": "error", "removed": "passed"})
        current = report("new", {"new": "failed", "fixed": "passed", "kept": "blocked", "added": "passed"})
        result = compare_reports(current, baseline)
        self.assertTrue(result["compatible"])
        self.assertEqual(result["summary"]["new_failures"], 1)
        self.assertEqual(result["summary"]["fixed"], 1)
        self.assertEqual(result["summary"]["persistent_failures"], 1)
        self.assertEqual(result["summary"]["added_tests"], 1)
        self.assertEqual(result["summary"]["removed_tests"], 1)
        classes = {item["id"]: item["classification"] for item in result["changes"]}
        self.assertEqual(classes, {
            "added": "added_test",
            "fixed": "fixed",
            "kept": "persistent_failure",
            "new": "new_failure",
            "removed": "removed_test",
        })

    def test_incompatible_hashes_withhold_failure_classification(self) -> None:
        baseline = report("old", {"health": "passed"}, suite_hash="suite-old")
        current = report("new", {"health": "failed"}, suite_hash="suite-new")
        result = compare_reports(current, baseline)
        self.assertFalse(result["compatible"])
        self.assertEqual(result["summary"]["new_failures"], 0)
        self.assertEqual(result["summary"]["incomparable"], 1)
        self.assertTrue(result["limitations"])
        self.assertIn("suite configuration hash", result["limitations"][0])

    def test_load_errors_identify_baseline_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "current.json"
            baseline = Path(directory) / "baseline.json"
            current.write_text(json.dumps(report("new", {"health": "passed"})), encoding="utf-8")
            baseline.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(CompareError, "Baseline report.*baseline.json"):
                from qa_sentinel.compare import compare_files

                compare_files(current, baseline)

    def test_text_comparison_is_actionable(self) -> None:
        result = compare_reports(report("new", {"health": "failed"}), report("old", {"health": "passed"}))
        text = format_comparison(result)
        self.assertIn("New failures: 1", text)
        self.assertIn("health", text)


class TrendTests(unittest.TestCase):
    def test_trend_calculates_success_retry_percentiles_and_failures(self) -> None:
        runs = [
            (Path("one.json"), report("one", {"health": "passed", "search": "failed"}, latency_base=100, finished_at="2026-08-28T00:00:00+00:00")),
            (Path("two.json"), report("two", {"health": "passed", "search": "passed"}, latency_base=200, attempts=2, finished_at="2026-08-29T00:00:00+00:00")),
            (Path("three.json"), report("three", {"health": "failed", "search": "passed"}, latency_base=300, finished_at="2026-08-30T00:00:00+00:00")),
        ]
        result = build_trend(runs)
        self.assertEqual(result["run_count"], 3)
        tests = {item["id"]: item for item in result["groups"][0]["tests"]}
        self.assertEqual(tests["health"]["success_rate"], 66.67)
        self.assertEqual(tests["health"]["retry_rate"], 33.33)
        self.assertEqual(tests["health"]["p50_latency_ms"], 200)
        self.assertEqual(tests["health"]["p95_latency_ms"], 290)
        self.assertEqual(tests["health"]["first_failure"], "2026-08-30T00:00:00+00:00")
        self.assertEqual(tests["health"]["last_failure"], "2026-08-30T00:00:00+00:00")
        self.assertEqual(tests["search"]["success_rate"], 66.67)
        self.assertIn("health", format_trend(result))

    def test_incompatible_runs_are_separate_groups(self) -> None:
        runs = [
            (Path("one.json"), report("one", {"health": "passed"}, suite_hash="old")),
            (Path("two.json"), report("two", {"health": "failed"}, suite_hash="new")),
        ]
        result = build_trend(runs)
        self.assertEqual(result["group_count"], 2)
        self.assertTrue(result["limitations"])

    def test_duplicate_run_id_and_empty_directory_are_clear(self) -> None:
        duplicate = report("same", {"health": "passed"})
        with self.assertRaisesRegex(TrendError, "Duplicate run_id"):
            build_trend([(Path("one.json"), duplicate), (Path("two.json"), duplicate)])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TrendError, "No JSON reports"):
                trend_directory(directory)

    def test_corrupt_report_identifies_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(TrendError, "broken.json"):
                trend_directory(directory)

    def test_cli_compare_and_trend_json_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current.json"
            baseline = root / "baseline.json"
            current.write_text(json.dumps(report("new", {"health": "passed"})), encoding="utf-8")
            baseline.write_text(json.dumps(report("old", {"health": "passed"})), encoding="utf-8")
            self.assertEqual(main(["compare", str(current), "--baseline", str(baseline), "--format", "json"]), 0)
            history = root / "history"
            history.mkdir()
            (history / "one.json").write_text(current.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(main(["trend", str(history), "--format", "json"]), 0)


if __name__ == "__main__":
    unittest.main()
