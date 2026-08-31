"""Typed response captures and safe substitution for workflow steps."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .assertions import JsonPathError, resolve_json_path
from .models import HttpResponse, TestCase


class CaptureError(ValueError):
    """Raised when a capture definition or response value is invalid."""


class MissingCaptureError(CaptureError):
    """Raised when a dependent step cannot resolve a capture."""


_REFERENCE_PATTERN = re.compile(
    r"\{\{\s*steps\.([A-Za-z][A-Za-z0-9._-]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
)
_REFERENCE_FULL_PATTERN = re.compile(
    r"^\{\{\s*steps\.([A-Za-z][A-Za-z0-9._-]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$"
)
_DEFINITION_FIELDS = {"from", "path", "name", "secret"}
_SOURCES = {"json", "header", "cookie", "status"}


@dataclass(frozen=True)
class CapturedValue:
    """A value extracted from one response, with its secret classification."""

    name: str
    source: str
    value: Any
    secret: bool = False


@dataclass(frozen=True)
class CaptureStore:
    """Immutable step-scoped capture storage."""

    entries: tuple[tuple[str, tuple[CapturedValue, ...]], ...] = ()

    def add(self, step_id: str, values: tuple[CapturedValue, ...]) -> "CaptureStore":
        if any(existing_step == step_id for existing_step, _ in self.entries):
            raise CaptureError(f"captures for step '{step_id}' already exist")
        return CaptureStore(self.entries + ((step_id, values),))

    def get(self, step_id: str, capture_name: str) -> CapturedValue:
        for existing_step, values in self.entries:
            if existing_step == step_id:
                for captured in values:
                    if captured.name == capture_name:
                        return captured
                break
        raise MissingCaptureError(
            f"capture '{step_id}.{capture_name}' is not available"
        )

    @property
    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            str(captured.value)
            for _, values in self.entries
            for captured in values
            if captured.secret and isinstance(captured.value, str) and len(captured.value) >= 4
        )

    def public_metadata(self) -> tuple[dict[str, Any], ...]:
        """Return capture names/source/presence only for reports."""

        return tuple(
            {
                "step": step_id,
                "name": captured.name,
                "source": captured.source,
                "present": captured.value is not None,
                "secret": captured.secret,
            }
            for step_id, values in self.entries
            for captured in values
        )


def _definition(name: str, raw: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise CaptureError(f"capture '{name}' must be a JSON object")
    unknown = sorted(set(raw) - _DEFINITION_FIELDS)
    if unknown:
        raise CaptureError(
            f"capture '{name}' contains unsupported field(s): "
            + ", ".join(unknown)
        )
    source = raw.get("from")
    if not isinstance(source, str) or source not in _SOURCES:
        raise CaptureError(
            f"capture '{name}.from' must be one of: " + ", ".join(sorted(_SOURCES))
        )
    secret = raw.get("secret", False)
    if not isinstance(secret, bool):
        raise CaptureError(f"capture '{name}.secret' must be a boolean")
    if source == "json" and (not isinstance(raw.get("path"), str) or not raw["path"].strip()):
        raise CaptureError(f"capture '{name}.path' must be a non-empty string")
    if source in {"header", "cookie"} and (
        not isinstance(raw.get("name"), str) or not raw["name"].strip()
    ):
        raise CaptureError(f"capture '{name}.name' must be a non-empty string")
    if source == "status" and ("path" in raw or "name" in raw):
        raise CaptureError(f"capture '{name}' status source does not accept path/name")
    return source, {"secret": secret, **raw}


def _definitions(case: TestCase) -> tuple[tuple[str, str, dict[str, Any]], ...]:
    definitions: list[tuple[str, str, dict[str, Any]]] = []
    for name, raw in case.extract.items():
        source, definition = _definition(str(name), raw)
        definitions.append((str(name), source, definition))
    return tuple(definitions)


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return None


def _cookie_value(headers: Mapping[str, str], name: str) -> str | None:
    raw = _header_value(headers, "Set-Cookie")
    if raw is None:
        raw = _header_value(headers, "Cookie")
    if raw is None:
        return None
    for item in raw.split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key.strip() == name:
            return value.strip()
    return None


def _value_for(source: str, definition: Mapping[str, Any], response: HttpResponse) -> Any:
    if source == "status":
        if response.status is None:
            raise CaptureError("status capture is unavailable because the request failed")
        return response.status
    if source == "header":
        value = _header_value(response.headers, str(definition["name"]))
    elif source == "cookie":
        value = _cookie_value(response.headers, str(definition["name"]))
    else:
        try:
            document = json.loads(response.text)
            value = resolve_json_path(document, str(definition["path"]))
        except json.JSONDecodeError as exc:
            raise CaptureError(f"JSON capture response is invalid JSON: {exc.msg}") from exc
        except JsonPathError as exc:
            raise MissingCaptureError(f"JSON capture path is unavailable: {exc}") from exc
    if value is None:
        raise MissingCaptureError(
            f"capture source value for '{definition.get('name', definition.get('path', source))}' is missing"
        )
    if isinstance(value, (dict, list)):
        # Structured values are useful in a JSON body, but do not silently
        # stringify them for a header or URL substitution.
        return value
    return value


def capture_response(
    case: TestCase,
    response: HttpResponse,
    store: CaptureStore | None = None,
) -> CaptureStore:
    """Extract all declarations from a response into a new immutable store."""

    current = store or CaptureStore()
    if response.error:
        raise CaptureError(
            f"cannot capture from failed step '{case.case_id}': {response.error}"
        )
    values = tuple(
        CapturedValue(
            name=name,
            source=source,
            value=_value_for(source, definition, response),
            secret=bool(definition.get("secret", False)),
        )
        for name, source, definition in _definitions(case)
    )
    return current.add(case.case_id, values)


def _substitute(value: Any, store: CaptureStore) -> Any:
    if isinstance(value, str):
        full = _REFERENCE_FULL_PATTERN.fullmatch(value.strip())
        if full:
            return store.get(full.group(1), full.group(2)).value

        def replace(match: re.Match[str]) -> str:
            captured = store.get(match.group(1), match.group(2))
            if isinstance(captured.value, (dict, list)):
                raise CaptureError(
                    f"structured capture '{match.group(1)}.{match.group(2)}' "
                    "must occupy an entire JSON value"
                )
            return str(captured.value)

        return _REFERENCE_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_substitute(item, store) for item in value]
    if isinstance(value, tuple):
        return tuple(_substitute(item, store) for item in value)
    if isinstance(value, Mapping):
        return {key: _substitute(item, store) for key, item in value.items()}
    return value


def resolve_case_references(case: TestCase, store: CaptureStore) -> TestCase:
    """Resolve step references immediately before a dependent request."""

    from dataclasses import replace

    resolved_url = _substitute(case.url, store)
    resolved_headers = _substitute(case.headers, store)
    if not isinstance(resolved_url, str):
        resolved_url = str(resolved_url)
    if not isinstance(resolved_headers, Mapping):
        raise CaptureError("resolved request headers must be an object")
    return replace(
        case,
        url=resolved_url,
        headers={str(key): str(value) for key, value in resolved_headers.items()},
        body=_substitute(case.body, store),
    )
