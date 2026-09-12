from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from research_kb.domain.canonical import canonical_hash
from research_kb.domain.transition_registry import validate_operation_payload
from research_kb.domain.vocab import PROPOSAL_STATES
from research_kb.errors import (
    approval_required,
    approval_stale,
    not_found,
    schema_validation_failed,
)
from research_kb.service import approvals as approval_service
from research_kb.service.context import ServiceContext
from research_kb.service.idempotency import (
    record as idempotency_record,
    replay_or_conflict,
    request_payload_hash,
)
from research_kb.domain.schemas import iter_refs
from research_kb.service.policy import (
    auto_apply_allowed,
    check_operation_capabilities,
    proposal_requires_approval,
    required_capabilities,
)
from research_kb.storage import repo
from research_kb.storage.db import new_id, utc_now, write_tx

PROPOSAL_TTL_SECONDS = 7 * 24 * 3600


def proposal_hash(operations: list[dict[str, Any]], reason: str | None) -> str:
    return canonical_hash({"operations": operations, "reason": reason or ""})


def bound_versions_for(operations: list[dict[str, Any]]) -> dict[str, Any]:
    bound: dict[str, Any] = {}
    for operation in operations:
        for path, ref in iter_refs(operation.get("payload", {})):
            object_id = ref.get("object_id")
            revision = ref.get("revision")
            if object_id and revision is not None:
                bound[f"{object_id}:{revision}"] = revision
        payload = operation.get("payload", {})
        target = payload.get("ref")
        if isinstance(target, dict) and target.get("object_id") and target.get("revision") is not None:
            bound[f"{target['object_id']}:{target['revision']}"] = target["revision"]
    return bound


def _collect_revision_dependencies(engagement: ServiceContext, operations: list[dict[str, Any]]) -> dict[str, int]:
    conn = engagement.conn
    pinned: dict[str, int] = {}
    for operation in operations:
        for _, ref in iter_refs(operation.get("payload", {})):
            object_id = ref.get("object_id")
            revision = ref.get("revision")
            if object_id and revision:
                repo.resolve_ref(
                    conn,
                    engagement.project_id,
                    {"object_id": object_id, "revision": revision},
                    require_revision=True,
                )
                pinned[object_id] = int(revision)
    return pinned


