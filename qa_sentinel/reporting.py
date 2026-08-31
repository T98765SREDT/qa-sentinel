"""Secret-safe JSON and self-contained HTML reports."""

from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from .models import SuiteResult
from .redact import redact, redact_text
from .reproduce import reproduction_for_test


# Reports are meant to help diagnose a failing check, not to become an
# accidental response-body archive.  Keep previews small enough for terminal,
# CI, and browser consumers while retaining a little more context for JSON.
_ERROR_PREVIEW_LIMIT = 1200
_ASSERTION_PREVIEW_LIMIT = 1200
_RESPONSE_PREVIEW_LIMITS = {
    "json": 2400,
    "text": 1600,
}
_TRUNCATION_MARKER = "… [truncated]"


def _media_type(headers: Any) -> str:
    """Return a normalized response media type without parameters."""

    content_type = next(
        (value for key, value in headers.items() if str(key).lower() == "content-type"),
        "",
    )
    return str(content_type).split(";", 1)[0].strip().lower() or "unknown"


def _bounded_redacted_text(
    value: Any, limit: int, known_secrets: tuple[str, ...] = ()
) -> str:
    """Redact before and after clipping a diagnostic string to a hard limit."""

    text = "" if value is None else str(value)
    # Redact the complete value first so a credential cannot evade matching by
    # landing across the preview boundary.  Run it again after clipping as a
    # defense-in-depth output check.
    text = redact_text(text, known_secrets)
    if len(text) > limit:
        text = text[: max(0, limit - len(_TRUNCATION_MARKER))] + _TRUNCATION_MARKER
    text = redact_text(text, known_secrets)
    if len(text) > limit:
        text = text[:limit]
    return text


def _diagnostic_value(value: Any, known_secrets: tuple[str, ...] = ()) -> str:
    """Format an assertion value for HTML without allowing huge structures."""

    if value is None:
        return "—"
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _bounded_redacted_text(value, _ASSERTION_PREVIEW_LIMIT, known_secrets)


def _response_preview(response: Any, known_secrets: tuple[str, ...]) -> dict[str, Any]:
    """Return a bounded, content-type-aware preview for a failed response."""

    media_type = _media_type(response.headers)
    byte_length = len(response.body)
    response_text = response.text
    is_json = media_type == "application/json" or media_type.endswith("+json")
    is_text = media_type == "unknown" or media_type.startswith("text/") or media_type in {
        "application/xml",
        "application/javascript",
        "application/x-javascript",
    } or media_type.endswith("+xml")
    if not is_json and not is_text:
        return {
            "content_type": media_type,
            "byte_length": byte_length,
            "text": "[binary response body omitted]",
            "truncated": bool(byte_length),
        }

    limit = _RESPONSE_PREVIEW_LIMITS["json" if is_json else "text"]
    text = _bounded_redacted_text(response_text, limit, known_secrets)
    return {
        "content_type": media_type,
        "byte_length": byte_length,
        "text": text,
        "truncated": len(response_text) > limit,
    }


