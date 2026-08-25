"""Command-line interface for QA Sentinel."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError, load_suite
from .demo_api import serve
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
    run.add_argument("--quiet", action="store_true", help="only print the final summary")
    demo = subparsers.add_parser("serve-demo", help="start the deterministic local demo API")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--verbose", action="store_true", help="log incoming demo requests")
    return parser


def _print_results(result: object, quiet: bool = False) -> None:
    from .models import SuiteResult

    if not isinstance(result, SuiteResult):
        return
    if not quiet:
        for test in result.tests:
            marker = "PASS" if test.passed else "FAIL"
            status = test.response.status if test.response.status is not None else "ERR"
            print(
                f"[{marker}] {test.case.name}  status={status}  "
                f"latency={test.response.elapsed_ms:.1f}ms  attempts={test.response.attempts}"
            )
            for assertion in test.assertions:
                if not assertion.passed:
                    print(f"       - {assertion.message}")
    print(
        f"\n{result.suite_name}: {result.passed}/{result.total} passed "
        f"({result.success_rate:.1f}%) in {result.duration_ms:.1f}ms"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve-demo":
        serve(args.host, args.port, verbose=args.verbose)
        return 0

    try:
        overrides = _variables(args.var)
        suite = load_suite(args.suite, overrides)
        result = SuiteRunner().run(suite, workers=args.workers)
        html_path = write_html_report(result, args.html, suite.known_secrets)
        json_path = write_json_report(result, args.json, suite.known_secrets)
        junit_path = (
            write_junit_report(result, args.junit, suite.known_secrets) if args.junit else None
        )
    except (ConfigError, argparse.ArgumentTypeError, ValueError) as exc:
        print(f"qa-sentinel: {exc}", file=sys.stderr)
        return 2
    _print_results(result, args.quiet)
    print(f"HTML report: {html_path}")
    print(f"JSON report: {json_path}")
    if junit_path:
        print(f"JUnit report: {junit_path}")
    return 0 if result.is_successful else 1


def entrypoint() -> None:
    raise SystemExit(main())
