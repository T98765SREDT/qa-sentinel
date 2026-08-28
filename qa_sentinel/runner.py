"""Parallel suite orchestration."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .assertions import evaluate_assertions
from .http_client import HttpClient
from .models import HttpResponse, SuiteResult, TestCase, TestResult, TestSuite


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SuiteRunner:
    """Run suites concurrently while retaining declaration order in reports."""

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()

    def _run_case(self, case: TestCase) -> TestResult:
        started_at = _timestamp()
        try:
            response = self.client.execute(case)
            assertions = evaluate_assertions(case.assertions, response)
        except Exception as exc:  # Protect the rest of a parallel suite from one bad test.
            response = HttpResponse(None, {}, b"", 0.0, 1, f"{type(exc).__name__}: {exc}")
            assertions = evaluate_assertions(case.assertions, response)
        return TestResult(
            case=case,
            passed=all(assertion.passed for assertion in assertions),
            response=response,
            assertions=assertions,
            started_at=started_at,
            finished_at=_timestamp(),
        )

    def run(self, suite: TestSuite, workers: int | None = None) -> SuiteResult:
        """Execute a suite with at most *workers* concurrent requests."""

        worker_count = workers if workers is not None else suite.workers
        if not 1 <= worker_count <= 64:
            raise ValueError("workers must be between 1 and 64")
        started_at = _timestamp()
        start = time.perf_counter()
        ordered: list[TestResult | None] = [None] * len(suite.tests)
        with ThreadPoolExecutor(max_workers=min(worker_count, len(suite.tests))) as executor:
            pending = {
                executor.submit(self._run_case, case): index
                for index, case in enumerate(suite.tests)
            }
            for future in as_completed(pending):
                ordered[pending[future]] = future.result()
        duration_ms = (time.perf_counter() - start) * 1000
        return SuiteResult(
            suite_name=suite.name,
            tests=tuple(result for result in ordered if result is not None),
            started_at=started_at,
            finished_at=_timestamp(),
            duration_ms=duration_ms,
            description=suite.description,
            environment=suite.environment,
        )
