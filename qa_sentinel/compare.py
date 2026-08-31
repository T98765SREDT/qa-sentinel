"""Compare two QA Sentinel JSON reports without loading sensitive bodies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


class CompareError(ValueError):
    """Raised when a report cannot be safely compared."""


_FAILURE_STATUSES = frozenset({"failed", "error", "blocked"})
_SUCCESS_STATUSES = frozenset({"passed"})
_COMPATIBILITY_FIELDS = (
    ("suite", "suite name"),
    ("suite_schema_version", "suite schema version"),
    ("suite_config_hash", "suite configuration hash"),
    ("environment", "environment"),
    ("environment_config_hash", "environment configuration hash"),
)


def load_report(path: str | Path) -> dict[str, Any]:
    """Load one JSON report and return a redacted-safe structural mapping.

    The compare/trend tools intentionally inspect only metadata and test
    outcomes.  They never copy response bodies or assertion values into their
    summaries.
    """

    report_path = Path(path)
    try:
        raw = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompareError(f"Unable to read report '{report_path}': {exc}") from exc
    try:
        report = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CompareError(f"Report '{report_path}' is not valid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise CompareError(f"Report '{report_path}' must contain a JSON object")
    tests = report.get("tests", [])
    if not isinstance(tests, list):
        raise CompareError(f"Report '{report_path}' has a non-array 'tests' field")
    return report


def _status(test: Mapping[str, Any]) -> str:
    status = str(test.get("status", "")).strip().casefold()
    if status:
        return status
    passed = test.get("passed")
    if passed is True:
        return "passed"
    if passed is False:
        return "failed"
    return "error"


def _test_id(test: Mapping[str, Any]) -> str:
    for key in ("id", "request_id", "name"):
        value = test.get(key)
        if value is not None and str(value).strip():
            return str(value)
    raise CompareError("Every report test needs a non-empty id, request_id, or name")


def _tests_by_id(report: Mapping[str, Any], label: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    tests = report.get("tests", [])
    if not isinstance(tests, list):
        raise CompareError(f"{label} report has a non-array 'tests' field")
    for index, test in enumerate(tests):
        if not isinstance(test, Mapping):
            raise CompareError(f"{label} report tests[{index}] must be an object")
        case_id = _test_id(test)
        if case_id in result:
            raise CompareError(f"{label} report contains duplicate test id '{case_id}'")
        result[case_id] = test
    return result


def _compatibility(current: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    for key, label in _COMPATIBILITY_FIELDS:
        current_value = current.get(key)
        baseline_value = baseline.get(key)
        if current_value != baseline_value:
            reasons.append(
                f"{label} differs ({baseline_value!r} vs {current_value!r})"
            )
    return {"compatible": not reasons, "reasons": reasons}


def _is_failure(status: str) -> bool:
    return status in _FAILURE_STATUSES


def compare_reports(current: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Compare reports by stable test ID.

    Structural additions/removals remain visible even when the suite or
    environment fingerprints are incompatible.  Failure/fixed/persistent
    classifications are intentionally withheld in that case because a direct
    success comparison could be misleading.
    """

    if not isinstance(current, Mapping) or not isinstance(baseline, Mapping):
        raise CompareError("Both current and baseline reports must be JSON objects")
    compatibility = _compatibility(current, baseline)
    current_tests = _tests_by_id(current, "Current")
    baseline_tests = _tests_by_id(baseline, "Baseline")
    all_ids = sorted(set(current_tests) | set(baseline_tests))
    changes: list[dict[str, Any]] = []
    counts = {
        "new_failures": 0,
        "fixed": 0,
        "persistent_failures": 0,
        "added_tests": 0,
        "removed_tests": 0,
        "incomparable": 0,
    }

    for case_id in all_ids:
        current_test = current_tests.get(case_id)
        baseline_test = baseline_tests.get(case_id)
        if baseline_test is None:
            current_status = _status(current_test or {})
            changes.append(
                {
                    "id": case_id,
                    "name": str((current_test or {}).get("name", case_id)),
                    "classification": "added_test",
                    "current_status": current_status,
                    "current_failure": _is_failure(current_status),
                }
            )
            counts["added_tests"] += 1
            continue
        if current_test is None:
            baseline_status = _status(baseline_test)
            changes.append(
                {
                    "id": case_id,
                    "name": str(baseline_test.get("name", case_id)),
                    "classification": "removed_test",
                    "baseline_status": baseline_status,
                    "baseline_failure": _is_failure(baseline_status),
                }
            )
            counts["removed_tests"] += 1
            continue

        current_status = _status(current_test)
        baseline_status = _status(baseline_test)
        current_failure = _is_failure(current_status)
        baseline_failure = _is_failure(baseline_status)
        if not compatibility["compatible"]:
            changes.append(
                {
                    "id": case_id,
                    "name": str(current_test.get("name", case_id)),
                    "classification": "incomparable",
                    "baseline_status": baseline_status,
                    "current_status": current_status,
                }
            )
            counts["incomparable"] += 1
            continue
        classification: str | None = None
        if current_failure and not baseline_failure:
            classification = "new_failure"
            counts["new_failures"] += 1
        elif not current_failure and baseline_failure and current_status in _SUCCESS_STATUSES:
            classification = "fixed"
            counts["fixed"] += 1
        elif current_failure and baseline_failure:
            classification = "persistent_failure"
            counts["persistent_failures"] += 1
        if classification:
            changes.append(
                {
                    "id": case_id,
                    "name": str(current_test.get("name", case_id)),
                    "classification": classification,
                    "baseline_status": baseline_status,
                    "current_status": current_status,
                }
            )

    return {
        "schema_version": 1,
        "compatible": compatibility["compatible"],
        "compatibility": compatibility,
        "current_run_id": current.get("run_id"),
        "baseline_run_id": baseline.get("run_id"),
        "current_report": current.get("suite"),
        "baseline_report": baseline.get("suite"),
        "summary": counts,
        "changes": changes,
        "limitations": list(compatibility["reasons"])
        if not compatibility["compatible"]
        else [],
    }


