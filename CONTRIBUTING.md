# Contributing

QA Sentinel is designed to keep test execution, assertions, redaction, and reporting independently understandable.

## Local checklist

1. Run `python3 -m unittest discover -s tests -v` (149 checks with the sibling JobFlow checkout; standalone clones report its three optional integration checks as skipped).
2. Run `python3 -m compileall -q qa_sentinel tests examples`.
3. Run `qa-sentinel init` in a disposable directory, then verify that a repeat init refuses without changing files.
4. Run `qa-sentinel doctor suites/smoke.json --env environments/local.json` and confirm it sends no requests or probe writes.
5. Exercise a profile with `--env`, including precedence and refusal of `--var` for declared secrets.
6. Run the demo suite and inspect the generated HTML and JSON reports, plus JUnit XML when `--junit` is used.
7. Verify that failures never reveal secret-looking values or absolute local paths in terminal output or reports.
8. Build both release artifacts with `python -m build --sdist --wheel` and run the clean-wheel smoke check when packaging changes are involved.
9. For performance-sensitive changes, run `python3 scripts/benchmark.py --requests 100 --repetitions 3 --output benchmark.json` and report the machine/runtime with the result; do not treat it as a production load test.

## Design expectations

- Preserve the dependency-free runtime unless a dependency has a documented operational benefit.
- Keep optional dependencies isolated behind an explicit install extra; the core CLI must remain installable without them.
- Keep output order deterministic even when execution is concurrent.
- Treat report redaction as defense in depth, not credential storage.
