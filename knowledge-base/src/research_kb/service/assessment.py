from __future__ import annotations

import json
import sqlite3
from typing import Any

from research_kb.domain.evidence import validate_assessment_payload
from research_kb.domain.selection import (
    comparison_assessment_document,
    default_ordinary_selection,
    independent_replicates,
    validate_selection_manifest,
)
from research_kb.domain.vocab import COMPARISON_ASSESSMENTS
from research_kb.errors import permission_denied, schema_validation_failed
from research_kb.service.objects import Mutation, create_link, revise_object
from research_kb.storage import repo


def assess_evidence(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    conn = mutation.ctx.conn
    capabilities = mutation.ctx.capabilities()
    if "assess_evidence" not in capabilities and "accept_conclusions" not in capabilities:
        raise permission_denied(
            "Recording a reviewed evidence assessment requires the assess_evidence capability.",
            capability="assess_evidence",
            actor=mutation.actor_id,
        )
    target = repo.resolve_ref(conn, mutation.project_id, payload["ref"], require_revision=True)
    if target["kind"] not in ("claim", "knowledge", "run"):
        raise schema_validation_failed(
            "Evidence assessment target must be a claim, knowledge, or run record.",
            kind=target["kind"],
        )
    expected_revision = int(payload.get("expected_revision", target["revision"]))
    if expected_revision != target["revision"]:
        from research_kb.errors import revision_conflict

        raise revision_conflict(expected_revision, target["revision"])
    assessment = {
        "evidence_state": payload["evidence_state"],
        "review_state": payload["review_state"],
        "rationale": payload["rationale"],
        "assessor_actor_id": mutation.actor_id,
        "assessed_revision": int(payload["assessed_revision"]),
        "criterion_ref": payload.get("criterion_ref"),
        "support_refs": payload.get("support_refs", []),
        "contradiction_refs": payload.get("contradiction_refs", []),
        "exclusions": payload.get("exclusions", []),
        "missing_checks": payload.get("missing_checks", []),
        "assessed_seq": mutation.commit_seq,
    }
    validate_assessment_payload(
        {
            "evidence_state": assessment["evidence_state"],
            "review_state": assessment["review_state"],
            "rationale": assessment["rationale"],
            "assessor_actor_id": mutation.actor_id,
        }
    )
    if assessment["assessed_revision"] != target["revision"]:
        raise schema_validation_failed(
            "The assessed revision must equal the revision the assessment is recorded against.",
            assessed_revision=assessment["assessed_revision"],
            target_revision=target["revision"],
        )
    for ref in assessment["support_refs"] + assessment["contradiction_refs"]:
        repo.resolve_ref(conn, mutation.project_id, ref, require_revision=True)
    if assessment["evidence_state"] == "supported" and assessment["review_state"] == "reviewed":
        from research_kb.domain.evidence import assert_assessment_allowed

        blockers = applying_blockers(conn, mutation.project_id, target["object_id"])
        inaccessible = inaccessible_support(conn, mutation.project_id, assessment["support_refs"])
        assert_assessment_allowed(
            kind=target["kind"],
            open_critical_blockers=blockers,
            unreviewed_selection=_unreviewed_selection(repo.parse_state(target)),
            inaccessible_indispensable=inaccessible,
        )
    state = repo.parse_state(target)
    state["assessment"] = assessment
    if target["kind"] == "claim":
        state["evidence_state"] = assessment["evidence_state"]
        state["review_state"] = assessment["review_state"]
        state["assessment_rationale"] = assessment["rationale"]
    link_objects = []
    for ref in assessment["support_refs"]:
        link = create_link(
            mutation,
            predicate="supports",
            src_ref=ref,
            dst_ref={"object_id": target["object_id"], "revision": target["revision"]},
            rationale=assessment["rationale"],
            review_state="reviewed",
        )
        link_objects.append(link["object_id"])
    for ref in assessment["contradiction_refs"]:
        link = create_link(
            mutation,
            predicate="contradicts",
            src_ref=ref,
            dst_ref={"object_id": target["object_id"], "revision": target["revision"]},
            rationale=assessment["rationale"],
            review_state="reviewed",
        )
        link_objects.append(link["object_id"])
    updated = revise_object(
        mutation,
        object_id=target["object_id"],
        expected_revision=target["revision"],
        state_json=state,
    )
    return {"assessment": assessment, "updated": updated, "evidence_links": link_objects}


def applying_blockers(conn: sqlite3.Connection, project_id: str, target_object_id: str) -> list[dict[str, Any]]:
    from research_kb.domain.blocking_rules import applicability_context, blocker_evaluation

    target_revision = repo.current_revision(conn, project_id, target_object_id)
    context = applicability_context(repo.parse_state(target_revision))
    blockers: list[dict[str, Any]] = []
    for link in repo.list_links(conn, project_id=project_id, object_id=target_object_id, predicate="blocks", direction="in"):
        source_row = repo.object_row_or_none(conn, project_id, link["src_object_id"])
        if source_row is None:
            continue
        state = repo.parse_state(repo.current_revision(conn, project_id, link["src_object_id"]))
        if state.get("status") == "resolved":
            continue
        qualifiers = json.loads(link["qualifiers_json"] or "{}")
        applies, evaluation = blocker_evaluation(qualifiers, context)
        if not applies:
            continue
        if evaluation == "unknown":
            blockers.append(
                {
                    "object_id": link["src_object_id"],
                    "revision": link["src_revision"],
                    "severity": state.get("severity", "medium"),
                    "effect": state.get("effect"),
                    "rule_status": "unknown_applicability",
                }
            )
            continue
        if state.get("severity") in ("high", "critical"):
            blockers.append(
                {
                    "object_id": link["src_object_id"],
                    "revision": link["src_revision"],
                    "severity": state.get("severity"),
                    "effect": state.get("effect"),
                    "rule_status": evaluation,
                }
            )
    return blockers


def inaccessible_support(
    conn: sqlite3.Connection, project_id: str, support_refs: list[dict[str, Any]]
) -> list[str]:
    inaccessible: list[str] = []
    for ref in support_refs:
        object_id = ref.get("object_id")
        if not object_id:
            continue
        row = repo.object_row_or_none(conn, project_id, object_id)
        if row is None:
            inaccessible.append(f"{object_id}:missing")
            continue
        if row["kind"] == "artifact":
            state = repo.parse_state(repo.current_revision(conn, project_id, object_id))
            if state.get("availability") != "available":
                inaccessible.append(f"{object_id}:{state.get('availability')}")
    return inaccessible


def _selection_input(conn: sqlite3.Connection, project_id: str, ref: dict[str, Any]) -> dict[str, Any]:
    object_id = ref.get("object_id") if isinstance(ref, dict) else ref
    item: dict[str, Any] = {"object_id": object_id, "revision": ref.get("revision") if isinstance(ref, dict) else None}
    attempt = conn.execute(
        """
        SELECT attempt_no, execution_id, slot_id FROM run_attempts
        WHERE project_id = ? AND run_object_id = ?
        ORDER BY attempt_no LIMIT 1
        """,
        (project_id, object_id),
    ).fetchone()
    if attempt is not None:
        item["attempt_no"] = attempt["attempt_no"]
        slot = conn.execute(
            "SELECT replicate_identity, trial_key FROM trial_slots WHERE slot_id = ?",
            (attempt["slot_id"],),
        ).fetchone()
        if slot is not None:
            item["replicate_identity"] = slot["replicate_identity"]
            item["trial_key"] = slot["trial_key"]
    return item


def _validity_map(conn: sqlite3.Connection, project_id: str, inputs: list[dict[str, Any]]) -> dict[str, str]:
    validity: dict[str, str] = {}
    for item in inputs:
        object_row = repo.object_row_or_none(conn, project_id, item["object_id"])
        if object_row is None:
            validity[item["object_id"]] = "missing"
            continue
        state = repo.parse_state(repo.current_revision(conn, project_id, item["object_id"]))
        if object_row["kind"] == "run":
            validity[item["object_id"]] = state.get("validity", "unknown")
        elif object_row["kind"] == "artifact":
            validity[item["object_id"]] = "valid" if state.get("availability") == "available" else "unknown"
        else:
            validity[item["object_id"]] = "valid"
    return validity


def _unreviewed_selection(state: dict[str, Any]) -> bool:
    manifest = state.get("selection_manifest")
    if not manifest:
        return False
    return not manifest.get("review_refs")


def assess_comparison(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    from research_kb.domain.selection import compare_two_revisions

    conn = mutation.ctx.conn
    if payload["assessment"] not in COMPARISON_ASSESSMENTS:
        raise schema_validation_failed("Unknown comparison assessment.", allowed=list(COMPARISON_ASSESSMENTS))
    run = repo.resolve_ref(conn, mutation.project_id, payload["run_ref"])
    target = repo.resolve_ref(conn, mutation.project_id, payload["target_ref"])
    if run["kind"] != "run":
        raise schema_validation_failed("Comparison must originate from a run record.", kind=run["kind"])
    left: dict[str, Any] = {}
    right: dict[str, Any] = {}
    matching: list[str] = []
    for dimension in payload["dimensions"]:
        name = dimension.get("name")
        if not name:
            raise schema_validation_failed("Each comparison dimension requires a name.", dimension=dimension)
        matching.append(name)
        left[name] = dimension.get("run_value")
        right[name] = dimension.get("target_value")
    computed = compare_two_revisions(
        left=left,
        right=right,
        matching_dimensions=matching,
        permitted_differences=payload.get("permitted_differences", []),
        rationale=payload["rationale"],
        checker=payload.get("checker") or mutation.actor_id,
    )
    if computed["assessment"] != payload["assessment"] and payload["assessment"] != "not_assessed":
        raise schema_validation_failed(
            "The declared comparison assessment conflicts with the recorded dimension values.",
            declared=payload["assessment"],
            computed=computed["assessment"],
            mismatched=computed["mismatched_dimensions"],
            unknown=computed["unknown_dimensions"],
        )
    document = {
        "assessment": payload["assessment"],
        "dimensions": payload["dimensions"],
        "matching_dimensions": matching,
        "mismatched_dimensions": computed["mismatched_dimensions"],
        "unknown_dimensions": computed["unknown_dimensions"],
        "permitted_differences": payload.get("permitted_differences", []),
        "rationale": payload["rationale"],
        "checker": payload.get("checker") or mutation.actor_id,
        "run_revision": run["revision"],
        "target_revision": target["revision"],
        "assessed_seq": mutation.commit_seq,
    }
    link = create_link(
        mutation,
        predicate="related_to",
        src_ref={"object_id": run["object_id"], "revision": run["revision"]},
        dst_ref={"object_id": target["object_id"], "revision": target["revision"]},
        pin_mode="pinned",
        qualifiers={"comparison_assessment": document},
        rationale=payload["rationale"],
        review_state="reviewed",
    )
    state = repo.parse_state(run)
    comparisons = dict(state.get("comparison_assessments") or {})
    comparisons[target["object_id"]] = comparison_assessment_document(document)
    updated = revise_object(
        mutation,
        object_id=run["object_id"],
        expected_revision=run["revision"],
        state_json={**state, "comparison_assessments": comparisons},
    )
    return {"comparison": document, "link_object_id": link["object_id"], "updated": updated}


def create_selection_manifest(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    conn = mutation.ctx.conn
    manifest = {
        "target_ref": payload["target_ref"],
        "claim_refs": payload.get("claim_refs", []),
        "input_refs": payload["input_refs"],
        "inclusion_rule": payload["inclusion_rule"],
        "selection_mode": payload.get("selection_mode") or "earliest_valid_attempt",
        "analysis_plan_ref": payload.get("analysis_plan_ref"),
        "exclusions": payload.get("exclusions", []),
        "exclusion_reasons": payload.get("exclusion_reasons", []),
        "code_identity": payload.get("code_identity"),
        "environment_identity": payload.get("environment_identity"),
        "generated_artifact_hashes": payload["generated_artifact_hashes"],
        "review_refs": payload.get("review_refs", []),
        "frozen_seq": mutation.commit_seq,
        "frozen_by": mutation.actor_id,
    }
    validate_selection_manifest(manifest)
    target = repo.resolve_ref(conn, mutation.project_id, payload["target_ref"], require_revision=True)
    if target["kind"] != "artifact":
        raise schema_validation_failed("Selection manifests attach to artifact targets.", kind=target["kind"])
    inputs = [_selection_input(conn, mutation.project_id, ref) for ref in payload["input_refs"]]
    for ref in payload["input_refs"]:
        repo.resolve_ref(conn, mutation.project_id, ref, require_revision=True)
    if manifest["selection_mode"] == "earliest_valid_attempt":
        choice = default_ordinary_selection(inputs, validity_by_object=_validity_map(conn, mutation.project_id, inputs))
        manifest["selected_input_ref"] = choice["selected"]
        manifest["eligible_input_refs"] = choice["eligible"]
        manifest["selection_reason"] = choice["reason"]
    elif manifest["selection_mode"] == "aggregate":
        replicates = independent_replicates(inputs)
        if len(replicates) < 2:
            raise schema_validation_failed(
                "Aggregate selection requires at least two declared independent replicates.",
                found=len(replicates),
            )
        manifest["replicates"] = replicates
    elif manifest["selection_mode"] == "best_metric" and not payload.get("analysis_plan_ref"):
        raise schema_validation_failed(
            "Selecting evidence by best metric requires an explicit analysis plan."
        )
    state = repo.parse_state(target)
    existing = state.get("selection_manifest")
    if existing:
        raise schema_validation_failed(
            "The target already has a frozen selection manifest; create a new target revision or amend explicitly."
        )
    state["selection_manifest"] = manifest
    updated = revise_object(
        mutation,
        object_id=target["object_id"],
        expected_revision=target["revision"],
        state_json=state,
    )
    for ref in payload.get("claim_refs", []):
        create_link(
            mutation,
            predicate="included_in",
            src_ref=ref,
            dst_ref={"object_id": target["object_id"], "revision": updated["revision"]},
            rationale="Frozen selection manifest.",
            review_state="reviewed",
        )
    return {"selection_manifest": manifest, "updated": updated}


def resolve_issue(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    conn = mutation.ctx.conn
    issue = repo.resolve_ref(conn, mutation.project_id, payload["ref"], require_revision=True)
    if issue["kind"] != "knowledge" or issue["subkind"] not in ("issue", "caveat"):
        raise schema_validation_failed("resolve_issue requires an issue or caveat record.")
    resolution = repo.resolve_ref(conn, mutation.project_id, payload["resolution_ref"], require_revision=True)
    state = repo.parse_state(issue)
    criterion = state.get("resolution_criterion")
    if not criterion:
        raise schema_validation_failed("The issue has no recorded resolution criterion.")
    state["status"] = "resolved"
    state["resolved_by_ref"] = payload["resolution_ref"]
    state["resolution_rationale"] = payload["rationale"]
    state["criterion_met"] = payload["criterion_met"]
    link = create_link(
        mutation,
        predicate="resolves",
        src_ref={"object_id": resolution["object_id"], "revision": resolution["revision"]},
        dst_ref={"object_id": issue["object_id"], "revision": issue["revision"]},
        qualifiers={"criterion": criterion, "criterion_met": payload["criterion_met"]},
        rationale=payload["rationale"],
        review_state="reviewed",
    )
    updated = revise_object(
        mutation,
        object_id=issue["object_id"],
        expected_revision=issue["revision"],
        state_json=state,
    )
    return {"resolved": updated, "resolution_link_id": link["object_id"]}


def set_study_state(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    from research_kb.domain.vocab import STUDY_TRANSITIONS

    conn = mutation.ctx.conn
    study = repo.resolve_ref(conn, mutation.project_id, payload["ref"])
    if study["kind"] != "study":
        raise schema_validation_failed("set_study_state requires a study record.", kind=study["kind"])
    state = repo.parse_state(study)
    from_state = state.get("study_state", "planned")
    to_state = payload["to_state"]
    allowed = STUDY_TRANSITIONS.get(from_state, ())
    if to_state not in allowed:
        raise schema_validation_failed(
            "This study transition is not permitted.",
            from_state=from_state,
            to_state=to_state,
            allowed=list(allowed),
        )
    if to_state == "concluded":
        evidence_refs = payload.get("evidence_refs") or []
        if not evidence_refs and not payload.get("conclusion"):
            raise schema_validation_failed(
                "Concluding a study requires evidence or a recorded conclusion.",
                hint="A study is not concluded automatically because its last process exited.",
            )
        for ref in evidence_refs:
            repo.resolve_ref(conn, mutation.project_id, ref, require_revision=True)
    state["study_state"] = to_state
    if payload.get("conclusion") is not None:
        state["conclusion"] = payload["conclusion"]
    if payload.get("reason"):
        state["state_reason"] = payload["reason"]
    updated = revise_object(
        mutation,
        object_id=study["object_id"],
        expected_revision=study["revision"],
        state_json=state,
    )
    return {"updated": updated}


def supersede(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    conn = mutation.ctx.conn
    replaced = repo.resolve_ref(conn, mutation.project_id, payload["ref"], require_revision=True)
    replacement = repo.resolve_ref(conn, mutation.project_id, payload["superseded_by_ref"], require_revision=True)
    if replaced["object_id"] == replacement["object_id"]:
        raise schema_validation_failed("A record cannot supersede itself.")
    old_aliases = repo.aliases_for(conn, mutation.project_id, replaced["object_id"])
    link = create_link(
        mutation,
        predicate="supersedes",
        src_ref={"object_id": replacement["object_id"], "revision": replacement["revision"]},
        dst_ref={"object_id": replaced["object_id"], "revision": replaced["revision"]},
        qualifiers={"reason": payload["reason"], "scope": payload.get("scope")},
        rationale=payload["reason"],
        review_state="reviewed",
    )
    for alias in old_aliases:
        repo.add_alias(
            conn,
            project_id=mutation.project_id,
            object_id=replacement["object_id"],
            alias_text=alias,
            namespace="redirect",
            seq=mutation.commit_seq,
        )
    conn.execute(
        """
        UPDATE aliases SET retired_seq = ?
        WHERE project_id = ? AND object_id = ? AND retired_seq IS NULL
        """,
        (mutation.commit_seq, mutation.project_id, replaced["object_id"]),
    )
    updated = revise_object(
        mutation,
        object_id=replaced["object_id"],
        expected_revision=replaced["revision"],
        record_state="retired",
        state_json=repo.parse_state(replaced),
    )
    return {
        "superseded": updated,
        "supersedes_link_id": link["object_id"],
        "aliases_redirected": old_aliases,
    }


def tombstone(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    conn = mutation.ctx.conn
    target = repo.resolve_ref(conn, mutation.project_id, payload["ref"], require_revision=True)
    state = repo.parse_state(target)
    state["tombstone_reason"] = payload["reason"]
    state["tombstone_note"] = "History and dependents are retained; dependents require review."
    updated = revise_object(
        mutation,
        object_id=target["object_id"],
        expected_revision=target["revision"],
        record_state="tombstoned",
        state_json=state,
    )
    conn.execute(
        """
        INSERT INTO review_flags
          (project_id, target_project_id, target_object_id, target_revision, cause_project_id,
           cause_object_id, cause_revision, predicate, reason, created_seq, created_at)
        SELECT ?, ?, l.src_object_id, o.current_revision, ?, ?, ?, l.predicate,
               'A depended-upon record was tombstoned; review the dependent record.', ?, ?
        FROM link_revisions l
        JOIN objects o ON o.project_id = l.project_id AND o.object_id = l.src_object_id
        WHERE l.project_id = ? AND l.dst_object_id = ? AND l.predicate IN ('derived_from', 'assumes', 'uses', 'included_in')
        """,
        (
            mutation.project_id,
            mutation.project_id,
            mutation.project_id,
            target["object_id"],
            target["revision"],
            mutation.commit_seq,
            mutation.recorded_at,
            mutation.project_id,
            target["object_id"],
        ),
    )
    return {"tombstoned": updated}