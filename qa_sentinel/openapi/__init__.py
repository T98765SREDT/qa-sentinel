"""A deliberately small, safe OpenAPI 3 to suite-v2 importer.

The importer is a generator for reviewable smoke checks, not a complete
OpenAPI validator or fuzzing engine.  It reads a local specification, follows
only local JSON Pointer references, and never sends a request while importing.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlsplit, urlunsplit


class OpenAPIImportError(ValueError):
    """Raised when an OpenAPI document cannot be imported safely."""


MAX_SPEC_BYTES = 5 * 1024 * 1024
MAX_REF_DEPTH = 20
_HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SECRET_KEY = re.compile(
    r"authorization|password|passwd|secret|token|api[-_]?key|cookie|credential|private[-_]?key",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ImportResult:
    """Generated suite plus human-readable coverage information."""

    suite: Mapping[str, Any]
    imported: tuple[Mapping[str, Any], ...]
    skipped: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]

    @property
    def coverage(self) -> dict[str, Any]:
        return {
            "imported": [dict(item) for item in self.imported],
            "skipped": [dict(item) for item in self.skipped],
            "warnings": list(self.warnings),
            "summary": {
                "imported": len(self.imported),
                "skipped": len(self.skipped),
                "warnings": len(self.warnings),
            },
        }


def _read_document(path: Path) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OpenAPIImportError(f"Unable to read OpenAPI document '{path}': {exc}") from exc
    if len(raw) > MAX_SPEC_BYTES:
        raise OpenAPIImportError(
            f"OpenAPI document '{path}' exceeds the {MAX_SPEC_BYTES} byte safety limit"
        )
    suffix = path.suffix.casefold()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise OpenAPIImportError(
                "YAML import requires the optional PyYAML package; JSON OpenAPI documents "
                "work with the standard library only"
            ) from exc
        try:
            document = yaml.safe_load(raw.decode("utf-8"))
        except Exception as exc:  # PyYAML exposes several parser exception classes.
            raise OpenAPIImportError(f"Invalid YAML in '{path}': {exc}") from exc
    else:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenAPIImportError(f"Invalid JSON in '{path}': {exc}") from exc
    if not isinstance(document, Mapping):
        raise OpenAPIImportError("OpenAPI document must be an object")
    return document


def _resolve_pointer(root: Mapping[str, Any], reference: str, depth: int = 0) -> Any:
    if depth > MAX_REF_DEPTH:
        raise OpenAPIImportError("OpenAPI local reference depth exceeds the safety limit")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise OpenAPIImportError(
            f"Only local OpenAPI references are supported; rejected '{reference}'"
        )
    value: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        else:
            raise OpenAPIImportError(f"OpenAPI reference does not exist: {reference}")
    if isinstance(value, Mapping) and "$ref" in value:
        if len(value) != 1:
            raise OpenAPIImportError(
                f"OpenAPI reference object contains unsupported sibling fields: {reference}"
            )
        return _resolve_pointer(root, str(value["$ref"]), depth + 1)
    return value


def _resolve(root: Mapping[str, Any], value: Any) -> Any:
    if isinstance(value, Mapping) and "$ref" in value:
        if len(value) != 1:
            raise OpenAPIImportError("OpenAPI $ref objects cannot contain sibling fields")
        return _resolve_pointer(root, str(value["$ref"]))
    return value


def _slug(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    result = result or "operation"
    return result if result[0].isalpha() else f"operation-{result}"


def _safe_server(root: Mapping[str, Any], document: Mapping[str, Any]) -> tuple[str, list[str]]:
    warnings: list[str] = []
    servers = document.get("servers", [])
    if servers is not None and not isinstance(servers, list):
        raise OpenAPIImportError("servers must be an array")
    if not servers:
        warnings.append("The document has no servers; generated URLs use https://example.invalid as a placeholder.")
        return "https://example.invalid", warnings
    server = _resolve(root, servers[0])
    if not isinstance(server, Mapping) or not isinstance(server.get("url"), str):
        raise OpenAPIImportError("servers[0].url must be a string")
    url = server["url"]
    variables = server.get("variables", {})
    if not isinstance(variables, Mapping):
        raise OpenAPIImportError("servers[0].variables must be an object")
    for name, definition in variables.items():
        resolved = _resolve(root, definition)
        if not isinstance(resolved, Mapping) or "default" not in resolved:
            raise OpenAPIImportError(
                f"servers[0].variables.{name} needs a default before it can be imported safely"
            )
        url = url.replace("{" + str(name) + "}", quote(str(resolved["default"]), safe="-._~"))
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OpenAPIImportError("servers[0].url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or any(character.isspace() for character in parsed.netloc):
        raise OpenAPIImportError("servers[0].url must not contain credentials or whitespace")
    if parsed.query:
        raise OpenAPIImportError("servers[0].url must not contain a query string")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")), warnings


def _example(root: Mapping[str, Any], definition: Mapping[str, Any]) -> tuple[bool, Any]:
    definition = _resolve(root, definition)
    if not isinstance(definition, Mapping):
        return False, None
    if "example" in definition:
        return True, _resolve(root, definition["example"])
    examples = definition.get("examples")
    if isinstance(examples, Mapping) and examples:
        key = sorted(str(item) for item in examples)[0]
        item = _resolve(root, examples[key])
        if isinstance(item, Mapping) and "value" in item:
            item = _resolve(root, item["value"])
        return True, item
    schema = definition.get("schema")
    if schema is not None:
        schema = _resolve(root, schema)
        if isinstance(schema, Mapping):
            if "example" in schema:
                return True, _resolve(root, schema["example"])
            if "default" in schema:
                return True, _resolve(root, schema["default"])
    if "default" in definition:
        return True, _resolve(root, definition["default"])
    return False, None


def _safe_value(value: Any, key: str, warnings: list[str]) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for item_key, item_value in value.items():
            item_name = str(item_key)
            if _SECRET_KEY.search(item_name) and isinstance(item_value, str):
                warnings.append(
                    f"Credential-shaped example '{item_name}' was redacted in the generated suite."
                )
                result[item_name] = "[REDACTED]"
            else:
                result[item_name] = _safe_value(item_value, item_name, warnings)
        return result
    if isinstance(value, list):
        return [_safe_value(item, key, warnings) for item in value]
    if isinstance(value, str) and _SECRET_KEY.search(key):
        warnings.append(f"Credential-shaped example '{key}' was redacted in the generated suite.")
        return "[REDACTED]"
    return value


def _parameters(
    root: Mapping[str, Any],
    path_item: Mapping[str, Any],
    operation: Mapping[str, Any],
    path: str,
    warnings: list[str],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    merged: dict[tuple[str, str], Mapping[str, Any]] = {}
    for source in (path_item.get("parameters", []), operation.get("parameters", [])):
        if not isinstance(source, list):
            raise OpenAPIImportError(f"paths.{path}.parameters must be an array")
        for raw in source:
            item = _resolve(root, raw)
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str) or not isinstance(item.get("in"), str):
                raise OpenAPIImportError(f"paths.{path}.parameters contains an invalid parameter")
            merged[(item["name"], item["in"])] = item
    path_values: dict[str, str] = {}
    query_values: dict[str, str] = {}
    missing: list[str] = []
    for (name, location), item in sorted(merged.items()):
        required = bool(item.get("required")) or location == "path"
        if location not in {"path", "query", "header"}:
            if required:
                missing.append(f"{location}:{name} (unsupported parameter location)")
            continue
        has_value, value = _example(root, item)
        if not has_value:
            if required:
                missing.append(f"{location}:{name} (required parameter has no example/default)")
            continue
        if _SECRET_KEY.search(name):
            if required:
                missing.append(f"{location}:{name} (credential-shaped parameter is never generated)")
            else:
                warnings.append(f"Optional credential-shaped parameter '{name}' was omitted.")
            continue
        value_string = str(_safe_value(value, name, warnings))
        if location == "path":
            path_values[name] = value_string
        elif location == "query":
            query_values[name] = value_string
        else:
            query_values[f"header:{name}"] = value_string
    return path_values, query_values, missing


def _response_status(root: Mapping[str, Any], operation: Mapping[str, Any], path: str) -> int | None:
    responses = _resolve(root, operation.get("responses", {}))
    if not isinstance(responses, Mapping):
        raise OpenAPIImportError(f"paths.{path}.responses must be an object")
    concrete: list[int] = []
    for key in responses:
        # Resolve every response entry, including entries with numeric status
        # keys, so remote and cyclic refs cannot be silently ignored.
        _resolve(root, responses[key])
        try:
            code = int(str(key))
        except ValueError:
            continue
        if 100 <= code <= 599:
            concrete.append(code)
    if not concrete:
        return None
    success = sorted(code for code in concrete if 200 <= code < 300)
    return (success or sorted(concrete))[0]


def _request_body(root: Mapping[str, Any], operation: Mapping[str, Any], path: str, warnings: list[str]) -> tuple[Any, str | None, str | None]:
    if "requestBody" not in operation:
        return None, None, None
    request_body = _resolve(root, operation["requestBody"])
    if not isinstance(request_body, Mapping):
        raise OpenAPIImportError(f"paths.{path}.requestBody must be an object")
    content = request_body.get("content", {})
    if not isinstance(content, Mapping) or not content:
        if request_body.get("required"):
            return None, None, "required request body has no content example"
        return None, None, None
    media_types = sorted(str(item) for item in content)
    media_type = "application/json" if "application/json" in content else media_types[0]
    definition = _resolve(root, content[media_type])
    if not isinstance(definition, Mapping):
        return None, media_type, "request body content is invalid"
    has_value, value = _example(root, definition)
    if not has_value:
        if request_body.get("required"):
            return None, media_type, "required request body has no example/default"
        return None, media_type, None
    return _safe_value(value, "requestBody", warnings), media_type, None


def import_openapi(path: str | Path, allow_methods: tuple[str, ...] = ()) -> ImportResult:
    """Generate a deterministic suite-v2 draft from a local OpenAPI 3 document."""

    source_path = Path(path)
    root = _read_document(source_path)
    version = str(root.get("openapi", ""))
    if not version.startswith("3."):
        raise OpenAPIImportError(f"Only OpenAPI 3 documents are supported; received '{version or 'missing'}'")
    paths = root.get("paths")
    if not isinstance(paths, Mapping):
        raise OpenAPIImportError("OpenAPI document must contain a paths object")
    allowed = {str(method).upper() for method in allow_methods}
    invalid_methods = sorted(allowed - set(_HTTP_METHODS))
    if invalid_methods:
        raise OpenAPIImportError(
            "Unsupported --allow-method value(s): " + ", ".join(invalid_methods)
        )
    base_url, warnings = _safe_server(root, root)
    tests: list[dict[str, Any]] = []
    imported: list[Mapping[str, Any]] = []
    skipped: list[Mapping[str, Any]] = []
    used_ids: set[str] = set()
    for raw_path, raw_path_item in sorted(paths.items(), key=lambda item: str(item[0])):
        path_template = str(raw_path)
        path_item = _resolve(root, raw_path_item)
        if not isinstance(path_item, Mapping):
            skipped.append({"path": path_template, "reason": "path item is not an object"})
            continue
        for raw_method in _HTTP_METHODS:
            method = raw_method.casefold()
            if method not in path_item:
                continue
            operation = _resolve(root, path_item[method])
            location = f"paths.{path_template}.{method}"
            if not isinstance(operation, Mapping):
                skipped.append({"path": location, "reason": "operation is not an object"})
                continue
            if raw_method in _WRITE_METHODS and raw_method not in allowed:
                skipped.append({"path": location, "reason": f"write method {raw_method} requires --allow-method {raw_method}"})
                continue
            status = _response_status(root, operation, path_template)
            if status is None:
                skipped.append({"path": location, "reason": "responses has no concrete HTTP status"})
                continue
            operation_warnings = list(warnings)
            path_values, parameter_values, missing = _parameters(
                root, path_item, operation, path_template, operation_warnings
            )
            if missing:
                skipped.append({"path": location, "reason": "; ".join(missing)})
                continue
            rendered_path = path_template
            for name, value in path_values.items():
                rendered_path = rendered_path.replace("{" + name + "}", quote(value, safe="-._~"))
            query = {
                key: value
                for key, value in parameter_values.items()
                if not key.startswith("header:")
            }
            url = base_url + (rendered_path if rendered_path.startswith("/") else "/" + rendered_path)
            if query:
                url += "?" + urlencode(sorted(query.items()))
            headers = {
                key.removeprefix("header:"): value
                for key, value in parameter_values.items()
                if key.startswith("header:")
            }
            body, media_type, body_skip = _request_body(root, operation, path_template, operation_warnings)
            if body_skip:
                skipped.append({"path": location, "reason": body_skip})
                continue
            if body is not None and media_type:
                normalized_media_type = media_type.split(";", 1)[0].casefold()
                if not (
                    normalized_media_type == "application/json"
                    or normalized_media_type.endswith("+json")
                    or normalized_media_type.startswith("text/")
                ):
                    skipped.append(
                        {
                            "path": location,
                            "reason": f"request body media type '{media_type}' is not supported safely",
                        }
                    )
                    continue
            if body is not None and media_type:
                headers.setdefault("Content-Type", media_type)
            if operation.get("security") or root.get("security"):
                operation_warnings.append(f"{location} declares security; authentication was not generated")
            operation_id = operation.get("operationId")
            raw_id = str(operation_id) if operation_id else f"{raw_method}-{path_template}"
            base_id = _slug(raw_id)
            case_id = base_id
            suffix = 2
            while case_id in used_ids:
                case_id = f"{base_id}-{suffix}"
                suffix += 1
            used_ids.add(case_id)
            name = str(operation.get("summary") or operation_id or f"{raw_method} {path_template}")
            test: dict[str, Any] = {
                "id": case_id,
                "name": name,
                "method": raw_method,
                "url": url,
                "headers": headers,
                "assertions": [{"type": "status", "in": [status]}],
                "tags": ["openapi", raw_method.casefold()],
            }
            if body is not None:
                test["json"] = body
            tests.append(test)
            imported.append({"path": location, "id": case_id, "method": raw_method, "url": url})
            for warning in operation_warnings:
                if warning not in warnings:
                    warnings.append(warning)
    if not tests:
        details = "; ".join(
            f"{item.get('path')}: {item.get('reason')}" for item in skipped[:5]
        )
        raise OpenAPIImportError(
            "No safe operations could be imported"
            + (f": {details}" if details else "; inspect the skipped coverage reasons")
        )
    info = root.get("info", {})
    if info is not None and not isinstance(info, Mapping):
        raise OpenAPIImportError("OpenAPI info must be an object when present")
    suite = {
        "schemaVersion": 2,
        "name": str((info or {}).get("title", source_path.stem)) + " smoke suite",
        "description": "Generated from an OpenAPI 3 document; review examples and skipped operations before running.",
        "variables": {"base_url": base_url},
        "tests": tests,
    }
    return ImportResult(tuple_to_mapping(suite), tuple(imported), tuple(skipped), tuple(warnings))


def tuple_to_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a plain mapping helper kept separate for a stable public result."""

    return dict(value)


def write_import(result: ImportResult, output: str | Path, *, force: bool = False) -> Path:
    """Write only the generated suite atomically; never overwrite by default."""

    path = Path(output)
    if path.exists() and not force:
        raise OpenAPIImportError(
            f"Output '{path}' already exists; choose another path or pass --force explicitly"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result.suite, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass
    return path
