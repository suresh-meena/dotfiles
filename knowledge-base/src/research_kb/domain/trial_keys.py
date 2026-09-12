from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from research_kb.domain.canonical import canonical_hash, canonicalize
from research_kb.errors import schema_validation_failed
from research_kb.version import TRIAL_KEY_VERSION

_UNIT_ALIASES: dict[str, tuple[str, float]] = {
    "ns": ("s", 1e-9),
    "us": ("s", 1e-6),
    "µs": ("s", 1e-6),
    "ms": ("s", 1e-3),
    "s": ("s", 1.0),
    "sec": ("s", 1.0),
    "seconds": ("s", 1.0),
    "m": ("m", 1.0),
    "cm": ("m", 1e-2),
    "mm": ("m", 1e-3),
    "km": ("m", 1e3),
    "B": ("B", 1.0),
    "bytes": ("B", 1.0),
    "KiB": ("B", 1024.0),
    "MiB": ("B", 1024.0**2),
    "GiB": ("B", 1024.0**3),
    "K": ("K", 1.0),
    "kelvin": ("K", 1.0),
    "Hz": ("Hz", 1.0),
    "kHz": ("Hz", 1e3),
    "MHz": ("Hz", 1e6),
    "GHz": ("Hz", 1e9),
}


def _check_finite(value: Any, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise schema_validation_failed(f"{path}: non-finite numbers are not permitted in trial conditions.")
    if isinstance(value, dict):
        for key, item in value.items():
            _check_finite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_finite(item, f"{path}[{index}]")


def normalize_quantity(quantity: dict[str, Any]) -> dict[str, Any]:
    value = quantity.get("value")
    unit = quantity.get("unit")
    if value is None or unit is None:
        return quantity
    if not isinstance(value, (int, float, Decimal, str)):
        raise schema_validation_failed("Quantity value must be numeric or a decimal string.")
    if isinstance(value, str):
        value = Decimal(value)
    canonical_unit, factor = _UNIT_ALIASES.get(unit, (unit, 1.0))
    converted = float(value) * factor
    if converted == 0:
        converted = 0.0
    return {"value": converted, "unit": canonical_unit}


def _typed_encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": format(value, "f")}
    if isinstance(value, int) and abs(value) > 2**53:
        return {"$int": str(value)}
    if isinstance(value, dict):
        return {key: _typed_encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_typed_encode(item) for item in value]
    return value


def normalize_conditions(
    conditions: dict[str, Any],
    *,
    defaults: dict[str, Any] | None = None,
    set_fields: tuple[str, ...] = (),
    unit_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    _check_finite(conditions, "conditions")
    merged: dict[str, Any] = {}
    for key, value in (defaults or {}).items():
        merged[key] = value
    for key, value in conditions.items():
        merged[key] = value
    normalized: dict[str, Any] = {}
    for key in sorted(merged):
        value = merged[key]
        if key in set_fields and isinstance(value, list):
            value = sorted(_typed_encode(value), key=lambda item: canonicalize(item))
        elif key in unit_fields and isinstance(value, dict) and "unit" in value:
            value = normalize_quantity(value)
        else:
            value = _typed_encode(value)
        normalized[key] = value
    return normalized


def trial_key(
    *,
    project_id: str,
    study_object_id: str,
    study_revision: int,
    conditions: dict[str, Any],
    replicate_identity: str | None = None,
) -> dict[str, Any]:
    payload = {
        "project_id": project_id,
        "study_object_id": study_object_id,
        "study_revision": study_revision,
        "conditions": conditions,
        "replicate_identity": replicate_identity,
    }
    digest = canonical_hash(payload)
    return {
        "format": TRIAL_KEY_VERSION,
        "hash": digest,
        "study_revision": study_revision,
        "replicate_identity": replicate_identity,
    }


def trial_key_string(key: dict[str, Any]) -> str:
    if key.get("format") != TRIAL_KEY_VERSION:
        raise schema_validation_failed("Unsupported trial key format.", found=key.get("format"))
    suffix = key.get("replicate_identity") or "solo"
    return f"tk1:{key['hash']}:{suffix}"
