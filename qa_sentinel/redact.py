"""Secret-safe serialization helpers for reports and console output."""

from __future__ import annotations

import re
from typing import Any, Mapping


_SENSITIVE_KEY = re.compile(
    r"authorization|password|passwd|secret|token|api[-_]?key|cookie", re.IGNORECASE
)
_AUTHORIZATION = re.compile(
    r"(?i)(authorization\s*[=:]\s*)(?:Bearer\s+)?([^&\s,;\"']+)"
)
_KEY_VALUE = re.compile(
    r"(?i)(password|passwd|secret|token|api[-_]?key|x-api-key)(\s*[=:]\s*)([^&\s,;\"']+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def redact_text(value: str, known_secrets: tuple[str, ...] = ()) -> str:
    """Remove credentials from free-form text and URLs."""

    redacted = _AUTHORIZATION.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _KEY_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
    for secret in known_secrets:
        if len(secret) >= 4:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def redact(value: Any, known_secrets: tuple[str, ...] = ()) -> Any:
    """Recursively redact sensitive keys and known credential values."""

    if isinstance(value, str):
        return redact_text(value, known_secrets)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_string = str(key)
            result[key_string] = (
                "[REDACTED]" if _SENSITIVE_KEY.search(key_string) else redact(item, known_secrets)
            )
        return result
    if isinstance(value, tuple):
        return [redact(item, known_secrets) for item in value]
    if isinstance(value, list):
        return [redact(item, known_secrets) for item in value]
    return value
