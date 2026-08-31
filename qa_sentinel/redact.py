"""Secret-safe serialization helpers for reports and console output."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import quote, quote_plus


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
_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_/:])/(?:Users|home|private|tmp|var|opt|etc|Volumes|Applications|workspace|workspaces|root|mnt|builds|runner|github)(?:/[^\s:'\"<>]+)+"
)
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\(?:[^\s\\:'\"<>]+\\)*[^\s\\:'\"<>]+")
_SAFE_METADATA_KEYS = {"secret_sources"}


def redact_text(value: str, known_secrets: tuple[str, ...] = ()) -> str:
    """Remove credentials from free-form text and URLs."""

    redacted = _ABSOLUTE_PATH.sub("[PATH]", value)
    redacted = _WINDOWS_PATH.sub("[PATH]", redacted)
    redacted = _AUTHORIZATION.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _KEY_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
    for secret in known_secrets:
        if len(secret) >= 4:
            redacted = redacted.replace(secret, "[REDACTED]")
            # Credentials commonly travel in query strings or form-encoded
            # headers.  Replace their encoded forms as well, without applying
            # broad substitutions to short values that could corrupt normal
            # diagnostic text.
            encoded = {
                quote(secret, safe=""),
                quote(secret, safe="").lower(),
                quote_plus(secret),
                quote_plus(secret).lower(),
            }
            for candidate in sorted(encoded, key=len, reverse=True):
                if len(candidate) >= 4:
                    redacted = redacted.replace(candidate, "[REDACTED]")
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
                redact(item, known_secrets)
                if key_string.casefold() in _SAFE_METADATA_KEYS
                else (
                    "[REDACTED]"
                    if _SENSITIVE_KEY.search(key_string) and isinstance(item, str)
                    else redact(item, known_secrets)
                )
            )
        return result
    if isinstance(value, tuple):
        return [redact(item, known_secrets) for item in value]
    if isinstance(value, list):
        return [redact(item, known_secrets) for item in value]
    return value
