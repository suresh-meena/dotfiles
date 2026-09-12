from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KBError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    recovery: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }
        if self.recovery:
            payload["recovery"] = self.recovery
        return payload


def project_required(message: str = "A project identity is required.") -> KBError:
    return KBError(
        "PROJECT_REQUIRED",
        message,
        recovery="Resolve the project from the trusted .research/project.toml or an explicit project ID.",
    )


def project_mismatch(message: str, expected: str, actual: str) -> KBError:
    return KBError(
        "PROJECT_MISMATCH",
        message,
        {"expected_project_id": expected, "actual_project_id": actual},
        recovery="Stop cross-project action and resolve project identity.",
    )


def not_found(message: str, **details: Any) -> KBError:
    return KBError(
        "NOT_FOUND",
        message,
        details,
        recovery="Check the identity/revision or register the referenced record.",
    )


def permission_denied(message: str, capability: str = "", actor: str = "") -> KBError:
    return KBError(
        "PERMISSION_DENIED",
        message,
        {"required_capability": capability, "actor_id": actor},
        recovery="Use a principal holding the required capability; do not bypass policy through another transport.",
    )


def schema_validation_failed(message: str, **details: Any) -> KBError:
    return KBError(
        "SCHEMA_VALIDATION_FAILED",
        message,
        details,
        recovery="Correct the specified fields; do not weaken validation.",
    )


def reference_unresolved(message: str, **details: Any) -> KBError:
    return KBError(
        "REFERENCE_UNRESOLVED",
        message,
        details,
        recovery="Retrieve or register the real endpoint; do not invent an identity.",
    )


def wrong_reference_type(message: str, **details: Any) -> KBError:
    return KBError(
        "WRONG_REFERENCE_TYPE",
        message,
        details,
        recovery="Use an endpoint of the permitted kind for this predicate.",
    )


def revision_conflict(expected: int, actual: int, diff: dict[str, Any] | None = None) -> KBError:
    return KBError(
        "REVISION_CONFLICT",
        "The object changed since the expected revision was read.",
        {"expected_revision": expected, "actual_revision": actual, "diff": diff or {}},
        recovery="Reread the record and reconcile the proposed change before proposing again.",
    )


def idempotency_conflict(message: str = "The same request ID was used with a different payload.") -> KBError:
    return KBError(
        "IDEMPOTENCY_CONFLICT",
        message,
        recovery="Investigate the original intent; do not mask the conflict with a new request ID.",
    )


def approval_required(proposal_hash: str, required: list[str]) -> KBError:
    return KBError(
        "APPROVAL_REQUIRED",
        "This mutation requires reviewer approval.",
        {"proposal_hash": proposal_hash, "required_capabilities": required},
        recovery="Obtain a bounded approval bound to this proposal hash and expected revisions.",
    )


def approval_stale(message: str, **details: Any) -> KBError:
    return KBError(
        "APPROVAL_STALE",
        message,
        details,
        recovery="Obtain a fresh approval bound to the current proposal and revisions.",
    )


def blocked(message: str, **details: Any) -> KBError:
    return KBError(
        "BLOCKED",
        message,
        details,
        recovery="Surface exact blocking criteria and any narrowly scoped authorized exception.",
    )


def provenance_incomplete(message: str, **details: Any) -> KBError:
    return KBError(
        "PROVENANCE_INCOMPLETE",
        message,
        details,
        recovery="Supply the required provenance fields or keep the record explicitly provisional.",
    )


def cursor_invalid(message: str, **details: Any) -> KBError:
    return KBError(
        "CURSOR_INVALID",
        message,
        details,
        recovery="Restart from an explicit snapshot cursor.",
    )


def epoch_changed(expected: str, actual: str) -> KBError:
    return KBError(
        "EPOCH_CHANGED",
        "The controller epoch changed; the cursor or token is no longer valid.",
        {"expected_epoch": expected, "actual_epoch": actual},
        recovery="Restart from an explicit snapshot; do not assume no changes.",
    )


def artifact_unavailable(message: str, **details: Any) -> KBError:
    return KBError(
        "ARTIFACT_UNAVAILABLE",
        message,
        details,
        recovery="Preserve the reference and report the unavailable evidence; do not fabricate contents.",
    )


def execution_pending(execution_id: str) -> KBError:
    return KBError(
        "EXECUTION_PENDING",
        "The execution outcome is not yet established.",
        {"execution_id": execution_id},
        retryable=True,
        recovery="Reconcile the same execution ID; do not launch another attempt.",
    )


def unsupported_version(message: str, **details: Any) -> KBError:
    return KBError(
        "UNSUPPORTED_VERSION",
        message,
        details,
        recovery="Use compatible reads/proposals; do not perform speculative writes.",
    )


def capability_unavailable(message: str, **details: Any) -> KBError:
    return KBError(
        "CAPABILITY_UNAVAILABLE",
        message,
        details,
        recovery="Use only operations actually exposed by the runtime; do not simulate tools.",
    )


def storage_failure(message: str, **details: Any) -> KBError:
    return KBError(
        "STORAGE_FAILURE",
        message,
        details,
        recovery="Do not claim persistence; retain the intended request and retry safely.",
    )


def temporary_unavailable(message: str, **details: Any) -> KBError:
    return KBError(
        "TEMPORARY_UNAVAILABLE",
        message,
        details,
        retryable=True,
        recovery="Retry the same logical request when the dependency is reachable.",
    )
