# QA Sentinel architecture

QA Sentinel turns a small JSON document into a repeatable, reviewable API run.
The design keeps configuration, transport, orchestration, and reporting
separate so a failure can be explained without reading the whole codebase.

```text
suite.json + optional environment profile
                │
                ▼
        config.py / environment.py
        parse, validate, interpolate, fingerprint
                │
                ├── v1 independent cases
                │
                ▼
        planner.py (schema v2 only)
        validate references, cycles, and stable DAG layers
                │
                ▼
        runner.py
        schedule layers, preserve declaration order, capture values,
        block failed dependencies, and always run cleanup steps
                │
                ▼
        http_client.py + assertions.py
        bounded response reads, redirect/retry policy, contract checks
                │
                ▼
        reporting.py / reproduce.py / redact.py
        schema-v2 JSON, offline HTML, JUnit, safe diagnostics, atomic writes
```

## Boundaries that matter

- Configuration is rejected before the first request. Unknown fields, malformed
  assertions, invalid URLs, cycles, and missing references are configuration
  errors rather than partial runs.
- Profiles hold non-secret variables and secret *sources*. Secret values are
  resolved in memory and are never put into captures, reports, or artifacts.
- The transport reads response bodies in bounded chunks, blocks HTTPS-to-HTTP
  downgrades, removes credential headers across origins, and retries only
  eligible methods/statuses.
- Workflow captures are typed and immutable. A later step may use an explicitly
  declared JSON/header/cookie/status capture, but cannot execute arbitrary shell
  code or embed a structured value into a scalar accidentally.
- Reporting is downstream of execution. A failed assertion remains a failed
  assertion regardless of whether the reviewer opens terminal output, HTML,
  JSON, or JUnit. Every output is written through an atomic same-directory
  replacement.

## Request lifecycle

1. `load_suite()` merges the suite, profile, and safe CLI overrides, then
   computes secret-free fingerprints.
2. `plan_suite()` validates schema-v2 dependencies and emits declaration-order
   topological layers without network access.
3. `SuiteRunner` executes independent cases concurrently or workflow layers in
   order. It records `passed`, `failed`, `error`, `blocked`, and `skipped`
   distinctly.
4. Successful captures are stored for dependent steps. A failed dependency
   blocks ordinary children; `run_if: "always"` cleanup remains eligible.
5. `write_json_report()`, `write_html_report()`, and `write_junit_report()`
   serialize the same result model and apply redaction at the output boundary.

## Why the project stays small

The core runtime uses Python's standard library: `urllib`, `http.server`,
`concurrent.futures`, `sqlite`-free configuration, and immutable dataclasses.
OpenAPI YAML support is isolated behind the optional `qa-sentinel[yaml]` extra.
This keeps a clean install useful for JSON suites while making the extra
explicit for users who need YAML input.

The project is intentionally not a browser runner, load-testing system, or
general-purpose workflow engine. Those boundaries make the examples easy to
audit and keep the security model understandable.

## Evidence and verification

- `tests/` covers validation, transport, retries, redaction, workflow cleanup,
  reports, comparison, trends, safe OpenAPI import, and the optional JobFlow
  cross-project fixture.
- `scripts/benchmark.py` measures localhost-only independent requests at
  workers 1/4/16, a multi-layer workflow, report generation, and trend history.
  It records machine/runtime metadata and states its limitations; it does not
  make scalability claims.
- `.github/workflows/ci.yml` runs supported Python versions, cross-platform
  installed-CLI smoke checks, and a clean wheel install. Pages regenerates the
  public demo reports from the current source before deployment.
