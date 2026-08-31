"""Command-line interface for QA Sentinel."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError, load_suite
from .compare import CompareError, compare_files, format_comparison, has_regressions
from .demo_api import serve
from .doctor import diagnose, format_report
from .environment import load_environment_profile
from .planner import format_plan, plan_suite
from .redact import redact_text
from .reporting import write_html_report, write_json_report, write_junit_report
from .runner import SuiteRunner
from .scaffold import ScaffoldConflict, init_project
from .trend import TrendError, format_trend, trend_directory
from .openapi import OpenAPIImportError, import_openapi, write_import


def _variables(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError(f"Invalid --var '{value}'; expected KEY=VALUE")
        key, item = value.split("=", 1)
        if not key:
            raise argparse.ArgumentTypeError("--var key cannot be empty")
        result[key] = item
    return result


def _tag(value: str) -> str:
    tag = value.strip()
    if not tag:
        raise argparse.ArgumentTypeError("tag cannot be empty")
    return tag


def select_tests(suite: object, include_tags: list[str], exclude_tags: list[str]) -> object:
    """Return a suite filtered by tags; repeated include tags use OR semantics."""

    from .models import TestSuite

    if not isinstance(suite, TestSuite):
        return suite
    includes = {tag.casefold() for tag in include_tags}
    excludes = {tag.casefold() for tag in exclude_tags}
    selected = [
        case for case in suite.tests
        if (not includes or includes.intersection(tag.casefold() for tag in case.tags))
        and not excludes.intersection(tag.casefold() for tag in case.tags)
    ]
    if not selected:
        requested = ", ".join(include_tags) if include_tags else "the exclude filters"
        raise ConfigError(f"Tag selection matched zero tests for {requested}.")
    return replace(suite, tests=tuple(selected), selected_tags=tuple(include_tags))


def _add_tag_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tag",
        action="append",
        type=_tag,
        default=[],
        metavar="TAG",
        help="include tests carrying any selected tag (repeat for OR semantics)",
    )
    parser.add_argument(
        "--exclude-tag",
        action="append",
        type=_tag,
        default=[],
        metavar="TAG",
        help="exclude tests carrying any selected tag (applied after --tag)",
    )


def _add_environment(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env",
        type=Path,
        metavar="PROFILE",
        help="load reusable variables and declared secrets from an environment profile",
    )
    parser.add_argument(
        "--environment",
        metavar="NAME",
        help="label this run with an environment name (overrides the suite label)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qa-sentinel",
        description="Run API regression suites with fast, secret-safe reports.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run a JSON test suite")
    run.add_argument("suite", type=Path, help="path to the JSON suite")
    run.add_argument("--html", type=Path, default=Path("qa-sentinel-report.html"))
    run.add_argument("--json", type=Path, default=Path("qa-sentinel-report.json"))
    run.add_argument(
        "--junit",
        type=Path,
        help="optional JUnit XML report for CI test-report consumers",
    )
    run.add_argument("--workers", type=int, help="override suite concurrency (1-64)")
    run.add_argument("--fail-fast", action="store_true", help="stop ordinary workflow steps after the first failure")
    run.add_argument("--max-failures", type=int, metavar="N", help="stop ordinary workflow steps after N failures")
    run.add_argument("--dry-run", action="store_true", help="show the dependency plan without sending requests")
    run.add_argument("--var", action="append", default=[], metavar="KEY=VALUE", help="override a suite variable")
    _add_tag_filters(run)
    run.add_argument("--quiet", action="store_true", help="only print the final summary")
    _add_environment(run)
    validate = subparsers.add_parser("validate", help="validate a suite without sending requests")
    validate.add_argument("suite", type=Path, help="path to the JSON suite")
    validate.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a suite variable during validation",
    )
    _add_tag_filters(validate)
    _add_environment(validate)
    doctor = subparsers.add_parser(
        "doctor", help="check a suite and local tooling without sending requests"
    )
    doctor.add_argument("suite", type=Path, help="path to the JSON suite")
    doctor.add_argument(
        "--env", type=Path, help="optional environment profile used for local variable values"
    )
    doctor.add_argument(
        "--html", type=Path, help="optional HTML report path to check for writability"
    )
    doctor.add_argument(
        "--json", type=Path, help="optional JSON report path to check for writability"
    )
    doctor.add_argument(
        "--junit", type=Path, help="optional JUnit report path to check for writability"
    )
    doctor.add_argument(
        "--var", action="append", default=[], metavar="KEY=VALUE", help="override a suite variable"
    )
    compare = subparsers.add_parser(
        "compare", help="compare a current JSON report with a compatible baseline"
    )
    compare.add_argument("current", type=Path, help="current JSON report")
    compare.add_argument("--baseline", required=True, type=Path, help="previous JSON report")
    compare.add_argument(
        "--format", choices=("text", "json"), default="text", help="output format"
    )
    trend = subparsers.add_parser(
        "trend", help="summarize JSON report history in a directory"
    )
    trend.add_argument("directory", type=Path, help="directory containing JSON reports")
    trend.add_argument(
        "--format", choices=("text", "json"), default="text", help="output format"
    )
    import_openapi_parser = subparsers.add_parser(
        "import-openapi", help="generate a reviewable suite from an OpenAPI 3 document"
    )
    import_openapi_parser.add_argument("spec", type=Path, help="local OpenAPI 3 JSON/YAML document")
    import_openapi_parser.add_argument("--out", required=True, type=Path, help="generated suite JSON path")
    import_openapi_parser.add_argument(
        "--allow-method",
        action="append",
        default=[],
        metavar="METHOD",
        help="allow a write method (repeat for POST/PUT/PATCH/DELETE)",
    )
    import_openapi_parser.add_argument(
        "--force", action="store_true", help="replace an existing generated suite"
    )
    init = subparsers.add_parser(
        "init", help="create a minimal suite, environment profile, and CI starter"
    )
    init.add_argument(
        "directory", nargs="?", type=Path, default=Path("."), help="directory to initialize"
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="overwrite only the generated starter files listed by init",
    )
    demo = subparsers.add_parser("serve-demo", help="start the deterministic local demo API")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--verbose", action="store_true", help="log incoming demo requests")
    return parser


def _print_results(
    result: object, quiet: bool = False, known_secrets: tuple[str, ...] = ()
) -> None:
    from .models import SuiteResult

    if not isinstance(result, SuiteResult):
        return
    if not quiet:
        for test in result.tests:
            marker = {
                "passed": "PASS",
                "failed": "FAIL",
                "error": "ERROR",
                "blocked": "BLOCKED",
                "skipped": "SKIPPED",
            }.get(test.status, "FAIL")
            status = test.response.status if test.response.status is not None else "ERR"
            print(redact_text(
                f"[{marker}] {test.case.name}  status={status}  "
                f"latency={test.response.elapsed_ms:.1f}ms  attempts={test.response.attempts}",
                known_secrets,
            ))
            for assertion in test.assertions:
                if not assertion.passed:
                    print(redact_text(f"       - {assertion.message}", known_secrets))
                    if assertion.expected is not None or assertion.actual is not None:
                        expected = _format_value(assertion.expected)
                        actual = _format_value(assertion.actual)
                        print(redact_text(f"         expected={expected} actual={actual}", known_secrets))
    print(redact_text(
        f"\n{result.suite_name}: {result.passed}/{result.total} passed "
        f"({result.success_rate:.1f}%) · failed={result.failed} "
        f"errors={result.errors} blocked={result.blocked} skipped={result.skipped} "
        f"in {result.duration_ms:.1f}ms"
        f'{" · interrupted" if result.interrupted else ""}',
        known_secrets,
    ))


def _format_value(value: object) -> str:
    """Keep CLI assertion diagnostics compact while preserving structured values."""

    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "—" if value is None else str(value)


def _format_dry_run(plan: object, suite: object) -> str:
    """Render a redacted execution preview without contacting any endpoint."""

    from .models import TestSuite

    if not isinstance(suite, TestSuite):
        return str(plan)
    lines = [format_plan(plan)]
    by_id = plan.by_id  # type: ignore[attr-defined]
    for layer in plan.layers:  # type: ignore[attr-defined]
        for step_id in layer.step_ids:
            case = by_id[step_id]
            tags = ", ".join(case.tags) or "—"
            dependency = ", ".join(case.depends_on) or "—"
            lines.append(
                redact_text(
                    f"    {case.case_id}: {case.method} {case.url} "
                    f"tags=[{tags}] depends_on=[{dependency}] run_if={case.run_if}",
                    suite.known_secrets,
                )
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        try:
            created = init_project(args.directory, force=args.force)
        except ScaffoldConflict as exc:
            print("qa-sentinel init: no files changed.", file=sys.stderr)
            print("Existing generated file(s):", file=sys.stderr)
            for path in exc.conflicts:
                print(f"  - {path}", file=sys.stderr)
            print("Use --force only when you intend to replace these starter files.", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"qa-sentinel init: unable to create starter files: {redact_text(str(exc))}", file=sys.stderr)
            return 2
        print(f"Initialized QA Sentinel in {args.directory.resolve()}")
        print("Created:")
        for path in created:
            print(f"  - {path}")
        print("\nNext commands:")
        print("  qa-sentinel validate suites/smoke.json")
        print("  qa-sentinel doctor suites/smoke.json --env environments/local.json")
        print("  python3 -m qa_sentinel serve-demo")
        print("  qa-sentinel run suites/smoke.json")
        return 0
    if args.command == "serve-demo":
        serve(args.host, args.port, verbose=args.verbose)
        return 0

    if args.command == "compare":
        try:
            comparison = compare_files(args.current, args.baseline)
        except CompareError as exc:
            print(f"qa-sentinel compare: {exc}", file=sys.stderr)
            return 2
        if args.format == "json":
            print(json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_comparison(comparison))
        return 1 if has_regressions(comparison) else 0

    if args.command == "trend":
        try:
            summary = trend_directory(args.directory)
        except TrendError as exc:
            print(f"qa-sentinel trend: {exc}", file=sys.stderr)
            return 2
        if args.format == "json":
            print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_trend(summary))
        return 0

    if args.command == "import-openapi":
        try:
            allowed_methods = tuple(str(method).upper() for method in args.allow_method)
            result = import_openapi(args.spec, allowed_methods)
            output_path = write_import(result, args.out, force=args.force)
        except OpenAPIImportError as exc:
            print(f"qa-sentinel import-openapi: {exc}", file=sys.stderr)
            return 2
        print(f"Generated suite: {output_path}")
        print(
            f"Imported {len(result.imported)} operation(s); skipped {len(result.skipped)}; "
            f"warnings {len(result.warnings)}"
        )
        for item in result.skipped:
            print(f"  SKIP {item['path']}: {item['reason']}")
        for warning in result.warnings:
            print(f"  WARN {warning}")
        return 0

    try:
        overrides = _variables(args.var)
        if args.command == "doctor":
            output_paths = tuple(
                path for path in (args.html, args.json, args.junit) if path is not None
            )
            report = diagnose(
                args.suite,
                environment_path=args.env,
                output_paths=output_paths,
                overrides=overrides,
            )
            print(format_report(report))
            return 0 if report.passed else 2
        profile = (
            load_environment_profile(args.env, require_secrets=True)
            if getattr(args, "env", None) is not None
            else None
        )
        suite = load_suite(args.suite, overrides, environment_profile=profile)
        if args.environment is not None:
            environment = args.environment.strip()
            if not environment or len(environment) > 80:
                raise ValueError("--environment must contain 1 to 80 characters")
            suite = replace(suite, environment=environment)
        suite = select_tests(suite, args.tag, args.exclude_tag)
        # Validate the full dependency graph before either a validation result
        # or a network run is reported.
        plan = plan_suite(suite)
        if args.command == "validate":
            print(
                redact_text(
                    f"Valid suite: {suite.name}"
                    f"{f' [{suite.environment}]' if suite.environment else ''} "
                    f"({len(suite.tests)} test(s))",
                    suite.known_secrets,
                )
            )
            return 0
        if args.dry_run:
            print(_format_dry_run(plan, suite))
            print(f"Environment: {suite.environment or 'default'}")
            print(f"Workers: {args.workers or suite.workers}")
            print(f"Tests: {len(suite.tests)}")
            print("No requests sent (dry run).")
            return 0
        result = SuiteRunner().run(
            suite,
            workers=args.workers,
            fail_fast=args.fail_fast,
            max_failures=args.max_failures,
        )
        report_secrets = result.known_secrets or suite.known_secrets
        html_path = write_html_report(result, args.html, report_secrets)
        json_path = write_json_report(result, args.json, report_secrets)
        junit_path = (
            write_junit_report(result, args.junit, report_secrets) if args.junit else None
        )
    except (ConfigError, argparse.ArgumentTypeError, ValueError) as exc:
        print(f"qa-sentinel: {exc}", file=sys.stderr)
        return 2
    report_secrets = result.known_secrets or suite.known_secrets
    _print_results(result, args.quiet, report_secrets)
    print(f"HTML report: {html_path}")
    print(f"JSON report: {json_path}")
    if junit_path:
        print(f"JUnit report: {junit_path}")
    return 0 if result.is_successful else 1


def entrypoint() -> None:
    raise SystemExit(main())