def propose(
    ctx: ServiceContext,
    *,
    operations: list[dict[str, Any]],
    reason: str | None,
    request_id: str,
    persist: bool,
    auto_apply: bool = False,
) -> dict[str, Any]:
    if not operations:
        raise schema_validation_failed("A proposal must contain at least one operation.")
    payload_hash = request_payload_hash("propose", {"operations": operations, "reason": reason})
    for operation in operations:
        validate_operation_payload(operation["op"], operation.get("payload", {}))
    check_operation_capabilities(operations, ctx.capabilities(), actor_id=ctx.actor_id)
    stored = replay_or_conflict(
        ctx.conn,
        project_id=ctx.project_id,
        actor_id=ctx.actor_id,
        request_id=request_id,
        operation="propose",
        payload_hash=payload_hash,
    )
    if stored is not None:
        return stored
    _collect_revision_dependencies(ctx, operations)
    bound = bound_versions_for(operations)
    digest = proposal_hash(operations, reason)
    needs_approval = proposal_requires_approval(operations, ctx.policy)
    required = required_capabilities(operations)
    response: dict[str, Any] = {
        "schema_version": "1.0",
        "project_id": ctx.project_id,
        "proposal_hash": digest,
        "operations": operations,
        "required_capabilities": required,
        "requires_approval": needs_approval,
        "bound_versions": bound,
        "policy_revision": ctx.policy_revision,
        "persisted": False,
        "applied": False,
        "commit_seq": None,
        "created": [],
        "updated": [],
    }
    if not persist:
        from research_kb.service.operations import apply_operations

        validation = apply_operations(
            ctx,
            operations=operations,
            request_id=request_id,
            reason=reason,
            action="proposal_validate",
            dry_run=True,
        )
        response.update(
            {
                "status": "validated_dry_run",
                "created": validation.created,
                "updated": validation.updated,
                "details": validation.details,
            }
        )
        return response
    from research_kb.service.operations import apply_operations

    if auto_apply and auto_apply_allowed(operations, ctx.policy):
        response.update({"status": "applied", "persisted": True, "applied": True})

        def _record_applied(conn, seq, batch) -> None:
            response.update(
                {
                    "commit_seq": seq,
                    "created": batch.created,
                    "updated": batch.updated,
                    "details": batch.details,
                }
            )
            idempotency_record(
                conn,
                project_id=ctx.project_id,
                actor_id=ctx.actor_id,
                request_id=request_id,
                operation="propose",
                payload_hash=payload_hash,
                result=response,
                commit_seq=seq,
            )

        batch = apply_operations(
            ctx,
            operations=operations,
            request_id=request_id,
            reason=reason,
            action="proposal_apply",
            record_idempotent=False,
            on_commit=_record_applied,
        )
        response.update(
            {
                "commit_seq": batch.commit_seq,
                "created": batch.created,
                "updated": batch.updated,
                "details": batch.details,
            }
        )
        return response
    ctx.require_actor()
    with write_tx(ctx.conn):
        seq = repo.append_commit_event(
            ctx.conn,
            project_id=ctx.project_id,
            actor_id=ctx.actor_id,
            action="proposal_store",
            epoch=ctx.epoch,
            reason=reason,
            request_id=request_id,
            policy_revision=ctx.policy_revision,
            changed=[{"proposal_hash": digest}],
        )
        proposal_id = new_id()
        expires_at = (datetime.now(UTC) + timedelta(seconds=PROPOSAL_TTL_SECONDS)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        ctx.conn.execute(
            """
            INSERT INTO proposals
              (proposal_id, project_id, proposal_hash, operations_json, bound_versions_json,
               policy_revision, reason, status, required_capabilities_json, requires_approval,
               created_seq, created_at, expires_at, actor_id, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'stored', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                ctx.project_id,
                digest,
                json.dumps(operations, sort_keys=True),
                json.dumps(bound, sort_keys=True),
                ctx.policy_revision,
                reason,
                json.dumps(required),
                1 if needs_approval else 0,
                seq,
                utc_now(),
                expires_at,
                ctx.actor_id,
                request_id,
            ),
        )
        response.update(
            {
                "status": "stored",
                "persisted": True,
                "proposal_id": proposal_id,
                "commit_seq": seq,
                "expires_at": expires_at,
            }
        )
        idempotency_record(
            ctx.conn,
            project_id=ctx.project_id,
            actor_id=ctx.actor_id,
            request_id=request_id,
            operation="propose",
            payload_hash=payload_hash,
            result=response,
            commit_seq=seq,
        )
    return response


def fetch_proposal(ctx: ServiceContext, proposal_id: str | None, proposal_hash: str | None) -> dict[str, Any]:
    conn = ctx.conn
    if proposal_id:
        row = conn.execute(
            "SELECT * FROM proposals WHERE project_id = ? AND proposal_id = ?",
            (ctx.project_id, proposal_id),
        ).fetchone()
    elif proposal_hash:
        row = conn.execute(
            "SELECT * FROM proposals WHERE project_id = ? AND proposal_hash = ? ORDER BY created_seq DESC LIMIT 1",
            (ctx.project_id, proposal_hash),
        ).fetchone()
    else:
        raise schema_validation_failed("A proposal ID or hash is required.")
    if row is None:
        raise not_found("The proposal does not resolve.", proposal_id=proposal_id)
    proposal = dict(row)
    proposal["operations"] = json.loads(proposal.pop("operations_json"))
    proposal["bound_versions"] = json.loads(proposal.pop("bound_versions_json") or "{}")
    proposal["required_capabilities"] = json.loads(proposal.pop("required_capabilities_json") or "[]")
    proposal["requires_approval"] = bool(proposal["requires_approval"])
    return proposal


def apply(
    ctx: ServiceContext,
    *,
    proposal_id: str | None,
    proposal_hash: str | None,
    request_id: str,
    approval_id: str | None = None,
    approval_token: str | None = None,
) -> dict[str, Any]:
    proposal = fetch_proposal(ctx, proposal_id, proposal_hash)
    if proposal["status"] != "stored":
        raise schema_validation_failed(
            "Only stored proposals can be applied.", status=proposal["status"]
        )
    if proposal.get("expires_at") and proposal["expires_at"] < utc_now():
        ctx.conn.execute(
            "UPDATE proposals SET status = 'expired' WHERE project_id = ? AND proposal_id = ?",
            (ctx.project_id, proposal["proposal_id"]),
        )
        raise approval_stale("The proposal has expired.", proposal_id=proposal["proposal_id"])
    operations = proposal["operations"]
    check_operation_capabilities(operations, ctx.capabilities(), actor_id=ctx.actor_id)
    for bound_key, bound_revision in proposal["bound_versions"].items():
        object_id = bound_key.rsplit(":", 1)[0]
        row = repo.object_row_or_none(ctx.conn, ctx.project_id, object_id)
        if row is None or int(row["current_revision"]) != int(bound_revision):
            raise approval_stale(
                "A bound record changed after the proposal was stored.",
                object_id=object_id,
                bound_revision=bound_revision,
                current_revision=row["current_revision"] if row else None,
            )
    approval = None
    if proposal["requires_approval"]:
        if not approval_id and not approval_token:
            raise approval_required(proposal["proposal_hash"], proposal["required_capabilities"])
        approval = approval_service.fetch_approval(
            ctx.conn, ctx.project_id, approval_id=approval_id, token=approval_token
        )
        from research_kb.service.policy import assert_approval_current

        assert_approval_current(
            approval,
            proposal_hash=proposal["proposal_hash"],
            expected_versions=proposal["bound_versions"],
            policy_revision=ctx.policy_revision,
            epoch=ctx.epoch,
        )
    payload_hash = request_payload_hash(
        "apply",
        {
            "proposal_hash": proposal["proposal_hash"],
            "approval_id": approval["approval_id"] if approval else None,
        },
    )
    stored = replay_or_conflict(
        ctx.conn,
        project_id=ctx.project_id,
        actor_id=ctx.actor_id,
        request_id=request_id,
        operation="apply",
        payload_hash=payload_hash,
    )
    if stored is not None:
        return stored
    from research_kb.service.operations import apply_operations

    def _finalize(conn: sqlite3.Connection, seq: int, batch: Any) -> None:
        conn.execute(
            "UPDATE proposals SET status = 'applied', applied_seq = ? WHERE project_id = ? AND proposal_id = ?",
            (seq, ctx.project_id, proposal["proposal_id"]),
        )
        if approval:
            approval_service.consume_approval(conn, approval["approval_id"], seq)

    batch = apply_operations(
        ctx,
        operations=operations,
        request_id=request_id,
        reason=proposal.get("reason"),
        action="proposal_apply",
        record_idempotent=True,
        idempotency_operation="apply",
        idempotency_payload_hash=payload_hash,
        on_commit=_finalize,
    )
    from research_kb.service.backup import maybe_backup_after_critical

    readback = []
    for item in [*batch.created, *batch.updated]:
        row = repo.get_revision(ctx.conn, ctx.project_id, item["object_id"], item["revision"])
        readback.append(
            {
                "object_id": item["object_id"],
                "revision": item["revision"],
                "record_state": row["record_state"],
                "content_hash": row["content_hash"],
            }
        )
    verified = len(readback) == len(batch.created) + len(batch.updated)
    backup = maybe_backup_after_critical(ctx, operations)
    return {
        "schema_version": "1.0",
        "project_id": ctx.project_id,
        "proposal_id": proposal["proposal_id"],
        "proposal_hash": proposal["proposal_hash"],
        "status": "applied",
        "committed": True,
        "commit_seq": batch.commit_seq,
        "created": batch.created,
        "updated": batch.updated,
        "details": batch.details,
        "approval_id": approval["approval_id"] if approval else None,
        "verified": verified,
        "readback": readback,
        "critical_backup": backup["backup_dir"] if backup else None,
    }


def list_proposals(ctx: ServiceContext, *, status: str | None = None) -> list[dict[str, Any]]:
    clauses = ["project_id = ?"]
    params: list[Any] = [ctx.project_id]
    if status:
        if status not in PROPOSAL_STATES:
            raise schema_validation_failed("Unknown proposal status.", allowed=list(PROPOSAL_STATES))
        clauses.append("status = ?")
        params.append(status)
    rows = ctx.conn.execute(
        f"SELECT proposal_id, proposal_hash, status, reason, created_at, expires_at, requires_approval, policy_revision "
        f"FROM proposals WHERE {' AND '.join(clauses)} ORDER BY created_seq",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def approve(
    ctx: ServiceContext,
    *,
    proposal_id: str,
    ttl_seconds: int = approval_service.DEFAULT_APPROVAL_TTL_SECONDS,
) -> dict[str, Any]:
    proposal = fetch_proposal(ctx, proposal_id, None)
    required = proposal["required_capabilities"]
    capability = required[0] if required else "resolve_critical"
    ctx.require_actor()
    with write_tx(ctx.conn):
        result = approval_service.issue_approval(
            ctx.conn,
            project_id=ctx.project_id,
            actor_id=ctx.actor_id,
            epoch=ctx.epoch,
            proposal_hash=proposal["proposal_hash"],
            expected_versions=proposal["bound_versions"],
            policy_revision=ctx.policy_revision,
            capability=capability,
            ttl_seconds=ttl_seconds,
        )
        result["proposal_id"] = proposal_id
        return result
