from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


# Stable error codes per spec §17.1 and §45
ERROR_CODES = {
    # config/target
    "E_CONFIG_INVALID",
    "E_CONFIG_UNKNOWN_FIELD",
    "E_TARGET_NOT_FOUND",
    "E_MACHINE_NOT_FOUND",
    "E_MODEL_NOT_FOUND",
    # ssh/remote
    "E_SSH_UNREACHABLE",
    "E_SSH_HOSTKEY_CHANGED",
    "E_REMOTE_IDENTITY_MISMATCH",
    # artifact
    "E_ARTIFACT_MISSING",
    "E_ARTIFACT_STALE",
    "E_ARTIFACT_CHANGED",
    "E_ARTIFACT_AMBIGUOUS",
    "E_ARTIFACT_UNREADABLE",
    # gpu
    "E_GPU_NOT_FOUND",
    "E_GPU_BUSY_MANAGED",
    "E_GPU_BUSY_FOREIGN",
    "E_GPU_STATE_UNKNOWN",
    "E_GPU_RESERVATION_CONFLICT",
    # port/runtime
    "E_PORT_BUSY",
    "E_RUNTIME_NOT_FOUND",
    "E_RUNTIME_VERSION_UNSUPPORTED",
    "E_PREFLIGHT_FAILED",
    # start/health
    "E_START_TIMEOUT",
    "E_START_EXITED",
    "E_HEALTH_FAILED",
    "E_MODEL_IDENTITY_MISMATCH",
    # stop
    "E_STOP_TIMEOUT",
    "E_PROCESS_OWNERSHIP_UNPROVEN",
    "E_VRAM_RELEASE_UNVERIFIED",
    "E_LEAK_SUSPECTED",
    # other
    "E_TUNNEL_FAILED",
    "E_STATE_CORRUPT",
    "E_RECONCILE_REQUIRED",
    "E_INTERNAL",
    # delegation
    "E_OPENCODE_NOT_FOUND",
    "E_OPENCODE_VERSION_UNSUPPORTED",
    "E_OPENCODE_AUTH_REQUIRED",
    "E_DELEGATE_MODEL_UNAVAILABLE",
    "E_DELEGATE_MODEL_UNCLASSIFIED",
    "E_DELEGATE_PROVIDER_FAILURE",
    "E_DELEGATE_RATE_LIMITED",
    "E_DELEGATE_CONTEXT_TOO_LARGE",
    "E_DELEGATION_POLICY_DENIED",
    "E_DELEGATION_PRIVACY_DENIED",
    "E_DELEGATION_PRIVACY_STALE",
    "E_DELEGATION_BUDGET_EXCEEDED",
    "E_OPENCODE_POLICY_DRIFT",
    "E_DELEGATION_POLICY_LOCK_STALE",
    "E_DELEGATION_DEPTH_EXCEEDED",
    "E_DELEGATION_SCOPE_REQUIRED",
    "E_DELEGATE_OUTPUT_MALFORMED",
    "E_DELEGATE_OUT_OF_SCOPE_CHANGE",
    "E_DELEGATE_VALIDATION_FAILED",
    "E_DELEGATE_TIMEOUT",
    "E_DELEGATE_CANCEL_FAILED",
    "E_WORKTREE_CREATE_FAILED",
    "E_WORKTREE_DIRTY_BASE",
    "E_WORKTREE_CLEANUP_FAILED",
}


_SUGGESTED_ACTIONS = {
    "E_CONFIG_INVALID": "modelctl config validate",
    "E_CONFIG_UNKNOWN_FIELD": "modelctl config validate",
    "E_TARGET_NOT_FOUND": "modelctl targets list",
    "E_MACHINE_NOT_FOUND": "modelctl machines list",
    "E_MODEL_NOT_FOUND": "modelctl models list",
    "E_SSH_UNREACHABLE": "modelctl machines probe --machine <machine>",
    "E_ARTIFACT_MISSING": "modelctl inventory sync --machine <machine>",
    "E_ARTIFACT_STALE": "modelctl inventory sync --machine <machine>",
    "E_GPU_BUSY_FOREIGN": "modelctl gpu status --machine <machine>",
    "E_GPU_BUSY_MANAGED": "modelctl ps --machine <machine>",
    "E_PORT_BUSY": "modelctl ps --machine <machine>",
    "E_RUNTIME_NOT_FOUND": "modelctl doctor --machine <machine>",
    "E_HEALTH_FAILED": "modelctl logs <model> --machine <machine>",
    "E_LEAK_SUSPECTED": "modelctl doctor --target <target>",
    "E_TUNNEL_FAILED": "modelctl doctor --target <target>",
    "E_RECONCILE_REQUIRED": "modelctl reconcile --machine <machine>",
    "E_DELEGATE_MODEL_UNAVAILABLE": "modelctl delegates sync && modelctl delegates list",
    "E_DELEGATION_BUDGET_EXCEEDED": "modelctl budget status",
    "E_DELEGATION_PRIVACY_DENIED": "modelctl delegates doctor",
    "E_DELEGATION_POLICY_LOCK_STALE": "modelctl delegates doctor",
}


@dataclass(frozen=True)
class ModelctlError(Exception):
    code: str
    message: str
    target: str | None = None
    machine: str | None = None
    retryable: bool = False
    details: dict[str, Any] | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {self.code}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.target:
            d["target"] = self.target
        if self.machine:
            d["machine"] = self.machine
        if self.details:
            d["details"] = self.details
        if self.trace_id:
            d["trace_id"] = self.trace_id
        d["suggested_action"] = _SUGGESTED_ACTIONS.get(self.code, "modelctl doctor")
        return d


def e_internal(msg: str, *, trace_id: str | None = None, details: dict[str, Any] | None = None) -> ModelctlError:
    return ModelctlError(
        code="E_INTERNAL",
        message=msg,
        retryable=False,
        trace_id=trace_id or uuid.uuid4().hex[:12],
        details=details,
    )


def is_retryable(code: str) -> bool:
    return code in {
        "E_SSH_UNREACHABLE",
        "E_START_TIMEOUT",
        "E_HEALTH_FAILED",
        "E_DELEGATE_PROVIDER_FAILURE",
        "E_DELEGATE_RATE_LIMITED",
        "E_GPU_STATE_UNKNOWN",
    }
