"""Secret-safe JSON and self-contained HTML reports."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .models import SuiteResult
from .redact import redact, redact_text


def build_report(result: SuiteResult, known_secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    """Convert domain results into a redacted, JSON-serializable report."""

    data: dict[str, Any] = {
        "schema_version": "1.0",
        "suite": result.suite_name,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_ms": round(result.duration_ms, 3),
        "summary": {
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "success_rate": round(result.success_rate, 2),
        },
        "tests": [],
    }
    for test in result.tests:
        data["tests"].append(
            {
                "id": test.case.case_id,
                "name": test.case.name,
                "method": test.case.method,
                "url": test.case.url,
                "tags": list(test.case.tags),
                "passed": test.passed,
                "status": test.response.status,
                "latency_ms": round(test.response.elapsed_ms, 3),
                "attempts": test.response.attempts,
                "error": test.response.error,
                "started_at": test.started_at,
                "finished_at": test.finished_at,
                "assertions": [
                    {
                        "type": assertion.kind,
                        "passed": assertion.passed,
                        "message": assertion.message,
                        "expected": assertion.expected,
                        "actual": assertion.actual,
                    }
                    for assertion in test.assertions
                ],
            }
        )
    return redact(data, known_secrets)


def write_json_report(
    result: SuiteResult, destination: str | Path, known_secrets: tuple[str, ...] = ()
) -> Path:
    """Write an indented machine-readable JSON report."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_report(result, known_secrets), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def render_junit_xml(result: SuiteResult, known_secrets: tuple[str, ...] = ()) -> str:
    """Render a secret-safe JUnit XML document for CI test-report consumers."""

    suite = ET.Element(
        "testsuite",
        {
            "name": redact_text(result.suite_name, known_secrets),
            "tests": str(result.total),
            "failures": str(result.failed),
            "errors": str(sum(bool(test.response.error) for test in result.tests)),
            "time": f"{result.duration_ms / 1000:.3f}",
            "timestamp": result.started_at,
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
        if test.response.error:
            error = ET.SubElement(
                case,
                "error",
                {"message": redact_text(test.response.error, known_secrets)},
            )
            error.text = redact_text(test.response.error, known_secrets)
        elif not test.passed:
            messages = "\n".join(
                assertion.message for assertion in test.assertions if not assertion.passed
            )
            failure = ET.SubElement(
                case,
                "failure",
                {
                    "message": redact_text(messages or "API assertions failed", known_secrets),
                    "type": "AssertionError",
                },
            )
            failure.text = redact_text(messages, known_secrets)
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
    path.write_text(render_junit_xml(result, known_secrets), encoding="utf-8")
    return path


def _escape(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(str(value))


def _test_card(test: dict[str, Any]) -> str:
    state = "passed" if test["passed"] else "failed"
    icon = "✓" if test["passed"] else "×"
    tags = "".join(f'<span class="tag">{_escape(tag)}</span>' for tag in test["tags"])
    assertions = "".join(
        f'''<li class="assertion {"ok" if assertion["passed"] else "bad"}">
          <span class="assertion-icon">{"✓" if assertion["passed"] else "×"}</span>
          <div><strong>{_escape(assertion["type"])}</strong><p>{_escape(assertion["message"])}</p></div>
        </li>'''
        for assertion in test["assertions"]
    )
    error = f'<div class="error">{_escape(test["error"])}</div>' if test["error"] else ""
    return f'''<article class="test-card {state}" data-status="{state}" data-search="{_escape((test["name"] + " " + " ".join(test["tags"])).lower())}">
      <div class="test-heading">
        <div class="status-icon">{icon}</div>
        <div class="test-title"><h3>{_escape(test["name"])}</h3><div class="endpoint"><span>{_escape(test["method"])}</span> {_escape(test["url"])}</div></div>
        <span class="pill {state}">{state.upper()}</span>
      </div>
      <div class="metrics"><span>Status <b>{_escape(test["status"])}</b></span><span>Latency <b>{test["latency_ms"]:.1f} ms</b></span><span>Attempts <b>{test["attempts"]}</b></span></div>
      <div class="tags">{tags}</div>{error}
      <details><summary>{len(test["assertions"])} assertion(s)</summary><ul>{assertions}</ul></details>
    </article>'''


def render_html(result: SuiteResult, known_secrets: tuple[str, ...] = ()) -> str:
    """Render a polished standalone report with no external assets."""

    report = build_report(result, known_secrets)
    summary = report["summary"]
    cards = "".join(_test_card(test) for test in report["tests"])
    pass_width = summary["success_rate"]
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(report["suite"])} · QA Sentinel</title>
<style>
:root{{--ink:#172033;--muted:#697386;--surface:#fff;--line:#e7eaf0;--pass:#12a36d;--fail:#e5484d;--navy:#111c44;--violet:#6657d9}}
*{{box-sizing:border-box}}body{{margin:0;background:#f5f7fb;color:var(--ink);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.hero{{background:radial-gradient(circle at 90% 20%,#5362c5 0,transparent 28%),linear-gradient(135deg,#101a3c,#242760 65%,#392e70);color:#fff;padding:48px 24px 88px}}
.wrap{{max-width:1080px;margin:auto}}.brand{{display:flex;align-items:center;gap:12px;color:#cbd1ff;font-size:13px;font-weight:700;letter-spacing:.15em;text-transform:uppercase}}
.logo{{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:#fff;color:#272663;font-size:20px;box-shadow:0 8px 25px #070b1c55}}
h1{{font-size:clamp(30px,5vw,48px);margin:28px 0 8px;letter-spacing:-.04em}}.subtitle{{color:#cbd1dd;margin:0;font-size:15px}}
.summary-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:-50px}}.summary-card{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:20px 22px;box-shadow:0 12px 32px #1b244112}}
.summary-card small{{display:block;color:var(--muted);font-weight:650;text-transform:uppercase;letter-spacing:.08em;font-size:11px}}.summary-card b{{display:block;font-size:28px;margin-top:7px}}.summary-card.pass b{{color:var(--pass)}}.summary-card.fail b{{color:var(--fail)}}
.progress{{height:8px;background:#e8ebf1;border-radius:20px;overflow:hidden;margin:28px 0 32px}}.progress div{{height:100%;width:{pass_width}%;background:linear-gradient(90deg,#0c9,#45c993);border-radius:20px}}
.toolbar{{display:flex;gap:10px;align-items:center;margin-bottom:18px;flex-wrap:wrap}}.search{{flex:1;min-width:230px;border:1px solid var(--line);background:#fff;padding:12px 15px;border-radius:11px;font:inherit;outline:none}}.search:focus{{border-color:#8b85de;box-shadow:0 0 0 3px #6657d91a}}
.filter{{border:1px solid var(--line);background:#fff;padding:10px 14px;border-radius:10px;color:var(--muted);cursor:pointer;font-weight:650}}.filter.active{{background:var(--navy);color:#fff;border-color:var(--navy)}}
.test-list{{display:grid;gap:14px;padding-bottom:50px}}.test-card{{background:#fff;border:1px solid var(--line);border-left:4px solid var(--pass);border-radius:15px;padding:18px 20px;box-shadow:0 6px 20px #1b244109}}.test-card.failed{{border-left-color:var(--fail)}}
.test-heading{{display:flex;align-items:flex-start;gap:13px}}.status-icon{{width:31px;height:31px;display:grid;place-items:center;background:#e7f8f0;color:var(--pass);border-radius:50%;font-weight:900}}.failed .status-icon{{background:#ffebec;color:var(--fail)}}.test-title{{min-width:0;flex:1}}h3{{margin:1px 0 7px;font-size:17px}}.endpoint{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);font-size:12px;word-break:break-all}}.endpoint span{{font-weight:800;color:#5261b8}}
.pill{{font-size:10px;font-weight:800;padding:5px 8px;border-radius:20px;background:#e7f8f0;color:var(--pass)}}.pill.failed{{background:#ffebec;color:var(--fail)}}.metrics{{display:flex;gap:22px;margin:17px 0 10px 44px;color:var(--muted);font-size:12px}}.metrics b{{color:var(--ink)}}
.tags{{margin-left:44px}}.tag{{display:inline-block;background:#f0f1fa;color:#4f5490;border-radius:6px;padding:4px 7px;font-size:10px;margin:0 5px 5px 0}}details{{margin:12px 0 0 44px;border-top:1px solid var(--line);padding-top:11px}}summary{{cursor:pointer;color:var(--muted);font-size:12px;font-weight:650}}ul{{list-style:none;padding:8px 0 0;margin:0}}.assertion{{display:flex;gap:9px;padding:7px 0}}.assertion-icon{{color:var(--pass);font-weight:900}}.assertion.bad .assertion-icon{{color:var(--fail)}}.assertion strong{{font-size:12px}}.assertion p{{margin:2px 0;color:var(--muted);font-size:12px}}.error{{margin:12px 0 0 44px;padding:10px;background:#fff0f1;color:#a9272c;border-radius:8px;font:12px ui-monospace,monospace}}
footer{{color:var(--muted);font-size:12px;padding:5px 0 35px;text-align:center}}@media(max-width:700px){{.summary-grid{{grid-template-columns:1fr 1fr}}.metrics{{flex-wrap:wrap}}.test-heading{{align-items:center}}.hero{{padding-top:32px}}}}
</style></head><body>
<header class="hero"><div class="wrap"><div class="brand"><span class="logo">◆</span> QA Sentinel</div><h1>{_escape(report["suite"])}</h1><p class="subtitle">Run completed {_escape(report["finished_at"])} · {report["duration_ms"]:.1f} ms total</p></div></header>
<main class="wrap"><section class="summary-grid"><div class="summary-card"><small>Total tests</small><b>{summary["total"]}</b></div><div class="summary-card pass"><small>Passed</small><b>{summary["passed"]}</b></div><div class="summary-card fail"><small>Failed</small><b>{summary["failed"]}</b></div><div class="summary-card"><small>Success rate</small><b>{summary["success_rate"]:.1f}%</b></div></section><div class="progress"><div></div></div>
<div class="toolbar"><input id="search" class="search" placeholder="Search tests or tags…" aria-label="Search tests"><button class="filter active" data-filter="all">All</button><button class="filter" data-filter="passed">Passed</button><button class="filter" data-filter="failed">Failed</button></div><section id="tests" class="test-list">{cards}</section></main>
<footer>Generated by QA Sentinel · secrets automatically redacted</footer>
<script>const q=document.querySelector('#search'),buttons=[...document.querySelectorAll('.filter')];let filter='all';function update(){{const term=q.value.toLowerCase();document.querySelectorAll('.test-card').forEach(card=>{{card.hidden=!card.dataset.search.includes(term)||(filter!=='all'&&card.dataset.status!==filter)}})}}q.addEventListener('input',update);buttons.forEach(button=>button.addEventListener('click',()=>{{buttons.forEach(x=>x.classList.remove('active'));button.classList.add('active');filter=button.dataset.filter;update()}}));</script>
</body></html>'''


def write_html_report(
    result: SuiteResult, destination: str | Path, known_secrets: tuple[str, ...] = ()
) -> Path:
    """Write a self-contained, interactive HTML report."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(result, known_secrets), encoding="utf-8")
    return path
