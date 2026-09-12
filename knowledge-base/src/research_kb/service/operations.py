from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from research_kb.domain.schemas import validate_value
from research_kb.domain.transition_registry import validate_operation_payload
from research_kb.errors import schema_validation_failed
from research_kb.service import assessment, sources, work
from research_kb.service.context import ServiceContext
from research_kb.service.idempotency import record as record_idempotency
from research_kb.service.objects import (
    Mutation,
    acknowledge_flag,
    create_object,
    revise_object,
)
from research_kb.storage import blobs
from research_kb.storage.db import utc_now, write_tx
from research_kb.storage.repo import append_commit_event


@dataclass
class BatchResult:
    created: list[dict[str, Any]] = field(default_factory=list)
    updated: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    commit_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "details": self.details,
            "commit_seq": self.commit_seq,
        }


def _handle_capture(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    record_state = payload.get("record_state", "draft")
    unresolved = payload.get("unresolved_references") or []
    if unresolved and record_state != "draft":
        raise schema_validation_failed(
            "Unresolved references may only be held in a draft record.",
            hint="Do not invent target IDs to promote a record.",
        )
    citations = list(payload.get("citations") or [])
    for anchor_id in payload.get("source_anchor_ids") or []:
        citations.append({"anchor_id": anchor_id, "role": "evidence"})
    result = create_object(
        mutation,
        kind=payload["kind"],
        subkind=payload["subkind"],
        title=payload["title"],
        state_json=payload["state_json"],
        body_md=payload.get("body_md", ""),
        record_state=record_state,
        attribution=payload.get("attribution"),
        aliases=payload.get("aliases"),
        citations=citations,
        links=payload.get("links"),
        occurred_at=payload.get("occurred_at"),
        occurred_at_unknown=bool(payload.get("occurred_at_unknown", payload.get("occurred_at") is None)),
        effective_from=payload.get("effective_from"),
        effective_to=payload.get("effective_to"),
    )
    if unresolved:
        result["unresolved_references"] = unresolved
        result["pending_reference_state"] = "unresolved_in_draft"
    return result


def _handle_revise(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    ref = payload["ref"]
    expected = payload.get("expected_revision")
    if expected is None:
        expected = ref.get("revision")
    if expected is None:
        raise schema_validation_failed("A revise operation requires an expected revision.")
    result = revise_object(
        mutation,
        object_id=ref["object_id"],
        expected_revision=int(expected),
        title=payload.get("title"),
        body_md=payload.get("body_md"),
        state_json=payload.get("state_json"),
        record_state=payload.get("record_state"),
        citations=payload.get("citations"),
        reaffirm_citation_ids=payload.get("reaffirm_citation_ids"),
        source_object_id=None,
    )
    if result["kind"] == "source":
        sources.reproject_source(mutation.ctx, ref["object_id"], result["revision"])
    return result


def _create_link(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    from research_kb.service.objects import create_link

    return create_link(
        mutation,
        predicate=payload["predicate"],
        src_ref=payload["src_ref"],
        dst_ref=payload["dst_ref"],
        pin_mode=payload.get("pin_mode"),
        qualifiers=payload.get("qualifiers"),
        rationale=payload.get("rationale"),
        review_state=payload.get("review_state"),
        applicability=payload.get("applicability"),
    )


def _handle_set_work_state(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    ref = payload["ref"]
    expected = payload.get("expected_revision", ref.get("revision"))
    if expected is None:
        raise schema_validation_failed("set_work_state requires an expected revision.")
    return work.set_work_state(
        mutation,
        object_id=ref["object_id"],
        expected_revision=int(expected),
        to_state=payload["to_state"],
        reason=payload.get("reason"),
        evidence_refs=payload.get("evidence_refs"),
        completion_report=payload.get("completion_report"),
        blocked_reason=payload.get("blocked_reason"),
    )


def _handle_register_artifact(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    ctx = mutation.ctx
    state = {
        key: value
        for key, value in payload.items()
        if key not in ("local_path", "role_note")
    }
    local_path = payload.get("local_path")
    if local_path:
        from research_kb.service.sources import resolve_import_path

        path = resolve_import_path(ctx, local_path)
        if mutation.dry_run:
            if not path.is_file():
                raise schema_validation_failed(f"Artifact file is not available: {path}")
            state["content_identity"] = state.get("content_identity") or {"kind": "sha256", "value": "dry-run"}
            state["assurance"] = state.get("assurance", "content_sha256")
            state["byte_size"] = path.stat().st_size
            state["availability"] = "available"
        else:
            digest, size, stored_path = blobs.stage_and_store(ctx.routing.sources_dir.parent, path)
            state["content_identity"] = {"kind": "sha256", "value": digest}
            state["assurance"] = "content_sha256"
            state["byte_size"] = size
            state["availability"] = "available"
            state["locations"] = list(state.get("locations") or []) + [
                {"kind": "local_blob", "location": str(stored_path), "availability": "available"}
            ]
            sources._register_blob(
                ctx,
                digest=digest,
                byte_size=size,
                media_type=state.get("media_type"),
                assurance="content_sha256",
                location=str(stored_path),
                location_kind="local_blob",
                seq=mutation.commit_seq,
                role="artifact",
            )
    if state.get("results") is not None:
        from research_kb.domain.schemas import SUPPLEMENTAL_SCHEMAS

        validate_value(SUPPLEMENTAL_SCHEMAS["results"], state["results"], "$.results")
    result = create_object(
        mutation,
        kind="artifact",
        subkind=payload["subkind"],
        title=payload.get("role_note") or payload["role"],
        state_json=state,
        record_state="active",
    )
    return result


def _handle_create_handoff(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    state = {
        "subkind": "session_handoff",
        "summary": payload["summary"],
        "completed_refs": payload.get("completed_refs", []),
        "open_refs": payload.get("open_refs", []),
        "pending_proposals": payload.get("pending_proposals", []),
        "in_flight_executions": payload.get("in_flight_executions", []),
        "next_step": payload["next_step"],
        "snapshot_cursor": payload["snapshot_cursor"],
        "unresolved_issues": payload.get("unresolved_issues", []),
    }
    for ref in state["completed_refs"] + state["open_refs"]:
        from research_kb.storage import repo

        repo.resolve_ref(mutation.ctx.conn, mutation.project_id, ref)
    return create_object(
        mutation,
        kind="handoff",
        subkind="session_handoff",
        title=f"Handoff {mutation.recorded_at}",
        state_json=state,
        record_state="active",
        attribution={
            "session_id": payload.get("session_id"),
            "provenance_category": "agent_inference",
        },
    )


def _handle_policy_change(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    import json as _json

    from research_kb.config import policy_hash, save_policy
    from research_kb.storage.db import utc_now as _now

    profile = payload["profile"]
    for required in ("capture", "retrieval", "review", "provenance", "execution", "preservation", "limits"):
        if required not in profile:
            raise schema_validation_failed(f"Policy profile is missing section '{required}'.")
    unknown = set(profile) - {
        "policy_name",
        "capture",
        "retrieval",
        "review",
        "provenance",
        "execution",
        "preservation",
        "limits",
    }
    if unknown:
        raise schema_validation_failed("Unknown policy sections.", unknown=sorted(unknown))
    revision = policy_hash(profile)
    existing = mutation.ctx.conn.execute(
        "SELECT policy_revision FROM policy_revisions WHERE policy_revision = ?", (revision,)
    ).fetchone()
    if existing is not None:
        save_policy(mutation.ctx.state_dir, profile)
        return {"policy_revision": revision, "already_accepted": True}
    mutation.ctx.conn.execute(
        """
        INSERT INTO policy_revisions (policy_revision, project_id, profile_json, accepted_seq, accepted_by, accepted_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision,
            mutation.project_id,
            _json.dumps(profile, sort_keys=True),
            mutation.commit_seq,
            mutation.actor_id,
            _now(),
            payload.get("reason"),
        ),
    )
    save_policy(mutation.ctx.state_dir, profile)
    return {"policy_revision": revision}


def _handle_execution(mutation: Mutation, payload: dict[str, Any], operation: str) -> dict[str, Any]:
    from research_kb.execution import orchestrator

    return orchestrator.handle_operation(mutation, operation, payload)


HANDLERS: dict[str, Callable[[Mutation, dict[str, Any]], dict[str, Any]]] = {
    "capture": _handle_capture,
    "revise": _handle_revise,
    "link": _create_link,
    "assess_evidence": assessment.assess_evidence,
    "set_work_state": _handle_set_work_state,
    "resolve_issue": assessment.resolve_issue,
    "acknowledge_flag": lambda mutation, payload: acknowledge_flag(
        mutation,
        flag_id=int(payload["flag_id"]),
        rationale=payload["rationale"],
        review_ref=payload.get("review_ref"),
    ),
    "create_handoff": _handle_create_handoff,
    "register_source": sources.register_source,
    "register_artifact": _handle_register_artifact,
    "assess_comparison": assessment.assess_comparison,
    "create_selection_manifest": assessment.create_selection_manifest,
    "set_study_state": assessment.set_study_state,
    "retire": assessment.supersede,
    "tombstone": assessment.tombstone,
    "claim_work": lambda mutation, payload: work.claim_work(
        mutation,
        object_id=payload["ref"]["object_id"],
        expected_revision=int(payload.get("expected_revision") or payload["ref"].get("revision") or 0),
        ttl_seconds=int(payload.get("ttl_seconds", 3600)),
    ),
    "release_work": lambda mutation, payload: work.release_work(
        mutation,
        lease_id=payload["lease_id"],
        reason=payload.get("reason"),
    ),
    "execute_prepare": lambda mutation, payload: _handle_execution(mutation, payload, "execute_prepare"),
    "execute_launch": lambda mutation, payload: _handle_execution(mutation, payload, "execute_launch"),
    "execute_cancel": lambda mutation, payload: _handle_execution(mutation, payload, "execute_cancel"),
    "execute_reconcile": lambda mutation, payload: _handle_execution(mutation, payload, "execute_reconcile"),
    "policy_change": _handle_policy_change,
}


class _DryRunRollback(Exception):
    def __init__(self, result: "BatchResult") -> None:
        self.result = result


def _outcome_ref(outcome: dict[str, Any]) -> dict[str, Any] | None:
    preferred = ("updated", "resolved", "tombstoned", "superseded", "created")
    for key in preferred:
        value = outcome.get(key)
        if isinstance(value, dict) and "object_id" in value and "revision" in value:
            return value
    if "object_id" in outcome and "revision" in outcome:
        return outcome
    for value in outcome.values():
        if isinstance(value, dict) and "object_id" in value and "revision" in value:
            return value
    return None


def apply_operations(
    ctx: ServiceContext,
    *,
    operations: list[dict[str, Any]],
    request_id: str | None = None,
    reason: str | None = None,
    action: str = "proposal_apply",
    record_idempotent: bool = False,
    idempotency_operation: str = "",
    idempotency_payload_hash: str = "",
    on_commit: Callable[[sqlite3.Connection, int, "BatchResult"], None] | None = None,
    dry_run: bool = False,
) -> BatchResult:
    if not operations:
        raise schema_validation_failed("A batch must contain at least one operation.")
    ctx.require_actor()
    result = BatchResult()
    changed: list[dict[str, Any]] = []
    try:
        with write_tx(ctx.conn):
            seq = append_commit_event(
                ctx.conn,
                project_id=ctx.project_id,
                actor_id=ctx.actor_id,
                action="pending",
                epoch=ctx.epoch,
                reason=reason,
                request_id=request_id,
                policy_revision=ctx.policy_revision,
                changed=[],
            )
            mutation = Mutation(
                ctx=ctx,
                commit_seq=seq,
                recorded_at=utc_now(),
                request_id=request_id,
                reason=reason,
                dry_run=dry_run,
            )
            for operation in operations:
                name = operation["op"]
                payload = operation["payload"]
                validate_operation_payload(name, payload)
                handler = HANDLERS.get(name)
                if handler is None:
                    raise schema_validation_failed(f"No handler is registered for operation '{name}'.")
                outcome = handler(mutation, payload)
                ref = _outcome_ref(outcome)
                if ref is not None:
                    if ref.get("previous_revision") is not None:
                        result.updated.append(
                            {
                                "object_id": ref["object_id"],
                                "revision": ref["revision"],
                                "previous_revision": ref.get("previous_revision"),
                            }
                        )
                    else:
                        result.created.append(
                            {
                                "object_id": ref["object_id"],
                                "revision": ref["revision"],
                                "kind": ref.get("kind"),
                                "subkind": ref.get("subkind"),
                            }
                        )
                    changed.append(
                        {
                            "object_id": ref["object_id"],
                            "revision": ref["revision"],
                        }
                    )
                result.details.setdefault(name, []).append(outcome)
            result.commit_seq = seq
            ctx.conn.execute(
                "UPDATE commit_events SET action = ?, changed_json = ? WHERE seq = ?",
                (action, json.dumps(changed, sort_keys=True), seq),
            )
            if dry_run:
                raise _DryRunRollback(result)
            if on_commit is not None:
                on_commit(ctx.conn, seq, result)
            if record_idempotent and request_id:
                record_idempotency(
                    ctx.conn,
                    project_id=ctx.project_id,
                    actor_id=ctx.actor_id,
                    request_id=request_id,
                    operation=idempotency_operation or action,
                    payload_hash=idempotency_payload_hash,
                    result=result.to_dict(),
                    commit_seq=seq,
                )
    except _DryRunRollback as rollback:
        return rollback.result
    return result
