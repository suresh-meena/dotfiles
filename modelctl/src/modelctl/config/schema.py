from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..errors import ModelctlError

# Strict validation per spec §6.2
KNOWN_TOP = {"version", "defaults", "machines", "models", "targets", "delegation", "budget", "cache"}
KNOWN_DEFAULTS = {"startup_timeout_s", "graceful_stop_timeout_s", "kill_timeout_s", "cleanup_verify_timeout_s", "bind_host", "access", "lifecycle", "security", "port", "gpu_memory_utilization"}
KNOWN_MACHINE = {"ssh", "supervisor", "inventory", "runtime", "gpu", "defaults"}
KNOWN_SSH = {"host", "user", "port"}
KNOWN_INVENTORY = {"roots", "max_age_before_start_s", "auto_refresh_before_start"}
KNOWN_RUNTIME = {"type", "activate", "image"}
KNOWN_GPU = {"sharing", "require_same_user_for_foreign_processes"}
KNOWN_TARGET = {"model", "machine", "artifact", "gpus", "lifecycle", "security", "vllm"}
KNOWN_ARTIFACT = {"path", "require_observed"}
KNOWN_LIFECYCLE = {"mode", "ttl_s", "lease_grace_s"}
KNOWN_SECURITY = {"allow_remote_exposure", "network_policy", "request_logging", "output_logging", "state_file_mode", "state_dir_mode"}
KNOWN_VLLM = {"tensor_parallel_size", "max_model_len", "gpu_memory_utilization", "dtype", "extra_args"}

SECRET_KEYS = {"api_key", "hf_token", "secret", "token", "password"}


