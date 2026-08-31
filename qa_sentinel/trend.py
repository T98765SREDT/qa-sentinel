"""Build deterministic, secret-free trend summaries from QA reports."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .compare import CompareError, load_report


class TrendError(ValueError):
    """Raised when a trend directory cannot be summarized safely."""


_FAILURE_STATUSES = frozenset({"failed", "error", "blocked"})


def _status(test: Mapping[str, Any]) -> str:
    status = str(test.get("status", "")).strip().casefold()
    if status:
        return status
    return "passed" if test.get("passed") is True else "failed"


def _test_id(test: Mapping[str, Any]) -> str:
    for key in ("id", "request_id", "name"):
        value = test.get(key)
        if value is not None and str(value).strip():
            return str(value)
    raise TrendError("Every trend test needs a non-empty id, request_id, or name")


def _tests_by_id(report: Mapping[str, Any], path: Path) -> dict[str, Mapping[str, Any]]:
    tests = report.get("tests", [])
    if not isinstance(tests, list):
        raise TrendError(f"Report '{path}' has a non-array 'tests' field")
    result: dict[str, Mapping[str, Any]] = {}
    for index, test in enumerate(tests):
        if not isinstance(test, Mapping):
            raise TrendError(f"Report '{path}' tests[{index}] must be an object")
        case_id = _test_id(test)
        if case_id in result:
            raise TrendError(f"Report '{path}' contains duplicate test id '{case_id}'")
        result[case_id] = test
    return result


def _fingerprint(report: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the fields that must match before aggregating a run."""

    return tuple(
        report.get(key)
        for key in (
            "suite",
            "suite_schema_version",
            "suite_config_hash",
            "environment",
            "environment_config_hash",
        )
    )


