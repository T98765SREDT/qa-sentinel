"""Build a clean virtual environment and exercise the installed wheel."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_demo(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"demo API exited early: {output}")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError(f"demo API did not become ready: {url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--artifacts-dir", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if wheel.suffix != ".whl" or not wheel.is_file():
        raise SystemExit(f"wheel not found: {wheel}")

    artifacts = args.artifacts_dir.resolve() if args.artifacts_dir else None
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="qa-sentinel-wheel-smoke-") as directory:
        root = Path(directory)
        env_dir = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
        python = env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        _run(str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel))
        metadata = _run(
            str(python),
            "-c",
            "import importlib.metadata as m; d=m.metadata('qa-sentinel'); "
            "print(d['Name']); print(d['Version']); "
            "print(','.join(d.get_all('Provides-Extra') or []))",
        )
        metadata_lines = metadata.stdout.splitlines()
        if len(metadata_lines) < 3 or metadata_lines[0] != "qa-sentinel":
            raise RuntimeError(f"unexpected package metadata: {metadata.stdout!r}")
        if "yaml" not in {item.strip() for item in metadata_lines[2].split(",") if item.strip()}:
            raise RuntimeError("installed package metadata did not expose the yaml extra")
        # Run outside the checkout so the subprocess cannot accidentally import
        # the source tree instead of the wheel installed in the clean venv.
        version = _run(
            str(python), "-m", "qa_sentinel", "--version", cwd=root
        ).stdout.strip()
        if not version.startswith("qa-sentinel "):
            raise RuntimeError(f"unexpected CLI version output: {version!r}")
        cli = env_dir / (
            "Scripts/qa-sentinel.exe" if sys.platform == "win32" else "bin/qa-sentinel"
        )
        if not cli.is_file():
            raise RuntimeError(f"installed console entrypoint is missing: {cli}")
        cli_version = _run(str(cli), "--version", cwd=root).stdout.strip()
        if cli_version != version:
            raise RuntimeError(
                f"module and console versions differ: {version!r} != {cli_version!r}"
            )

        workspace = root / "workspace"
        _run(str(cli), "init", str(workspace), cwd=root)
        suite_path = workspace / "suites" / "smoke.json"
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
        port = _free_port()
        suite["tests"][0]["url"] = f"http://127.0.0.1:{port}/health"
        suite_path.write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
        _run(str(cli), "validate", str(suite_path), cwd=root)

        server = subprocess.Popen(
            [str(python), "-m", "qa_sentinel", "serve-demo", "--port", str(port)],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_demo(f"http://127.0.0.1:{port}/health", server)
            html = artifacts / "report.html" if artifacts else root / "report.html"
            report_json = artifacts / "report.json" if artifacts else root / "report.json"
            junit = artifacts / "report.xml" if artifacts else root / "report.xml"
            _run(
                str(cli),
                "run",
                str(suite_path),
                "--html",
                str(html),
                "--json",
                str(report_json),
                "--junit",
                str(junit),
                cwd=root,
            )
            summary = json.loads(report_json.read_text(encoding="utf-8"))["summary"]
            if summary["passed"] != 1 or summary["failed"] or summary["errors"]:
                raise RuntimeError(f"installed wheel demo did not pass: {summary}")
            for artifact in (html, report_json, junit):
                if not artifact.is_file() or artifact.stat().st_size == 0:
                    raise RuntimeError(f"missing package smoke artifact: {artifact}")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    print(f"wheel smoke passed: {wheel.name}")


if __name__ == "__main__":
    main()
