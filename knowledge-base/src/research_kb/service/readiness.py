from __future__ import annotations

import sqlite3
from typing import Any

from research_kb.domain.evidence import assessment_is_paper_ready
from research_kb.service.assessment import applying_blockers, inaccessible_support
from research_kb.service.context import ServiceContext
from research_kb.storage import repo


def _current_row(conn: sqlite3.Connection, project_id: str, object_id: str) -> sqlite3.Row:
    return repo.current_revision(conn, project_id, object_id)


def evaluate_target(ctx: ServiceContext, object_id: str) -> dict[str, Any]:
    conn = ctx.conn
    row = _current_row(conn, ctx.project_id, object_id)
    state = repo.parse_state(row)
    assessment = state.get("assessment") or {}
    blockers = applying_blockers(conn, ctx.project_id, object_id)
    critical = [
        blocker for blocker in blockers
        if blocker.get("severity") in ("high", "critical") or blocker.get("rule_status") == "unknown_applicability"
    ]
    support_refs = list(assessment.get("support_refs") or []) + list(state.get("support_refs") or [])
    inaccessible = inaccessible_support(conn, ctx.project_id, support_refs)
    manifest = state.get("selection_manifest")
    unreviewed_selection = bool(manifest) and not manifest.get("review_refs")
    open_flags = repo.open_review_flags(conn, ctx.project_id, target_object_id=object_id)
    assessed_seq = assessment.get("assessed_seq")
    newer_revision = None
    if assessed_seq is not None:
        newer_revision = conn.execute(
            """
            SELECT 1 FROM revisions
            WHERE project_id = ? AND object_id = ? AND recorded_seq > ?
            LIMIT 1
            """,
            (ctx.project_id, object_id, assessed_seq),
        ).fetchone()
    review_current = bool(assessment) and not open_flags and assessed_seq is not None and newer_revision is None
    ready, reasons = assessment_is_paper_ready(
        assessment or None,
        open_critical_blockers=critical,
        unreviewed_selection=unreviewed_selection,
        inaccessible_indispensable=inaccessible,
        review_current=review_current,
    )
    return {
        "object_id": object_id,
        "revision": row["revision"],
        "kind": row["kind"],
        "subkind": row["subkind"],
        "ready": ready,
        "reasons": reasons,
        "blockers": critical,
        "open_review_flags": len(open_flags),
        "selection_manifest_frozen": bool(manifest),
        "note": (
            "Readiness is a recorded-state check, not scientific truth; it requires current review, "
            "available evidence, and no unresolved critical blockers."
        ),
    }


def readiness_summary(ctx: ServiceContext) -> dict[str, Any]:
    rows = ctx.conn.execute(
        """
        SELECT o.object_id FROM objects o
        JOIN revisions r ON r.project_id = o.project_id AND r.object_id = o.object_id
                        AND r.revision = o.current_revision
        WHERE o.project_id = ? AND o.kind = 'claim' AND r.record_state = 'active'
        ORDER BY o.object_id
        """,
        (ctx.project_id,),
    ).fetchall()
    evaluations = [evaluate_target(ctx, row["object_id"]) for row in rows]
    ready = [item for item in evaluations if item["ready"]]
    return {
        "claims": len(evaluations),
        "ready": len(ready),
        "not_ready": [item for item in evaluations if not item["ready"]],
    }
