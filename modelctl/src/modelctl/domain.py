from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Machine:
    alias: str
    ssh_host: str
    ssh_user: str | None
    ssh_port: int | None
    supervisor: str
    inventory_roots: list[str]
    runtime_type: str
    runtime_activate: str | None


@dataclass(frozen=True)
class LogicalModel:
    alias: str
    served_model_name: str
    family: str | None = None
    generation_config: str = "vllm"


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    machine: str
    model_alias: str | None
    canonical_path: str
    format: str | None = None
    size_bytes: int | None = None
    manifest_fingerprint: str | None = None
    observed_at: str | None = None
    status: str = "UNKNOWN"


@dataclass(frozen=True)
class Target:
    target_id: str  # model@machine
    model: str
    machine: str
    artifact_path: str
    gpus: list[int]
    vllm: dict[str, Any]
    lifecycle_mode: str = "persistent"
    bind_host: str = "127.0.0.1"
    security: dict[str, Any] | None = None


@dataclass(frozen=True)
class Deployment:
    deployment_id: str
    target_id: str
    machine_id: str
    config_digest: str
    artifact_id: str
    artifact_fingerprint: str | None
    state: str
    supervisor_unit: str
    started_at: str | None = None
    ready_at: str | None = None
    stopped_at: str | None = None


def target_id(model: str, machine: str) -> str:
    return f"{model}@{machine}"


def config_digest(canonical_json: str | bytes) -> str:
    if isinstance(canonical_json, str):
        canonical_json = canonical_json.encode()
    return hashlib.sha256(canonical_json).hexdigest()


def deployment_id(target: str, digest: str, nonce: str) -> str:
    return f"{target}:{digest[:12]}:{nonce}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sanitize_unit_name(target_id_str: str, digest: str) -> str:
    # modelctl-qwen-72b-gpu-a-58b3c7f1.service
    base = target_id_str.replace("@", "-").replace("_", "-").replace(".", "-")
    # keep alnum and dash
    base = re.sub(r"[^A-Za-z0-9-]", "-", base)
    base = re.sub(r"-+", "-", base).strip("-").lower()
    return f"modelctl-{base}-{digest[:8]}.service"


def validate_alias(alias: str) -> bool:
    return bool(ALIAS_RE.match(alias))


def validate_target_id(tid: str) -> bool:
    return bool(TARGET_RE.match(tid))