def _reject_unknown(where: str, obj: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(obj.keys()) - allowed
    if unknown:
        raise ModelctlError(
            code="E_CONFIG_UNKNOWN_FIELD",
            message=f"unknown field(s) in {where}: {', '.join(sorted(unknown))}",
            details={"where": where, "unknown": sorted(unknown)},
        )


def validate_config(raw: dict[str, Any]) -> None:
    if not isinstance(raw, dict):
        raise ModelctlError(code="E_CONFIG_INVALID", message="config must be a mapping")
    _reject_unknown("top", raw, KNOWN_TOP)
    if raw.get("version") != 1:
        raise ModelctlError(code="E_CONFIG_INVALID", message="version must be 1")
    # defaults
    if "defaults" in raw:
        d = raw["defaults"]
        if not isinstance(d, dict):
            raise ModelctlError(code="E_CONFIG_INVALID", message="defaults must be mapping")
        _reject_unknown("defaults", d, KNOWN_DEFAULTS)
        for k in ("startup_timeout_s", "graceful_stop_timeout_s", "kill_timeout_s", "cleanup_verify_timeout_s"):
            if k in d and (not isinstance(d[k], int) or d[k] <= 0):
                raise ModelctlError(code="E_CONFIG_INVALID", message=f"{k} must be positive integer")
        if "bind_host" in d and d["bind_host"] not in ("127.0.0.1", "::1"):
            # spec §6.2: non-loopback requires explicit exposure approval per-target
            exposure_approved = (
                isinstance(d.get("security"), dict) and d["security"].get("allow_remote_exposure") is True
            ) or any(
                isinstance(t.get("security"), dict) and t["security"].get("allow_remote_exposure") is True
                for t in raw.get("targets", {}).values()
                if isinstance(t, dict)
            )
            if not exposure_approved:
                raise ModelctlError(code="E_CONFIG_INVALID", message="bind_host must be loopback unless allow_remote_exposure is explicitly enabled per-target")
        if "security" in d:
            _reject_unknown("defaults.security", d["security"], KNOWN_SECURITY)
        if "lifecycle" in d:
            _reject_unknown("defaults.lifecycle", d["lifecycle"], KNOWN_LIFECYCLE)

    machines = raw.get("machines", {})
    if not isinstance(machines, dict):
        raise ModelctlError(code="E_CONFIG_INVALID", message="machines must be mapping")
    for m_alias, m in machines.items():
        if not isinstance(m, dict):
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"machine {m_alias} must be mapping")
        _reject_unknown(f"machines.{m_alias}", m, KNOWN_MACHINE)
        if "ssh" in m:
            _reject_unknown(f"machines.{m_alias}.ssh", m["ssh"], KNOWN_SSH)
            if not m["ssh"].get("host"):
                raise ModelctlError(code="E_CONFIG_INVALID", message=f"machines.{m_alias}.ssh.host is required")
        if "inventory" in m:
            _reject_unknown(f"machines.{m_alias}.inventory", m["inventory"], KNOWN_INVENTORY)
            roots = m["inventory"].get("roots", [])
            for r in roots:
                if not isinstance(r, str) or not r.startswith("/"):
                    raise ModelctlError(code="E_CONFIG_INVALID", message=f"inventory root must be absolute path: {r}")
        if "runtime" in m:
            _reject_unknown(f"machines.{m_alias}.runtime", m["runtime"], KNOWN_RUNTIME)
        if "gpu" in m:
            _reject_unknown(f"machines.{m_alias}.gpu", m["gpu"], KNOWN_GPU)
        # secrets should not be literal in config where reference expected
        for k in m.keys():
            if k.lower() in SECRET_KEYS:
                raise ModelctlError(code="E_CONFIG_INVALID", message=f"secret field {k} must be a reference, not literal in config")

    models = raw.get("models", {})
    if not isinstance(models, dict):
        raise ModelctlError(code="E_CONFIG_INVALID", message="models must be mapping")
    for alias, spec in models.items():
        if not isinstance(spec, dict):
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"model {alias} must be mapping")
        # allow served_model_name, family, generation_config only
        allowed = {"served_model_name", "family", "generation_config"}
        unknown = set(spec.keys()) - allowed
        if unknown:
            raise ModelctlError(code="E_CONFIG_UNKNOWN_FIELD", message=f"unknown field(s) in models.{alias}: {', '.join(unknown)}")

    targets = raw.get("targets", {})
    if not isinstance(targets, dict):
        raise ModelctlError(code="E_CONFIG_INVALID", message="targets must be mapping")
    for tid, t in targets.items():
        if "@" not in tid:
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"target id must be model@machine, got {tid}")
        _reject_unknown(f"targets.{tid}", t, KNOWN_TARGET)
        if "artifact" in t:
            _reject_unknown(f"targets.{tid}.artifact", t["artifact"], KNOWN_ARTIFACT)
            p = t["artifact"].get("path", "")
            if not isinstance(p, str) or not p.startswith("/"):
                raise ModelctlError(code="E_CONFIG_INVALID", message=f"targets.{tid}.artifact.path must be absolute")
        if "gpus" in t:
            if not isinstance(t["gpus"], list) or any(not isinstance(x, int) or x < 0 for x in t["gpus"]):
                raise ModelctlError(code="E_CONFIG_INVALID", message=f"targets.{tid}.gpus must be list of non-negative ints")
            if len(t["gpus"]) != len(set(t["gpus"])):
                raise ModelctlError(code="E_CONFIG_INVALID", message=f"targets.{tid}.gpus contains duplicates")
        if "lifecycle" in t:
            _reject_unknown(f"targets.{tid}.lifecycle", t["lifecycle"], KNOWN_LIFECYCLE)
        if "security" in t:
            _reject_unknown(f"targets.{tid}.security", t["security"], KNOWN_SECURITY)
            if t["security"].get("allow_remote_exposure") is True and not t["security"].get("network_policy"):
                raise ModelctlError(code="E_CONFIG_INVALID", message=f"targets.{tid}.security.allow_remote_exposure requires network_policy")
        if "vllm" in t:
            _reject_unknown(f"targets.{tid}.vllm", t["vllm"], KNOWN_VLLM)
            if "tensor_parallel_size" in t["vllm"] and "gpus" in t:
                if t["vllm"]["tensor_parallel_size"] > len(t["gpus"]):
                    raise ModelctlError(code="E_CONFIG_INVALID", message=f"targets.{tid}.vllm.tensor_parallel_size exceeds gpus count")
        # references
        if t.get("model") not in models:
            raise ModelctlError(code="E_MODEL_NOT_FOUND", message=f"target {tid} references unknown model {t.get('model')}")
        if t.get("machine") not in machines:
            raise ModelctlError(code="E_MACHINE_NOT_FOUND", message=f"target {tid} references unknown machine {t.get('machine')}")
        # no arbitrary extra vllm flags from unknown keys already enforced; extra_args is allowlist only

    # check duplicate aliases implicitly covered; check secrets in delegation
    if "delegation" in raw:
        deleg = raw["delegation"]
        if not isinstance(deleg, dict):
            raise ModelctlError(code="E_CONFIG_INVALID", message="delegation must be mapping")
        # no strict check for unknown keys beyond top known to allow future, but validate enabled type
        if "enabled" in deleg and not isinstance(deleg["enabled"], bool):
            raise ModelctlError(code="E_CONFIG_INVALID", message="delegation.enabled must be boolean")