def _fingerprint_object(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "suite": report.get("suite"),
        "suite_schema_version": report.get("suite_schema_version"),
        "suite_config_hash": report.get("suite_config_hash"),
        "environment": report.get("environment"),
        "environment_config_hash": report.get("environment_config_hash"),
    }


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    numbers = sorted(float(value) for value in values)
    if not numbers:
        return None
    position = (len(numbers) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(numbers[lower], 3)
    fraction = position - lower
    return round(numbers[lower] + (numbers[upper] - numbers[lower]) * fraction, 3)


def _run_id(report: Mapping[str, Any], path: Path) -> str:
    value = report.get("run_id")
    if value is None or not str(value).strip():
        raise TrendError(f"Report '{path}' is missing a non-empty run_id")
    return str(value)


def _run_sort_key(item: tuple[Path, Mapping[str, Any]]) -> tuple[str, str]:
    path, report = item
    return (str(report.get("finished_at") or report.get("started_at") or ""), str(path))


def _test_summary(case_id: str, samples: list[tuple[Path, Mapping[str, Any], Mapping[str, Any]]]) -> dict[str, Any]:
    statuses = [_status(test) for _, _, test in samples]
    latencies = [
        float(test["latency_ms"])
        for _, _, test in samples
        if isinstance(test.get("latency_ms"), (int, float))
    ]
    retries = sum(
        int(test.get("attempts", 1)) > 1
        for _, _, test in samples
        if isinstance(test.get("attempts", 1), (int, float))
    )
    failures = [
        (str(report.get("finished_at") or report.get("started_at") or ""), _status(test))
        for _, report, test in samples
        if _status(test) in _FAILURE_STATUSES
    ]
    failures.sort(key=lambda item: item[0])
    name = next((str(test.get("name")) for _, _, test in samples if test.get("name")), case_id)
    run_count = len(samples)
    return {
        "id": case_id,
        "name": name,
        "run_count": run_count,
        "passed": statuses.count("passed"),
        "failed": sum(status in _FAILURE_STATUSES for status in statuses),
        "success_rate": round(statuses.count("passed") / run_count * 100, 2) if run_count else None,
        "retry_count": retries,
        "retry_rate": round(retries / run_count * 100, 2) if run_count else None,
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "first_failure": failures[0][0] if failures else None,
        "last_failure": failures[-1][0] if failures else None,
    }


def build_trend(reports: Iterable[tuple[Path, Mapping[str, Any]]]) -> dict[str, Any]:
    """Aggregate reports, keeping incompatible fingerprints in separate groups."""

    items = list(reports)
    if not items:
        raise TrendError("No JSON reports were found; provide a directory containing run artifacts")
    seen_run_ids: dict[str, Path] = {}
    groups: dict[tuple[Any, ...], list[tuple[Path, Mapping[str, Any]]]] = {}
    for path, report in items:
        run_id = _run_id(report, path)
        previous = seen_run_ids.get(run_id)
        if previous is not None:
            raise TrendError(
                f"Duplicate run_id '{run_id}' in '{previous}' and '{path}'; "
                "remove one artifact before building a trend"
            )
        seen_run_ids[run_id] = path
        groups.setdefault(_fingerprint(report), []).append((path, report))

    group_data: list[dict[str, Any]] = []
    for fingerprint, group_items in sorted(groups.items(), key=lambda item: repr(item[0])):
        ordered = sorted(group_items, key=_run_sort_key)
        tests_by_run = [(_path, report, _tests_by_id(report, _path)) for _path, report in ordered]
        case_ids = sorted({case_id for _, _, tests in tests_by_run for case_id in tests})
        tests = []
        for case_id in case_ids:
            samples = [
                (path, report, tests_by_id[case_id])
                for path, report, tests_by_id in tests_by_run
                if case_id in tests_by_id
            ]
            tests.append(_test_summary(case_id, samples))
        group_data.append(
            {
                "fingerprint": _fingerprint_object(ordered[0][1]),
                "run_count": len(ordered),
                "run_ids": [str(report.get("run_id")) for _, report in ordered],
                "tests": tests,
            }
        )

    limitations: list[str] = []
    if len(group_data) > 1:
        limitations.append(
            "Runs with different suite/configuration/environment fingerprints were kept in separate groups; "
            "their success and latency metrics were not combined."
        )
    return {
        "schema_version": 1,
        "run_count": len(items),
        "group_count": len(group_data),
        "groups": group_data,
        "limitations": limitations,
    }


def trend_directory(directory: str | Path) -> dict[str, Any]:
    """Load all JSON reports in a directory and build a trend summary."""

    directory_path = Path(directory)
    if not directory_path.exists():
        raise TrendError(f"Trend directory '{directory_path}' does not exist")
    if not directory_path.is_dir():
        raise TrendError(f"Trend path '{directory_path}' is not a directory")
    paths = sorted(path for path in directory_path.iterdir() if path.is_file() and path.suffix.lower() == ".json")
    if not paths:
        raise TrendError(f"No JSON reports were found in '{directory_path}'")
    reports: list[tuple[Path, Mapping[str, Any]]] = []
    for path in paths:
        try:
            reports.append((path, load_report(path)))
        except CompareError as exc:
            raise TrendError(str(exc)) from exc
    result = build_trend(reports)
    result["directory"] = str(directory_path)
    return result


def format_trend(result: Mapping[str, Any]) -> str:
    """Render a concise deterministic trend summary for terminal/CI logs."""

    lines = [
        f"Trend summary: {result.get('run_count', 0)} run(s) · "
        f"{result.get('group_count', 0)} compatible group(s)"
    ]
    for index, group in enumerate(result.get("groups", []), start=1):
        fingerprint = group.get("fingerprint", {})
        label = fingerprint.get("suite") or "unnamed suite"
        environment = fingerprint.get("environment")
        if environment:
            label = f"{label} [{environment}]"
        lines.append(f"Group {index}: {label} · runs={group.get('run_count', 0)}")
        for test in group.get("tests", []):
            lines.append(
                f"  {test.get('id')}: success={test.get('success_rate')}% · "
                f"retry={test.get('retry_rate')}% · "
                f"p50={test.get('p50_latency_ms', '—')}ms · "
                f"p95={test.get('p95_latency_ms', '—')}ms"
            )
            if test.get("first_failure"):
                lines.append(
                    f"    failures: {test.get('first_failure')} → {test.get('last_failure')}"
                )
    for limitation in result.get("limitations", []):
        lines.append(f"Limitation: {limitation}")
    return "\n".join(lines)
