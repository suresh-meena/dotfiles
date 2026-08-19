from __future__ import annotations

from pathlib import Path
from typing import Any

ALLOWED_AGENTS = {"modelctl-worker-read", "modelctl-worker-edit", "modelctl-driver"}
ALLOWED_BINS = {"worker", "driver"}

# Per spec, every managed invocation uses --pure and never --auto
REQUIRED_FLAGS = ["--pure"]


def validate_task_contract(task: dict[str, Any]) -> None:
    from ..errors import ModelctlError

    if "role" not in task:
        raise ModelctlError(code="E_DELEGATION_SCOPE_REQUIRED", message="task role is required")
    if task["role"] not in ALLOWED_BINS and task["role"] not in ("brain",):
        raise ModelctlError(code="E_DELEGATION_POLICY_DENIED", message=f"invalid role {task['role']}")
    if task.get("agent_profile") and task["agent_profile"] not in ALLOWED_AGENTS:
        raise ModelctlError(code="E_DELEGATION_POLICY_DENIED", message=f"agent profile not allowlisted: {task['agent_profile']}")


def effective_policy_hash(task: dict[str, Any]) -> str:
    import hashlib, json

    j = json.dumps({k: task[k] for k in sorted(task.keys()) if k in ("role", "agent_profile", "allowed_write_paths", "validation")}, sort_keys=True)
    return hashlib.sha256(j.encode()).hexdigest()[:12]
