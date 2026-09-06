"""Versioned and bounded wire protocol for one-shot host snapshots."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1
MAX_JSON_BYTES = 256 * 1024
MAX_JSON_DEPTH = 8
MAX_STRING_BYTES = 1024
MAX_PROCESSES = 80
MAX_USERS = 128
MAX_GPUS = 32
MAX_GPU_ALLOCATIONS = 32
MAX_WIRE_INTEGER = 2**63 - 1

# The helper is deliberately a closed, small protocol.  Unknown fields are
# rejected so a future helper cannot accidentally smuggle command lines,
# paths, or other private data through an older hub.  Add fields only with a
# schema-version change.
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "captured_at",
    "observation_duration_seconds",
    "collection_duration_seconds",
    "boot_id",
    "helper_version",
    "status",
    "cpu",
    "memory",
    "disk",
    "gpus",
    "users",
    "processes",
    "visibility",
    "limits",
    "capabilities",
}
_CPU_FIELDS = {"logical_count", "busy_fraction", "load_1m", "load_5m", "load_15m"}
_MEMORY_FIELDS = {
    "total_bytes",
    "available_bytes",
    "used_bytes",
    "swap_total_bytes",
    "swap_used_bytes",
}
_DISK_FIELDS = {"total_bytes", "free_bytes"}
_GPU_FIELDS = {
    "uuid",
    "index",
    "model",
    "utilization_fraction",
    "vram_total_bytes",
    "vram_used_bytes",
    "temperature_c",
    "power_watts",
    "compute_process_count",
    "supported",
    "error",
    "mig_detected",
    "instance_supported",
}
_USER_FIELDS = {
    "uid",
    "username",
    "cpu_cores",
    "rss_bytes",
    "process_count",
    "gpu_process_count",
    "vram_bytes",
}
_PROCESS_FIELDS = {
    "pid",
    "create_time",
    "uid",
    "username",
    "name",
    "executable",
    "cpu_cores",
    "rss_bytes",
    "gpu_process",
    "gpu_uuid",
    "gpu_index",
    "vram_bytes",
    "gpu_allocations",
}
_ALLOCATION_FIELDS = {"gpu_uuid", "gpu_index", "vram_bytes"}
_VISIBILITY_FIELDS = {
    "partial",
    "permission_denied",
    "processes_visible",
    "processes_emitted",
    "counters_truncated",
}
_LIMIT_FIELDS = {"processes", "users", "truncated"}
_CAPABILITY_FIELDS = {"nvml_supported", "nvml_error", "psutil_error"}


class ProtocolError(ValueError):
    """Raised when a snapshot is not safe to accept or emit."""


def _walk(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ProtocolError("json depth exceeds limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError("object keys must be strings")
            if _utf8_length(key) > MAX_STRING_BYTES:
                raise ProtocolError("object key exceeds string limit")
            _walk(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, depth + 1)
    elif isinstance(value, str):
        if _utf8_length(value) > MAX_STRING_BYTES:
            raise ProtocolError("string exceeds limit")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ProtocolError("non-finite number")


def _utf8_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProtocolError("string is not valid UTF-8") from exc


def _object(document: dict[str, Any], field: str) -> dict[str, Any]:
    value = document.get(field)
    if not isinstance(value, dict):
        raise ProtocolError(f"missing or invalid {field}")
    return value


def _known_fields(document: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(document).difference(allowed)
    if unknown:
        raise ProtocolError(f"unknown {label} field")


def _list(document: dict[str, Any], field: str, limit: int) -> list[dict[str, Any]]:
    value = document.get(field)
    if not isinstance(value, list) or len(value) > limit:
        raise ProtocolError(f"invalid {field} count")
    if any(not isinstance(item, dict) for item in value):
        raise ProtocolError(f"invalid {field} item")
    return value


def _string(
    value: Any, field: str, *, nullable: bool = False, empty: bool = False
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or (not empty and not value):
        raise ProtocolError(f"invalid {field}")
    return value


def _number(
    value: Any,
    field: str,
    *,
    nullable: bool = True,
    minimum: float | None = None,
    maximum: float | None = None,
) -> int | float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"invalid {field}")
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        raise ProtocolError(f"invalid {field}")
    if minimum is not None and value < minimum:
        raise ProtocolError(f"invalid {field}")
    if maximum is not None and value > maximum:
        raise ProtocolError(f"invalid {field}")
    return value


def _integer(
    value: Any,
    field: str,
    *,
    nullable: bool = True,
    minimum: int | None = None,
) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"invalid {field}")
    if minimum is not None and value < minimum:
        raise ProtocolError(f"invalid {field}")
    if abs(value) > MAX_WIRE_INTEGER:
        raise ProtocolError(f"invalid {field}")
    return value


def _validate_timestamp(value: Any) -> None:
    text = _string(value, "captured_at")
    if text is None:
        raise ProtocolError("invalid captured_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError("invalid captured_at") from exc
    if parsed.tzinfo is None:
        raise ProtocolError("invalid captured_at")


def _validate_gpu(gpu: dict[str, Any]) -> None:
    _known_fields(gpu, _GPU_FIELDS, "gpu")
    _string(gpu.get("uuid"), "gpu.uuid")
    _integer(gpu.get("index"), "gpu.index", nullable=False, minimum=0)
    _string(gpu.get("model"), "gpu.model", nullable=True)
    _number(
        gpu.get("utilization_fraction"),
        "gpu.utilization_fraction",
        minimum=0,
        maximum=1,
    )
    for field in ("vram_total_bytes", "vram_used_bytes"):
        _integer(gpu.get(field), f"gpu.{field}", minimum=0)
    _not_greater(
        gpu.get("vram_used_bytes"),
        gpu.get("vram_total_bytes"),
        "gpu.vram_used_bytes",
    )
    _number(gpu.get("temperature_c"), "gpu.temperature_c")
    _number(gpu.get("power_watts"), "gpu.power_watts", minimum=0)
    _integer(
        gpu.get("compute_process_count"),
        "gpu.compute_process_count",
        nullable=False,
        minimum=0,
    )
    for field in ("supported", "mig_detected", "instance_supported"):
        if not isinstance(gpu.get(field), bool):
            raise ProtocolError(f"invalid gpu.{field}")
    _string(gpu.get("error"), "gpu.error", nullable=True)


def _not_greater(value: Any, total: Any, field: str) -> None:
    if value is not None and total is not None and value > total:
        raise ProtocolError(f"invalid {field}")


def _validate_user(user: dict[str, Any]) -> None:
    _known_fields(user, _USER_FIELDS, "user")
    _integer(user.get("uid"), "user.uid", minimum=0)
    _string(user.get("username"), "user.username", nullable=True)
    _number(user.get("cpu_cores"), "user.cpu_cores", minimum=0)
    _integer(user.get("rss_bytes"), "user.rss_bytes", minimum=0)
    _integer(
        user.get("process_count"),
        "user.process_count",
        nullable=False,
        minimum=0,
    )
    _integer(user.get("gpu_process_count"), "user.gpu_process_count", minimum=0)
    _integer(user.get("vram_bytes"), "user.vram_bytes", minimum=0)


def _validate_process(process: dict[str, Any]) -> None:
    forbidden = {
        "args",
        "argv",
        "cmd",
        "cmdline",
        "command",
        "command_line",
        "env",
        "environ",
        "environment",
        "cwd",
        "working_directory",
    }
    if forbidden.intersection(process):
        raise ProtocolError("forbidden process field")
    _known_fields(process, _PROCESS_FIELDS, "process")
    _integer(process.get("pid"), "process.pid", nullable=False, minimum=1)
    _number(process.get("create_time"), "process.create_time", minimum=0)
    _integer(process.get("uid"), "process.uid", minimum=0)
    if "gpu_process" in process and not isinstance(process["gpu_process"], bool):
        raise ProtocolError("invalid process.gpu_process")
    for field in ("username", "name", "executable", "gpu_uuid"):
        _string(process.get(field), f"process.{field}", nullable=True)
    _number(process.get("cpu_cores"), "process.cpu_cores", minimum=0)
    _integer(process.get("rss_bytes"), "process.rss_bytes", minimum=0)
    _integer(process.get("gpu_index"), "process.gpu_index", minimum=0)
    _integer(process.get("vram_bytes"), "process.vram_bytes", minimum=0)
    allocations = process.get("gpu_allocations", [])
    if not isinstance(allocations, list) or len(allocations) > MAX_GPU_ALLOCATIONS:
        raise ProtocolError("invalid process.gpu_allocations")
    for allocation in allocations:
        if not isinstance(allocation, dict):
            raise ProtocolError("invalid process.gpu_allocations item")
        _known_fields(allocation, _ALLOCATION_FIELDS, "allocation")
        _string(allocation.get("gpu_uuid"), "allocation.gpu_uuid")
        _integer(
            allocation.get("gpu_index"),
            "allocation.gpu_index",
            nullable=False,
            minimum=0,
        )
        _integer(allocation.get("vram_bytes"), "allocation.vram_bytes", minimum=0)


def validate_snapshot(
    document: Any,
    *,
    max_bytes: int = MAX_JSON_BYTES,
    encoded_size: int | None = None,
) -> dict[str, Any]:
    """Validate types, ranges, privacy fields, shape, depth, and encoded size."""

    if not isinstance(document, dict):
        raise ProtocolError("snapshot must be an object")
    _known_fields(document, _TOP_LEVEL_FIELDS, "snapshot")
    _walk(document)
    version = document.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ProtocolError("unsupported schema version")
    _validate_timestamp(document.get("captured_at"))
    _string(document.get("boot_id"), "boot_id")
    _string(document.get("helper_version"), "helper_version")
    if document.get("status") not in {"ok", "partial"}:
        raise ProtocolError("invalid status")
    _number(
        document.get("observation_duration_seconds"),
        "observation_duration_seconds",
        nullable=False,
        minimum=0,
        maximum=5,
    )
    _number(
        document.get("collection_duration_seconds"),
        "collection_duration_seconds",
        nullable=False,
        minimum=0,
        maximum=5,
    )

    cpu = _object(document, "cpu")
    _known_fields(cpu, _CPU_FIELDS, "cpu")
    _integer(cpu.get("logical_count"), "cpu.logical_count", nullable=False, minimum=1)
    _number(cpu.get("busy_fraction"), "cpu.busy_fraction", minimum=0, maximum=1)
    for field in ("load_1m", "load_5m", "load_15m"):
        _number(cpu.get(field), f"cpu.{field}", minimum=0)

    memory = _object(document, "memory")
    _known_fields(memory, _MEMORY_FIELDS, "memory")
    for field in (
        "total_bytes",
        "available_bytes",
        "used_bytes",
        "swap_total_bytes",
        "swap_used_bytes",
    ):
        _integer(memory.get(field), f"memory.{field}", minimum=0)
    _not_greater(
        memory.get("available_bytes"),
        memory.get("total_bytes"),
        "memory.available_bytes",
    )
    _not_greater(
        memory.get("used_bytes"), memory.get("total_bytes"), "memory.used_bytes"
    )
    _not_greater(
        memory.get("swap_used_bytes"),
        memory.get("swap_total_bytes"),
        "memory.swap_used_bytes",
    )

    disk = _object(document, "disk")
    _known_fields(disk, _DISK_FIELDS, "disk")
    for field in ("total_bytes", "free_bytes"):
        _integer(disk.get(field), f"disk.{field}", minimum=0)
    _not_greater(disk.get("free_bytes"), disk.get("total_bytes"), "disk.free_bytes")

    visibility = _object(document, "visibility")
    _known_fields(visibility, _VISIBILITY_FIELDS, "visibility")
    for field in ("partial", "counters_truncated"):
        if not isinstance(visibility.get(field), bool):
            raise ProtocolError(f"invalid visibility.{field}")
    for field in ("permission_denied", "processes_visible", "processes_emitted"):
        _integer(
            visibility.get(field),
            f"visibility.{field}",
            nullable=False,
            minimum=0,
        )
    _not_greater(
        visibility.get("processes_emitted"),
        visibility.get("processes_visible"),
        "visibility.processes_emitted",
    )
    limits = _object(document, "limits")
    _known_fields(limits, _LIMIT_FIELDS, "limits")
    _integer(limits.get("processes"), "limits.processes", nullable=False, minimum=0)
    _integer(limits.get("users"), "limits.users", nullable=False, minimum=0)
    if limits["processes"] > MAX_PROCESSES or limits["users"] > MAX_USERS:
        raise ProtocolError("advertised limit exceeds protocol cap")
    if not isinstance(limits.get("truncated"), bool):
        raise ProtocolError("invalid limits.truncated")

    capabilities = _object(document, "capabilities")
    _known_fields(capabilities, _CAPABILITY_FIELDS, "capabilities")
    if not isinstance(capabilities.get("nvml_supported"), bool):
        raise ProtocolError("invalid capabilities.nvml_supported")
    for field in ("nvml_error", "psutil_error"):
        _string(capabilities.get(field), f"capabilities.{field}", nullable=True)

    gpus = _list(document, "gpus", MAX_GPUS)
    users = _list(document, "users", MAX_USERS)
    processes = _list(document, "processes", MAX_PROCESSES)
    for gpu in gpus:
        _validate_gpu(gpu)
    for user in users:
        _validate_user(user)
    for process in processes:
        _validate_process(process)

    if len({gpu["uuid"] for gpu in gpus}) != len(gpus):
        raise ProtocolError("duplicate gpu uuid")
    if len({process["pid"] for process in processes}) != len(processes):
        raise ProtocolError("duplicate process pid")
    user_ids = [user.get("uid") for user in users]
    if len(set(user_ids)) != len(user_ids):
        raise ProtocolError("duplicate user uid")
    if visibility["processes_emitted"] != len(processes):
        raise ProtocolError("visibility process count mismatch")
    if len(processes) > limits["processes"] or len(users) > limits["users"]:
        raise ProtocolError("document exceeds advertised limit")
    if (
        visibility["processes_visible"] > visibility["processes_emitted"]
        and not limits["truncated"]
    ):
        raise ProtocolError("missing truncation flag")
    partial_evidence = bool(
        visibility["partial"]
        or visibility["counters_truncated"]
        or limits["truncated"]
        or capabilities["nvml_error"]
        or capabilities["psutil_error"]
    )
    if partial_evidence and document["status"] != "partial":
        raise ProtocolError("partial data reported as ok")

    if encoded_size is None:
        encode_snapshot(document, max_bytes=max_bytes)
    elif encoded_size > max_bytes:
        raise ProtocolError("snapshot exceeds byte limit")
    return document


def encode_snapshot(
    document: dict[str, Any], *, max_bytes: int = MAX_JSON_BYTES
) -> bytes:
    """Encode compact UTF-8 JSON and enforce the hard wire cap."""

    _walk(document)
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("snapshot is not JSON encodable") from exc
    if len(encoded) > max_bytes:
        raise ProtocolError("snapshot exceeds byte limit")
    return encoded


def decode_snapshot(
    payload: bytes | str, *, max_bytes: int = MAX_JSON_BYTES
) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProtocolError("invalid JSON UTF-8") from exc
    if not isinstance(payload, bytes) or len(payload) > max_bytes:
        raise ProtocolError("snapshot payload exceeds byte limit")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProtocolError("invalid JSON") from exc
    return validate_snapshot(document, max_bytes=max_bytes)
