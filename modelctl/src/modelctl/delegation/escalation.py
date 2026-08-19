from __future__ import annotations

from typing import Any

MAX_ESCALATIONS = 2
MAX_RETRY_PER_CANDIDATE = 1


def should_escalate(*, current: str, failure_code: str, escalations_used: int) -> str | None:
    if escalations_used >= MAX_ESCALATIONS:
        return None
    if current == "worker" and failure_code in ("E_DELEGATE_VALIDATION_FAILED", "E_DELEGATE_OUT_OF_SCOPE_CHANGE"):
        return "driver"
    if current == "driver" and failure_code in ("E_DELEGATE_VALIDATION_FAILED",):
        return "frontier"
    if failure_code in ("E_DELEGATE_PROVIDER_FAILURE", "E_DELEGATE_RATE_LIMITED") and escalations_used < 1:
        return current  # retry same level
    return None
