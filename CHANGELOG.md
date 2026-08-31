# Changelog

All notable changes to QA Sentinel are documented here. This project follows a lightweight semantic versioning approach for its public CLI and report formats.

## Unreleased

- Hardened CI with read-only repository permissions and cancellation of stale runs on the same ref.
- Decode response bodies using a declared `Content-Type` charset, with a deterministic UTF-8 fallback for unknown declarations.
- Expanded the automated suite to 52 checks with regression coverage for legacy non-UTF-8 API responses.
- Bound response buffering to 2 MiB by default, with a lower per-test limit and a clear non-retryable diagnostic when a response is too large.
- Expanded the automated suite to 56 checks with response-limit validation and transport coverage.
- Reject unsupported fields at suite, defaults, test, and assertion boundaries instead of silently ignoring configuration mistakes.
- Expanded the automated suite to 60 checks with typo and path-specific validation coverage.
- Reject ambiguous JSON assertions that combine multiple predicates, while retaining path-only existence checks for compatibility.
- Expanded the automated suite to 61 checks with predicate validation coverage.
- Make the report's `Slow` filter use the suite-level `slow_threshold_ms` setting, defaulting to 500 ms.
- Expanded the automated suite to 62 checks with configurable-threshold coverage.
- Add bounded, content-aware response previews for failed checks and bounded error diagnostics; binary bodies are omitted and preview values are redacted before and after clipping.
- Expanded the automated suite to 63 checks with report preview and path-scrubbing coverage.
- Added `qa-sentinel init` for a minimal smoke suite, local profile, CI starter, and exact next commands without overwriting existing generated files by default.
- Added request-free `qa-sentinel doctor` diagnostics for suite/profile validation, missing environment-variable names, output destinations, and Python/CLI readiness.
- Expanded the automated suite to 74 checks with scaffold conflict, profile, CLI, and doctor coverage.
- Added reusable environment profiles with deterministic variable precedence, declared secret sources, stable non-secret configuration fingerprints, and safe `--env` loading for `run` and `validate`.
- Rejected CLI overrides for declared secrets and added URL-encoded credential redaction, while reports expose only secret names and source environment-variable names.
- Expanded the automated suite to 82 checks with profile resolution, missing-secret, provenance, hashing, and encoded-redaction coverage.
- Added suite v2 schema normalization and a stable, request-free DAG planner with explicit dependencies, cycle/unknown-reference checks, declaration-order layers, `run_if`, `cleanup`, and extraction metadata validation.
- Independent v1 execution refuses workflow fields rather than silently ignoring dependencies; added the canonical `schemas/suite-v2.schema.json`.
- Expanded the automated suite to 94 checks with workflow schema and planner coverage.
- Added typed, immutable response captures for JSON/header/cookie/status sources and safe `{{steps.id.capture}}` substitution helpers; structured captures cannot be embedded into scalar strings.
- Expanded the automated suite to 107 checks with capture extraction, immutable-store, substitution, and static-reference coverage.
- Added the schema-v2 DAG scheduler with declaration-ordered dependency layers, typed runtime captures, blocked/skipped/error states, fail-fast and max-failures controls, always-run cleanup, safe interruption recovery, and request-free `--dry-run` previews.
- Reports now distinguish workflow state from HTTP status and include bounded capture metadata, status counts, and interrupted-run provenance.
- Expanded the automated suite to 116 checks with workflow scheduling, cleanup, failure-control, interruption, dry-run, and multi-status report coverage.
- Added a complete loopback-only order lifecycle example with login/token capture, stateful create/read/update/verify steps, intentional assertion failure, blocked audit behavior, and always-run cleanup.
- Expanded integration coverage to 120 checks, including success, assertion-failure cleanup, transport-error cleanup, and loopback binding paths.
- Upgraded reports to schema v2 with run/tool metadata, secret-free suite fingerprints, explicit CI provenance, selected execution settings, assertion paths, response metadata, and redacted curl reproduction commands.
- Added atomic report replacement for JSON, HTML, and JUnit artifacts and expanded coverage to 127 checks for schema shape, configuration hash stability, reproduction safety, CI provenance, and interrupted writes.
- Added baseline comparison and trend summaries with stable test-ID classifications, compatibility guards, latency percentiles, retry rates, failure windows, duplicate-run detection, and clear corrupt/empty artifact errors; coverage is now 136 checks.
- Added the limited safe OpenAPI 3 importer: local JSON Pointer refs only, deterministic GET/HEAD generation, explicit write-method opt-in, example-aware parameters and request bodies, credential redaction, atomic output, and coverage warnings; coverage is now 146 checks.
- Added the synthetic JobFlow cross-project integration workflow with captured IDs/versions, workspace contract checks, stale-version `409` verification, cleanup proof, and passing/failing HTML/JSON/JUnit artifacts; coverage is now 149 checks.
- Added an isolated `yaml` install extra for OpenAPI YAML parsing, cross-platform installed-CLI smoke checks, wheel/sdist metadata verification, clean-wheel demo execution, and Actions summaries/artifact uploads for packaging evidence.
- Added a localhost-only benchmark with independent worker comparisons, workflow/report/trend coverage, runtime metadata, explicit limitations, and a standalone architecture guide.
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
