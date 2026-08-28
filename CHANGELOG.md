# Changelog

All notable changes to QA Sentinel are documented here. This project follows a lightweight semantic versioning approach for its public CLI and report formats.

## Unreleased

- Added optional suite environment labels to CLI validation and HTML, JSON, and JUnit outputs, with a `--environment` run-time override.
- Added OR-based `--tag` selection and post-filter `--exclude-tag` support with an explicit zero-match configuration error.
- Expanded the automated suite to 50 checks and isolated the retry demo fixtures for repeatable integration runs.
- Added expected-versus-actual diagnostics to failed CLI and HTML assertions.
- Added searchable assertion text, accessible filter state, human-readable report timestamps, and an empty-results state.
- Refreshed the checked-in sample report and expanded integration coverage to 45 tests.

## 1.2.0 — 2026-08-27

- Added `qa-sentinel validate` so configuration and assertions can be checked without sending network requests.
- Validated assertion shapes, URL hostnames, URL user information, retry limits, and backoff limits before execution.
- Added a redirect policy that blocks HTTPS downgrades and removes credential-bearing headers across origins.
- Limited automatic retries to idempotent methods unless `retry_non_idempotent` is explicitly enabled.
- Redacted resolved environment secrets from terminal, JSON, HTML, and JUnit output.
- Corrected JUnit counts so transport errors are not also reported as assertion failures.

## 1.1.0 — 2026-08-25

- Added optional `--junit` XML output for CI test-report consumers.
- Applied the existing secret-redaction boundary to JUnit output.
- Added integration coverage for JUnit failure reporting and redaction.

## 1.0.0 — 2026-08-24

- Released the first portfolio version: declarative suites, concurrent execution, retry/backoff, assertions, redaction, and JSON/HTML reporting.