def compare_files(current_path: str | Path, baseline_path: str | Path) -> dict[str, Any]:
    """Load and compare two report paths."""

    try:
        current = load_report(current_path)
    except CompareError as exc:
        raise CompareError(f"Current report: {exc}") from exc
    try:
        baseline = load_report(baseline_path)
    except CompareError as exc:
        raise CompareError(f"Baseline report: {exc}") from exc
    return compare_reports(current, baseline)


def format_comparison(result: Mapping[str, Any]) -> str:
    """Render a concise human-readable comparison for CI logs."""

    compatibility = result.get("compatibility", {})
    state = "compatible" if compatibility.get("compatible") else "INCOMPATIBLE"
    summary = result.get("summary", {})
    lines = [
        f"Baseline comparison: {state}",
        f"New failures: {summary.get('new_failures', 0)} · "
        f"Fixed: {summary.get('fixed', 0)} · "
        f"Persistent failures: {summary.get('persistent_failures', 0)}",
        f"Added tests: {summary.get('added_tests', 0)} · "
        f"Removed tests: {summary.get('removed_tests', 0)} · "
        f"Incomparable: {summary.get('incomparable', 0)}",
    ]
    for change in result.get("changes", []):
        classification = str(change.get("classification", "change")).replace("_", " ")
        lines.append(
            f"  [{classification}] {change.get('id')}: "
            f"{change.get('baseline_status', '—')} → {change.get('current_status', '—')}"
        )
    for limitation in result.get("limitations", []):
        lines.append(f"Limitation: {limitation}")
    return "\n".join(lines)


def has_regressions(result: Mapping[str, Any]) -> bool:
    """Return whether a compatible comparison contains a new failure."""

    summary = result.get("summary", {})
    return bool(result.get("compatible") and summary.get("new_failures", 0))
