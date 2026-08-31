"""Non-destructive environment and suite diagnostics."""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .config import ConfigError, load_suite
from .environment import (
    EnvironmentError,
    EnvironmentProfile,
    load_environment_profile,
    profile_environment_references,
)
from .redact import redact_text


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
@dataclass(frozen=True)
class DoctorCheck:
    """One human-readable diagnostic result."""

    name: str
    passed: bool
    detail: str
    skipped: bool = False


@dataclass(frozen=True)
class DoctorReport:
    """The complete result of a request-free doctor run."""

    checks: tuple[DoctorCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed or check.skipped for check in self.checks)


def _profile_detail(profile: EnvironmentProfile) -> str:
    variables = ", ".join(sorted(profile.variables)) or "none"
    sources = ", ".join(
        f"{secret.name} from {secret.source}" for secret in profile.secrets
    ) or "none"
    return f"{profile.name} is valid; variables: {variables}; secret sources: {sources}"


def _missing_environment_names(path: Path) -> tuple[str, ...]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    return tuple(sorted({name for name in _ENV_PATTERN.findall(source) if name not in os.environ}))


def _output_check(path: Path) -> DoctorCheck:
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.exists():
        return DoctorCheck("output path", False, f"parent directory for {path} does not exist")
    if not parent.is_dir():
        return DoctorCheck("output path", False, f"parent path for {path} is not a directory")
    if not os.access(parent, os.W_OK | os.X_OK):
        return DoctorCheck("output path", False, f"parent directory for {path} is not writable")
    return DoctorCheck("output path", True, f"{path} can be written")


def diagnose(
    suite_path: str | Path,
    *,
    environment_path: str | Path | None = None,
    output_paths: tuple[str | Path, ...] = (),
    overrides: Mapping[str, str] | None = None,
) -> DoctorReport:
    """Run diagnostics without sending requests or writing probe files."""

    path = Path(suite_path)
    checks: list[DoctorCheck] = []
    profile: EnvironmentProfile | None = None
    if environment_path is None:
        checks.append(DoctorCheck("environment profile", True, "not requested", skipped=True))
    else:
        try:
            profile = load_environment_profile(environment_path)
        except EnvironmentError as exc:
            checks.append(DoctorCheck("environment profile", False, str(exc)))
        else:
            checks.append(DoctorCheck("environment profile", True, _profile_detail(profile)))

    missing_names = set(_missing_environment_names(path))
    if profile is not None:
        missing_names.update(profile_environment_references(profile))
        missing_names.update(profile.missing_secret_sources)
    missing = tuple(sorted(name for name in missing_names if name not in os.environ))
    if missing:
        checks.append(
            DoctorCheck(
                "environment variables",
                False,
                "missing name(s): " + ", ".join(missing),
            )
        )
    else:
        checks.append(DoctorCheck("environment variables", True, "all referenced names are set"))

    suite = None
    try:
        suite = load_suite(path, overrides, environment_profile=profile)
    except (ConfigError, OSError, ValueError) as exc:
        checks.append(
            DoctorCheck("suite", False, redact_text(str(exc)))
        )
    else:
        checks.append(
            DoctorCheck("suite", True, f"{suite.name} is valid ({len(suite.tests)} test(s))")
        )

    if output_paths:
        checks.extend(_output_check(Path(output)) for output in output_paths)
    else:
        checks.append(DoctorCheck("output paths", True, "not requested", skipped=True))

    version = f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(
        DoctorCheck(
            "Python",
            sys.version_info >= (3, 10),
            version if sys.version_info >= (3, 10) else f"{version}; Python 3.10+ is required",
        )
    )
    executable = shutil.which("qa-sentinel")
    checks.append(
        DoctorCheck(
            "CLI",
            True,
            f"running from {Path(sys.executable).name}"
            + (f"; installed command at {executable}" if executable else ""),
        )
    )
    return DoctorReport(tuple(checks))


def format_report(report: DoctorReport) -> str:
    """Format diagnostics without exposing profile values or secret contents."""

    lines = []
    for check in report.checks:
        marker = "SKIP" if check.skipped else "PASS" if check.passed else "FAIL"
        lines.append(f"[{marker}] {check.name}: {redact_text(check.detail)}")
    lines.append("\nDoctor result: " + ("ready" if report.passed else "needs attention"))
    return "\n".join(lines)
