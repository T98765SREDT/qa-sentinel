# Order lifecycle workflow

This example demonstrates the reason for QA Sentinel's schema-v2 workflow
features with a small local API. It uses a synthetic `demo` user and never
contacts a real service.

## Run it

From the repository root, start the loopback server in one terminal:

```bash
python3 examples/order_server.py
```

In a second terminal, inspect the dependency layers without sending requests:

```bash
python3 -m qa_sentinel run examples/order-workflow/order-workflow.json --dry-run
```

Then run the workflow:

```bash
python3 -m qa_sentinel run examples/order-workflow/order-workflow.json \
  --html order-workflow-report.html \
  --json order-workflow-report.json \
  --junit order-workflow-report.xml
```

The expected result is `7/7 passed`: login captures a redacted access token,
create captures an order ID, later requests reuse both values, and the final
`DELETE` leaves the server with zero orders. Open
`order-workflow-report.html` to inspect the declaration-ordered cards and
capture metadata (names and sources only; values never appear in reports).

To see failure handling, run the intentional variant:

```bash
python3 -m qa_sentinel run examples/order-workflow/failure-workflow.json \
  --html order-failure-report.html \
  --json order-failure-report.json
```

The verification assertion intentionally fails, the dependent audit is
`BLOCKED` and sends no request, and the `run_if: "always"` cleanup still runs.
The process exits `1`, while the server ends with zero orders.

The workflow is deliberately small enough to record in under a minute while
showing authentication, stateful CRUD, lifecycle assertions, dependency
blocking, cleanup, and secret-safe reports in one reproducible path.
