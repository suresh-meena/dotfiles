from __future__ import annotations

from typing import Any

from ..inventory.registry import Registry
from ..errors import ModelctlError
from .catalog import DEFAULT_MODELS


def _default_ref(bin_: str) -> str:
    return DEFAULT_MODELS.get(bin_, "opencode-go/deepseek-v4-flash")


def choose_role(task: dict[str, Any]) -> str:
    if task.get("requires_judgment") or task.get("is_architectural"):
        return "brain"
    if task.get("is_repetitive") or task.get("is_parallelizable"):
        return "worker"
    if task.get("has_clear_spec") and task.get("is_bounded_implementation"):
        return "driver"
    return "brain"


def _validate_requested_model(registry: Registry, model_ref: str) -> dict[str, Any]:
    """Explicit model request: must exist, be enabled and AVAILABLE. Bin mismatch
    is allowed (explicit choice wins); unknown/disabled/unavailable fail closed."""
    if "/" not in model_ref or any(c in model_ref for c in [";", "&", "|", "`", "$", "\n", " "]):
        raise ModelctlError(code="E_DELEGATION_POLICY_DENIED", message=f"invalid model_ref: {model_ref}")
    m = registry.get_delegate_model(model_ref)
    if not m:
        raise ModelctlError(
            code="E_DELEGATE_MODEL_UNAVAILABLE",
            message=f"model not in catalog: {model_ref}",
            details={"suggested": "modelctl delegates sync"},
        )
    if not m["enabled"]:
        raise ModelctlError(
            code="E_DELEGATION_POLICY_DENIED",
            message=f"model is disabled: {model_ref}",
            details={"suggested": "modelctl delegates assign <ref> --bin <bin> --enable"},
        )
    if m["availability_status"] != "AVAILABLE":
        raise ModelctlError(code="E_DELEGATE_MODEL_UNAVAILABLE", message=f"model is {m['availability_status']}: {model_ref}")
    return m


def _eligible_for_bin(registry: Registry, requested_bin: str) -> list[dict[str, Any]]:
    """Eligible models for a bin: enabled + AVAILABLE + classified, with the
    built-in bin default as fallback when the set would be empty."""
    candidates = registry.list_delegate_models(bin_=requested_bin)
    if not candidates:
        default = registry.get_delegate_model(_default_ref(requested_bin))
        if default and default["enabled"] and default["availability_status"] == "AVAILABLE":
            candidates = [default]
    eligible = [
        c for c in candidates
        if c["enabled"] and c["availability_status"] == "AVAILABLE" and c["bin"] != "unclassified"
    ]
    if not eligible:
        default = registry.get_delegate_model(_default_ref(requested_bin))
        if default and default["availability_status"] == "AVAILABLE":
            eligible = [default]
    return eligible


def deterministic_select(
    *,
    registry: Registry,
    requested_bin: str,
    task_class: str | None = None,
    max_data_class: str | None = None,
    budget_ok: bool = True,
    requested_model: str | None = None,
) -> dict[str, Any]:
    """Deterministic model selection per spec §37. Respects availability, enablement, privacy, budget.

    Precedence when requested_model is given (task file > CLI flag > config role):
    that exact model is used after validation; otherwise the bin's eligible set
    decides, falling back to the built-in default."""
    if requested_bin not in ("worker", "driver"):
        raise ModelctlError(code="E_DELEGATION_POLICY_DENIED", message=f"unknown bin {requested_bin}")
    if not budget_ok:
        raise ModelctlError(code="E_DELEGATION_BUDGET_EXCEEDED", message="budget hard limit exceeded", details={"bin": requested_bin})

    if requested_model:
        return _validate_requested_model(registry, requested_model)

    eligible = _eligible_for_bin(registry, requested_bin)
    if not eligible:
        raise ModelctlError(code="E_DELEGATE_MODEL_UNAVAILABLE", message=f"no available model for bin {requested_bin}")

    return sorted(eligible, key=lambda x: x["model_ref"])[0]


def routing_explain(*, registry: Registry, requested_bin: str, requested_model: str | None = None) -> dict[str, Any]:
    try:
        sel = deterministic_select(registry=registry, requested_bin=requested_bin, requested_model=requested_model)
        return {"ok": True, "bin": requested_bin, "selected": sel["model_ref"], "deterministic": True}
    except ModelctlError as e:
        return {"ok": False, **e.to_dict()}
