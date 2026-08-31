# QA Sentinel

**Run repeatable API checks from JSON and get useful terminal, HTML, JSON, and JUnit results.**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![stdlib only](https://img.shields.io/badge/runtime-stdlib%20only-12a36d)](pyproject.toml)
[![149 tests](https://img.shields.io/badge/tests-149%20passing-6657d9)](tests/)
[![license: MIT](https://img.shields.io/badge/license-MIT-172033)](LICENSE)
[![CI](https://github.com/T98765SREDT/qa-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/T98765SREDT/qa-sentinel/actions/workflows/ci.yml)

QA Sentinel is a dependency-free Python CLI for small API regression suites. It validates response contracts, runs independent checks concurrently, retries eligible transient failures, and writes reports that are suitable for local review or CI artifacts.

[View the passing sample report](docs/sample-report.html) · [View the intentional failure report](docs/sample-failure-report.html) · [Browse the source](https://github.com/T98765SREDT/qa-sentinel) · [Review the security policy](SECURITY.md)

![QA Sentinel HTML report from the synthetic local demo](docs/qa-sentinel-report.png)

## Quickstart

Requires Python 3.10 or newer. The core CLI has no runtime dependencies. To
import OpenAPI YAML files, install the optional parser extra:

```bash
python3 -m pip install 'qa-sentinel[yaml]'
```

To start from a clean repository, create a minimal suite and inspect it before
running any request:

```bash
qa-sentinel init
qa-sentinel validate suites/smoke.json
qa-sentinel doctor suites/smoke.json --env environments/local.json
```

`init` never overwrites an existing generated file unless `--force` is passed.
`doctor` is non-destructive and sends no requests; it checks suite parsing,
profile values, referenced environment-variable names, report destinations,
and the local Python/CLI setup.

Start the deterministic demo API in one terminal:

```bash
python3 -m qa_sentinel serve-demo
```

Validate the suite without sending requests, then run its five checks in another terminal:

```bash
python3 -m qa_sentinel validate examples/demo-suite.json

python3 -m qa_sentinel run examples/demo-suite.json \
  --html qa-sentinel-report.html \
  --json qa-sentinel-report.json \
  --junit qa-sentinel-report.xml
```

Run only a tagged subset when a full suite is unnecessary. Repeated `--tag` values use OR semantics; `--exclude-tag` is applied afterward. A selection that matches zero tests exits with code `2` before any request is sent. Add an optional top-level `environment` label to keep local, staging, and production reports distinguishable, or load a reusable profile with `--env` and override the label with `--environment` when needed.

```bash
python3 -m qa_sentinel run suite.json --tag smoke --tag contract --exclude-tag destructive
```

A successful run prints one line per check and finishes with a summary:

```text
[PASS] Service health and version contract  status=200  latency=...ms  attempts=1
[PASS] Nested user response contract  status=200  latency=...ms  attempts=1
[PASS] POST body serialization and echo  status=201  latency=...ms  attempts=1
[PASS] Transient failure recovers after retry  status=200  latency=...ms  attempts=2
[PASS] Endpoint stays within latency budget  status=200  latency=...ms  attempts=1

QA Sentinel Demo Regression Suite: 5/5 passed (100.0%) in ...ms
```

The same run creates:

- a self-contained HTML report with search and pass/fail filters;
- structured schema-v2 JSON for scripts and other automation ([schema](schemas/report-v2.schema.json));
- optional JUnit XML for CI test-report viewers.

Every report carries a run ID, tool version, suite/config fingerprints, selected
tags, retry/worker settings, and explicitly supplied CI provenance. Failed
checks include a redacted `curl` reproduction command, assertion paths, HTTP
status/content type, response size, latency, and attempts. Report files are
written through a same-directory atomic replacement, so an interrupted write
does not destroy the previous artifact.

Compare a run with a compatible JSON baseline to see only meaningful changes:

```bash
qa-sentinel compare qa-sentinel-report.json --baseline previous-report.json
qa-sentinel compare qa-sentinel-report.json --baseline previous-report.json --format json
```

The comparison identifies new, fixed, and persistent failures as well as added
or removed test IDs. It does not classify pass/fail changes when suite or
environment fingerprints differ. Keep a directory of JSON artifacts to build
a deterministic history summary:

```bash
qa-sentinel trend artifacts/runs/
qa-sentinel trend artifacts/runs/ --format json
```

Trend output reports per-test run count, success and retry rates, p50/p95
latency, and first/last failure timestamps. Incompatible runs remain in
separate groups, and duplicate run IDs or corrupt artifacts are reported with
the offending path.

Generate a reviewable suite from a local OpenAPI 3 document without sending
requests:

```bash
qa-sentinel import-openapi openapi.json --out suites/generated.json
qa-sentinel import-openapi openapi.json --out suites/generated.json \
  --allow-method POST --allow-method PUT
```

The importer follows only local JSON Pointer references, uses declared
examples/defaults for parameters and request bodies, and imports GET/HEAD by
default. Write methods require explicit opt-in. Unsupported operations and
missing required examples are listed as skipped coverage; credentials are
never generated from security schemes or credential-shaped examples. JSON
works with the standard library; YAML requires the optional PyYAML package.

The [JobFlow integration example](examples/jobflow/README.md) runs the same
workflow engine against a separate local-first application: it creates a
synthetic record, captures its ID and version, reads the workspace, verifies a
stale-version `409`, and always deletes the record. The automated integration
test uses an ephemeral port and temporary SQLite database; the intentional
failure fixture proves that cleanup and HTML/JSON/JUnit diagnostics still work.
When QA Sentinel is cloned by itself, these three cross-project checks are
reported as explicit skips until a sibling JobFlow checkout is available; the
core test suite remains runnable without that repository.

To inspect the failure-first diagnostics without touching a production service, run `examples/failure-suite.json` against the local demo API. It intentionally exercises status, JSON, header, latency, retry, and redaction failures.

Open the checked-in examples: [HTML report](docs/sample-report.html) · [JSON report](docs/sample-report.json)

To install the command locally:

```bash
python3 -m pip install -e .
qa-sentinel validate examples/demo-suite.json
```

The package metadata exposes the repository and issue tracker, and the CI
workflow verifies the installed console command on Ubuntu, macOS, and Windows.
It also builds both a wheel and source archive, installs the wheel into a clean
virtual environment, runs `init` → `validate` → the local demo API, and uploads
the resulting HTML, JSON, and JUnit reports as a reviewable artifact.

For measured local evidence, run the [localhost-only benchmark](docs/benchmark.md).
The [architecture guide](ARCHITECTURE.md) explains the planner, scheduler,
secret boundary, and report pipeline without claiming load-test scalability.

## What a suite can verify

| Check | Example | Result |
| --- | --- | --- |
| HTTP status | `{"type": "status", "equals": 200}` | exact status or allowed status list |
| JSON value | `{"type": "json_path", "path": "data.id", "equals": 7}` | nested objects and array indexes |
| JSON presence | `{"type": "json_path", "path": "data.email", "exists": false}` | required or intentionally absent fields |
| Header | `{"type": "header", "name": "Content-Type"}` | presence or exact value |
| Body text | `{"type": "body_contains", "value": "ready"}` | substring in decoded response text |
| Latency | `{"type": "latency", "max_ms": 750}` | response stays within a budget |

Configuration and assertion shapes are validated before the first network request. Unknown fields, malformed assertion shapes, and invalid limits are rejected with the relevant suite/test path. Invalid configuration exits with code `2` instead of producing a partial run.

For a workflow-shaped suite, opt into schema v2 and give every step a stable
ID. The planner checks dependencies, self-references, and cycles before a run;
it produces declaration-order topological layers without sending requests.

```json
{
  "schemaVersion": 2,
  "name": "Order smoke workflow",
  "tests": [
    {
      "id": "health",
      "name": "Health",
      "url": "https://staging.example.com/health",
      "assertions": [{"type": "status", "equals": 200}]
    },
    {
      "id": "cleanup",
      "name": "Cleanup",
      "depends_on": ["health"],
      "run_if": "always",
      "url": "https://staging.example.com/cleanup",
      "assertions": [{"type": "status", "equals": 204}]
    }
  ]
}
```

Workflow steps are scheduled only after their dependencies finish. JSON,
headers, cookies, and status values can be captured and referenced by later
steps with `{{steps.id.capture}}`. Unsuccessful dependencies become `blocked`
without sending a request; `run_if: "always"` is useful for cleanup steps.
Use `--dry-run` to inspect layers and redacted URLs, `--fail-fast` to stop
ordinary work after a failure, or `--max-failures N` to stop after a bounded
number of failed/error steps.

For one complete stateful example, see the [order lifecycle workflow](examples/order-workflow/README.md).
It uses a loopback-only synthetic API to authenticate, create an order, read
it, update its status, verify the contract, and always clean it up. The same
workflow has an intentional failure variant showing a blocked dependent step
and a successful cleanup.

## Suite format

```json
{
  "name": "Production smoke suite",
  "environment": "staging",
  "slow_threshold_ms": 500,
  "workers": 4,
  "variables": {
    "base_url": "https://api.example.com",
    "api_token": "${API_TOKEN}"
  },
  "defaults": {
    "timeout_seconds": 5,
    "max_response_bytes": 2097152,
    "retries": 2,
    "retry_delay_seconds": 0.25,
    "retry_on_status": [429, 500, 502, 503, 504],
    "headers": {
      "Authorization": "Bearer {{api_token}}"
    }
  },
  "tests": [
    {
      "name": "Current user contract",
      "method": "GET",
      "url": "{{base_url}}/v1/me",
      "tags": ["smoke", "contract"],
      "assertions": [
        {"type": "status", "equals": 200},
        {"type": "json_path", "path": "data.id", "exists": true},
        {"type": "json_path", "path": "data.plan", "equals": "pro"},
        {"type": "latency", "max_ms": 750}
      ]
    }
  ]
}
```

Variables use `{{name}}`; environment values use `${NAME}`. Non-secret values can be replaced from the command line:

```bash
python3 -m qa_sentinel run suite.json \
  --var base_url=https://staging.example.com
```

Use environment variables for credentials. Missing environment variables are rejected during validation.

For reusable local/staging/CI settings, keep non-secret variables and secret
sources in an environment profile instead of duplicating suites or putting a
credential in `--var`:

```json
{
  "name": "staging",
  "variables": {"base_url": "https://staging.example.com"},
  "secrets": {
    "api_token": {"from_env": "QA_API_TOKEN"}
  }
}
```

Run the same suite against that profile with:

```bash
QA_API_TOKEN="set-in-your-shell-or-CI" \
  python3 -m qa_sentinel run suite.json --env environments/staging.json
```

Resolution is deterministic: suite variables, then profile variables, then
explicit non-secret `--var` overrides. Declared secrets are resolved only from
their `from_env` source; `--var api_token=...` is rejected so credentials do
not end up in shell history. Reports include the profile name, a hash of its
non-secret configuration, and secret name/source metadata—never secret values.

## Reliability and safety boundaries

- Tests run in parallel while reports retain the order declared in the suite.
- Response bodies are read in bounded chunks with a default 2 MiB limit; a test can opt into a smaller limit with `max_response_bytes`.
- The HTML report's `Slow` filter uses the suite's `slow_threshold_ms` value (500 ms by default) instead of a hidden fixed cutoff.
- Failed checks include bounded response previews chosen by media type; binary bodies are omitted, long text is clipped, and preview/error values are redacted before and after truncation.
- Transport errors and configured transient statuses can be retried with exponential backoff.
- Automatic retries apply only to idempotent methods by default. A non-idempotent request requires `"retry_non_idempotent": true` to opt in.
- Retries are limited to five; each backoff delay is capped at 30 seconds.
- URLs require an HTTP or HTTPS scheme and a hostname. Embedded URL usernames and passwords are rejected.
- HTTPS-to-HTTP redirect downgrades are blocked. Authorization, cookies, API keys, and similar headers are removed on cross-origin redirects.
- Resolved environment secrets and credential-shaped values are redacted from terminal output, assertion diagnostics, HTML, JSON, and JUnit reports.
- Workflow results distinguish `passed`, `failed`, `error`, `blocked`, and `skipped`; reports retain declaration order and include capture names without capture values.
- `--dry-run` sends no requests. Failure controls stop only newly scheduled ordinary steps while eligible `run_if: "always"` cleanup steps still run.
- CLI exit codes are stable: `0` for a passing suite, `1` for failed checks, and `2` for invalid configuration.

Redaction is defense in depth, not credential storage. See [SECURITY.md](SECURITY.md) for the supported boundary.

For a failed test, the JSON and HTML reports add a `response_preview` object with
the media type, original byte count, a bounded text preview, and a `truncated`
flag. Text and JSON responses get useful snippets; binary response bodies are
represented by metadata only. This keeps CI artifacts actionable without
turning them into unbounded response dumps.

## Architecture

```text
JSON suite
   │
   ▼
environment.py ── profile validation, precedence, secret provenance
   │
   ▼
config.py ── interpolation, validation, normalization
   │
   ▼
planner.py ── schema v2 dependency validation and stable layers
   │
   ▼
capture.py ── typed response captures and step-reference substitution
   │
   ▼
runner.py ── bounded thread pool, declaration-order results
   │
   ├── http_client.py ── HTTP transport, redirects, retries, timeouts
   └── assertions.py  ── response contract evaluation
   │
   ▼
redact.py ── credential removal at the output boundary
   │
   ├── reporting.py ── HTML, JSON, and JUnit XML
   └── cli.py       ── commands, terminal output, exit codes
```

Immutable dataclasses carry normalized test cases and results between the transport, assertion, orchestration, and reporting layers. An optional suite environment is carried with the immutable result so terminal output, JSON, HTML, and JUnit artifacts can be traced back to `local`, `staging`, or another explicitly named target. Profile hashes and secret source names provide reproducible provenance without serializing secret values. Report formatting does not determine whether a test passes.

## Verification

Run all 149 unit and integration tests:

```bash
python3 -m unittest discover -s tests -v
```

Check that every Python source file compiles:

```bash
python3 -m compileall -q qa_sentinel tests examples
```

The test suite covers configuration validation, unknown-field paths, ambiguous JSON predicates, JSON paths, core assertion evaluation, response charset decoding and size limits, deterministic concurrency, retry eligibility and caps, redirect policy, report generation, tag selection, JUnit counts, CLI exit behavior, secret removal, URL-encoded redaction, environment precedence and provenance, workflow dependency planning, typed response captures and safe substitution, DAG scheduling, failure controls, cleanup, interruption recovery, safe scaffolding, request-free doctor diagnostics, report schema-v2 provenance and atomic artifacts, baseline classifications, incompatible fingerprints, trend percentiles, deterministic artifact validation, safe OpenAPI import coverage, and cross-project JobFlow lifecycle/cleanup contracts. Integration tests bind the demo API and JobFlow to ephemeral local ports. CI runs the checks defined in [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Project layout

```text
qa_sentinel/          CLI, configuration, transport, assertions, runner, reports, comparison, trend analysis and OpenAPI import
tests/                unit and integration tests
examples/             deterministic demo suites, order workflow, and servers
docs/                 generated HTML/JSON report examples and screenshot
.github/workflows/    test and Pages workflows
```

## Current scope and limitations

- Designed for small functional and regression suites, not load or performance testing.
- Does not run browser checks or execute arbitrary scripts. Workflow chaining is intentionally limited to declared response captures and JSON-defined HTTP steps; it is not a general-purpose shell or load-testing engine.
- JSON assertions support the documented dot-and-index path syntax; this is not a full JSONPath implementation.
- The standard-library transport keeps installation simple but does not expose the connection pooling and timeout controls of a dedicated HTTP client.
- Redaction recognizes configured secrets and common credential shapes, but reports should still be handled as potentially sensitive CI artifacts.

## Contributing and releases

[CONTRIBUTING.md](CONTRIBUTING.md) contains the local verification checklist. User-visible changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
