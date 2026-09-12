from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from decimal import Decimal
from typing import Any

from research_kb.errors import schema_validation_failed

def _normalize_number(value: float | int) -> str:
    if isinstance(value, bool):
        raise schema_validation_failed("Booleans are not numbers in canonical JSON.")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise schema_validation_failed("Non-finite numbers are not permitted in canonical payloads.")
    if value == 0:
        return "0"
    text = repr(float(value))
    if "e" not in text and "E" not in text:
        text = text.rstrip("0").rstrip(".")
    return text


def canonicalize(value: Any) -> str:
    return _serialize(value, seen=set())


def _serialize(value: Any, seen: set[int]) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _normalize_number(value)
    if isinstance(value, Decimal):
        return _normalize_number(float(value))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise schema_validation_failed("Cyclic structures cannot be canonicalized.")
        seen.add(identity)
        try:
            return "[" + ",".join(_serialize(item, seen) for item in value) + "]"
        finally:
            seen.discard(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in seen:
            raise schema_validation_failed("Cyclic structures cannot be canonicalized.")
        seen.add(identity)
        try:
            keys: list[str] = []
            for key in value:
                if not isinstance(key, str):
                    raise schema_validation_failed("Canonical JSON object keys must be strings.")
                keys.append(key)
            keys.sort(key=lambda item: item.encode("utf-16-be"))
            parts = []
            for key in keys:
                parts.append(json.dumps(key, ensure_ascii=False) + ":" + _serialize(value[key], seen))
            return "{" + ",".join(parts) + "}"
        finally:
            seen.discard(identity)
    raise schema_validation_failed(f"Unsupported value type for canonicalization: {type(value).__name__}")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonicalize(value).encode("utf-8")).hexdigest()


def normalize_alias(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    collapsed = re.sub(r"\s+", " ", stripped.strip().casefold())
    return collapsed


def hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def hash_text(text: str) -> str:
    return hash_bytes(text.encode("utf-8"))


def content_hash_for_revision(
    kind: str,
    subkind: str,
    title: str,
    body_md: str,
    record_state: str,
    state_json: Any,
    citations: list[dict[str, Any]] | None = None,
    endpoints: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "kind": kind,
        "subkind": subkind,
        "title": title,
        "body_md": body_md,
        "record_state": record_state,
        "state_json": state_json,
        "citations": citations or [],
    }
    if endpoints is not None:
        payload["endpoints"] = endpoints
    return canonical_hash(payload)


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_strict(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as exc:
        raise schema_validation_failed(f"Invalid JSON: {exc}") from exc


def _reject_constant(token: str) -> Any:
    raise ValueError(f"Non-finite JSON constant not permitted: {token}")


def parse_json_bytes(payload: bytes, *, max_bytes: int = 5_000_000) -> Any:
    if len(payload) > max_bytes:
        raise schema_validation_failed("Payload exceeds the bounded input size.", limit_bytes=max_bytes)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise schema_validation_failed("Payload is not valid UTF-8.") from exc
    return parse_json_strict(text)

