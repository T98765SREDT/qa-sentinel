"""Declarative assertions for normalized HTTP responses."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .models import AssertionResult, AssertionSpec, HttpResponse


class JsonPathError(KeyError):
    """Raised when a simple JSON path cannot be resolved."""


_PATH_TOKEN = re.compile(r"(?:^|\.)([^.\[\]]+)|\[([0-9]+)\]")


def resolve_json_path(document: Any, path: str) -> Any:
    """Resolve paths such as ``user.name`` or ``items[0].id``."""

    normalized = path.strip()
    if normalized in {"", "$"}:
        return document
    if normalized.startswith("$."):
        normalized = normalized[2:]
    elif normalized.startswith("$"):
        normalized = normalized[1:]
    tokens = list(_PATH_TOKEN.finditer(normalized))
    cursor = 0
    for match in tokens:
        if match.start() != cursor:
            raise JsonPathError(f"Invalid JSON path '{path}'")
        cursor = match.end()
    if not tokens or cursor != len(normalized) or normalized.startswith("."):
        raise JsonPathError(f"Invalid JSON path '{path}'")

    current = document
    for match in tokens:
        key, index = match.groups()
        if key is not None:
            if not isinstance(current, Mapping) or key not in current:
                raise JsonPathError(f"Path '{path}' is missing key '{key}'")
            current = current[key]
        else:
            position = int(index)
            if not isinstance(current, list) or position >= len(current):
                raise JsonPathError(f"Path '{path}' is missing index {position}")
            current = current[position]
    return current


def _result(
    spec: AssertionSpec,
    passed: bool,
    message: str,
    expected: Any = None,
    actual: Any = None,
) -> AssertionResult:
    return AssertionResult(spec.kind, passed, message, expected, actual)


def _status(spec: AssertionSpec, response: HttpResponse) -> AssertionResult:
    if "equals" in spec.params:
        expected = spec.params["equals"]
        passed = response.status == expected
        return _result(
            spec,
            passed,
            f"status {response.status} {'matched' if passed else 'did not match'} {expected}",
            expected,
            response.status,
        )
    allowed = spec.params.get("in")
    if isinstance(allowed, list):
        passed = response.status in allowed
        return _result(
            spec,
            passed,
            f"status {response.status} {'was' if passed else 'was not'} in {allowed}",
            allowed,
            response.status,
        )
    return _result(spec, False, "status assertion requires 'equals' or 'in'")


def _json_path(
    spec: AssertionSpec, response: HttpResponse, parsed_json: Any, json_error: str | None
) -> AssertionResult:
    path = spec.params.get("path")
    if not isinstance(path, str):
        return _result(spec, False, "json_path assertion requires a string 'path'")
    if json_error:
        return _result(spec, False, f"response is not valid JSON: {json_error}")
    try:
        actual = resolve_json_path(parsed_json, path)
        exists = True
    except JsonPathError as exc:
        actual = None
        exists = False
        path_error = str(exc).strip("'")

    if "exists" in spec.params:
        expected_exists = spec.params["exists"]
        if not isinstance(expected_exists, bool):
            return _result(spec, False, "json_path 'exists' must be a boolean")
        if exists != expected_exists:
            return _result(
                spec,
                False,
                f"path '{path}' existence was {exists}, expected {expected_exists}",
                expected_exists,
                exists,
            )
        if not exists:
            return _result(spec, True, f"path '{path}' was absent as expected", False, False)
    elif not exists:
        return _result(spec, False, path_error)

    if "equals" in spec.params:
        expected = spec.params["equals"]
        passed = actual == expected
        return _result(
            spec,
            passed,
            f"path '{path}' {'matched' if passed else 'did not match'} expected value",
            expected,
            actual,
        )
    if "not_equals" in spec.params:
        expected = spec.params["not_equals"]
        passed = actual != expected
        return _result(
            spec,
            passed,
            f"path '{path}' {'differed from' if passed else 'matched'} forbidden value",
            expected,
            actual,
        )
    if "contains" in spec.params:
        expected = spec.params["contains"]
        try:
            passed = expected in actual
        except TypeError:
            passed = False
        return _result(
            spec,
            passed,
            f"path '{path}' {'contained' if passed else 'did not contain'} expected value",
            expected,
            actual,
        )
    return _result(spec, True, f"path '{path}' exists", True, exists)


def _latency(spec: AssertionSpec, response: HttpResponse) -> AssertionResult:
    maximum = spec.params.get("max_ms")
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
        return _result(spec, False, "latency assertion requires numeric 'max_ms'")
    passed = response.elapsed_ms <= float(maximum)
    return _result(
        spec,
        passed,
        f"latency {response.elapsed_ms:.1f} ms {'was within' if passed else 'exceeded'} {maximum} ms",
        float(maximum),
        round(response.elapsed_ms, 3),
    )


def _header(spec: AssertionSpec, response: HttpResponse) -> AssertionResult:
    name = spec.params.get("name")
    if not isinstance(name, str):
        return _result(spec, False, "header assertion requires a string 'name'")
    headers = {key.lower(): value for key, value in response.headers.items()}
    actual = headers.get(name.lower())
    if "equals" in spec.params:
        expected = spec.params["equals"]
        passed = actual == expected
        return _result(spec, passed, f"header '{name}' {'matched' if passed else 'did not match'}", expected, actual)
    exists = actual is not None
    return _result(spec, exists, f"header '{name}' {'exists' if exists else 'is missing'}", True, exists)


def _body_contains(spec: AssertionSpec, response: HttpResponse) -> AssertionResult:
    expected = spec.params.get("value", spec.params.get("contains"))
    if not isinstance(expected, str):
        return _result(spec, False, "body_contains assertion requires string 'value'")
    passed = expected in response.text
    return _result(
        spec,
        passed,
        f"response body {'contained' if passed else 'did not contain'} expected text",
        expected,
        response.text[:500],
    )


def evaluate_assertions(
    specs: tuple[AssertionSpec, ...], response: HttpResponse
) -> tuple[AssertionResult, ...]:
    """Evaluate all assertion specs against one response."""

    if response.error:
        return (
            AssertionResult(
                kind="request",
                passed=False,
                message=f"request failed after {response.attempts} attempt(s): {response.error}",
                actual=response.error,
            ),
        )

    needs_json = any(spec.kind in {"json", "json_path"} for spec in specs)
    parsed_json: Any = None
    json_error: str | None = None
    if needs_json:
        try:
            parsed_json = json.loads(response.text)
        except json.JSONDecodeError as exc:
            json_error = exc.msg

    results: list[AssertionResult] = []
    for spec in specs:
        if spec.kind == "status":
            result = _status(spec, response)
        elif spec.kind in {"json", "json_path"}:
            result = _json_path(spec, response, parsed_json, json_error)
        elif spec.kind == "latency":
            result = _latency(spec, response)
        elif spec.kind == "header":
            result = _header(spec, response)
        elif spec.kind == "body_contains":
            result = _body_contains(spec, response)
        else:
            result = _result(spec, False, f"unknown assertion type '{spec.kind}'")
        results.append(result)
    return tuple(results)
