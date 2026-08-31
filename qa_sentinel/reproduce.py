"""Build safe, copyable reproduction commands for failed HTTP checks."""

from __future__ import annotations

import json
import shlex
from typing import Any

from .models import TestResult
from .redact import redact, redact_text


def curl_command(test: TestResult, known_secrets: tuple[str, ...] = ()) -> str:
    """Return a redacted curl command that describes *test* without credentials."""

    case = test.case
    parts = ["curl", "--request", case.method]
    safe_headers = redact(case.headers, known_secrets)
    for name, value in safe_headers.items():
        parts.extend(["--header", f"{name}: {value}"])
    if case.body is not None:
        safe_body: Any = redact(case.body, known_secrets)
        if isinstance(safe_body, (dict, list)):
            body = json.dumps(safe_body, ensure_ascii=False, separators=(",", ":"))
        else:
            body = str(safe_body)
        parts.extend(["--data-raw", body])
    parts.extend(["--max-time", f"{case.timeout_seconds:g}"])
    parts.append(redact_text(case.url, known_secrets))
    return shlex.join(parts)


def reproduction_for_test(
    test: TestResult, known_secrets: tuple[str, ...] = ()
) -> dict[str, str]:
    """Return stable reproduction metadata for report consumers."""

    return {"curl": curl_command(test, known_secrets)}
