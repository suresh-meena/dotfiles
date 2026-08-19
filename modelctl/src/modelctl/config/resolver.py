from __future__ import annotations

import hashlib
import json
from typing import Any

from ..domain import canonical_json, config_digest
from ..errors import ModelctlError


def resolve_target(config: dict[str, Any], target_id: str) -> dict[str, Any]:
    """Return canonical resolved target config for (model@machine). Raises ModelctlError if not found."""
    targets = config.get("targets", {})
    if target_id not in targets:
        raise ModelctlError(code="E_TARGET_NOT_FOUND", message=f"target not found: {target_id}", target=target_id)
    t = targets[target_id]
    model_alias = t["model"]
    machine_alias = t["machine"]
    machines = config.get("machines", {})
    models = config.get("models", {})
    defaults = config.get("defaults", {})

    machine = machines.get(machine_alias)
    model = models.get(model_alias)
    if machine is None:
        raise ModelctlError(code="E_MACHINE_NOT_FOUND", message=f"machine not found: {machine_alias}", machine=machine_alias)
    if model is None:
        raise ModelctlError(code="E_MODEL_NOT_FOUND", message=f"model not found: {model_alias}")

    # Build resolved canonical form
    # Merge defaults + machine defaults + target
    resolved: dict[str, Any] = {
        "target_id": target_id,
        "model": model_alias,
        "machine": machine_alias,
        "served_model_name": model.get("served_model_name", model_alias),
        "artifact": dict(t["artifact"]),
        "gpus": list(t.get("gpus", [])),
        "lifecycle": {**defaults.get("lifecycle", {}), **t.get("lifecycle", {})},
        "security": {**defaults.get("security", {}), **t.get("security", {})},
        "bind_host": defaults.get("bind_host", "127.0.0.1"),
        "startup_timeout_s": defaults.get("startup_timeout_s", 1200),
        "graceful_stop_timeout_s": defaults.get("graceful_stop_timeout_s", 30),
        "kill_timeout_s": defaults.get("kill_timeout_s", 15),
        "cleanup_verify_timeout_s": defaults.get("cleanup_verify_timeout_s", 30),
        "vllm": dict(t.get("vllm", {})),
        "machine_runtime": dict(machine.get("runtime", {})),
        "machine_inventory_roots": list(machine.get("inventory", {}).get("roots", [])),
        "ssh": dict(machine.get("ssh", {})),
        "port": t.get("vllm", {}).get("port") or machine.get("defaults", {}).get("port") or defaults.get("port") or 8000,
    }
    # include gpu_memory_utilization resolution
    if "gpu_memory_utilization" in t.get("vllm", {}):
        resolved["gpu_memory_utilization"] = t["vllm"]["gpu_memory_utilization"]
    elif "gpu_memory_utilization" in machine.get("defaults", {}):
        resolved["gpu_memory_utilization"] = machine["defaults"]["gpu_memory_utilization"]
    else:
        resolved["gpu_memory_utilization"] = defaults.get("gpu_memory_utilization", 0.90)

    # validate bind_host security
    if resolved["bind_host"] != "127.0.0.1" and resolved["bind_host"] != "::1":
        if not resolved["security"].get("allow_remote_exposure"):
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"target {target_id} binds to non-loopback without allow_remote_exposure")

    # ensure artifact path inside allowed roots if roots declared
    artifact_path = resolved["artifact"]["path"]
    roots = resolved["machine_inventory_roots"]
    if roots:
        if not any(artifact_path == r or artifact_path.startswith(r.rstrip("/") + "/") for r in roots):
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"artifact path {artifact_path} outside allowed inventory roots {roots}", target=target_id)

    return resolved


def target_digest(resolved: dict[str, Any]) -> str:
    # canonical_json per spec §6.3
    j = canonical_json(resolved)
    return config_digest(j)


def explain_target(config: dict[str, Any], target_id: str) -> dict[str, Any]:
    resolved = resolve_target(config, target_id)
    digest = target_digest(resolved)
    return {"resolved": resolved, "config_digest": digest, "canonical_json": canonical_json(resolved)}


def list_targets(config: dict[str, Any]) -> list[str]:
    return sorted(config.get("targets", {}).keys())


def list_machines(config: dict[str, Any]) -> list[str]:
    return sorted(config.get("machines", {}).keys())


def list_models(config: dict[str, Any]) -> list[str]:
    return sorted(config.get("models", {}).keys())
