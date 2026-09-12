from __future__ import annotations

from typing import Any

from research_kb.errors import schema_validation_failed

VALIDITY_CHECKS_DEFAULT = ("registered", "schema_valid", "checksum_verified", "not_invalid")


def validate_selection_manifest(manifest: dict[str, Any]) -> None:
    required = ("target_ref", "input_refs", "inclusion_rule", "generated_artifact_hashes")
    for field in required:
        if field not in manifest:
            raise schema_validation_failed(f"Selection manifest is missing required field '{field}'.")
    if not manifest["inclusion_rule"].strip():
        raise schema_validation_failed("A selection manifest requires a declared inclusion rule.")
    if manifest.get("selection_mode") == "best_metric" and not manifest.get("analysis_plan_ref"):
        raise schema_validation_failed(
            "Selecting evidence by best metric requires an explicit analysis plan.",
            hint="A hidden implementation shortcut is not permitted.",
        )
    if manifest.get("exclusions") and not manifest.get("exclusion_reasons"):
        raise schema_validation_failed("Exclusions require recorded reasons.")


def default_ordinary_selection(
    inputs: list[dict[str, Any]],
    *,
    validity_by_object: dict[str, str],
    checks: tuple[str, ...] = VALIDITY_CHECKS_DEFAULT,
) -> dict[str, Any]:
    eligible = [
        item
        for item in inputs
        if validity_by_object.get(item["object_id"], "unknown") == "valid"
    ]
    eligible.sort(key=lambda item: (item.get("attempt_no") or 0, item.get("recorded_seq") or 0))
    if not eligible:
        return {
            "selected": None,
            "rule": "earliest_valid_attempt",
            "checks": list(checks),
            "eligible": [],
            "reason": "No attempt passed the declared validity checks.",
        }
    selected = eligible[0]
    return {
        "selected": selected,
        "rule": "earliest_valid_attempt",
        "checks": list(checks),
        "eligible": [item["object_id"] for item in eligible],
        "reason": "Earliest attempt ordered by immutable attempt number that passed validity checks.",
    }


def independent_replicates(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, list[dict[str, Any]]] = {}
    for item in inputs:
        replicate = item.get("replicate_identity")
        if replicate is None:
            continue
        seen.setdefault(replicate, []).append(item)
    return [
        {"replicate_identity": replicate, "attempts": items}
        for replicate, items in sorted(seen.items())
    ]


def compare_two_revisions(
    *,
    left: dict[str, Any],
    right: dict[str, Any],
    matching_dimensions: list[str],
    permitted_differences: list[str],
    rationale: str,
    checker: str | None,
) -> dict[str, Any]:
    unknown = [
        dimension
        for dimension in matching_dimensions
        if left.get(dimension) is None or right.get(dimension) is None
    ]
    mismatched = [
        dimension
        for dimension in matching_dimensions
        if dimension not in unknown and left.get(dimension) != right.get(dimension)
    ]
    if unknown or mismatched:
        assessment = "needs_review" if unknown else "ineligible"
    else:
        assessment = "eligible"
    return {
        "assessment": assessment,
        "matching_dimensions": list(matching_dimensions),
        "permitted_differences": list(permitted_differences),
        "unknown_dimensions": unknown,
        "mismatched_dimensions": mismatched,
        "rationale": rationale,
        "checker": checker,
    }


def comparison_assessment_document(assessment: dict[str, Any]) -> dict[str, Any]:
    return {
        "assessment": assessment.get("assessment", "not_assessed"),
        "dimensions": assessment.get("matching_dimensions", []),
        "permitted_differences": assessment.get("permitted_differences", []),
        "unknown_dimensions": assessment.get("unknown_dimensions", []),
        "mismatched_dimensions": assessment.get("mismatched_dimensions", []),
        "rationale": assessment.get("rationale", ""),
        "checker": assessment.get("checker"),
    }
