from __future__ import annotations

from typing import Any

from research_kb.domain.transition_registry import get_operation
from research_kb.domain.vocab import HIGH_RISK_OPERATIONS
from research_kb.errors import approval_stale, permission_denied


def required_capabilities(operations: list[dict[str, Any]]) -> list[str]:
    from research_kb.domain.transition_registry import capability_for

    return sorted({capability_for(operation["op"]) for operation in operations})


def operation_is_high_risk(operation: dict[str, Any]) -> bool:
    spec = get_operation(operation["op"])
    return spec.high_risk or operation["op"] in HIGH_RISK_OPERATIONS


def proposal_requires_approval(operations: list[dict[str, Any]], policy: dict[str, Any]) -> bool:
    if not operations:
        return True
    if any(operation_is_high_risk(operation) for operation in operations):
        return True
    require_list = set(policy.get("review", {}).get("require_approval_for_default", []))
    return any(operation["op"] in require_list for operation in operations)


def auto_apply_allowed(operations: list[dict[str, Any]], policy: dict[str, Any]) -> bool:
    if not policy.get("review", {}).get("auto_apply_low_risk", True):
        return False
    if not policy.get("capture", {}).get("allow_low_risk_auto_apply", True):
        return False
    return not proposal_requires_approval(operations, policy)


def check_operation_capabilities(
    operations: list[dict[str, Any]],
    capabilities: set[str],
    *,
    actor_id: str,
) -> None:
    needed = required_capabilities(operations)
    missing = [capability for capability in needed if capability not in capabilities]
    if missing:
        raise permission_denied(
            "The principal lacks a capability required by this proposal.",
            capability=missing[0],
            actor=actor_id,
        )


def assert_approval_current(
    approval: dict[str, Any],
    *,
    proposal_hash: str,
    expected_versions: dict[str, Any],
    policy_revision: str,
    epoch: str,
) -> None:
    if approval.get("consumed_seq"):
        raise approval_stale("The approval has already been consumed.", approval_id=approval.get("approval_id"))
    if approval.get("proposal_hash") != proposal_hash:
        raise approval_stale("The approval is bound to a different proposal hash.")
    if approval.get("policy_revision") != policy_revision:
        raise approval_stale("The approval was issued under a different policy revision.")
    if approval.get("epoch") not in (None, epoch):
        raise approval_stale("The approval was issued under a different controller epoch.")
    bound = approval.get("expected_versions") or {}
    for key, value in bound.items():
        if expected_versions.get(key) != value:
            raise approval_stale("A bound expected version changed after approval.", field=key)
