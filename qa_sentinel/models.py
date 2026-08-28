"""Domain models shared across QA Sentinel modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE"})
MAX_RETRIES = 5
MAX_RETRY_DELAY_SECONDS = 30.0


@dataclass(frozen=True)
class AssertionSpec:
    """A single declarative response assertion."""

    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TestCase:
    """A fully resolved HTTP test case."""

    case_id: str
    name: str
    method: str
    url: str
    headers: Mapping[str, str]
    body: Any
    timeout_seconds: float
    retries: int
    retry_delay_seconds: float
    retry_on_status: tuple[int, ...]
    retry_non_idempotent: bool
    assertions: tuple[AssertionSpec, ...]
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestSuite:
    """A collection of tests and execution metadata."""

    name: str
    tests: tuple[TestCase, ...]
    workers: int = 4
    known_secrets: tuple[str, ...] = ()
    description: str = ""
    environment: str = ""


@dataclass(frozen=True)
class HttpResponse:
    """Normalized transport result, including network failures."""

    status: int | None
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: float
    attempts: int
    error: str | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class AssertionResult:
    """Outcome of one assertion."""

    kind: str
    passed: bool
    message: str
    expected: Any = None
    actual: Any = None


@dataclass(frozen=True)
class TestResult:
    """Outcome and diagnostics for one test case."""

    case: TestCase
    passed: bool
    response: HttpResponse
    assertions: tuple[AssertionResult, ...]
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class SuiteResult:
    """Aggregate result for a full suite run."""

    suite_name: str
    tests: tuple[TestResult, ...]
    started_at: str
    finished_at: str
    duration_ms: float
    description: str = ""
    environment: str = ""

    @property
    def total(self) -> int:
        return len(self.tests)

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.tests)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def success_rate(self) -> float:
        return 100.0 if not self.total else (self.passed / self.total) * 100.0

    @property
    def is_successful(self) -> bool:
        return self.failed == 0
