# Security policy

QA Sentinel redacts common credential shapes as a safety net, but real secrets must be provided through environment variables and never committed to suites, fixtures, or reports.

Redirects from HTTPS to HTTP are blocked. Credential-bearing headers are removed when a redirect crosses an origin boundary. Automatic retries are limited to idempotent HTTP methods unless a suite explicitly opts in with `retry_non_idempotent`.

If a redaction bypass, unsafe URL handling issue, or report disclosure is found, contact the repository owner privately with a minimal reproduction. Please do not include active credentials in reports or public issues.
