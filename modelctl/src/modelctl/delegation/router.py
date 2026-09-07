from __future__ import annotations

import json
from typing import Any

from ..inventory.registry import Registry
from ..errors import ModelctlError
from .catalog import DEFAULT_MODELS, DEFAULT_PROVIDER

DEFAULT_PROVIDER_ALLOWLIST = [DEFAULT_PROVIDER]


def _default_ref(bin_: str) -> str:
    return DEFAULT_MODELS.get(bin_, "zai-coding-plan/glm-5.3")


def provider_allowlist(config: dict[str, Any] | None) -> list[str]:
    """Providers an explicit model request may use/auto-admit from."""
    if not config:
        return list(DEFAULT_PROVIDER_ALLOWLIST)
    raw = config.get("delegation", {}).get("execution", {}).get("provider_allowlist")
    if isinstance(raw, list) and raw and all(isinstance(p, str) for p in raw):
        return [p for p in raw if p]
    return list(DEFAULT_PROVIDER_ALLOWLIST)


def _split_ref(model_ref: str) -> tuple[str, str]:
    provider, _, model_id = model_ref.partition("/")
    return provider, model_id


def choose_role(task: dict[str, Any]) -> str:
    if task.get("requires_judgment") or task.get("is_architectural"):
        return "brain"
    if task.get("is_repetitive") or task.get("is_parallelizable"):
        return "worker"
    if task.get("has_clear_spec") and task.get("is_bounded_implementation"):
        return "driver"
    return "brain"


def _validate_requested_model(
    registry: Registry,
    model_ref: str,
    *,
    allowlist: list[str] | None = None,
) -> dict[str, Any]:
    """Explicit model request: flexible by design — explicit choice wins over bin
    classification. Any model under an allowlisted provider is usable, even if it
    was never synced/assigned:

      - unknown to the catalog → auto-admitted (enabled + AVAILABLE, audited)
        when its provider is in the allowlist; other providers fail closed.
      - present but disabled while `unclassified` → auto-enabled (the sync
        default is "unlisted", not a denial; the explicit request classifies it).
      - deliberately disabled (has a real bin) or UNAVAILABLE → fail closed.
    """
    if "/" not in model_ref or any(c in model_ref for c in [";", "&", "|", "`", "$", "\n", " "]):
        raise ModelctlError(code="E_DELEGATION_POLICY_DENIED", message=f"invalid model_ref: {model_ref}")
    allowlist = allowlist if allowlist is not None else list(DEFAULT_PROVIDER_ALLOWLIST)
    provider, model_id = _split_ref(model_ref)
    m = registry.get_delegate_model(model_ref)

    if m is None:
        if provider not in allowlist:
            raise ModelctlError(
                code="E_DELEGATION_POLICY_DENIED",
                message=f"provider not allowed for explicit model request: {provider}",
                details={
                    "model_ref": model_ref,
                    "provider_allowlist": allowlist,
                    "suggested": f"add {provider} to delegation.execution.provider_allowlist",
                },
            )
        registry.upsert_delegate_model(
            model_ref,
            provider,
            model_id,
            "unclassified",
            True,
            "AVAILABLE",
            {"source": "explicit-request"},
        )
        registry.add_event(
            "DELEGATE_MODEL_ADMITTED",
            result="ok",
            details={"model_ref": model_ref, "provider": provider},
        )
        return registry.get_delegate_model(model_ref)  # type: ignore[return-value]

    if not m["enabled"]:
        if m["bin"] == "unclassified":
            # never classified: sync default, not operator intent → enable on use
            metadata = json.loads(m["metadata_json"]) if m["metadata_json"] else None
            registry.upsert_delegate_model(
                model_ref,
                m["provider_id"],
                m["model_id"],
                "unclassified",
                True,
                m["availability_status"],
                metadata,
            )
            registry.add_event(
                "DELEGATE_MODEL_ENABLED_BY_REQUEST", result="ok", details={"model_ref": model_ref}
            )
            return registry.get_delegate_model(model_ref)  # type: ignore[return-value]
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
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic model selection per spec §37. Respects availability,
    enablement, privacy, budget.

    Precedence when requested_model is given (task file > CLI flag > config
    role): that exact model is used after validation — flexible admission: any
    model under an allowlisted provider is usable without prior sync/assign;
    otherwise the bin's eligible set decides, falling back to the built-in
    default. Implicit (no explicit model) routing stays strictly classified."""
    if requested_bin not in ("worker", "driver"):
        raise ModelctlError(code="E_DELEGATION_POLICY_DENIED", message=f"unknown bin {requested_bin}")
    if not budget_ok:
        raise ModelctlError(code="E_DELEGATION_BUDGET_EXCEEDED", message="budget hard limit exceeded", details={"bin": requested_bin})

    if requested_model:
        return _validate_requested_model(
            registry, requested_model, allowlist=provider_allowlist(config)
        )

    eligible = _eligible_for_bin(registry, requested_bin)
    if not eligible:
        raise ModelctlError(code="E_DELEGATE_MODEL_UNAVAILABLE", message=f"no available model for bin {requested_bin}")

    return sorted(eligible, key=lambda x: x["model_ref"])[0]


def routing_explain(
    *,
    registry: Registry,
    requested_bin: str,
    requested_model: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        sel = deterministic_select(
            registry=registry,
            requested_bin=requested_bin,
            requested_model=requested_model,
            config=config,
        )
        return {"ok": True, "bin": requested_bin, "selected": sel["model_ref"], "deterministic": True}
    except ModelctlError as e:
        return {"ok": False, **e.to_dict()}
