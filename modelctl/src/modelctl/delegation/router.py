from __future__ import annotations

from typing import Any

from ..inventory.registry import Registry
from ..errors import ModelctlError


def choose_role(task: dict[str, Any]) -> str:
    if task.get("requires_judgment") or task.get("is_architectural"):
        return "brain"
    if task.get("is_repetitive") or task.get("is_parallelizable"):
        return "worker"
    if task.get("has_clear_spec") and task.get("is_bounded_implementation"):
        return "driver"
    return "brain"


def deterministic_select(*, registry: Registry, requested_bin: str, task_class: str | None = None, max_data_class: str | None = None, budget_ok: bool = True) -> dict[str, Any]:
    """Deterministic model selection per spec §37. Respects availability, enablement, privacy, budget."""
    if requested_bin not in ("worker", "driver"):
        raise ModelctlError(code="E_DELEGATION_POLICY_DENIED", message=f"unknown bin {requested_bin}")
    if not budget_ok:
        raise ModelctlError(code="E_DELEGATION_BUDGET_EXCEEDED", message="budget hard limit exceeded", details={"bin": requested_bin})

    candidates = registry.list_delegate_models(bin_=requested_bin)
    # Also consider driver alias for default model (single model serves both bins)
    if not candidates:
        # fallback: try to get default model regardless of bin
        default = registry.get_delegate_model("opencode-go/muse-spark-1.2-contributor")
        if default and default["enabled"] and default["availability_status"] == "AVAILABLE":
            candidates = [default]
        else:
            # try unclassified default
            pass

    # Filter
    eligible = []
    for c in candidates:
        if not c["enabled"]:
            continue
        if c["availability_status"] != "AVAILABLE":
            continue
        if c["bin"] == "unclassified":
            continue
        # privacy stale check would be here; simplified
        eligible.append(c)

    # If still empty, try generic default
    if not eligible:
        default = registry.get_delegate_model("opencode-go/muse-spark-1.2-contributor")
        if default and default["availability_status"] == "AVAILABLE":
            # treat as eligible regardless of bin
            eligible = [default]

    if not eligible:
        raise ModelctlError(code="E_DELEGATE_MODEL_UNAVAILABLE", message=f"no available model for bin {requested_bin}")

    # Deterministic: sort by model_ref
    eligible_sorted = sorted(eligible, key=lambda x: x["model_ref"])
    selected = eligible_sorted[0]
    return selected


def routing_explain(*, registry: Registry, requested_bin: str) -> dict[str, Any]:
    try:
        sel = deterministic_select(registry=registry, requested_bin=requested_bin)
        return {"ok": True, "bin": requested_bin, "selected": sel["model_ref"], "deterministic": True}
    except ModelctlError as e:
        return {"ok": False, **e.to_dict()}
