"""Parallel suite orchestration."""

from __future__ import annotations

import time
import os
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from dataclasses import replace

from .assertions import evaluate_assertions
from .capture import CaptureError, CaptureStore, MissingCaptureError, capture_response
from .http_client import HttpClient
from .models import AssertionResult, HttpResponse, SuiteResult, TestCase, TestResult, TestSuite
from .planner import plan_suite


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ci_metadata() -> tuple[str, str, str]:
    """Read explicitly provided CI provenance without invoking git or the shell."""

    git_sha = os.environ.get("GITHUB_SHA") or os.environ.get("CI_COMMIT_SHA", "")
    git_branch = os.environ.get("GITHUB_REF_NAME") or os.environ.get(
        "CI_COMMIT_REF_NAME", ""
    )
    ci_url = (
        os.environ.get("GITHUB_RUN_URL")
        or os.environ.get("CI_JOB_URL")
        or os.environ.get("BUILD_URL", "")
    )
    return git_sha.strip(), git_branch.strip(), ci_url.strip()


class SuiteRunner:
    """Run suites concurrently while retaining declaration order in reports."""

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()

    def _run_case(
        self, case: TestCase, captures: CaptureStore | None = None
    ) -> TestResult:
        started_at = _timestamp()
        try:
            response = (
                self.client.execute(case, captures)
                if captures is not None
                else self.client.execute(case)
            )
            assertions = evaluate_assertions(case.assertions, response)
            status = "passed" if all(assertion.passed for assertion in assertions) else "failed"
        except MissingCaptureError as exc:
            response = HttpResponse(None, {}, b"", 0.0, 0, str(exc))
            assertions = (AssertionResult("capture", False, str(exc)),)
            status = "blocked"
        except Exception as exc:  # Protect the rest of a parallel suite from one bad test.
            response = HttpResponse(None, {}, b"", 0.0, 1, f"{type(exc).__name__}: {exc}")
            assertions = evaluate_assertions(case.assertions, response)
            status = "error"
        return TestResult(
            case=case,
            passed=all(assertion.passed for assertion in assertions),
            response=response,
            assertions=assertions,
            started_at=started_at,
            finished_at=_timestamp(),
            status=status,
        )

    @staticmethod
    def _state_result(case: TestCase, status: str, reason: str) -> TestResult:
        now = _timestamp()
        return TestResult(
            case=case,
            passed=False,
            response=HttpResponse(None, {}, b"", 0.0, 0, reason),
            assertions=(AssertionResult("workflow", False, reason),),
            started_at=now,
            finished_at=now,
            status=status,
        )

    @staticmethod
    def _capture_result(
        result: TestResult, captures: CaptureStore
    ) -> tuple[TestResult, CaptureStore]:
        if not result.case.extract or result.response.error:
            return result, captures
        try:
            return result, capture_response(result.case, result.response, captures)
        except CaptureError as exc:
            response = HttpResponse(
                result.response.status,
                result.response.headers,
                result.response.body,
                result.response.elapsed_ms,
                result.response.attempts,
                str(exc),
                result.response.retryable_error,
            )
            assertion = AssertionResult("capture", False, str(exc))
            return (
                replace(
                    result,
                    passed=False,
                    response=response,
                    assertions=result.assertions + (assertion,),
                    status="error",
                ),
                captures,
            )

    def _run_independent(
        self, suite: TestSuite, worker_count: int
    ) -> tuple[tuple[TestResult, ...], CaptureStore, bool]:
        ordered: list[TestResult | None] = [None] * len(suite.tests)
        captures = CaptureStore()
        interrupted = False
        executor = ThreadPoolExecutor(max_workers=min(worker_count, len(suite.tests)))
        pending: dict[Future[TestResult], int] = {}
        try:
            pending = {
                executor.submit(self._run_case, case): index
                for index, case in enumerate(suite.tests)
            }
            for future in as_completed(pending):
                ordered[pending[future]] = future.result()
        except KeyboardInterrupt:
            interrupted = True
            for future in pending:
                future.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if interrupted:
            for index, future in enumerate(pending):
                if ordered[index] is not None or future.cancelled() or not future.done():
                    continue
                try:
                    ordered[index] = future.result()
                except BaseException:
                    pass
            for index, case in enumerate(suite.tests):
                if ordered[index] is None:
                    ordered[index] = self._state_result(
                        case, "skipped", "skipped because the run was interrupted"
                    )
        return tuple(result for result in ordered if result is not None), captures, interrupted

    def _run_workflow(
        self,
        suite: TestSuite,
        worker_count: int,
        *,
        fail_fast: bool = False,
        max_failures: int | None = None,
    ) -> tuple[tuple[TestResult, ...], CaptureStore, bool]:
        plan = plan_suite(suite)
        results: dict[str, TestResult] = {}
        captures = CaptureStore()
        stop_ordinary = False
        failure_count = 0
        interrupted = False
        executor = ThreadPoolExecutor(max_workers=worker_count)
        try:
            for layer in plan.layers:
                futures: dict[Future[TestResult], TestCase] = {}
                immediate: list[TestResult] = []
                for step_id in layer.step_ids:
                    case = plan.by_id[step_id]
                    dependencies = [results[dependency] for dependency in case.depends_on]
                    if case.run_if == "success" and any(
                        dependency.status != "passed" for dependency in dependencies
                    ):
                        failed_ids = ", ".join(
                            dependency.case.case_id
                            for dependency in dependencies
                            if dependency.status != "passed"
                        )
                        immediate.append(
                            self._state_result(
                                case,
                                "blocked",
                                f"blocked by unsuccessful dependency: {failed_ids}",
                            )
                        )
                    elif stop_ordinary and case.run_if != "always":
                        immediate.append(
                            self._state_result(
                                case,
                                "skipped",
                                "skipped because the failure limit was reached",
                            )
                        )
                    else:
                        futures[executor.submit(self._run_case, case, captures)] = case

                layer_results: dict[str, TestResult] = {
                    result.case.case_id: result for result in immediate
                }
                for future in as_completed(futures):
                    result = future.result()
                    result, captures = self._capture_result(result, captures)
                    layer_results[result.case.case_id] = result
                for step_id in layer.step_ids:
                    result = layer_results[step_id]
                    results[step_id] = result
                    if result.status in {"failed", "error"}:
                        failure_count += 1
                if fail_fast and any(
                    results[step_id].status in {"failed", "error"}
                    for step_id in layer.step_ids
                ):
                    stop_ordinary = True
                if max_failures is not None and failure_count >= max_failures:
                    stop_ordinary = True
        except KeyboardInterrupt:
            interrupted = True
            for future in locals().get("futures", {}):
                future.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if interrupted:
            for case in suite.tests:
                if case.case_id not in results:
                    results[case.case_id] = self._state_result(
                        case, "skipped", "skipped because the run was interrupted"
                    )
        return tuple(results[case.case_id] for case in suite.tests), captures, interrupted

    def run(
        self,
        suite: TestSuite,
        workers: int | None = None,
        *,
        fail_fast: bool = False,
        max_failures: int | None = None,
    ) -> SuiteResult:
        """Execute a suite with at most *workers* concurrent requests."""

        plan = plan_suite(suite)
        worker_count = workers if workers is not None else suite.workers
        if not 1 <= worker_count <= 64:
            raise ValueError("workers must be between 1 and 64")
        if max_failures is not None and max_failures < 1:
            raise ValueError("max_failures must be at least 1")
        started_at = _timestamp()
        start = time.perf_counter()
        workflow = any(
            step.depends_on or step.extract or step.cleanup or step.run_if != "success"
            for step in plan.steps
        )
        if workflow:
            ordered, captures, interrupted = self._run_workflow(
                suite,
                worker_count,
                fail_fast=fail_fast,
                max_failures=max_failures,
            )
        else:
            ordered, captures, interrupted = self._run_independent(suite, worker_count)
        duration_ms = (time.perf_counter() - start) * 1000
        git_sha, git_branch, ci_url = _ci_metadata()
        retry_statuses = sorted(
            {status for case in suite.tests for status in case.retry_on_status}
        )
        retry_settings = {
            "max_retries": max((case.retries for case in suite.tests), default=0),
            "retry_on_status": retry_statuses,
            "retry_non_idempotent": any(
                case.retry_non_idempotent for case in suite.tests
            ),
        }
        from . import __version__

        return SuiteResult(
            suite_name=suite.name,
            tests=ordered,
            started_at=started_at,
            finished_at=_timestamp(),
            duration_ms=duration_ms,
            description=suite.description,
            environment=suite.environment,
            slow_threshold_ms=suite.slow_threshold_ms,
            environment_config_hash=suite.environment_config_hash,
            suite_config_hash=suite.config_hash,
            secret_sources=suite.secret_sources,
            schema_version=suite.schema_version,
            known_secrets=tuple(
                sorted(set(suite.known_secrets).union(captures.secret_values), key=len, reverse=True)
            ),
            capture_metadata=captures.public_metadata(),
            interrupted=interrupted,
            run_id=uuid.uuid4().hex,
            tool_version=__version__,
            git_sha=git_sha,
            git_branch=git_branch,
            ci_url=ci_url,
            selected_tags=suite.selected_tags,
            worker_count=worker_count,
            retry_settings=retry_settings,
        )
