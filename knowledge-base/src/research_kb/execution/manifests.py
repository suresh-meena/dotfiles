from __future__ import annotations

from typing import Any

from research_kb.domain.canonical import canonical_hash
from research_kb.domain.provenance import check_provenance
from research_kb.errors import schema_validation_failed

SECRET_MARKERS = ("token", "secret", "password", "passwd", "api_key", "apikey", "credential", "private_key")


def _scan_for_secrets(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if any(marker in lowered for marker in SECRET_MARKERS) and item not in (None, "", "[redacted]"):
                found.append(f"{path}.{key}")
            found.extend(_scan_for_secrets(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_scan_for_secrets(item, f"{path}[{index}]"))
    return found


def build_manifest(
    *,
    project_id: str,
    study_object_id: str,
    study_revision: int,
    trial_key: str,
    attempt_no: int,
    protocol_revision: str | None,
    policy_revision: str,
    trial: dict[str, Any],
    workspace_identity: str | None,
    repo_root: str | None,
) -> dict[str, Any]:
    invocation = trial.get("invocation") or {}
    if isinstance(invocation.get("command"), str):
        raise schema_validation_failed(
            "Record argument arrays, not a shell string requiring interpretation.",
            field="invocation.command",
        )
    manifest = {
        "scientific_identity": {
            "project_id": project_id,
            "study_object_id": study_object_id,
            "study_revision": study_revision,
            "trial_key": trial_key,
            "attempt_no": attempt_no,
            "protocol_revision": protocol_revision,
            "policy_revision": policy_revision,
        },
        "invocation": {
            "executable": invocation.get("executable"),
            "arguments": invocation.get("arguments", []),
            "working_directory": invocation.get("working_directory"),
            "resolved_config": invocation.get("resolved_config"),
            "resolved_config_hash": canonical_hash(invocation.get("resolved_config") or {}),
            "runner": invocation.get("runner"),
        },
        "code": trial.get("code") or {},
        "inputs": trial.get("inputs") or [],
        "environment": trial.get("environment") or {},
        "randomness": trial.get("randomness") or {},
        "placement": trial.get("placement") or {},
        "outputs": trial.get("outputs") or {},
        "authorization": {
            "approved": trial.get("approved", False),
            "resource_limits": trial.get("limits") or {},
            "budget": trial.get("budget"),
            "walltime_seconds": trial.get("walltime_seconds"),
            "exceptions": trial.get("exceptions") or [],
        },
        "workspace_identity": workspace_identity,
        "repo_root": repo_root,
        "provenance": {
            "code_identity": trial.get("code_identity"),
            "environment_identity": trial.get("environment_identity"),
            "inputs_identity": trial.get("inputs_identity"),
            "assumptions": trial.get("assumptions"),
            "source_anchor_ids": trial.get("source_anchor_ids"),
            "randomness_policy": trial.get("randomness_policy"),
            "seed_policy": trial.get("seed_policy"),
            "seed": trial.get("seed"),
            "splits": trial.get("splits"),
            "preprocessing": trial.get("preprocessing"),
            "metric_definition": trial.get("metric_definition"),
            "dirty_state": (trial.get("code") or {}).get("dirty_state"),
            "identity_assurance": (trial.get("inputs") or [{}])[0].get("identity_assurance")
            if isinstance(trial.get("inputs"), list) and trial.get("inputs")
            else None,
        },
    }
    secrets = _scan_for_secrets(manifest)
    if secrets:
        raise schema_validation_failed(
            "Secrets must be referenced by name, never stored in a manifest.",
            fields=secrets,
        )
    if not isinstance(manifest["invocation"]["arguments"], list):
        raise schema_validation_failed("Invocation arguments must be an array.")
    return manifest


def validate_manifest_provenance(
    manifest: dict[str, Any],
    *,
    method_profile: str,
    intended_use: str,
    deterministic_declared: bool,
    allow_dirty_snapshot: bool,
) -> dict[str, Any]:
    flat = dict(manifest)
    flat.update(manifest.get("provenance") or {})
    result = check_provenance(
        method_profile,
        flat,
        intended_use=intended_use,
        allow_dirty_snapshot=allow_dirty_snapshot,
        deterministic_declared=deterministic_declared,
    )
    return result.to_dict()


def manifest_fingerprint(manifest: dict[str, Any]) -> str:
    return canonical_hash(manifest)
