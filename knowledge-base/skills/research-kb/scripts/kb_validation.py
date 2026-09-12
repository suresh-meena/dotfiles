from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
SCHEMA_DIR = SKILL_ROOT / "assets" / "schemas"
MAX_PAYLOAD_BYTES = 5_000_000

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


class PayloadError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PayloadError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    raise PayloadError(f"Non-finite JSON constant not permitted: {token}")


def load_json_file(path: str | Path, *, max_bytes: int = MAX_PAYLOAD_BYTES) -> Any:
    payload = Path(path).read_bytes()
    if len(payload) > max_bytes:
        raise PayloadError(f"Payload exceeds the bounded input size ({max_bytes} bytes).")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadError("Payload is not valid UTF-8.") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as exc:
        raise PayloadError(f"Invalid JSON: {exc}") from exc


def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.is_file():
        raise PayloadError(f"Unknown bundled schema: {name}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise PayloadError(f"Schema {name} must be a JSON object.")
    return schema


def list_schemas() -> list[str]:
    return sorted(path.name.split(".")[0] for path in SCHEMA_DIR.glob("*.schema.json"))


def _resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise PayloadError(f"Only local schema references are supported: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    if not isinstance(node, dict):
        raise PayloadError(f"Schema reference does not resolve to an object: {ref}")
    return node


def validate(schema: dict[str, Any], instance: Any, path: str = "$", root: dict[str, Any] | None = None) -> list[str]:
    root = root or schema
    errors: list[str] = []
    if "$ref" in schema:
        return validate(_resolve_ref(root, schema["$ref"]), instance, path, root)
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']!r}")
    if "type" in schema:
        expected = schema["type"]
        options = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(option, lambda _value: False)(instance) for option in options):
            errors.append(f"{path}: must be of type {expected!r}")
            return errors
    if instance is None:
        return errors
    if "oneOf" in schema:
        matches = [option for option in schema["oneOf"] if not validate(option, instance, path, root)]
        if len(matches) != 1:
            errors.append(f"{path}: must match exactly one permitted shape")
    if "anyOf" in schema:
        if not any(not validate(option, instance, path, root) for option in schema["anyOf"]):
            errors.append(f"{path}: must match at least one permitted shape")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: must have at least {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: must have at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: must match pattern {schema['pattern']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: must contain at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: must contain at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item_schema, item, f"{path}[{index}]", root))
    if isinstance(instance, dict):
        for field in schema.get("required", []):
            if field not in instance:
                errors.append(f"{path}.{field}: is required")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(properties[key], value, f"{path}.{key}", root))
            elif additional is False:
                errors.append(f"{path}.{key}: is not a permitted field")
            elif isinstance(additional, dict):
                errors.extend(validate(additional, value, f"{path}.{key}", root))
    return errors


def canonical_hash(value: Any) -> str:
    def _canonical(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: _canonical(item[key]) for key in sorted(item)}
        if isinstance(item, list):
            return [_canonical(entry) for entry in item]
        return item

    serialized = json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_file(schema_name: str, input_path: str | Path) -> dict[str, Any]:
    schema = load_schema(schema_name)
    instance = load_json_file(input_path)
    errors = validate(schema, instance)
    return {
        "valid": not errors,
        "schema": schema_name,
        "input": str(input_path),
        "errors": errors,
        "note": (
            "Passing this offline schema check does not verify database references, evidence truth, "
            "authorization, or concurrency. The runtime revalidates every request."
        ),
    }
