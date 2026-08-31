# JobFlow integration example

This directory contains a cross-project, loopback-only example showing QA
Sentinel testing the JobFlow API with a temporary SQLite database. It is a
synthetic workflow; it never uses a personal JobFlow database or a public URL.

The passing workflow performs:

```text
health → create application → capture id/version → read workspace
→ transition Wishlist → Applied → assert stale-version 409 → cleanup
```

The intentional failure fixture expects the wrong stage, demonstrating a
failed contract and a cleanup step that still removes the synthetic record.

To run it from a checkout, start JobFlow against a disposable database:

```bash
python3 ../jobflow/app.py --host 127.0.0.1 --port 8765 --db /tmp/qa-sentinel-jobflow.db
python3 -m qa_sentinel run examples/jobflow/jobflow-workflow.json \
  --var base_url=http://127.0.0.1:8765 \
  --html /tmp/jobflow-report.html \
  --json /tmp/jobflow-report.json \
  --junit /tmp/jobflow-report.xml
```

The automated test starts JobFlow in-process on an ephemeral port and uses a
temporary database, so no server setup is required for the test suite. The
generated report includes the suite/config hashes and any explicitly supplied
CI provenance. `failure-workflow.json` is for diagnostics and should return a
non-zero run result by design.
