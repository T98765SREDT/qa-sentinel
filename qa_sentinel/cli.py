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
from .demo_api import serve
from .redact import redact_text
from .reporting import write_html_report, write_json_report, write_junit_report
from .runner import SuiteRunner


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
    return replace(suite, tests=tuple(selected))


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
            marker = "PASS" if test.passed else "FAIL"
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
        f"({result.success_rate:.1f}%) in {result.duration_ms:.1f}ms",
        known_secrets,
    ))


def _format_value(value: object) -> str:
    """Keep CLI assertion diagnostics compact while preserving structured values."""

    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "—" if value is None else str(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve-demo":
        serve(args.host, args.port, verbose=args.verbose)
        return 0

    try:
        overrides = _variables(args.var)
        suite = load_suite(args.suite, overrides)
        if args.environment is not None:
            environment = args.environment.strip()
            if not environment or len(environment) > 80:
                raise ValueError("--environment must contain 1 to 80 characters")
            suite = replace(suite, environment=environment)
        suite = select_tests(suite, args.tag, args.exclude_tag)
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
        result = SuiteRunner().run(suite, workers=args.workers)
        html_path = write_html_report(result, args.html, suite.known_secrets)
        json_path = write_json_report(result, args.json, suite.known_secrets)
        junit_path = (
            write_junit_report(result, args.junit, suite.known_secrets) if args.junit else None
        )
    except (ConfigError, argparse.ArgumentTypeError, ValueError) as exc:
        print(f"qa-sentinel: {exc}", file=sys.stderr)
        return 2
    _print_results(result, args.quiet, suite.known_secrets)
    print(f"HTML report: {html_path}")
    print(f"JSON report: {json_path}")
    if junit_path:
        print(f"JUnit report: {junit_path}")
    return 0 if result.is_successful else 1


def entrypoint() -> None:
    raise SystemExit(main())
