# Security policy

QA Sentinel redacts common credential shapes as a safety net, but real secrets must be provided through environment variables and never committed to suites, fixtures, or reports. Environment profiles declare a source such as `QA_API_TOKEN`; they do not store the value. Declared secrets cannot be supplied through `--var`.

Redirects from HTTPS to HTTP are blocked. Credential-bearing headers are removed when a redirect crosses an origin boundary. Automatic retries are limited to idempotent HTTP methods unless a suite explicitly opts in with `retry_non_idempotent`.

If a redaction bypass, unsafe URL handling issue, or report disclosure is found, contact the repository owner privately with a minimal reproduction. Please do not include active credentials in reports or public issues.

Failure reports include only bounded response previews. JSON and text bodies are
clipped, binary bodies are omitted, and common absolute local paths are removed
from diagnostics. These controls reduce accidental disclosure but do not make
reports safe for unrestricted distribution; treat CI artifacts as potentially
sensitive.

Reports may include an environment name, a stable hash of non-secret profile
configuration, and secret name/source metadata for traceability. Secret values
are used in memory for requests and redaction only; they are never serialized
into JSON, HTML, or JUnit output. URL-encoded forms are redacted when the
secret is at least four characters long. Short values are intentionally not
globally replaced because doing so would corrupt ordinary diagnostic text.

The OpenAPI importer is intentionally limited. It reads local files only and
follows local JSON Pointer references; remote URLs, path-like references,
cyclic/deep references, and oversized documents are rejected. It never sends
requests or imports security credentials. GET/HEAD operations are enabled by
default; POST/PUT/PATCH/DELETE require explicit `--allow-method` flags. A
credential-shaped example is redacted, and an existing output file is not
overwritten unless `--force` is provided. Generated suites are drafts that
must be reviewed before they are run against a real service.

The core distribution remains standard-library only. YAML parsing is an
optional `qa-sentinel[yaml]` extra so users who do not need YAML do not receive
an additional runtime dependency. Keep PyYAML up to date when the extra is
installed and treat imported suites as untrusted drafts until reviewed.
