"""Domain models shared across QA Sentinel modules."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from typing import Any, Mapping


IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE"})
MAX_RETRIES = 5
MAX_RETRY_DELAY_SECONDS = 30.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_SLOW_THRESHOLD_MS = 500.0


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
    max_response_bytes: int = MAX_RESPONSE_BYTES
    depends_on: tuple[str, ...] = ()
    run_if: str = "success"
    extract: Mapping[str, Any] = field(default_factory=dict)
    cleanup: bool = False


@dataclass(frozen=True)
class TestSuite:
    """A collection of tests and execution metadata."""

    name: str
    tests: tuple[TestCase, ...]
    workers: int = 4
    known_secrets: tuple[str, ...] = ()
    description: str = ""
    environment: str = ""
    slow_threshold_ms: float = DEFAULT_SLOW_THRESHOLD_MS
    environment_config_hash: str = ""
    config_hash: str = ""
    secret_sources: tuple["SecretProvenance", ...] = ()
    schema_version: int = 2
    selected_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecretProvenance:
    """Public metadata for a secret without carrying its value into reports."""

    name: str
    source: str
    # The value is deliberately internal to configuration/transport. Reporters
    # must use ``name`` and ``source`` only.
    value: str | None = None


@dataclass(frozen=True)
class HttpResponse:
    """Normalized transport result, including network failures."""

    status: int | None
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: float
    attempts: int
    error: str | None = None
    retryable_error: bool = False

    @property
    def text(self) -> str:
        """Decode using the response's declared charset when available."""
        content_type = next(
            (value for key, value in self.headers.items() if key.lower() == "content-type"),
            "",
        )
        match = re.search(
            r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"'\s]+)",
            content_type,
            re.IGNORECASE,
        )
        encoding = "utf-8"
        if match:
            try:
                encoding = codecs.lookup(match.group(1).strip()).name
            except LookupError:
                encoding = "utf-8"
        return self.body.decode(encoding, errors="replace")


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
    status: str = ""

    def __post_init__(self) -> None:
        if self.status:
            return
        inferred = "error" if self.response.error else "passed" if self.passed else "failed"
        object.__setattr__(self, "status", inferred)


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
    slow_threshold_ms: float = DEFAULT_SLOW_THRESHOLD_MS
    environment_config_hash: str = ""
    suite_config_hash: str = ""
    secret_sources: tuple[SecretProvenance, ...] = ()
    schema_version: int = 2
    known_secrets: tuple[str, ...] = ()
    capture_metadata: tuple[Mapping[str, Any], ...] = ()
    interrupted: bool = False
    run_id: str = ""
    tool_version: str = ""
    git_sha: str = ""
    git_branch: str = ""
    ci_url: str = ""
    selected_tags: tuple[str, ...] = ()
    worker_count: int = 0
    retry_settings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.tests)

    @property
    def passed(self) -> int:
        return sum(result.status == "passed" for result in self.tests)

    @property
    def failed(self) -> int:
        return sum(result.status == "failed" for result in self.tests)

    @property
    def errors(self) -> int:
        return sum(result.status == "error" for result in self.tests)

    @property
    def blocked(self) -> int:
        return sum(result.status == "blocked" for result in self.tests)

    @property
    def skipped(self) -> int:
        return sum(result.status == "skipped" for result in self.tests)

    @property
    def success_rate(self) -> float:
        return 100.0 if not self.total else (self.passed / self.total) * 100.0

    @property
    def is_successful(self) -> bool:
        return self.total == self.passed
