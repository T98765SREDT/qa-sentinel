"""Environment profiles and safe secret resolution.

Profiles keep reusable, non-secret target settings beside a declaration of
where credentials come from.  Values are read from the process environment at
run time; the profile file never contains a credential value.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import SecretProvenance


class EnvironmentError(ValueError):
    """Raised when an environment profile cannot be safely resolved."""


_PROFILE_FIELDS = {"name", "description", "variables", "secrets"}
_SECRET_FIELDS = {"from_env"}
_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRET_NAME_PATTERN = re.compile(
    r"(?:^|_)(?:api[_-]?key|token|secret|password|passwd|cookie|credential)(?:$|_)",
    re.IGNORECASE,
)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EnvironmentError(f"{label} must be a JSON object")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        names = ", ".join(f"'{name}'" for name in unknown)
        raise EnvironmentError(f"{label} contains unsupported field(s): {names}")


def _variable_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _NAME_PATTERN.fullmatch(value):
        raise EnvironmentError(
            f"{label} must be a valid name matching [A-Za-z_][A-Za-z0-9_]*"
        )
    return value


def _scalar_variables(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EnvironmentError(f"{label} must map names to scalar values")
    mapping = value
    result: dict[str, Any] = {}
    for key, item in mapping.items():
        name = _variable_name(key, f"{label} key")
        if _SECRET_NAME_PATTERN.search(name):
            raise EnvironmentError(
                f"{label}.{name} looks sensitive; declare it under secrets "
                "with a from_env source"
            )
        if not isinstance(item, (str, int, float, bool)):
            raise EnvironmentError(f"{label}.{name} must be a scalar value")
        result[name] = item
    return result


@dataclass(frozen=True)
class EnvironmentProfile:
    """A validated environment and its secret-source declarations."""

    name: str
    description: str
    variables: Mapping[str, Any]
    secrets: tuple[SecretProvenance, ...]
    path: Path
    config_hash: str

    @property
    def secret_names(self) -> tuple[str, ...]:
        return tuple(secret.name for secret in self.secrets)

    @property
    def missing_secret_sources(self) -> tuple[str, ...]:
        """Return missing source variable names without exposing values."""

        return tuple(
            secret.source
            for secret in self.secrets
            if not secret.value
        )

    @property
    def resolved_secret_values(self) -> tuple[str, ...]:
        """Return values for redaction/transport inside the process only."""

        return tuple(
            secret.value
            for secret in self.secrets
            if secret.value
        )

    def public_secret_sources(self) -> tuple[dict[str, str], ...]:
        """Return report-safe name/source metadata."""

        return tuple(
            {"name": secret.name, "source": secret.source}
            for secret in self.secrets
        )


def _profile_hash(
    name: str,
    description: str,
    variables: Mapping[str, Any],
    secrets: tuple[SecretProvenance, ...],
) -> str:
    """Hash only stable, non-secret profile data and source declarations."""

    canonical = {
        "name": name,
        "description": description,
        "variables": dict(sorted(variables.items())),
        "secrets": {
            secret.name: {"from_env": secret.source}
            for secret in sorted(secrets, key=lambda item: item.name)
        },
    }
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _environment_references(value: Any) -> tuple[str, ...]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(_ENV_PATTERN.findall(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            found.update(_environment_references(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_environment_references(item))
    return tuple(sorted(found))


def load_environment_profile(
    path: str | Path,
    *,
    require_secrets: bool = False,
) -> EnvironmentProfile:
    """Read and validate a profile, resolving secret sources from ``os.environ``.

    ``require_secrets=False`` is used by the request-free doctor command so it
    can report missing source names.  A real suite run always resolves all
    declared secrets before a request can be sent.
    """

    profile_path = Path(path)
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EnvironmentError(
            f"Unable to read environment profile '{profile_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise EnvironmentError(
            f"Invalid JSON in environment profile '{profile_path}' "
            f"at line {exc.lineno}: {exc.msg}"
        ) from exc

    document = _mapping(raw, "Environment profile")
    _reject_unknown(document, _PROFILE_FIELDS, "Environment profile")
    name = document.get("name", profile_path.stem)
    if not isinstance(name, str) or not name.strip():
        raise EnvironmentError("Environment profile.name must be a non-empty string")
    name = name.strip()
    if len(name) > 80:
        raise EnvironmentError("Environment profile.name must be 80 characters or fewer")
    description = document.get("description", "")
    if not isinstance(description, str):
        raise EnvironmentError("Environment profile.description must be a string")
    raw_variables_value = document.get("variables", {})
    if not isinstance(raw_variables_value, dict):
        # Keep the actionable "must map names" diagnostic for malformed
        # profiles instead of reducing it to a generic object error.
        variables = _scalar_variables(raw_variables_value, "Environment profile.variables")
        raw_variables: Mapping[str, Any] = {}
    else:
        raw_variables = raw_variables_value
        variables = {}
    raw_secrets = _mapping(document.get("secrets", {}), "Environment profile.secrets")
    overlap = sorted(set(raw_variables).intersection(raw_secrets))
    if overlap:
        raise EnvironmentError(
            "Environment profile secret name(s) cannot be both a variable and a secret: "
            + ", ".join(overlap)
        )
    if raw_variables:
        variables = _scalar_variables(raw_variables, "Environment profile.variables")
    secrets: list[SecretProvenance] = []
    for raw_name, raw_spec in raw_secrets.items():
        secret_name = _variable_name(raw_name, "Environment profile.secrets key")
        if secret_name in variables:
            raise EnvironmentError(
                f"Environment profile.{secret_name} cannot be both a variable and a secret"
            )
        spec = _mapping(raw_spec, f"Environment profile.secrets.{secret_name}")
        _reject_unknown(spec, _SECRET_FIELDS, f"Environment profile.secrets.{secret_name}")
        source = _variable_name(
            spec.get("from_env"),
            f"Environment profile.secrets.{secret_name}.from_env",
        )
        value = os.environ.get(source)
        secrets.append(SecretProvenance(secret_name, source, value if value else None))

    normalized_secrets = tuple(sorted(secrets, key=lambda item: item.name))
    profile = EnvironmentProfile(
        name=name,
        description=description.strip(),
        variables=variables,
        secrets=normalized_secrets,
        path=profile_path,
        config_hash=_profile_hash(name, description.strip(), variables, normalized_secrets),
    )
    if require_secrets and profile.missing_secret_sources:
        missing = ", ".join(profile.missing_secret_sources)
        raise EnvironmentError(
            f"Environment profile '{profile.name}' is missing required secret "
            f"environment variable(s): {missing}"
        )
    return profile


def resolve_profile_variables(
    profile: EnvironmentProfile,
    suite_variables: Mapping[str, Any],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Merge variables with explicit precedence and inject declared secrets.

    Suite variables are the base, profile variables override them, and
    explicit non-secret ``--var`` values override both.  Secret values are
    always sourced from the profile's declared environment variable and can
    never be supplied through a CLI override.
    """

    explicit = dict(overrides or {})
    blocked = sorted(set(explicit).intersection(profile.secret_names))
    if blocked:
        names = ", ".join(blocked)
        raise EnvironmentError(
            f"--var cannot override declared secret variable(s): {names}; "
            "set the profile's source environment variable instead"
        )
    if profile.missing_secret_sources:
        missing = ", ".join(profile.missing_secret_sources)
        raise EnvironmentError(
            f"Environment profile '{profile.name}' is missing required secret "
            f"environment variable(s): {missing}"
        )
    resolved: dict[str, Any] = dict(suite_variables)
    resolved.update(profile.variables)
    resolved.update(explicit)
    for secret in profile.secrets:
        # Missing values were checked above; this keeps the type narrow for
        # callers and makes the no-empty-string invariant explicit.
        assert secret.value
        resolved[secret.name] = secret.value
    return resolved


def profile_environment_references(profile: EnvironmentProfile) -> tuple[str, ...]:
    """Return environment names referenced by profile variable templates."""

    return _environment_references(profile.variables)