def build_report(result: SuiteResult, known_secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    """Convert domain results into a redacted, JSON-serializable report."""

    retry_settings = {
        "max_retries": 0,
        "retry_on_status": [],
        "retry_non_idempotent": False,
        **dict(result.retry_settings),
    }
    data: dict[str, Any] = {
        "schema_version": 2,
        "run_id": result.run_id or None,
        "tool_version": result.tool_version or None,
        "suite_schema_version": result.schema_version,
        "suite": result.suite_name,
        "description": result.description,
        "environment": result.environment,
        "environment_config_hash": result.environment_config_hash or None,
        "suite_config_hash": result.suite_config_hash or None,
        "provenance": {
            "git_sha": result.git_sha or None,
            "git_branch": result.git_branch or None,
            "ci_url": result.ci_url or None,
        },
        "execution": {
            "workers": result.worker_count or None,
            "selected_tags": list(result.selected_tags),
            "retry": retry_settings,
        },
        "secret_sources": [
            {"name": source.name, "source": source.source}
            for source in result.secret_sources
        ],
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_ms": round(result.duration_ms, 3),
        "slow_threshold_ms": result.slow_threshold_ms,
        "interrupted": result.interrupted,
        "summary": {
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "errors": result.errors,
            "blocked": result.blocked,
            "skipped": result.skipped,
            "success_rate": round(result.success_rate, 2),
        },
        "capture_metadata": [dict(item) for item in result.capture_metadata],
        "tests": [],
    }
    for test_index, test in enumerate(result.tests):
        test_data: dict[str, Any] = {
            "id": test.case.case_id,
            "request_id": test.case.case_id,
            "name": test.case.name,
            "method": test.case.method,
            "url": test.case.url,
            "tags": list(test.case.tags),
            "passed": test.passed,
            "status": test.status,
            "response_status": test.response.status,
            "response_size_bytes": len(test.response.body),
            "response_content_type": _media_type(test.response.headers),
            "latency_ms": round(test.response.elapsed_ms, 3),
            "attempts": test.response.attempts,
            "error": (
                _bounded_redacted_text(
                    test.response.error, _ERROR_PREVIEW_LIMIT, known_secrets
                )
                if test.response.error
                else None
            ),
            "response_preview": (
                _response_preview(test.response, known_secrets)
                if not test.passed and not test.response.error
                else None
            ),
            "started_at": test.started_at,
            "finished_at": test.finished_at,
            "assertions": [
                {
                    "path": f"tests[{test_index}].assertions[{assertion_index}]",
                    "type": assertion.kind,
                    "passed": assertion.passed,
                    "message": assertion.message,
                    "expected": assertion.expected,
                    "actual": assertion.actual,
                }
                for assertion_index, assertion in enumerate(test.assertions)
            ],
        }
        if test.status in {"failed", "error"}:
            test_data["reproduction"] = reproduction_for_test(test, known_secrets)
        data["tests"].append(test_data)
    return redact(data, known_secrets)


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace a report atomically so interrupted writes cannot corrupt it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def write_json_report(
    result: SuiteResult, destination: str | Path, known_secrets: tuple[str, ...] = ()
) -> Path:
    """Write an indented machine-readable JSON report."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        path,
        json.dumps(build_report(result, known_secrets), indent=2, ensure_ascii=False) + "\n",
    )
    return path


def render_junit_xml(result: SuiteResult, known_secrets: tuple[str, ...] = ()) -> str:
    """Render a secret-safe JUnit XML document for CI test-report consumers."""

    error_count = result.errors
    failure_count = result.failed
    suite = ET.Element(
        "testsuite",
        {
            "name": redact_text(
                f"{result.suite_name} [{result.environment}]" if result.environment else result.suite_name,
                known_secrets,
            ),
            "tests": str(result.total),
            "failures": str(failure_count),
            "errors": str(error_count),
            "time": f"{result.duration_ms / 1000:.3f}",
            "timestamp": result.started_at,
        },
    )
    properties = ET.SubElement(suite, "properties")
    if result.environment:
        ET.SubElement(
            properties,
            "property",
            {"name": "environment", "value": redact_text(result.environment, known_secrets)},
        )
    if result.environment_config_hash:
        ET.SubElement(
            properties,
            "property",
            {
                "name": "environment_config_hash",
                "value": redact_text(result.environment_config_hash, known_secrets),
            },
        )
    ET.SubElement(
        properties,
        "property",
        {"name": "interrupted", "value": str(result.interrupted).lower()},
    )
    for name, value in (
        ("run_id", result.run_id),
        ("tool_version", result.tool_version),
        ("suite_config_hash", result.suite_config_hash),
        ("git_sha", result.git_sha),
        ("git_branch", result.git_branch),
        ("ci_url", result.ci_url),
        ("workers", str(result.worker_count) if result.worker_count else ""),
    ):
        if value:
            ET.SubElement(
                properties,
                "property",
                {"name": name, "value": redact_text(value, known_secrets)},
            )
    if result.selected_tags:
        ET.SubElement(
            properties,
            "property",
            {
                "name": "selected_tags",
                "value": redact_text(",".join(result.selected_tags), known_secrets),
            },
        )
    ET.SubElement(
        properties,
        "property",
        {"name": "suite_schema_version", "value": str(result.schema_version)},
    )
    for source in result.secret_sources:
        ET.SubElement(
            properties,
            "property",
            {
                "name": f"secret_source.{source.name}",
                "value": redact_text(source.source, known_secrets),
            },
        )
    for test in result.tests:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "qa_sentinel.api",
                "name": redact_text(test.case.name, known_secrets),
                "time": f"{test.response.elapsed_ms / 1000:.3f}",
            },
        )
        if test.status == "error":
            safe_error = _bounded_redacted_text(
                test.response.error, _ERROR_PREVIEW_LIMIT, known_secrets
            )
            error = ET.SubElement(
                case,
                "error",
                {"message": safe_error},
            )
            error.text = safe_error
        elif test.status == "failed":
            messages = _bounded_redacted_text(
                "\n".join(
                    assertion.message
                    for assertion in test.assertions
                    if not assertion.passed
                ),
                _ERROR_PREVIEW_LIMIT,
                known_secrets,
            )
            failure = ET.SubElement(
                case,
                "failure",
                {
                    "message": messages or "API assertions failed",
                    "type": "AssertionError",
                },
            )
            failure.text = messages
        elif test.status in {"blocked", "skipped"}:
            skipped = ET.SubElement(case, "skipped")
            skipped.text = redact_text(
                next(
                    (
                        assertion.message
                        for assertion in test.assertions
                        if not assertion.passed
                    ),
                    test.status,
                ),
                known_secrets,
            )
        ET.SubElement(
            case,
            "system-out",
        ).text = redact_text(
            f"{test.case.method} {test.case.url}\n"
            f"status={test.response.status} attempts={test.response.attempts} "
            f"latency_ms={test.response.elapsed_ms:.3f}",
            known_secrets,
        )
    ET.indent(suite, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        suite, encoding="unicode"
    ) + "\n"


def write_junit_report(
    result: SuiteResult, destination: str | Path, known_secrets: tuple[str, ...] = ()
) -> Path:
    """Write a JUnit XML report compatible with common CI report viewers."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, render_junit_xml(result, known_secrets))
    return path


def _escape(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(str(value))


def _format_timestamp(value: str | None) -> str:
    """Return a compact, human-readable UTC timestamp while keeping the raw value in markup."""

    if not value:
        return "Unknown time"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%b %-d, %Y at %H:%M:%S UTC")
    except ValueError:
        return value


def _assertion_diagnostic(
    assertion: dict[str, Any], known_secrets: tuple[str, ...] = ()
) -> str:
    """Render expected/actual values for a failed assertion without hiding the original message."""

    if assertion["passed"]:
        return ""
    expected = _escape(_diagnostic_value(assertion.get("expected"), known_secrets))
    actual = _escape(_diagnostic_value(assertion.get("actual"), known_secrets))
    return f'''<div class="comparison" role="group" aria-label="Expected and actual values">
      <span><small>Expected</small><code>{expected}</code></span>
      <span><small>Actual</small><code>{actual}</code></span>
    </div>'''


def _test_card(test: dict[str, Any], known_secrets: tuple[str, ...] = ()) -> str:
    state = test.get("status") or ("passed" if test["passed"] else "failed")
    icon = "✓" if state == "passed" else "×" if state in {"failed", "error"} else "!"
    tags = "".join(f'<span class="tag">{_escape(tag)}</span>' for tag in test["tags"])
    assertions = "".join(
        f'''<li class="assertion {"ok" if assertion["passed"] else "bad"}">
          <span class="assertion-icon">{"✓" if assertion["passed"] else "×"}</span>
          <div><strong>{_escape(assertion["type"])}</strong><p>{_escape(_bounded_redacted_text(assertion["message"], _ASSERTION_PREVIEW_LIMIT, known_secrets))}</p>{_assertion_diagnostic(assertion, known_secrets)}</div>
        </li>'''
        for assertion in test["assertions"]
    )
    error = (
        f'<div class="error">{_escape(_bounded_redacted_text(test["error"], _ERROR_PREVIEW_LIMIT, known_secrets))}</div>'
        if test["error"]
        else ""
    )
    preview = test.get("response_preview")
    response_markup = ""
    if preview:
        preview_label = (
            f'Response preview · {_escape(preview["content_type"])} · '
            f'{preview["byte_length"]} bytes'
        )
        response_markup = (
            f'<details class="response-preview"><summary>{preview_label}</summary>'
            f'<pre>{_escape(preview["text"])}</pre></details>'
        )
    search_text = " ".join(
        [
            test["name"],
            test["method"],
            test["url"],
            str(test["status"]),
            str(test.get("response_status") or ""),
            " ".join(test["tags"]),
            " ".join(assertion["type"] for assertion in test["assertions"]),
            " ".join(assertion["message"] for assertion in test["assertions"]),
        ]
    ).lower()
    return f'''<article class="test-card {state}" data-status="{state}" data-attempts="{test["attempts"]}" data-latency="{test["latency_ms"]}" data-search="{_escape(search_text)}">
      <div class="test-heading">
        <div class="status-icon">{icon}</div>
        <div class="test-title"><h3>{_escape(test["name"])}</h3><div class="endpoint"><span>{_escape(test["method"])}</span> {_escape(test["url"])}</div></div>
        <span class="pill {state}">{state.upper()}</span>
      </div>
      <div class="metrics"><span>State <b>{_escape(test["status"])}</b></span><span>HTTP status <b>{_escape(test.get("response_status"))}</b></span><span>Latency <b>{test["latency_ms"]:.1f} ms</b></span><span>Attempts <b>{test["attempts"]}</b></span></div>
      <div class="tags">{tags}</div>{error}{response_markup}
      <details><summary>{len(test["assertions"])} assertion(s)</summary><ul>{assertions}</ul></details>
    </article>'''


def render_html(result: SuiteResult, known_secrets: tuple[str, ...] = ()) -> str:
    """Render a polished standalone report with no external assets."""

    report = build_report(result, known_secrets)
    summary = report["summary"]
    cards = "".join(_test_card(test, known_secrets) for test in report["tests"])
    pass_width = summary["success_rate"]
    failure_class = " fail" if summary["failed"] else ""
    report_json = json.dumps(report, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    junit_json = json.dumps(render_junit_xml(result, known_secrets), ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    description_markup = (
        f'<p class="run-note">{_escape(report["description"])}</p>'
        if report.get("description")
        else ""
    )
    profile_markup = ""
    if report.get("environment_config_hash"):
        profile_markup = (
            f'<p class="run-note">Environment profile fingerprint: '
            f'{_escape(report["environment_config_hash"])}</p>'
        )
    source_markup = ""
    if report.get("secret_sources"):
        source_names = ", ".join(
            f'{source["name"]} ← {source["source"]}'
            for source in report["secret_sources"]
        )
        source_markup = (
            f'<p class="run-note">Secret sources: {_escape(source_names)}</p>'
        )
    verdict_markup = (
        f'<p class="run-note">Result: {_escape(str(summary["passed"]))} passed · '
        f'{_escape(str(summary["failed"]))} failed · '
        f'{_escape(str(summary["errors"]))} errors · '
        f'{_escape(str(summary["blocked"]))} blocked · '
        f'{_escape(str(summary["skipped"]))} skipped'
        f'{" · interrupted" if report.get("interrupted") else ""}</p>'
    )
    provenance = report.get("provenance", {})
    provenance_bits = []
    if report.get("run_id"):
        provenance_bits.append(f'Run {_escape(report["run_id"])}')
    if report.get("tool_version"):
        provenance_bits.append(f'Tool {_escape(report["tool_version"])}')
    if provenance.get("git_sha"):
        provenance_bits.append(f'Git {_escape(provenance["git_sha"])}')
    if provenance.get("git_branch"):
        provenance_bits.append(f'Branch {_escape(provenance["git_branch"])}')
    if provenance.get("ci_url"):
        provenance_bits.append(f'CI {_escape(provenance["ci_url"])}')
    provenance_markup = (
        f'<p class="run-note">Provenance: {" · ".join(provenance_bits)}</p>'
        if provenance_bits
        else ""
    )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(report["suite"])} · QA Sentinel</title>
<style>
:root{{--ink:#172033;--muted:#697386;--surface:#fff;--line:#e7eaf0;--pass:#12a36d;--fail:#e5484d;--navy:#111c44;--violet:#6657d9}}
*{{box-sizing:border-box}}body{{margin:0;background:#f5f7fb;color:var(--ink);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.hero{{background:radial-gradient(circle at 90% 20%,#5362c5 0,transparent 28%),linear-gradient(135deg,#101a3c,#242760 65%,#392e70);color:#fff;padding:48px 0 88px}}
.wrap{{max-width:1128px;margin:auto;padding:0 24px}}.brand{{display:flex;align-items:center;gap:12px;color:#cbd1ff;font-size:13px;font-weight:700;letter-spacing:.15em;text-transform:uppercase}}
.logo{{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:#fff;color:#272663;font-size:20px;box-shadow:0 8px 25px #070b1c55}}
h1{{font-size:clamp(30px,5vw,48px);margin:28px 0 8px;letter-spacing:-.04em}}.subtitle{{color:#cbd1dd;margin:0;font-size:15px}}
.run-note{{display:inline-block;margin:18px 0 0;padding:8px 11px;border:1px solid #ffffff3b;border-radius:8px;background:#ffffff12;color:#eef0ff;font-size:12px}}
.summary-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:-50px}}.summary-card{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:20px 22px;box-shadow:0 12px 32px #1b244112}}
.summary-card small{{display:block;color:var(--muted);font-weight:650;text-transform:uppercase;letter-spacing:.08em;font-size:11px}}.summary-card b{{display:block;font-size:28px;margin-top:7px}}.summary-card.pass b{{color:var(--pass)}}.summary-card.fail b{{color:var(--fail)}}
.progress{{height:8px;background:#e8ebf1;border-radius:20px;overflow:hidden;margin:28px 0 32px}}.progress div{{height:100%;width:{pass_width}%;background:linear-gradient(90deg,#0c9,#45c993);border-radius:20px}}
.toolbar{{display:flex;gap:10px;align-items:center;margin-bottom:18px;flex-wrap:wrap}}.search{{flex:1;min-width:230px;border:1px solid var(--line);background:#fff;padding:12px 15px;border-radius:11px;font:inherit;outline:none}}.search:focus{{border-color:#8b85de;box-shadow:0 0 0 3px #6657d91a}}
.filter-group{{display:flex;gap:8px;flex-wrap:wrap}}.filter{{border:1px solid var(--line);background:#fff;padding:10px 14px;border-radius:10px;color:var(--muted);cursor:pointer;font-weight:650}}.filter.active{{background:var(--navy);color:#fff;border-color:var(--navy)}}.filter:focus-visible,.search:focus-visible,.sort select:focus-visible,.toolbar button:focus-visible{{outline:3px solid #b3aaff;outline-offset:2px}}.sort{{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px;font-weight:700}}.sort select{{border:1px solid var(--line);background:#fff;padding:10px 12px;border-radius:10px;color:var(--ink);font:inherit}}
.test-list{{display:grid;gap:14px;padding-bottom:50px}}.test-card{{background:#fff;border:1px solid var(--line);border-left:4px solid var(--pass);border-radius:15px;padding:18px 20px;box-shadow:0 6px 20px #1b244109}}.test-card.failed,.test-card.error{{border-left-color:var(--fail)}}.test-card.blocked{{border-left-color:#e09b22}}.test-card.skipped{{border-left-color:#98a2b3}}
.test-heading{{display:flex;align-items:flex-start;gap:13px}}.status-icon{{width:31px;height:31px;display:grid;place-items:center;background:#e7f8f0;color:var(--pass);border-radius:50%;font-weight:900}}.failed .status-icon,.error .status-icon{{background:#ffebec;color:var(--fail)}}.blocked .status-icon{{background:#fff6df;color:#a96b00}}.skipped .status-icon{{background:#eef1f5;color:#697386}}.test-title{{min-width:0;flex:1}}h3{{margin:1px 0 7px;font-size:17px}}.endpoint{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);font-size:12px;word-break:break-all}}.endpoint span{{font-weight:800;color:#5261b8}}
.pill{{font-size:10px;font-weight:800;padding:5px 8px;border-radius:20px;background:#e7f8f0;color:var(--pass)}}.pill.failed,.pill.error{{background:#ffebec;color:var(--fail)}}.pill.blocked{{background:#fff6df;color:#a96b00}}.pill.skipped{{background:#eef1f5;color:#697386}}.metrics{{display:flex;gap:22px;margin:17px 0 10px 44px;color:var(--muted);font-size:12px}}.metrics b{{color:var(--ink)}}
.tags{{margin-left:44px}}.tag{{display:inline-block;background:#f0f1fa;color:#4f5490;border-radius:6px;padding:4px 7px;font-size:10px;margin:0 5px 5px 0}}details{{margin:12px 0 0 44px;border-top:1px solid var(--line);padding-top:11px}}summary{{cursor:pointer;color:var(--muted);font-size:12px;font-weight:650}}.response-preview{{background:#f7f8fb;border:1px solid var(--line);border-radius:8px;padding:10px 12px}}.response-preview pre{{margin:10px 0 0;max-height:260px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink)}}ul{{list-style:none;padding:8px 0 0;margin:0}}.assertion{{display:flex;gap:9px;padding:7px 0}}.assertion-icon{{color:var(--pass);font-weight:900}}.assertion.bad .assertion-icon{{color:var(--fail)}}.assertion strong{{font-size:12px}}.assertion p{{margin:2px 0;color:var(--muted);font-size:12px}}.comparison{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}}.comparison span{{display:grid;gap:4px;background:#f7f8fb;border:1px solid var(--line);border-radius:8px;padding:8px}}.comparison small{{color:var(--muted);font-size:10px;font-weight:750;text-transform:uppercase;letter-spacing:.06em}}.comparison code{{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere;white-space:pre-wrap}}.error{{margin:12px 0 0 44px;padding:10px;background:#fff0f1;color:#a9272c;border-radius:8px;font:12px ui-monospace,monospace}}.empty{{background:#fff;border:1px dashed var(--line);border-radius:14px;color:var(--muted);margin:0 0 18px;padding:24px;text-align:center}}.results-count{{margin:0 0 12px;color:var(--muted);font-size:11px}}.toolbar-actions{{display:flex;gap:7px;align-items:center;margin-left:auto;flex-wrap:wrap}}.toolbar-actions button{{border:1px solid var(--line);background:#fff;padding:9px 11px;border-radius:9px;color:var(--ink-soft);cursor:pointer;font-size:11px;font-weight:700}}
footer{{color:var(--muted);font-size:12px;padding:5px 0 35px;text-align:center}}footer a{{color:#4f5cb4}}@media(max-width:700px){{.wrap{{padding:0 14px}}.summary-grid{{grid-template-columns:1fr 1fr}}.metrics{{flex-wrap:wrap}}.test-heading{{align-items:center}}.hero{{padding-top:32px}}.comparison{{grid-template-columns:1fr}}}}
</style></head><body>
<header class="hero"><div class="wrap"><div class="brand"><span class="logo">◆</span> QA Sentinel</div><h1>{_escape(report["suite"])}</h1><p class="subtitle">Run completed <time datetime="{_escape(report["finished_at"])}">{_escape(_format_timestamp(report["finished_at"]))}</time> · {report["duration_ms"]:.1f} ms total{f' · Environment: {_escape(report["environment"])}' if report.get("environment") else ''}</p>{verdict_markup}{provenance_markup}{description_markup}{profile_markup}{source_markup}</div></header>
<main class="wrap"><section class="summary-grid"><div class="summary-card"><small>Total tests</small><b>{summary["total"]}</b></div><div class="summary-card pass"><small>Passed</small><b>{summary["passed"]}</b></div><div class="summary-card{failure_class}"><small>Failed</small><b>{summary["failed"]}</b></div><div class="summary-card"><small>Success rate</small><b>{summary["success_rate"]:.1f}%</b></div></section><div class="progress" aria-label="{summary["success_rate"]:.1f}% of tests passed"><div></div></div>
<div class="toolbar"><input id="search" class="search" placeholder="Search tests, tags, or assertions…" aria-label="Search tests, tags, or assertions"><div class="filter-group" role="group" aria-label="Filter tests"><button class="filter active" data-filter="all" aria-pressed="true">All</button><button class="filter" data-filter="passed" aria-pressed="false">Passed</button><button class="filter" data-filter="failed" aria-pressed="false">Failed</button><button class="filter" data-filter="error" aria-pressed="false">Errors</button><button class="filter" data-filter="blocked" aria-pressed="false">Blocked</button><button class="filter" data-filter="skipped" aria-pressed="false">Skipped</button><button class="filter" data-filter="retried" aria-pressed="false">Retried</button><button class="filter" data-filter="slow" aria-pressed="false">Slow</button></div><label class="sort">Sort <select id="sort"><option value="default">Suite order</option><option value="status">Failures first</option><option value="latency">Slowest first</option><option value="name">Name</option></select></label><div class="toolbar-actions"><button type="button" data-action="clear">Clear search</button><button type="button" data-action="expand">Expand all</button><button type="button" data-action="download-json">Download JSON</button><button type="button" data-action="download-junit">Download JUnit</button></div></div><p id="result-count" class="results-count" aria-live="polite"></p><p id="empty" class="empty" hidden>No tests match this view. Clear the search or choose another filter.</p><section id="tests" class="test-list" aria-live="polite">{cards}</section></main>
<footer>Generated by QA Sentinel · configured and credential-shaped values redacted · <a href="../">Back to overview</a> · <a href="https://github.com/T98765SREDT/qa-sentinel">Source</a></footer>
<script type="application/json" id="report-data">{report_json}</script><script type="application/json" id="junit-data">{junit_json}</script>
<script>const q=document.querySelector('#search'),buttons=[...document.querySelectorAll('.filter')],empty=document.querySelector('#empty'),count=document.querySelector('#result-count'),list=document.querySelector('#tests'),sort=document.querySelector('#sort'),clear=document.querySelector('[data-action="clear"]'),reportData=JSON.parse(document.querySelector('#report-data').textContent),junitData=JSON.parse(document.querySelector('#junit-data').textContent);let filter='all';function matches(card,term){{const status=card.dataset.status;return card.dataset.search.includes(term)&&(filter==='all'||filter===status||(filter==='retried'&&Number(card.dataset.attempts)>1)||(filter==='slow'&&Number(card.dataset.latency)>=Number(reportData.slow_threshold_ms))}}function update(){{const term=q.value.toLowerCase();const cards=[...list.querySelectorAll('.test-card')];cards.forEach(card=>{{card.hidden=!matches(card,term)}});const visible=cards.filter(card=>!card.hidden);count.textContent=`Showing ${{visible.length}} of ${{cards.length}} tests`;empty.textContent=cards.length?'No tests match this view. Clear the search or choose another filter.':'This run contains no tests.';empty.hidden=visible.length!==0;clear.hidden=!q.value}}function sortCards(){{const cards=[...list.querySelectorAll('.test-card')];const order=sort.value;cards.sort((a,b)=>order==='latency'?Number(b.dataset.latency)-Number(a.dataset.latency):order==='status'?Number(b.dataset.status==='failed')-Number(a.dataset.status==='failed')||Number(b.dataset.latency)-Number(a.dataset.latency):order==='name'?a.querySelector('h3').textContent.localeCompare(b.querySelector('h3').textContent):0);cards.forEach(card=>list.append(card));update()}}function download(name,text,type){{const url=URL.createObjectURL(new Blob([text],{{type}}));const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),0)}}q.addEventListener('input',update);sort.addEventListener('change',sortCards);buttons.forEach(button=>button.addEventListener('click',()=>{{buttons.forEach(x=>{{x.classList.remove('active');x.setAttribute('aria-pressed','false')}});button.classList.add('active');button.setAttribute('aria-pressed','true');filter=button.dataset.filter;update()}}));clear.addEventListener('click',()=>{{q.value='';q.focus();update()}});document.querySelector('[data-action="expand"]').addEventListener('click',(event)=>{{const open=[...list.querySelectorAll('details')].some(detail=>!detail.open);list.querySelectorAll('details').forEach(detail=>detail.open=open);event.currentTarget.textContent=open?'Collapse all':'Expand all'}});document.querySelector('[data-action="download-json"]').addEventListener('click',()=>download('qa-sentinel-report.json',JSON.stringify(reportData,null,2)+'\n','application/json'));document.querySelector('[data-action="download-junit"]').addEventListener('click',()=>download('qa-sentinel-report.xml',junitData,'application/xml'));update();</script>
</body></html>'''


def write_html_report(
    result: SuiteResult, destination: str | Path, known_secrets: tuple[str, ...] = ()
) -> Path:
    """Write a self-contained, interactive HTML report."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, render_html(result, known_secrets))
    return path
