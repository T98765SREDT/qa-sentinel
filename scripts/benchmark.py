#!/usr/bin/env python3
"""Measure QA Sentinel locally without contacting an external service.

The benchmark is intentionally a small evidence tool, not a load generator. It
uses the deterministic demo API, records the machine/runtime, and writes a
machine-readable result that can be reviewed alongside the source revision.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import statistics
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from qa_sentinel.config import load_suite  # noqa: E402
from qa_sentinel.demo_api import create_demo_server  # noqa: E402
from qa_sentinel.reporting import write_html_report, write_json_report, write_junit_report  # noqa: E402
from qa_sentinel.runner import SuiteRunner  # noqa: E402
from qa_sentinel.trend import trend_directory  # noqa: E402


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int((len(ordered) * percentile) + 0.9999) - 1))
    return ordered[index]


def _suite(base_url: str, count: int, *, workflow: bool = False) -> object:
    tests = []
    for index in range(count):
        case = {
            "id": f"health-{index + 1:03d}",
            "name": f"Health check {index + 1:03d}",
            "url": f"{base_url}/health",
            "tags": ["benchmark", "workflow" if workflow else "independent"],
            "assertions": [
                {"type": "status", "equals": 200},
                {"type": "json_path", "path": "status", "equals": "ok"},
            ],
        }
        if workflow:
            dependencies = {
                1: ["health-001"],
                2: ["health-001"],
                3: ["health-002", "health-003"],
                4: ["health-004"],
                5: ["health-004"],
            }
            if index in dependencies:
                case["depends_on"] = dependencies[index]
        tests.append(case)
    payload = {
        "name": "QA Sentinel local benchmark",
        "description": "Synthetic localhost benchmark; no external traffic.",
        "workers": 4,
        "tests": tests,
    }
    if workflow:
        payload["schemaVersion"] = 2
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        path = Path(handle.name)
    try:
        return load_suite(path)
    finally:
        path.unlink(missing_ok=True)


def _run_timed(suite: object, workers: int) -> tuple[float, object]:
    started = time.perf_counter()
    result = SuiteRunner().run(suite, workers=workers)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not result.is_successful:
        raise RuntimeError(f"benchmark run failed for workers={workers}: {result}")
    return elapsed_ms, result


def _write_result(path: Path | None, payload: dict[str, object]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if path is None:
        print(text, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    print(f"Benchmark written to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3, help="timed repetitions per worker count")
    parser.add_argument("--requests", type=int, default=100, help="independent requests per repetition")
    parser.add_argument("--output", type=Path, help="optional JSON result path")
    args = parser.parse_args()
    if args.repetitions < 1 or args.repetitions > 20:
        parser.error("--repetitions must be between 1 and 20")
    if args.requests < 1 or args.requests > 1000:
        parser.error("--requests must be between 1 and 1000")

    server = create_demo_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        independent = _suite(base_url, args.requests)
        timings: dict[str, dict[str, object]] = {}
        for workers in (1, 4, 16):
            samples: list[float] = []
            for _ in range(args.repetitions):
                elapsed_ms, _ = _run_timed(independent, workers)
                samples.append(elapsed_ms)
            timings[str(workers)] = {
                "requests": args.requests,
                "repetitions": args.repetitions,
                "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(_percentile(samples, 0.95), 3),
                "samples_ms": [round(value, 3) for value in samples],
            }

        workflow = _suite(base_url, 6, workflow=True)
        workflow_ms, workflow_result = _run_timed(workflow, 4)
        with tempfile.TemporaryDirectory(prefix="qa-sentinel-benchmark-") as directory:
            root = Path(directory)
            write_html_report(workflow_result, root / "workflow.html")
            write_json_report(workflow_result, root / "workflow.json")
            write_junit_report(workflow_result, root / "workflow.xml")
            history = root / "history"
            history.mkdir()
            write_json_report(workflow_result, history / "run-1.json")
            write_json_report(
                replace(workflow_result, run_id=f"{workflow_result.run_id}-repeat"),
                history / "run-2.json",
            )
            trend = trend_directory(history)

        payload: dict[str, object] = {
            "benchmark": "qa-sentinel-local",
            "generated_at_epoch": time.time(),
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "pid": os.getpid(),
            "fixture": {
                "host": "127.0.0.1",
                "requests_per_repetition": args.requests,
                "workflow_steps": 6,
                "external_traffic": False,
            },
            "independent_requests": timings,
            "workflow": {
                "workers": 4,
                "elapsed_ms": round(workflow_ms, 3),
                "passed": workflow_result.passed,
                "trend_run_count": trend["run_count"],
            },
            "limitations": [
                "Synthetic localhost requests only; this is not a load or scalability claim.",
                "Results vary with operating system, Python version, and machine load.",
            ],
        }
        _write_result(args.output, payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
