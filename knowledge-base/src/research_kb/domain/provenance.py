from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from research_kb.errors import provenance_incomplete

METHOD_PROFILES: dict[str, dict[str, Any]] = {
    "theoretical": {
        "required": ("assumptions",),
        "randomness": "not_applicable",
        "description": "Analytical work requires explicit assumptions and derivation provenance.",
    },
    "numerical": {
        "required": ("code_identity", "environment_identity", "inputs_identity"),
        "randomness": "declared_policy",
        "description": "Numerical work requires code/environment identity and input manifests.",
    },
    "experimental": {
        "required": ("code_identity", "environment_identity", "inputs_identity", "randomness_policy"),
        "randomness": "required",
        "description": "Experimental work requires seed or an explicit not_applicable for deterministic methods.",
    },
    "literature": {
        "required": ("source_anchor_ids",),
        "randomness": "not_applicable",
        "description": "Literature work requires precise source anchors.",
    },
    "ml_evaluation": {
        "required": (
            "code_identity",
            "environment_identity",
            "inputs_identity",
            "splits",
            "preprocessing",
            "seed_policy",
            "metric_definition",
        ),
        "randomness": "required",
        "description": "ML evaluation requires splits, preprocessing, seed policy, and metric definition.",
    },
}

DIAGNOSTIC_EXCEPTION_FIELDS = ("blocker_ref", "operation", "study_ref", "reason", "expires_at")


@dataclass
class ProvenanceResult:
    profile: str
    complete: bool
    missing: list[str]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "complete": self.complete,
            "missing": self.missing,
            "notes": self.notes,
        }


def randomness_policy(manifest: dict[str, Any]) -> str | None:
    policy = manifest.get("randomness_policy")
    if policy in ("not_applicable", "fixed_seed", "seed_policy_documented"):
        return policy
    if manifest.get("seed") is not None:
        return "fixed_seed"
    return None


def check_provenance(
    method_profile: str,
    manifest: dict[str, Any],
    *,
    intended_use: str = "exploratory",
    allow_dirty_snapshot: bool = False,
    diagnostic_exception: dict[str, Any] | None = None,
    deterministic_declared: bool = False,
) -> ProvenanceResult:
    profile = METHOD_PROFILES.get(method_profile)
    if profile is None:
        return ProvenanceResult(method_profile, False, ["unknown_method_profile"], [])
    missing: list[str] = []
    notes: list[str] = []
    for field in profile["required"]:
        if field not in manifest or manifest[field] is None:
            missing.append(field)
    if profile["randomness"] == "required" and randomness_policy(manifest) is None and not deterministic_declared:
        missing.append("randomness_policy")
    if method_profile in ("numerical", "experimental", "ml_evaluation"):
        code_identity = manifest.get("code_identity")
        if code_identity in (None, "", "unknown"):
            missing.append("code_identity")
    if deterministic_declared:
        notes.append("Deterministic method declared randomness not applicable.")
    if intended_use == "paper_critical":
        if not allow_dirty_snapshot and manifest.get("dirty_state") is True:
            missing.append("clean_snapshot")
        if manifest.get("identity_assurance") == "metadata_only":
            missing.append("content_identity")
    if diagnostic_exception:
        for field in DIAGNOSTIC_EXCEPTION_FIELDS:
            if not diagnostic_exception.get(field):
                missing.append(f"diagnostic_exception.{field}")
        notes.append("Diagnostic exception recorded; it does not waive blocker checks for paper conclusions.")
    complete = not missing
    if not complete and intended_use == "paper_critical":
        raise provenance_incomplete(
            "Paper-critical use fails closed on missing provenance.",
            missing=missing,
            profile=method_profile,
        )
    return ProvenanceResult(method_profile, complete, missing, notes)
