from __future__ import annotations

from typing import Any

from research_kb.domain.vocab import EVIDENCE_STATES, REVIEW_STATES
from research_kb.errors import blocked, schema_validation_failed

_ASSESSABLE_KINDS = {"claim", "knowledge"}


def validate_assessment_payload(payload: dict[str, Any]) -> None:
    if payload["evidence_state"] not in EVIDENCE_STATES:
        raise schema_validation_failed("Unknown evidence state.", allowed=list(EVIDENCE_STATES))
    if payload["review_state"] not in REVIEW_STATES:
        raise schema_validation_failed("Unknown review state.", allowed=list(REVIEW_STATES))
    if payload["evidence_state"] in ("supported", "contested", "refuted") and not payload.get("rationale"):
        raise schema_validation_failed("An assessment with a non-provisional evidence state requires a rationale.")
    if payload["review_state"] == "reviewed" and not payload.get("assessor_actor_id"):
        raise schema_validation_failed("A reviewed assessment must record its assessor.")


def assessment_is_paper_ready(
    assessment: dict[str, Any] | None,
    *,
    open_critical_blockers: list[dict[str, Any]],
    unreviewed_selection: bool,
    inaccessible_indispensable: list[str],
    review_current: bool,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not assessment:
        reasons.append("No evidence assessment is recorded.")
        return False, reasons
    if assessment.get("review_state") != "reviewed":
        reasons.append("The evidence assessment is not reviewed.")
    if assessment.get("evidence_state") not in ("supported",):
        reasons.append("The evidence state is not 'supported' under the reviewed criteria.")
    if not review_current:
        reasons.append("The review is not current for the pinned criterion/evidence revision.")
    if open_critical_blockers:
        reasons.append("Unresolved critical blockers apply to this assessment.")
    if unreviewed_selection:
        reasons.append("Result selection is unreviewed.")
    for item in inaccessible_indispensable:
        reasons.append(f"Indispensable evidence is inaccessible: {item}")
    return (not reasons), reasons


def assert_assessment_allowed(
    *,
    kind: str,
    open_critical_blockers: list[dict[str, Any]],
    unreviewed_selection: bool,
    inaccessible_indispensable: list[str],
) -> None:
    if kind not in _ASSESSABLE_KINDS and kind != "run":
        raise schema_validation_failed("Evidence assessment requires a claim, knowledge, or run target.")
    if open_critical_blockers:
        raise blocked(
            "Critical blockers prevent a paper-ready assessment.",
            blockers=[blocker.get("object_id") for blocker in open_critical_blockers],
        )
    if unreviewed_selection:
        raise blocked("Result selection is unreviewed; assessment cannot be paper-ready.")
    if inaccessible_indispensable:
        raise blocked(
            "Indispensable evidence is inaccessible.",
            evidence=inaccessible_indispensable,
        )


def dedupe_by_underlying_provenance(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for link in links:
        key = link.get("provenance_identity") or link.get("source_object_id") or link.get("object_id")
        if key is None:
            result.append(link)
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(link)
    return result
