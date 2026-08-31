"""JSON suite loading, interpolation, validation, and normalization."""

from __future__ import annotations

import json
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .environment import EnvironmentError, EnvironmentProfile, resolve_profile_variables
from .models import (
    DEFAULT_SLOW_THRESHOLD_MS,
    MAX_RETRIES,
    MAX_RETRY_DELAY_SECONDS,
    MAX_RESPONSE_BYTES,
    AssertionSpec,
    TestCase,
    TestSuite,
)


class ConfigError(ValueError):
    """Raised when a suite configuration is invalid."""


_VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_STEP_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_STEP_REFERENCE_PATTERN = re.compile(
    r"\{\{\s*steps\.([A-Za-z][A-Za-z0-9._-]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
)
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


def _reject_unknown_fields(
    mapping: Mapping[str, Any], allowed: set[str], label: str
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        names = ", ".join(f"'{name}'" for name in unknown)
        raise ConfigError(f"{label} contains unsupported field(s): {names}")


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


def _response_limit(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{label} must be a positive integer")
    if value > MAX_RESPONSE_BYTES:
        raise ConfigError(f"{label} must not exceed {MAX_RESPONSE_BYTES} bytes")
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


def _step_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _STEP_ID_PATTERN.fullmatch(value):
        raise ConfigError(
            f"{label} must match ^[A-Za-z][A-Za-z0-9._-]*$"
        )
    return value


def _dependencies(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{label} must be an array of step ids")
    dependencies = tuple(_step_id(item, f"{label}[{index}]") for index, item in enumerate(value))
    if len(set(dependencies)) != len(dependencies):
        raise ConfigError(f"{label} must not contain duplicate step ids")
    return dependencies


def _extract(value: Any, label: str) -> dict[str, Any]:
    mapping = _expect_mapping(value, label)
    result: dict[str, Any] = {}
    for key, definition in mapping.items():
        name = _step_id(key, f"{label} key")
        if not isinstance(definition, dict):
            raise ConfigError(f"{label}.{name} must be a JSON object")
        result[name] = dict(definition)
    return result


def _validate_assertion(spec: Mapping[str, Any], label: str) -> None:
    kind = spec["type"].strip().lower()
    allowed_fields = {
        "status": {"type", "equals", "in"},
        "json": {"type", "path", "exists", "equals", "not_equals", "contains"},
        "json_path": {"type", "path", "exists", "equals", "not_equals", "contains"},
        "latency": {"type", "max_ms"},
        "header": {"type", "name", "equals"},
        "body_contains": {"type", "value", "contains"},
    }
    if kind not in allowed_fields:
        raise ConfigError(f"{label}.type has unsupported assertion type '{kind}'")
    _reject_unknown_fields(spec, allowed_fields[kind], label)
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
        predicates = [
            key for key in ("exists", "equals", "not_equals", "contains") if key in spec
        ]
        if len(predicates) > 1:
            raise ConfigError(
                f"{label} must define at most one JSON predicate; found {', '.join(predicates)}"
            )
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


def _hash_safe(value: Any, known_secrets: tuple[str, ...], key: str = "") -> Any:
    """Normalize configuration for a stable fingerprint without secret values."""

    if isinstance(value, Mapping):
        return {
            str(item_key): (
                "[SECRET]"
                if _SECRET_KEY_PATTERN.search(str(item_key))
                else _hash_safe(item_value, known_secrets, str(item_key))
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_hash_safe(item, known_secrets, key) for item in value]
    if isinstance(value, tuple):
        return [_hash_safe(item, known_secrets, key) for item in value]
    if isinstance(value, str):
        normalized = value
        for secret in known_secrets:
            if secret:
                normalized = normalized.replace(secret, "[SECRET]")
        return normalized
    return value


def _configuration_hash(
    *,
    schema_version: int,
    name: str,
    description: str,
    environment: str,
    variables: Mapping[str, Any],
    defaults: Mapping[str, Any],
    tests: list[Any],
    workers: int,
    slow_threshold_ms: float,
    known_secrets: tuple[str, ...],
    secret_sources: tuple[Any, ...],
) -> str:
    payload = {
        "schemaVersion": schema_version,
        "name": name,
        "description": description,
        "environment": environment,
        "variables": variables,
        "defaults": defaults,
        "tests": tests,
        "workers": workers,
        "slow_threshold_ms": slow_threshold_ms,
        "secret_sources": [
            {"name": source.name, "source": source.source}
            for source in secret_sources
        ],
    }
    canonical = json.dumps(
        _hash_safe(payload, known_secrets),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


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


def load_suite(
    path: str | Path,
    overrides: Mapping[str, str] | None = None,
    *,
    environment_profile: EnvironmentProfile | None = None,
) -> TestSuite:
    """Load, interpolate, and validate a JSON suite from *path*."""

    suite_path = Path(path)
    try:
        raw = json.loads(suite_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Unable to read suite '{suite_path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in '{suite_path}' at line {exc.lineno}: {exc.msg}") from exc

    document = _expect_mapping(raw, "Suite")
    _reject_unknown_fields(
        document,
        {
            "schemaVersion",
            "schema_version",
            "name",
            "description",
            "environment",
            "variables",
            "defaults",
            "tests",
            "workers",
            "slow_threshold_ms",
        },
        "Suite",
    )
    schema_values = [
        document[key] for key in ("schemaVersion", "schema_version") if key in document
    ]
    if len(schema_values) > 1 and schema_values[0] != schema_values[1]:
        raise ConfigError("Suite.schemaVersion and Suite.schema_version must match")
    if schema_values:
        schema_version = schema_values[0]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ConfigError("schemaVersion must be the integer 2")
        if schema_version != 2:
            raise ConfigError(f"Unsupported suite schemaVersion {schema_version}; expected 2")
    else:
        # Existing suites are independent v1 suites.  They are normalized to
        # the v2 model with empty dependency metadata below.
        schema_version = 2
        legacy_suite = True
    if schema_values:
        legacy_suite = False
    name = document.get("name", suite_path.stem)
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("name must be a non-empty string")
    description = document.get("description", "")
    if not isinstance(description, str):
        raise ConfigError("description must be a string")
    environment = document.get("environment", "")
    if not isinstance(environment, str):
        raise ConfigError("environment must be a string")
    environment = environment.strip()
    if len(environment) > 80:
        raise ConfigError("environment must be 80 characters or fewer")
    if environment_profile is not None:
        environment = environment_profile.name
    slow_threshold_ms = _positive_number(
        document.get("slow_threshold_ms", DEFAULT_SLOW_THRESHOLD_MS),
        "slow_threshold_ms",
        allow_zero=True,
    )

    raw_variables = _expect_mapping(document.get("variables", {}), "variables")
    if environment_profile is None:
        variables = dict(raw_variables)
        variables.update(overrides or {})
    else:
        try:
            variables = resolve_profile_variables(
                environment_profile, raw_variables, overrides
            )
        except EnvironmentError as exc:
            raise ConfigError(str(exc)) from exc

    raw_defaults = _expect_mapping(document.get("defaults", {}), "defaults")
    _reject_unknown_fields(
        raw_defaults,
        {
            "headers",
            "timeout_seconds",
            "max_response_bytes",
            "retries",
            "retry_delay_seconds",
            "retry_on_status",
            "retry_non_idempotent",
        },
        "defaults",
    )
    raw_tests = document.get("tests")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise ConfigError("tests must be a non-empty array")

    resolved_secrets: set[str] = set()
    resolved_variables = _interpolate(variables, variables, resolved_secrets)
    defaults = _interpolate(raw_defaults, resolved_variables, resolved_secrets)
    tests_data = _interpolate(raw_tests, resolved_variables, resolved_secrets)
    if environment_profile is not None:
        resolved_secrets.update(environment_profile.resolved_secret_values)
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
    default_max_response_bytes = _response_limit(
        defaults.get("max_response_bytes", MAX_RESPONSE_BYTES),
        "defaults.max_response_bytes",
    )

    seen_ids: set[str] = set()
    cases: list[TestCase] = []
    for index, raw_test in enumerate(tests_data):
        item = _expect_mapping(raw_test, f"tests[{index}]")
        _reject_unknown_fields(
            item,
            {
                "id",
                "name",
                "method",
                "url",
                "headers",
                "json",
                "body",
                "timeout_seconds",
                "max_response_bytes",
                "retries",
                "retry_delay_seconds",
                "retry_on_status",
                "retry_non_idempotent",
                "assertions",
                "tags",
                "depends_on",
                "run_if",
                "extract",
                "cleanup",
            },
            f"tests[{index}]",
        )
        test_name = item.get("name")
        if not isinstance(test_name, str) or not test_name.strip():
            raise ConfigError(f"tests[{index}].name must be a non-empty string")
        if not legacy_suite and "id" not in item:
            raise ConfigError(
                f"tests[{index}].id is required when schemaVersion is 2"
            )
        case_id = item.get("id", _slugify(test_name))
        if not isinstance(case_id, str) or not case_id.strip():
            raise ConfigError(f"tests[{index}].id must be a non-empty string")
        if not legacy_suite:
            case_id = _step_id(case_id, f"tests[{index}].id")
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
        max_response_bytes = _response_limit(
            item.get("max_response_bytes", default_max_response_bytes),
            f"tests[{index}].max_response_bytes",
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
        depends_on = _dependencies(
            item.get("depends_on", []), f"tests[{index}].depends_on"
        )
        run_if = item.get("run_if", "success")
        if not isinstance(run_if, str) or run_if not in {"success", "always"}:
            raise ConfigError(
                f"tests[{index}].run_if must be either 'success' or 'always'"
            )
        cleanup = item.get("cleanup", False)
        if not isinstance(cleanup, bool):
            raise ConfigError(f"tests[{index}].cleanup must be a boolean")
        if cleanup:
            if "run_if" in item and run_if != "always":
                raise ConfigError(
                    f"tests[{index}].cleanup=true requires run_if='always'"
                )
            run_if = "always"
        extract = _extract(item.get("extract", {}), f"tests[{index}].extract")
        if legacy_suite and (depends_on or run_if != "success" or extract or cleanup):
            raise ConfigError(
                f"tests[{index}] uses workflow fields; add schemaVersion: 2"
            )
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
                max_response_bytes=max_response_bytes,
                depends_on=depends_on,
                run_if=run_if,
                extract=extract,
                cleanup=cleanup,
            )
        )

    workers = _non_negative_integer(document.get("workers", 4), "workers")
    if not 1 <= workers <= 64:
        raise ConfigError("workers must be between 1 and 64")
    secret_sources = (
        environment_profile.secrets if environment_profile is not None else ()
    )
    config_hash = _configuration_hash(
        schema_version=schema_version,
        name=name.strip(),
        description=description.strip(),
        environment=environment,
        variables=resolved_variables,
        defaults=defaults,
        tests=tests_data,
        workers=workers,
        slow_threshold_ms=slow_threshold_ms,
        known_secrets=known_secrets,
        secret_sources=secret_sources,
    )
    return TestSuite(
        name=name.strip(),
        tests=tuple(cases),
        workers=workers,
        known_secrets=known_secrets,
        description=description.strip(),
        environment=environment,
        slow_threshold_ms=slow_threshold_ms,
        environment_config_hash=(
            environment_profile.config_hash if environment_profile is not None else ""
        ),
        config_hash=config_hash,
        secret_sources=secret_sources,
        schema_version=schema_version,
    )
