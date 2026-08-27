"""JSON suite loading, interpolation, validation, and normalization."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .models import (
    MAX_RETRIES,
    MAX_RETRY_DELAY_SECONDS,
    AssertionSpec,
    TestCase,
    TestSuite,
)


class ConfigError(ValueError):
    """Raised when a suite configuration is invalid."""


_VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRET_KEY_PATTERN = re.compile(
    r"authorization|password|passwd|secret|token|api[-_]?key|cookie|credential|private[-_]?key",
    re.IGNORECASE,
)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "test"


def _expect_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a JSON object")
    return value


def _interpolate_string(
    value: str, variables: Mapping[str, Any], resolved_secrets: set[str] | None = None
) -> str:
    def replace_variable(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise ConfigError(f"Unknown variable '{{{{{key}}}}}'")
        return str(variables[key])

    def replace_environment(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in os.environ:
            raise ConfigError(f"Environment variable '{key}' is not set")
        resolved = os.environ[key]
        if (
            resolved_secrets is not None
            and _SECRET_KEY_PATTERN.search(key)
            and len(resolved) >= 4
        ):
            resolved_secrets.add(resolved)
        return resolved

    return _ENV_PATTERN.sub(replace_environment, _VARIABLE_PATTERN.sub(replace_variable, value))


def _interpolate(
    value: Any, variables: Mapping[str, Any], resolved_secrets: set[str] | None = None
) -> Any:
    if isinstance(value, str):
        return _interpolate_string(value, variables, resolved_secrets)
    if isinstance(value, list):
        return [_interpolate(item, variables, resolved_secrets) for item in value]
    if isinstance(value, dict):
        return {
            key: _interpolate(item, variables, resolved_secrets)
            for key, item in value.items()
        }
    return value


def _positive_number(value: Any, label: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a number")
    number = float(value)
    if number < 0 or (number == 0 and not allow_zero):
        comparator = "non-negative" if allow_zero else "greater than zero"
        raise ConfigError(f"{label} must be {comparator}")
    return number


def _non_negative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{label} must be a non-negative integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean")
    return value


def _headers(value: Any, label: str) -> dict[str, str]:
    mapping = _expect_mapping(value, label)
    result: dict[str, str] = {}
    for key, item in mapping.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ConfigError(f"{label} keys and values must be strings")
        result[key] = item
    return result


def _validate_assertion(spec: Mapping[str, Any], label: str) -> None:
    kind = spec["type"].strip().lower()
    if kind == "status":
        has_equals = "equals" in spec
        has_in = "in" in spec
        if has_equals == has_in:
            raise ConfigError(f"{label} must define exactly one of 'equals' or 'in'")
        statuses = [spec["equals"]] if has_equals else spec["in"]
        if not isinstance(statuses, list) or not statuses or not all(
            isinstance(status, int)
            and not isinstance(status, bool)
            and 100 <= status <= 599
            for status in statuses
        ):
            raise ConfigError(f"{label} status values must be HTTP integers from 100 to 599")
    elif kind in {"json", "json_path"}:
        if not isinstance(spec.get("path"), str) or not spec["path"].strip():
            raise ConfigError(f"{label}.path must be a non-empty string")
        if "exists" in spec and not isinstance(spec["exists"], bool):
            raise ConfigError(f"{label}.exists must be a boolean")
    elif kind == "latency":
        _positive_number(spec.get("max_ms"), f"{label}.max_ms", allow_zero=True)
    elif kind == "header":
        if not isinstance(spec.get("name"), str) or not spec["name"].strip():
            raise ConfigError(f"{label}.name must be a non-empty string")
        if "equals" in spec and not isinstance(spec["equals"], str):
            raise ConfigError(f"{label}.equals must be a string")
    elif kind == "body_contains":
        candidates = [key for key in ("value", "contains") if key in spec]
        if len(candidates) != 1 or not isinstance(spec[candidates[0]], str):
            raise ConfigError(f"{label} must define one string 'value' or 'contains'")
    else:
        raise ConfigError(f"{label}.type has unsupported assertion type '{kind}'")


def _assertions(value: Any, label: str) -> tuple[AssertionSpec, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label} must be a non-empty array")
    assertions: list[AssertionSpec] = []
    for index, raw in enumerate(value):
        spec = _expect_mapping(raw, f"{label}[{index}]")
        kind = spec.get("type")
        if not isinstance(kind, str) or not kind.strip():
            raise ConfigError(f"{label}[{index}].type must be a non-empty string")
        _validate_assertion(spec, f"{label}[{index}]")
        assertions.append(
            AssertionSpec(kind=kind.strip().lower(), params={k: v for k, v in spec.items() if k != "type"})
        )
    return tuple(assertions)


def _collect_secrets(
    variables: Mapping[str, Any],
    defaults: Mapping[str, Any],
    tests: list[Any],
    resolved_secrets: set[str] | None = None,
) -> tuple[str, ...]:
    secrets: set[str] = set(resolved_secrets or ())
    for key, value in variables.items():
        if _SECRET_KEY_PATTERN.search(key) and isinstance(value, str) and len(value) >= 4:
            secrets.add(value)

    def scan_headers(raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if _SECRET_KEY_PATTERN.search(str(key)) and isinstance(value, str) and len(value) >= 4:
                secrets.add(value)
                if value.lower().startswith("bearer ") and len(value[7:]) >= 4:
                    secrets.add(value[7:])

    scan_headers(defaults.get("headers"))
    for raw_test in tests:
        if isinstance(raw_test, dict):
            scan_headers(raw_test.get("headers"))
    return tuple(sorted(secrets, key=len, reverse=True))


def _validate_http_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be an absolute HTTP(S) URL")
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{label} must be a valid absolute HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError(f"{label} must be an absolute HTTP(S) URL with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError(f"{label} must not include URL user information")
    if "\\" in parsed.netloc or any(character.isspace() for character in parsed.netloc):
        raise ConfigError(f"{label} contains an invalid hostname")
    return value


def load_suite(path: str | Path, overrides: Mapping[str, str] | None = None) -> TestSuite:
    """Load, interpolate, and validate a JSON suite from *path*."""

    suite_path = Path(path)
    try:
        raw = json.loads(suite_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Unable to read suite '{suite_path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in '{suite_path}' at line {exc.lineno}: {exc.msg}") from exc

    document = _expect_mapping(raw, "Suite")
    name = document.get("name", suite_path.stem)
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("name must be a non-empty string")

    raw_variables = _expect_mapping(document.get("variables", {}), "variables")
    variables = dict(raw_variables)
    variables.update(overrides or {})

    raw_defaults = _expect_mapping(document.get("defaults", {}), "defaults")
    raw_tests = document.get("tests")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise ConfigError("tests must be a non-empty array")

    resolved_secrets: set[str] = set()
    resolved_variables = _interpolate(variables, variables, resolved_secrets)
    defaults = _interpolate(raw_defaults, resolved_variables, resolved_secrets)
    tests_data = _interpolate(raw_tests, resolved_variables, resolved_secrets)
    known_secrets = _collect_secrets(
        resolved_variables, defaults, tests_data, resolved_secrets
    )

    default_headers = _headers(defaults.get("headers", {}), "defaults.headers")
    default_timeout = _positive_number(defaults.get("timeout_seconds", 5), "defaults.timeout_seconds")
    default_retries = _non_negative_integer(defaults.get("retries", 0), "defaults.retries")
    if default_retries > MAX_RETRIES:
        raise ConfigError(f"defaults.retries must not exceed {MAX_RETRIES}")
    default_delay = _positive_number(
        defaults.get("retry_delay_seconds", 0.25), "defaults.retry_delay_seconds", allow_zero=True
    )
    if default_delay > MAX_RETRY_DELAY_SECONDS:
        raise ConfigError(
            f"defaults.retry_delay_seconds must not exceed {MAX_RETRY_DELAY_SECONDS:g}"
        )
    default_retry_non_idempotent = _boolean(
        defaults.get("retry_non_idempotent", False),
        "defaults.retry_non_idempotent",
    )
    default_retry_status = defaults.get("retry_on_status", [408, 429, 500, 502, 503, 504])
    if not isinstance(default_retry_status, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and 100 <= item <= 599
        for item in default_retry_status
    ):
        raise ConfigError("defaults.retry_on_status must be an array of HTTP status integers")

    seen_ids: set[str] = set()
    cases: list[TestCase] = []
    for index, raw_test in enumerate(tests_data):
        item = _expect_mapping(raw_test, f"tests[{index}]")
        test_name = item.get("name")
        if not isinstance(test_name, str) or not test_name.strip():
            raise ConfigError(f"tests[{index}].name must be a non-empty string")
        case_id = item.get("id", _slugify(test_name))
        if not isinstance(case_id, str) or not case_id.strip():
            raise ConfigError(f"tests[{index}].id must be a non-empty string")
        if case_id in seen_ids:
            raise ConfigError(f"Duplicate test id '{case_id}'")
        seen_ids.add(case_id)

        method = item.get("method", "GET")
        if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z]+", method):
            raise ConfigError(f"tests[{index}].method must contain letters only")
        url = _validate_http_url(item.get("url"), f"tests[{index}].url")

        headers = dict(default_headers)
        headers.update(_headers(item.get("headers", {}), f"tests[{index}].headers"))
        timeout = _positive_number(item.get("timeout_seconds", default_timeout), f"tests[{index}].timeout_seconds")
        retries = _non_negative_integer(item.get("retries", default_retries), f"tests[{index}].retries")
        if retries > MAX_RETRIES:
            raise ConfigError(f"tests[{index}].retries must not exceed {MAX_RETRIES}")
        delay = _positive_number(
            item.get("retry_delay_seconds", default_delay),
            f"tests[{index}].retry_delay_seconds",
            allow_zero=True,
        )
        if delay > MAX_RETRY_DELAY_SECONDS:
            raise ConfigError(
                f"tests[{index}].retry_delay_seconds must not exceed "
                f"{MAX_RETRY_DELAY_SECONDS:g}"
            )
        retry_non_idempotent = _boolean(
            item.get("retry_non_idempotent", default_retry_non_idempotent),
            f"tests[{index}].retry_non_idempotent",
        )
        retry_status = item.get("retry_on_status", default_retry_status)
        if not isinstance(retry_status, list) or not all(
            isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
            for status in retry_status
        ):
            raise ConfigError(f"tests[{index}].retry_on_status must contain HTTP status integers")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in tags):
            raise ConfigError(f"tests[{index}].tags must be an array of non-empty strings")
        if "body" in item and "json" in item:
            raise ConfigError(f"tests[{index}] cannot define both 'body' and 'json'")
        body = item.get("json", item.get("body"))
        cases.append(
            TestCase(
                case_id=case_id,
                name=test_name.strip(),
                method=method.upper(),
                url=url,
                headers=headers,
                body=body,
                timeout_seconds=timeout,
                retries=retries,
                retry_delay_seconds=delay,
                retry_on_status=tuple(retry_status),
                retry_non_idempotent=retry_non_idempotent,
                assertions=_assertions(item.get("assertions"), f"tests[{index}].assertions"),
                tags=tuple(tags),
            )
        )

    workers = _non_negative_integer(document.get("workers", 4), "workers")
    if not 1 <= workers <= 64:
        raise ConfigError("workers must be between 1 and 64")
    return TestSuite(name=name.strip(), tests=tuple(cases), workers=workers, known_secrets=known_secrets)
