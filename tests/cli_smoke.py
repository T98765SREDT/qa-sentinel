"""Cross-platform smoke checks for an installed QA Sentinel package."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise SystemExit(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="qa-sentinel-cli-smoke-"))
    try:
        cli = shutil.which("qa-sentinel")
        if cli is None:
            raise SystemExit("installed qa-sentinel console entrypoint is not on PATH")
        run(cli, "--version", cwd=root)
        run(cli, "init", str(root), cwd=root)
        suite = root / "suites" / "smoke.json"
        profile = root / "environments" / "local.json"
        if not suite.is_file() or not profile.is_file():
            raise SystemExit("init did not create the expected suite/profile files")
        run(cli, "validate", str(suite), cwd=root)
        run(
            cli,
            "doctor",
            str(suite),
            "--env",
            str(profile),
            cwd=root,
        )
        print(f"installed CLI smoke passed in {root}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
