# QA Sentinel

**Dependency-free API regression testing with concurrency, retries, secret-safe diagnostics, and polished reports.**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![stdlib only](https://img.shields.io/badge/runtime-stdlib%20only-12a36d)](pyproject.toml)
[![tests: unittest](https://img.shields.io/badge/tests-unittest-6657d9)](tests/)
[![license: MIT](https://img.shields.io/badge/license-MIT-172033)](LICENSE)

QA Sentinel turns readable JSON test suites into repeatable API quality checks. It validates status codes, nested JSON values, headers, response bodies, and latency budgets; retries transient failures with exponential backoff; runs independent checks in parallel; and generates machine-readable JSON plus a self-contained, filterable HTML dashboard.

No third-party runtime packages are required.

![QA Sentinel HTML report](docs/qa-sentinel-report.png)

## Why this project exists

Small teams often need repeatable API contract checks without adopting a large test platform. QA Sentinel keeps the test definition portable, makes failure output useful in CI, and prevents common credentials from leaking into reports.

## Key features

- Declarative JSON suites with `{{variable}}`, `${ENVIRONMENT_VARIABLE}`, and CLI overrides
- Status, nested JSON path/value, existence, latency, header, and body assertions
- Parallel execution with deterministic report ordering
- Retry policy for transport errors and configurable transient HTTP statuses
- JSON request serialization and GET/POST/other HTTP method support
- Recursive secret redaction for authorization headers, tokens, API keys, passwords, URLs, and known values
- Attractive standalone HTML reports with search and pass/fail filters
- Machine-readable JSON reports and meaningful CLI exit codes (`0` pass, `1` test failure, `2` configuration error)
- Deterministic local demo API and stdlib `unittest` coverage

## Quickstart

Requires Python 3.10 or newer.

Terminal 1 — start the local demo API:

```bash
python3 -m qa_sentinel serve-demo
```

Terminal 2 — execute five regression checks:

```bash
python3 -m qa_sentinel run examples/demo-suite.json \
  --html qa-sentinel-report.html \
  --json qa-sentinel-report.json
```

The suite demonstrates nested JSON contracts, POST serialization, a latency budget, and a `503 → retry → 200` recovery flow.

You can also install the local CLI:

```bash
python3 -m pip install -e .
qa-sentinel run examples/demo-suite.json
```

## Sample report

- [Generated standalone HTML report](docs/sample-report.html)
- [Generated JSON report](docs/sample-report.json)

The HTML artifact contains all styles and filtering logic, so it can be opened locally or uploaded directly as a CI artifact. The JSON artifact uses a stable schema for downstream automation.

## Suite format

```json
{
  "name": "Production smoke suite",
  "workers": 4,
  "variables": {
    "base_url": "https://api.example.com",
    "api_token": "${API_TOKEN}"
  },
  "defaults": {
    "timeout_seconds": 5,
    "retries": 2,
    "retry_on_status": [429, 500, 502, 503, 504],
    "headers": {"Authorization": "Bearer {{api_token}}"}
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

Override non-secret variables per environment:

```bash
python3 -m qa_sentinel run suite.json --var base_url=https://staging.example.com
```

Prefer `${API_TOKEN}` for real credentials. QA Sentinel rejects missing environment variables early and redacts credential-shaped fields and values from both report formats.

## Architecture

```text
JSON suite
   │
   ▼
config.py ── interpolation, validation, normalization
   │
   ▼
runner.py ── ThreadPoolExecutor, order preservation
   │
   ├── http_client.py ── urllib transport, timeouts, retry/backoff
   └── assertions.py  ── response contract evaluation
   │
   ▼
redact.py ── recursive secret removal
   │
   ├── reporting.py ── standalone HTML + structured JSON
   └── cli.py       ── terminal output + CI exit code
```

The transport, assertion engine, orchestration, and presentation layers use immutable dataclasses as their shared boundary. This separation keeps network behavior independently testable and prevents report formatting from influencing pass/fail logic.

## Test and verification commands

Run the complete unit and integration suite:

```bash
python3 -m unittest discover -s tests -v
```

Check every Python file compiles:

```bash
python3 -m compileall -q qa_sentinel tests examples
```

Integration tests start the demo server on an ephemeral local port and verify concurrent execution, retry recovery, failing assertions, CLI exit behavior, report creation, and secret removal.

## LinkedIn-ready project entry

**QA Sentinel — API Regression & Quality Monitoring CLI**  
Built a Python command-line tool that executes declarative API regression suites and produces secret-safe HTML and JSON quality reports.

- Implemented concurrent HTTP execution, configurable timeouts, transient-error retries with exponential backoff, and deterministic result ordering using Python's standard library.
- Designed a reusable assertion engine for status codes, nested JSON paths and values, headers, response bodies, and latency budgets, with clear failure diagnostics.
- Built recursive credential redaction, a self-contained interactive HTML dashboard, a deterministic demo API, and automated unit/integration tests without runtime dependencies.

**Skills:** Python · API Testing · Quality Assurance · Test Automation · HTTP · JSON · Concurrency · Secure Logging

## Five interview questions and honest answers

### 1. Why use only the Python standard library?

The constraint made deployment simple and forced clear boundaries between transport, assertions, and reporting. `urllib` provides the HTTP behavior, `concurrent.futures` provides bounded parallelism, and `unittest` covers the system. For a larger production product, I would evaluate `httpx` for connection pooling and richer timeout controls, but the current implementation remains easy to run anywhere Python is available.

### 2. How does retry behavior avoid hiding real failures?

Retries happen only for transport errors and explicitly configured transient HTTP statuses. Assertion failures are never retried. The final report records the total attempt count, and exponential delay reduces immediate pressure on an unhealthy service.

### 3. How is result ordering deterministic when tests run concurrently?

Each submitted future is mapped to its original suite index. Results are written into a pre-sized list as futures finish, then returned in declaration order. This preserves readable, stable reports without giving up concurrent requests.

### 4. What security problem does redaction solve, and what are its limits?

The report pipeline removes values under credential-shaped keys, bearer tokens, sensitive query parameters, and known secret variable values. This lowers the chance of exposing credentials in CI artifacts. It is defense in depth, not a secret manager: credentials should still come from environment variables and should never be committed to a real suite.

### 5. What would you build next?

I would add JSON Schema assertions, JUnit XML output for CI platforms, request hooks for OAuth token refresh, and pluggable transports. I would also benchmark connection pooling before changing the networking layer, rather than claiming it improves performance without measurements.

## License

[MIT](LICENSE)
