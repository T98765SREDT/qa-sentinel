"""Safe starter-project scaffolding for QA Sentinel."""

from __future__ import annotations

import json
from pathlib import Path


class ScaffoldConflict(FileExistsError):
    """Raised when init would overwrite a generated file by default."""

    def __init__(self, conflicts: tuple[Path, ...]) -> None:
        self.conflicts = conflicts
        names = ", ".join(str(path) for path in conflicts)
        super().__init__(f"refusing to overwrite existing generated file(s): {names}")


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def generated_files() -> dict[Path, str]:
    """Return the small, reviewable set of files created by ``qa-sentinel init``."""

    return {
        Path("suites/smoke.json"): _json_text(
            {
                "name": "Local smoke suite",
                "description": "A minimal health check for the local QA Sentinel demo API.",
                "environment": "local",
                "tests": [
                    {
                        "id": "health",
                        "name": "Health endpoint responds",
                        "url": "http://127.0.0.1:8765/health",
                        "tags": ["smoke"],
                        "assertions": [{"type": "status", "equals": 200}],
                    }
                ],
            }
        ),
        Path("environments/local.json"): _json_text(
            {
                "name": "local",
                "description": "Local demo defaults; no credentials are stored here.",
                "variables": {"base_url": "http://127.0.0.1:8765"},
            }
        ),
        Path(".github/workflows/qa-sentinel.yml"): """name: QA Sentinel\n\non:\n  push:\n  pull_request:\n\njobs:\n  validate-suite:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: \"3.11\"\n      - name: Install QA Sentinel\n        run: python -m pip install qa-sentinel\n      - name: Validate smoke suite\n        run: qa-sentinel validate suites/smoke.json\n""",
        Path("README-qa-sentinel.md"): """# QA Sentinel starter\n\nThis directory was created by `qa-sentinel init`. The generated suite is a\nsmall local health check that is safe to inspect before it sends any request.\n\n## First run\n\n```bash\nqa-sentinel validate suites/smoke.json\nqa-sentinel doctor suites/smoke.json --env environments/local.json\npython3 -m qa_sentinel serve-demo\nqa-sentinel run suites/smoke.json\n```\n\nThe demo API is local and deterministic. Replace the URL in the suite before\nusing it against a real service, and keep credentials in environment variables.\n\n## Suggested ignore entries\n\nIf this repository does not already ignore generated reports, add:\n\n```gitignore\nqa-sentinel-report.html\nqa-sentinel-report.json\nqa-sentinel-report.xml\n```\n""",
    }


def init_project(directory: str | Path = ".", *, force: bool = False) -> tuple[Path, ...]:
    """Create starter files without touching existing generated files by default."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    files = generated_files()
    conflicts = tuple(root / relative for relative in files if (root / relative).exists())
    if conflicts and not force:
        raise ScaffoldConflict(conflicts)

    created: list[Path] = []
    for relative, content in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        created.append(relative)
    return tuple(created)
