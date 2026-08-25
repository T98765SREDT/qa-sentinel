# Contributing

QA Sentinel is designed to keep test execution, assertions, redaction, and reporting independently understandable.

## Local checklist

1. Run `python3 -m unittest discover -s tests -v`.
2. Run `python3 -m compileall -q qa_sentinel tests examples`.
3. Run the demo suite and inspect the generated HTML and JSON reports, plus JUnit XML when `--junit` is used.
4. Verify that failures never reveal secret-looking values in terminal output or reports.

## Design expectations

- Preserve the dependency-free runtime unless a dependency has a documented operational benefit.
- Keep output order deterministic even when execution is concurrent.
- Treat report redaction as defense in depth, not credential storage.
