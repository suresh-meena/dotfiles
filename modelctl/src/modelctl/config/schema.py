from __future__ import annotations

from typing import Any

from ..errors import ModelctlError

# Strict validation per spec §6.2
KNOWN_TOP = {"version", "defaults", "machines", "models", "targets", "delegation", "budget", "cache"}
KNOWN_DEFAULTS = {"startup_timeout_s", "graceful_stop_timeout_s", "kill_timeout_s", "cleanup_verify_timeout_s", "bind_host", "access", "lifecycle", "security", "port", "gpu_memory_utilization"}
KNOWN_MACHINE = {"ssh", "supervisor", "inventory", "runtime", "gpu", "defaults"}
KNOWN_SSH = {"host", "user", "port", "password_file"}
KNOWN_INVENTORY = {"roots", "max_age_before_start_s", "auto_refresh_before_start"}
KNOWN_RUNTIME = {"type", "activate", "image"}
KNOWN_GPU = {"sharing", "require_same_user_for_foreign_processes"}
KNOWN_TARGET = {"model", "machine", "artifact", "gpus", "lifecycle", "security", "tunnel", "vllm"}
KNOWN_ARTIFACT = {"path", "require_observed"}
KNOWN_LIFECYCLE = {"mode", "ttl_s", "lease_grace_s"}
KNOWN_SECURITY = {"allow_remote_exposure", "network_policy", "request_logging", "output_logging", "state_file_mode", "state_dir_mode"}
KNOWN_TUNNEL = {"local_port"}
KNOWN_VLLM = {"tensor_parallel_size", "max_model_len", "gpu_memory_utilization", "dtype", "extra_args"}
KNOWN_BRAIN = {"owner", "final_authority", "authority", "labels"}
KNOWN_BACKEND = {"type", "executable", "provider", "model"}
KNOWN_ROLE = {"backend", "model", "variant", "max_parallel", "max_files_read", "max_files_write", "max_patch_lines", "default_timeout_s", "workspace", "validation"}
KNOWN_ORCHESTRATION = {"max_total_parallel", "max_task_depth", "max_task_fanout", "worker_batch_size", "worker_return_tokens", "driver_return_tokens", "aggregate_return_tokens", "require_structured_results", "recursive_delegation", "fail_fast", "overlap_policy"}
KNOWN_EXECUTION = {"pure_mode", "auto_approve", "auto_update", "provider_allowlist", "run_timeout_s", "graceful_cancel_timeout_s", "cleanup_timeout_s"}
KNOWN_DELEGATION_VALIDATION = {"require_diff_capture", "reject_out_of_scope_changes", "run_declared_tests"}
KNOWN_BUDGET = {"max_parallel_total", "max_parallel_worker", "max_parallel_driver", "max_task_fanout", "max_retry_per_candidate", "max_escalations", "soft_daily_usd", "hard_daily_usd", "go_usage_value"}

SECRET_KEYS = {"api_key", "hf_token", "secret", "token", "password"}


