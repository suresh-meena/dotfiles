from __future__ import annotations

import json
import sqlite3
from typing import Any

from research_kb.domain.vocab import WORK_STATES, WORK_TRANSITIONS
from research_kb.errors import blocked, not_found, schema_validation_failed
from research_kb.service.context import ServiceContext
from research_kb.service.objects import Mutation, revise_object
from research_kb.storage import repo
from research_kb.storage.db import utc_now


def _work_row(conn: sqlite3.Connection, project_id: str, object_id: str) -> sqlite3.Row:
    row = repo.object_row_or_none(conn, project_id, object_id)
    if row is None or row["kind"] != "work":
        raise not_found("Work record does not resolve.", object_id=object_id)
    return row


def set_work_state(
    mutation: Mutation,
    *,
    object_id: str,
    expected_revision: int,
    to_state: str,
    reason: str | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    completion_report: dict[str, Any] | str | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    conn = mutation.ctx.conn
    if to_state not in WORK_STATES:
        raise schema_validation_failed("Unknown work state.", allowed=list(WORK_STATES))
    row = _work_row(conn, mutation.project_id, object_id)
    if int(row["current_revision"]) != int(expected_revision):
        from research_kb.errors import revision_conflict

        raise revision_conflict(
            int(expected_revision),
            int(row["current_revision"]),
            repo.revision_diff(
                conn,
                mutation.project_id,
                object_id,
                min(int(expected_revision), int(row["current_revision"])),
                int(row["current_revision"]),
            ),
        )
    revision_row = repo.current_revision(conn, mutation.project_id, object_id)
    state = repo.parse_state(revision_row)
    from_state = state.get("work_state", "open")
    allowed = WORK_TRANSITIONS.get(from_state, ())
    if to_state not in allowed:
        raise schema_validation_failed(
            "This work-state transition is not permitted.",
            from_state=from_state,
            to_state=to_state,
            allowed=list(allowed),
        )
    if to_state == "done":
        criteria = state.get("completion_criteria") or []
        if not criteria:
            raise blocked("Completion requires explicit acceptance criteria first.", object_id=object_id)
        evidence = evidence_refs or state.get("completion_refs") or []
        report = completion_report if completion_report is not None else state.get("completion_report")
        if not evidence and not report:
            raise blocked(
                "Completion requires evidence or an authoritative completion report.",
                criteria=criteria,
                hint="'Code was written' or 'a process exited' does not close a research task.",
            )
        for ref in evidence:
            repo.resolve_ref(conn, mutation.project_id, ref)
    new_state = dict(state)
    new_state["work_state"] = to_state
    if evidence_refs is not None:
        new_state["completion_refs"] = evidence_refs
    if completion_report is not None:
        new_state["completion_report"] = completion_report
    if to_state == "done" and new_state.get("review_state") is None:
        new_state["review_state"] = "unreviewed"
    if blocked_reason is not None:
        new_state["blocked_reason"] = blocked_reason
    return revise_object(
        mutation,
        object_id=object_id,
        expected_revision=expected_revision,
        state_json=new_state,
    )


def dependency_condition(conn: sqlite3.Connection, project_id: str, link_row: sqlite3.Row) -> str:
    from research_kb.domain.vocab import DEPENDENCY_CONDITIONS

    qualifiers = json.loads(link_row["qualifiers_json"] or "{}")
    condition = qualifiers.get("condition", "exists")
    if condition not in DEPENDENCY_CONDITIONS:
        from research_kb.errors import schema_validation_failed

        raise schema_validation_failed(
            "Unknown dependency condition.",
            condition=condition,
            allowed=list(DEPENDENCY_CONDITIONS),
        )
    return condition


def evaluate_condition(
    conn: sqlite3.Connection,
    project_id: str,
    prerequisite_object_id: str,
    condition: str,
    *,
    as_of_seq: int | None = None,
    criterion_ref: dict[str, Any] | None = None,
    revision_row: sqlite3.Row | None = None,
) -> tuple[bool, str]:
    if revision_row is None:
        row = repo.object_row_or_none(conn, project_id, prerequisite_object_id)
        if row is None:
            return False, "prerequisite_missing"
        revision_row = (
            repo.visible_revision(conn, project_id, prerequisite_object_id, as_of_seq)
            if as_of_seq is not None
            else repo.current_revision(conn, project_id, prerequisite_object_id)
        )
        if revision_row is None:
            return False, "prerequisite_not_visible"
    return _evaluate_revision_state(revision_row, condition, as_of_seq, criterion_ref)


def _evaluate_revision_state(
    revision: sqlite3.Row,
    condition: str,
    as_of_seq: int | None,
    criterion_ref: dict[str, Any] | None,
) -> tuple[bool, str]:
    state = repo.parse_state(revision)
    if condition == "exists":
        return True, "prerequisite_exists"
    if condition in ("done", "done_with_review"):
        if state.get("work_state") != "done":
            return False, "prerequisite_not_done"
        if condition == "done_with_review" and state.get("review_state") != "reviewed":
            return False, "prerequisite_not_reviewed"
        return True, "prerequisite_done"
    if condition == "accepted_artifact_available":
        if state.get("availability") != "available":
            return False, "artifact_unavailable"
        if state.get("assurance") not in ("content_sha256", "manifest"):
            return False, "artifact_identity_unverified"
        return True, "artifact_available"
    if condition == "claim_assessed_under_criteria":
        assessment = state.get("assessment") or {}
        if assessment.get("review_state") != "reviewed":
            return False, "claim_not_assessed"
        if as_of_seq is not None and assessment.get("assessed_seq") and assessment["assessed_seq"] > as_of_seq:
            return False, "assessment_not_visible"
        if criterion_ref and assessment.get("criterion_ref") != criterion_ref:
            return False, "criterion_mismatch"
        return True, "claim_assessed"
    if condition == "source_registered":
        if revision["kind"] != "source":
            return False, "not_a_source"
        if revision["record_state"] not in ("active", "draft"):
            return False, "source_retired"
        return True, "source_registered"
    if condition == "diagnostic_check_passed":
        check = state.get("diagnostic_check") or {}
        if check.get("passed") is True:
            return True, "diagnostic_passed"
        return False, "diagnostic_not_passed"
    if condition == "custom":
        return False, "custom_condition_requires_review"
    return False, "unknown_condition"


def unsatisfied_dependencies(
    conn: sqlite3.Connection,
    project_id: str,
    object_id: str,
    *,
    as_of_seq: int | None = None,
    link_rows: list[sqlite3.Row] | None = None,
    revision_lookup: dict[str, sqlite3.Row] | None = None,
) -> list[dict[str, Any]]:
    rows = link_rows if link_rows is not None else repo.list_links(
        conn,
        project_id=project_id,
        object_id=object_id,
        predicate="depends_on",
        direction="out",
        as_of_seq=as_of_seq,
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        condition = dependency_condition(conn, project_id, row)
        criterion = json.loads(row["qualifiers_json"] or "{}").get("criterion_ref")
        satisfied, detail = evaluate_condition(
            conn,
            project_id,
            row["dst_object_id"],
            condition,
            as_of_seq=as_of_seq,
            criterion_ref=criterion,
            revision_row=(revision_lookup or {}).get(row["dst_object_id"]),
        )
        if not satisfied:
            results.append(
                {
                    "prerequisite_object_id": row["dst_object_id"],
                    "prerequisite_revision": row["dst_revision"],
                    "condition": condition,
                    "detail": detail,
                }
            )
    return results


def work_blockers(
    conn: sqlite3.Connection,
    project_id: str,
    object_id: str,
    *,
    as_of_seq: int | None = None,
    link_rows: list[sqlite3.Row] | None = None,
    revision_lookup: dict[str, sqlite3.Row] | None = None,
    work_state_row: sqlite3.Row | None = None,
) -> list[dict[str, Any]]:
    from research_kb.domain.blocking_rules import applicability_context, blocker_evaluation

    if work_state_row is None:
        work_state_row = (
            repo.visible_revision(conn, project_id, object_id, as_of_seq)
            if as_of_seq is not None
            else repo.current_revision(conn, project_id, object_id)
        )
    context = applicability_context(repo.parse_state(work_state_row))
    rows = link_rows if link_rows is not None else repo.list_links(
        conn,
        project_id=project_id,
        object_id=object_id,
        predicate="blocks",
        direction="in",
        as_of_seq=as_of_seq,
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        revision = (revision_lookup or {}).get(row["src_object_id"])
        if revision is None:
            if repo.object_row_or_none(conn, project_id, row["src_object_id"]) is None:
                continue
            revision = repo.current_revision(conn, project_id, row["src_object_id"])
        state = repo.parse_state(revision)
        if state.get("status") == "resolved":
            continue
        qualifiers = json.loads(row["qualifiers_json"] or "{}")
        applies, evaluation = blocker_evaluation(qualifiers, context)
        if not applies:
            continue
        if evaluation == "unknown" or state.get("severity") in ("high", "critical"):
            results.append(
                {
                    "blocker_object_id": row["src_object_id"],
                    "blocker_revision": row["src_revision"],
                    "severity": state.get("severity", "medium"),
                    "effect": state.get("effect"),
                    "operations": qualifiers.get("operations", state.get("blocking_operations", [])),
                    "resolution_criterion": state.get("resolution_criterion"),
                    "rule_status": "unknown_applicability" if evaluation == "unknown" else evaluation,
                }
            )
    return results


def readiness_many(
    ctx: ServiceContext,
    object_ids: list[str],
    *,
    as_of_seq: int | None = None,
) -> dict[str, dict[str, Any]]:
    conn = ctx.conn
    if not object_ids:
        return {}
    unique_ids = sorted(set(object_ids))
    depends_links = repo.links_many(
        conn,
        project_id=ctx.project_id,
        object_ids=unique_ids,
        predicate="depends_on",
        direction="out",
        as_of_seq=as_of_seq,
    )
    block_links = repo.links_many(
        conn,
        project_id=ctx.project_id,
        object_ids=unique_ids,
        predicate="blocks",
        direction="in",
        as_of_seq=as_of_seq,
    )
    related: set[str] = set(unique_ids)
    for rows in depends_links.values():
        related.update(row["dst_object_id"] for row in rows)
    for rows in block_links.values():
        related.update(row["src_object_id"] for row in rows)
    revision_lookup = repo.revisions_many(
        conn, project_id=ctx.project_id, object_ids=sorted(related), as_of_seq=as_of_seq
    )
    results: dict[str, dict[str, Any]] = {}
    for object_id in unique_ids:
        state_row = revision_lookup.get(object_id)
        if state_row is None:
            results[object_id] = {
                "object_id": object_id,
                "blocked": True,
                "unsatisfied_dependencies": [],
                "blockers": [],
                "error": "not_visible_at_snapshot",
            }
            continue
        state = repo.parse_state(state_row)
        unsatisfied = unsatisfied_dependencies(
            conn,
            ctx.project_id,
            object_id,
            as_of_seq=as_of_seq,
            link_rows=depends_links.get(object_id, []),
            revision_lookup=revision_lookup,
        )
        blockers = work_blockers(
            conn,
            ctx.project_id,
            object_id,
            as_of_seq=as_of_seq,
            link_rows=block_links.get(object_id, []),
            revision_lookup=revision_lookup,
            work_state_row=state_row,
        )
        results[object_id] = {
            "object_id": object_id,
            "work_state": state.get("work_state"),
            "blocked": bool(unsatisfied or blockers),
            "unsatisfied_dependencies": unsatisfied,
            "blockers": blockers,
            "required_inputs": state.get("required_inputs", []),
            "completion_criteria": state.get("completion_criteria", []),
            "next_action": state.get("next_action"),
            "revision": state_row["revision"],
        }
    return results


def claim_work(
    mutation: Mutation,
    *,
    object_id: str,
    expected_revision: int,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    conn = mutation.ctx.conn
    row = _work_row(conn, mutation.project_id, object_id)
    if int(row["current_revision"]) != int(expected_revision):
        from research_kb.errors import revision_conflict

        raise revision_conflict(int(expected_revision), int(row["current_revision"]))
    _expire_stale_leases(conn, mutation.project_id)
    existing = conn.execute(
        "SELECT * FROM work_leases WHERE project_id = ? AND object_id = ? AND state = 'active'",
        (mutation.project_id, object_id),
    ).fetchone()
    if existing is not None and existing["owner"] != mutation.actor_id:
        raise blocked(
            "Another owner holds an active ownership lease.",
            owner=existing["owner"],
            expires_at=existing["expires_at"],
            hint="Lease expiry means ownership is stale, not that work failed or a process stopped.",
        )
    if existing is not None:
        conn.execute(
            "UPDATE work_leases SET state = 'released', released_seq = ?, release_reason = 'reclaimed by owner' "
            "WHERE lease_id = ?",
            (mutation.commit_seq, existing["lease_id"]),
        )
    from research_kb.storage.db import new_id, utc_now
    from datetime import UTC, datetime, timedelta

    lease_id = new_id()
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn.execute(
        """
        INSERT INTO work_leases
          (lease_id, project_id, object_id, owner, state, acquired_seq, acquired_at, expires_at)
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (lease_id, mutation.project_id, object_id, mutation.actor_id, mutation.commit_seq, utc_now(), expires_at),
    )
    return {
        "lease_id": lease_id,
        "object_id": object_id,
        "owner": mutation.actor_id,
        "expires_at": expires_at,
        "note": (
            "A short ownership lease coordinates agents; it does not grant permission to change claims "
            "or run expensive jobs."
        ),
    }


def release_work(
    mutation: Mutation,
    *,
    lease_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    conn = mutation.ctx.conn
    row = conn.execute(
        "SELECT * FROM work_leases WHERE project_id = ? AND lease_id = ?",
        (mutation.project_id, lease_id),
    ).fetchone()
    if row is None:
        from research_kb.errors import not_found

        raise not_found("Ownership lease does not resolve.", lease_id=lease_id)
    if row["owner"] != mutation.actor_id and "administer" not in mutation.ctx.capabilities():
        from research_kb.errors import permission_denied

        raise permission_denied(
            "Only the lease owner or an administrator can release this lease.",
            capability="administer",
            actor=mutation.actor_id,
        )
    conn.execute(
        "UPDATE work_leases SET state = 'released', released_seq = ?, release_reason = ? "
        "WHERE lease_id = ? AND state = 'active'",
        (mutation.commit_seq, reason, lease_id),
    )
    return {"lease_id": lease_id, "state": "released"}


def _expire_stale_leases(conn: sqlite3.Connection, project_id: str) -> None:
    from research_kb.storage.db import utc_now

    conn.execute(
        "UPDATE work_leases SET state = 'expired', release_reason = 'expired without heartbeat' "
        "WHERE project_id = ? AND state = 'active' AND expires_at IS NOT NULL AND expires_at < ?",
        (project_id, utc_now()),
    )


def project_progress(
    ctx: ServiceContext,
    *,
    scope_ref: dict[str, Any] | None = None,
    as_of_seq: int | None = None,
) -> dict[str, Any]:
    conn = ctx.conn
    as_of = as_of_seq if as_of_seq is not None else ctx.latest_seq()
    work_rows = conn.execute(
        """
        SELECT o.object_id, r.revision, r.state_json, r.title, r.record_state
        FROM objects o
        JOIN revisions r ON r.project_id = o.project_id AND r.object_id = o.object_id
                        AND r.revision = o.current_revision
        WHERE o.project_id = ? AND o.kind = 'work' AND r.recorded_seq <= ?
        ORDER BY o.object_id
        """,
        (ctx.project_id, as_of),
    ).fetchall()
    scope_object_id = None
    if scope_ref:
        scope_object_id = repo.resolve_ref(conn, ctx.project_id, scope_ref)["object_id"]
    counts: dict[str, int] = {state: 0 for state in WORK_STATES}
    criteria_total = 0
    criteria_satisfied = 0
    items: list[dict[str, Any]] = []
    for row in work_rows:
        state = repo.parse_state(row)
        work_state = state.get("work_state", "open")
        if scope_object_id:
            parent = (state.get("parent_goal_ref") or {}).get("object_id")
            if parent != scope_object_id and row["object_id"] != scope_object_id:
                continue
        if row["record_state"] == "retired":
            continue
        counts[work_state] = counts.get(work_state, 0) + 1
        criteria = state.get("completion_criteria") or []
        criteria_total += len(criteria)
        if work_state == "done":
            criteria_satisfied += len(criteria)
        items.append(
            {
                "object_id": row["object_id"],
                "title": row["title"],
                "work_state": work_state,
                "criteria": len(criteria),
                "satisfied": len(criteria) if work_state == "done" else 0,
            }
        )
    return {
        "as_of_seq": as_of,
        "scope_object_id": scope_object_id,
        "counts": counts,
        "criteria": {
            "numerator": criteria_satisfied,
            "denominator": criteria_total,
            "exclusions": ["cancelled work is excluded from completion counting"],
        },
        "items": items,
        "note": "progress_counting_from_criteria_not_arbitrary_notes",
    }


def next_work(
    ctx: ServiceContext,
    *,
    limit: int = 5,
    as_of_seq: int | None = None,
) -> list[dict[str, Any]]:
    conn = ctx.conn
    as_of = as_of_seq if as_of_seq is not None else ctx.latest_seq()
    rows = conn.execute(
        """
        SELECT o.object_id, r.revision, r.state_json, r.title
        FROM objects o
        JOIN revisions r ON r.project_id = o.project_id AND r.object_id = o.object_id
                        AND r.revision = o.current_revision
        WHERE o.project_id = ? AND o.kind = 'work' AND r.record_state = 'active'
          AND r.recorded_seq <= ?
          AND json_extract(r.state_json, '$.work_state') NOT IN ('done', 'cancelled')
        """,
        (ctx.project_id, as_of),
    ).fetchall()
    candidate_ids = [row["object_id"] for row in rows]
    readiness = readiness_many(ctx, candidate_ids, as_of_seq=as_of)
    leases = _active_leases_many(conn, ctx.project_id, candidate_ids)
    resolving = _resolving_work_ids(conn, ctx.project_id, candidate_ids, as_of)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        object_id = row["object_id"]
        state = repo.parse_state(row)
        item_readiness = readiness.get(
            object_id,
            {"blocked": True, "unsatisfied_dependencies": [], "blockers": []},
        )
        rank = 4
        reason = "exploratory_or_low_priority"
        if object_id in resolving:
            rank = 1
            reason = "resolves_critical_blocker"
        elif not item_readiness["blocked"] and state.get("priority") == "critical":
            rank = 2
            reason = "critical_priority_actionable"
        elif not item_readiness["blocked"] and state.get("priority") == "high":
            rank = 3
            reason = "high_priority_actionable"
        lease = leases.get(object_id)
        candidates.append(
            {
                "object_id": object_id,
                "revision": row["revision"],
                "title": row["title"],
                "objective": state.get("objective"),
                "priority": state.get("priority"),
                "priority_reason": state.get("priority_reason"),
                "owner": state.get("owner"),
                "ownership": lease,
                "ownership_conflict": bool(lease and lease["owner"] != ctx.actor_id),
                "ordering": rank,
                "ordering_reason": reason,
                "blocked": item_readiness["blocked"],
                "unsatisfied_conditions": item_readiness["unsatisfied_dependencies"],
                "blockers": item_readiness["blockers"],
                "expected_output": state.get("completion_criteria"),
                "done_criterion": state.get("completion_criteria"),
            }
        )
    candidates.sort(key=lambda item: (item["ordering"], item["object_id"]))
    return candidates[:limit]


def _active_leases_many(
    conn: sqlite3.Connection, project_id: str, object_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not object_ids:
        return {}
    _expire_stale_leases(conn, project_id)
    placeholders = ",".join("?" for _ in object_ids)
    rows = conn.execute(
        f"""
        SELECT object_id, owner, expires_at, lease_id FROM work_leases
        WHERE project_id = ? AND state = 'active' AND object_id IN ({placeholders})
        """,
        (project_id, *object_ids),
    ).fetchall()
    return {
        row["object_id"]: {
            "owner": row["owner"],
            "expires_at": row["expires_at"],
            "lease_id": row["lease_id"],
        }
        for row in rows
    }


def _resolving_work_ids(
    conn: sqlite3.Connection, project_id: str, object_ids: list[str], as_of_seq: int
) -> set[str]:
    if not object_ids:
        return set()
    placeholders = ",".join("?" for _ in object_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT l.src_object_id FROM link_revisions l
        JOIN revisions r ON r.project_id = l.project_id AND r.object_id = l.object_id
                        AND r.revision = l.revision
        WHERE l.project_id = ? AND l.predicate = 'resolves' AND r.recorded_seq <= ?
          AND r.record_state != 'retired' AND l.src_object_id IN ({placeholders})
        """,
        (project_id, as_of_seq, *object_ids),
    ).fetchall()
    return {row["src_object_id"] for row in rows}