def _reject_unknown(where: str, obj: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(obj.keys()) - allowed
    if unknown:
        raise ModelctlError(
            code="E_CONFIG_UNKNOWN_FIELD",
            message=f"unknown field(s) in {where}: {', '.join(sorted(unknown))}",
            details={"where": where, "unknown": sorted(unknown)},
        )


def _invalid(message: str) -> ModelctlError:
    return ModelctlError(code="E_CONFIG_INVALID", message=message)


def _validate_defaults(raw: dict[str, Any]) -> None:
    if "defaults" not in raw:
        return
    d = raw["defaults"]
    if not isinstance(d, dict):
        raise _invalid("defaults must be mapping")
    _reject_unknown("defaults", d, KNOWN_DEFAULTS)
    for k in ("startup_timeout_s", "graceful_stop_timeout_s", "kill_timeout_s", "cleanup_verify_timeout_s"):
        if k in d and (not isinstance(d[k], int) or d[k] <= 0):
            raise _invalid(f"{k} must be positive integer")
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
            raise _invalid("bind_host must be loopback unless allow_remote_exposure is explicitly enabled per-target")
    if "security" in d:
        _reject_unknown("defaults.security", d["security"], KNOWN_SECURITY)
    if "lifecycle" in d:
        _reject_unknown("defaults.lifecycle", d["lifecycle"], KNOWN_LIFECYCLE)


def _validate_machines(raw: dict[str, Any]) -> None:
    machines = raw.get("machines", {})
    if not isinstance(machines, dict):
        raise _invalid("machines must be mapping")
    for m_alias, m in machines.items():
        if not isinstance(m, dict):
            raise _invalid(f"machine {m_alias} must be mapping")
        _reject_unknown(f"machines.{m_alias}", m, KNOWN_MACHINE)
        if "ssh" in m:
            _reject_unknown(f"machines.{m_alias}.ssh", m["ssh"], KNOWN_SSH)
            if not m["ssh"].get("host"):
                raise _invalid(f"machines.{m_alias}.ssh.host is required")
        if "inventory" in m:
            _reject_unknown(f"machines.{m_alias}.inventory", m["inventory"], KNOWN_INVENTORY)
            roots = m["inventory"].get("roots", [])
            for r in roots:
                if not isinstance(r, str) or not r.startswith("/"):
                    raise _invalid(f"inventory root must be absolute path: {r}")
        if "runtime" in m:
            _reject_unknown(f"machines.{m_alias}.runtime", m["runtime"], KNOWN_RUNTIME)
        if "gpu" in m:
            _reject_unknown(f"machines.{m_alias}.gpu", m["gpu"], KNOWN_GPU)
        # secrets should not be literal in config where reference expected
        for k in m.keys():
            if k.lower() in SECRET_KEYS:
                raise _invalid(f"secret field {k} must be a reference, not literal in config")


def _validate_models(raw: dict[str, Any]) -> None:
    models = raw.get("models", {})
    if not isinstance(models, dict):
        raise _invalid("models must be mapping")
    allowed = {"served_model_name", "family", "generation_config"}
    for alias, spec in models.items():
        if not isinstance(spec, dict):
            raise _invalid(f"model {alias} must be mapping")
        unknown = set(spec.keys()) - allowed
        if unknown:
            raise ModelctlError(code="E_CONFIG_UNKNOWN_FIELD", message=f"unknown field(s) in models.{alias}: {', '.join(unknown)}")


def _validate_targets(raw: dict[str, Any], models: dict[str, Any], machines: dict[str, Any]) -> None:
    targets = raw.get("targets", {})
    if not isinstance(targets, dict):
        raise _invalid("targets must be mapping")
    for tid, t in targets.items():
        if "@" not in tid:
            raise _invalid(f"target id must be model@machine, got {tid}")
        _reject_unknown(f"targets.{tid}", t, KNOWN_TARGET)
        if "artifact" in t:
            _reject_unknown(f"targets.{tid}.artifact", t["artifact"], KNOWN_ARTIFACT)
            p = t["artifact"].get("path", "")
            if not isinstance(p, str) or not p.startswith("/"):
                raise _invalid(f"targets.{tid}.artifact.path must be absolute")
        if "gpus" in t:
            if not isinstance(t["gpus"], list) or any(not isinstance(x, int) or x < 0 for x in t["gpus"]):
                raise _invalid(f"targets.{tid}.gpus must be list of non-negative ints")
            if len(t["gpus"]) != len(set(t["gpus"])):
                raise _invalid(f"targets.{tid}.gpus contains duplicates")
        if "lifecycle" in t:
            _reject_unknown(f"targets.{tid}.lifecycle", t["lifecycle"], KNOWN_LIFECYCLE)
        if "security" in t:
            _reject_unknown(f"targets.{tid}.security", t["security"], KNOWN_SECURITY)
            if t["security"].get("allow_remote_exposure") is True and not t["security"].get("network_policy"):
                raise _invalid(f"targets.{tid}.security.allow_remote_exposure requires network_policy")
        if "tunnel" in t:
            _reject_unknown(f"targets.{tid}.tunnel", t["tunnel"], KNOWN_TUNNEL)
            local_port = t["tunnel"].get("local_port")
            if local_port is not None and (not isinstance(local_port, int) or isinstance(local_port, bool) or not 1 <= local_port <= 65535):
                raise _invalid(f"targets.{tid}.tunnel.local_port must be an integer between 1 and 65535")
        if "vllm" in t:
            _reject_unknown(f"targets.{tid}.vllm", t["vllm"], KNOWN_VLLM)
            if "tensor_parallel_size" in t["vllm"] and "gpus" in t:
                if t["vllm"]["tensor_parallel_size"] > len(t["gpus"]):
                    raise _invalid(f"targets.{tid}.vllm.tensor_parallel_size exceeds gpus count")
        # references
        if t.get("model") not in models:
            raise ModelctlError(code="E_MODEL_NOT_FOUND", message=f"target {tid} references unknown model {t.get('model')}")
        if t.get("machine") not in machines:
            raise ModelctlError(code="E_MACHINE_NOT_FOUND", message=f"target {tid} references unknown machine {t.get('machine')}")
        # no arbitrary extra vllm flags from unknown keys already enforced; extra_args is allowlist only


def _validate_delegation(raw: dict[str, Any]) -> None:
    if "delegation" not in raw:
        return
    deleg = raw["delegation"]
    if not isinstance(deleg, dict):
        raise _invalid("delegation must be mapping")
    _reject_unknown("delegation", deleg, {"enabled", "brain", "backend", "roles", "orchestration", "execution", "validation"})
    if "enabled" in deleg and not isinstance(deleg["enabled"], bool):
        raise _invalid("delegation.enabled must be boolean")
    if "brain" in deleg:
        brain = deleg["brain"]
        if not isinstance(brain, dict):
            raise _invalid("delegation.brain must be mapping")
        _reject_unknown("delegation.brain", brain, KNOWN_BRAIN)
        if "owner" in brain and brain["owner"] != "codex":
            raise _invalid("delegation.brain.owner must be codex")
        if "final_authority" in brain and not isinstance(brain["final_authority"], bool):
            raise _invalid("delegation.brain.final_authority must be boolean")
        if "labels" in brain and (not isinstance(brain["labels"], list) or any(not isinstance(x, str) for x in brain["labels"])):
            raise _invalid("delegation.brain.labels must be a list of strings")
    if "backend" in deleg:
        backend = deleg["backend"]
        if not isinstance(backend, dict):
            raise _invalid("delegation.backend must be mapping")
        _reject_unknown("delegation.backend", backend, KNOWN_BACKEND)
        if "type" in backend and backend["type"] != "opencode":
            raise _invalid("delegation.backend.type must be opencode")
    roles = deleg.get("roles", {})
    if not isinstance(roles, dict):
        raise _invalid("delegation.roles must be mapping")
    _reject_unknown("delegation.roles", roles, {"driver", "worker"})
    for name, role in roles.items():
        if not isinstance(role, dict):
            raise _invalid(f"delegation.roles.{name} must be mapping")
        _reject_unknown(f"delegation.roles.{name}", role, KNOWN_ROLE)
        for key in ("max_parallel", "max_files_read", "max_patch_lines", "default_timeout_s"):
            if key in role and (not isinstance(role[key], int) or role[key] <= 0):
                raise _invalid(f"delegation.roles.{name}.{key} must be a positive integer")
        if "max_files_write" in role and (not isinstance(role["max_files_write"], int) or role["max_files_write"] < 0):
            raise _invalid(f"delegation.roles.{name}.max_files_write must be a non-negative integer")
        if "variant" in role and not isinstance(role["variant"], str):
            raise _invalid(f"delegation.roles.{name}.variant must be a string")
        if "model" in role and not isinstance(role["model"], str):
            raise _invalid(f"delegation.roles.{name}.model must be a string")
        if "workspace" in role and role["workspace"] not in (
            "project_dir",
            "isolated_worktree",
            "staged_or_worktree",
        ):
            raise _invalid(
                f"delegation.roles.{name}.workspace must be project_dir, isolated_worktree, or staged_or_worktree"
            )

    orchestration = deleg.get("orchestration", {})
    if not isinstance(orchestration, dict):
        raise _invalid("delegation.orchestration must be mapping")
    _reject_unknown("delegation.orchestration", orchestration, KNOWN_ORCHESTRATION)
    for key in ("max_total_parallel", "max_task_fanout", "worker_batch_size", "worker_return_tokens", "driver_return_tokens", "aggregate_return_tokens"):
        if key in orchestration and (not isinstance(orchestration[key], int) or orchestration[key] <= 0):
            raise _invalid(f"delegation.orchestration.{key} must be a positive integer")
    if "max_task_depth" in orchestration and (not isinstance(orchestration["max_task_depth"], int) or orchestration["max_task_depth"] < 0):
        raise _invalid("delegation.orchestration.max_task_depth must be a non-negative integer")
    for key in ("require_structured_results", "recursive_delegation", "fail_fast"):
        if key in orchestration and not isinstance(orchestration[key], bool):
            raise _invalid(f"delegation.orchestration.{key} must be boolean")
    if orchestration.get("overlap_policy", "serialize") not in ("serialize", "allow_disjoint"):
        raise _invalid("delegation.orchestration.overlap_policy must be serialize or allow_disjoint")

    execution = deleg.get("execution", {})
    if not isinstance(execution, dict):
        raise _invalid("delegation.execution must be mapping")
    _reject_unknown("delegation.execution", execution, KNOWN_EXECUTION)
    for key in ("pure_mode", "auto_approve", "auto_update"):
        if key in execution and not isinstance(execution[key], bool):
            raise _invalid(f"delegation.execution.{key} must be boolean")
    if "provider_allowlist" in execution:
        allowlist = execution["provider_allowlist"]
        if not isinstance(allowlist, list) or not allowlist or any(not isinstance(x, str) or not x for x in allowlist):
            raise _invalid("delegation.execution.provider_allowlist must be a non-empty list of strings")
    for key in ("run_timeout_s", "graceful_cancel_timeout_s", "cleanup_timeout_s"):
        if key in execution and (not isinstance(execution[key], int) or execution[key] <= 0):
            raise _invalid(f"delegation.execution.{key} must be a positive integer")

    validation = deleg.get("validation", {})
    if not isinstance(validation, dict):
        raise _invalid("delegation.validation must be mapping")
    _reject_unknown("delegation.validation", validation, KNOWN_DELEGATION_VALIDATION)
    for key, value in validation.items():
        if not isinstance(value, bool):
            raise _invalid(f"delegation.validation.{key} must be boolean")


def _validate_budget(raw: dict[str, Any]) -> None:
    if "budget" not in raw:
        return
    budget = raw["budget"]
    if not isinstance(budget, dict):
        raise _invalid("budget must be mapping")
    _reject_unknown("budget", budget, KNOWN_BUDGET)
    for key in ("max_parallel_total", "max_parallel_worker", "max_parallel_driver", "max_task_fanout"):
        if key in budget and (not isinstance(budget[key], int) or budget[key] <= 0):
            raise _invalid(f"budget.{key} must be a positive integer")
    for key in ("max_retry_per_candidate", "max_escalations"):
        if key in budget and (not isinstance(budget[key], int) or budget[key] < 0):
            raise _invalid(f"budget.{key} must be a non-negative integer")
    for key in ("soft_daily_usd", "hard_daily_usd"):
        if key in budget and (not isinstance(budget[key], (int, float)) or budget[key] < 0):
            raise _invalid(f"budget.{key} must be non-negative")


def validate_config(raw: dict[str, Any]) -> None:
    if not isinstance(raw, dict):
        raise _invalid("config must be a mapping")
    _reject_unknown("top", raw, KNOWN_TOP)
    if raw.get("version") != 1:
        raise _invalid("version must be 1")
    _validate_defaults(raw)
    _validate_machines(raw)
    _validate_models(raw)
    _validate_targets(raw, raw.get("models", {}), raw.get("machines", {}))
    _validate_delegation(raw)
    _validate_budget(raw)
