from __future__ import annotations

import json
import sqlite3
from typing import Any

from research_kb.domain.vocab import GENERATED_NOTICE
from research_kb.errors import cursor_invalid, schema_validation_failed
from research_kb.service.context import ServiceContext
from research_kb.service.proposals import propose
from research_kb.service.retrieval import resolve_snapshot
from research_kb.storage.db import encode_snapshot_cursor
from research_kb.version import SCHEMA_VERSION

DRAFT_SCHEMA_VERSION = "1.0"
EDITABLE_FIELDS = ("title", "body_md", "record_state", "state_json")


def build_draft(ctx: ServiceContext, *, as_of_cursor: str | None = None) -> dict[str, Any]:
    seq, historical = resolve_snapshot(ctx, as_of_cursor)
    rows = ctx.conn.execute(
        """
        SELECT o.object_id, r.revision, r.kind, r.subkind, r.title, r.body_md, r.record_state,
               r.state_json, r.content_hash
        FROM objects o
        JOIN revisions r ON r.project_id = o.project_id AND r.object_id = o.object_id
                        AND r.revision = o.current_revision
        WHERE o.project_id = ? AND r.recorded_seq <= ?
          AND r.record_state != 'tombstoned'
        ORDER BY r.kind, o.object_id
        """,
        (ctx.project_id, seq),
    ).fetchall()
    records = []
    for row in rows:
        records.append(
            {
                "ref": {"object_id": row["object_id"], "revision": row["revision"]},
                "kind": row["kind"],
                "subkind": row["subkind"],
                "title": row["title"],
                "body_md": row["body_md"],
                "record_state": row["record_state"],
                "state_json": json.loads(row["state_json"] or "{}"),
                "content_hash": row["content_hash"],
            }
        )
    return {
        "export_schema_version": DRAFT_SCHEMA_VERSION,
        "notice": GENERATED_NOTICE,
        "project_id": ctx.project_id,
        "cursor": encode_snapshot_cursor(ctx.epoch, seq),
        "historical": historical,
        "records": records,
        "instructions": (
            "Edit only title, body_md, record_state, and state_json. Keep each ref.revision unchanged; "
            "the proposal is bound to those expected revisions. Import with 'rkb propose --draft FILE'."
        ),
    }


def _diff_record(current: sqlite3.Row | None, record: dict[str, Any]) -> list[str]:
    if current is None:
        return ["missing_object"]
    changed: list[str] = []
    for field in EDITABLE_FIELDS:
        existing = current[field]
        proposed = record.get(field)
        if field == "state_json":
            try:
                existing_value = json.loads(existing or "{}")
            except ValueError:
                existing_value = existing
            if proposed is None or proposed == existing_value:
                continue
            changed.append(field)
            continue
        if proposed is None or proposed == existing:
            continue
        changed.append(field)
    return changed


def propose_draft(
    ctx: ServiceContext,
    *,
    draft: dict[str, Any],
    request_id: str,
    reason: str | None = None,
    persist: bool = True,
    auto_apply: bool = False,
) -> dict[str, Any]:
    if draft.get("project_id") and draft["project_id"] != ctx.project_id:
        raise schema_validation_failed(
            "The draft belongs to another project.",
            draft_project=draft["project_id"],
            active_project=ctx.project_id,
        )
    cursor = draft.get("cursor")
    if cursor:
        from research_kb.storage.db import decode_snapshot_cursor

        draft_epoch, _seq = decode_snapshot_cursor(cursor)
        if draft_epoch != ctx.epoch:
            raise cursor_invalid("The draft snapshot cursor belongs to another controller epoch.")
    operations: list[dict[str, Any]] = []
    diffs: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for record in draft.get("records", []):
        ref = record.get("ref") or {}
        object_id = ref.get("object_id")
        expected_revision = ref.get("revision")
        if not object_id or not expected_revision:
            raise schema_validation_failed("Every draft record requires ref.object_id and ref.revision.", record=ref)
        current = ctx.conn.execute(
            """
            SELECT o.current_revision, r.title, r.body_md, r.record_state, r.state_json
            FROM objects o
            JOIN revisions r ON r.project_id = o.project_id AND r.object_id = o.object_id
                            AND r.revision = o.current_revision
            WHERE o.project_id = ? AND o.object_id = ?
            """,
            (ctx.project_id, object_id),
        ).fetchone()
        changed = _diff_record(current, record)
        if not changed:
            unchanged.append(object_id)
            continue
        payload: dict[str, Any] = {
            "ref": {"object_id": object_id, "revision": expected_revision},
        }
        if "title" in changed:
            payload["title"] = record["title"]
        if "body_md" in changed:
            payload["body_md"] = record["body_md"]
        if "record_state" in changed:
            payload["record_state"] = record["record_state"]
        if "state_json" in changed:
            payload["state_json"] = record["state_json"]
        operations.append({"op": "revise", "payload": payload})
        diffs.append(
            {
                "object_id": object_id,
                "expected_revision": expected_revision,
                "current_revision": current["current_revision"] if current else None,
                "changed_fields": changed,
            }
        )
    if not operations:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "unchanged",
            "project_id": ctx.project_id,
            "operations": [],
            "diffs": [],
            "unchanged": unchanged,
        }
    result = propose(
        ctx,
        operations=operations,
        reason=reason or "Editable draft import.",
        request_id=request_id,
        persist=persist,
        auto_apply=auto_apply,
    )
    result["draft_diffs"] = diffs
    result["unchanged"] = unchanged
    return result


def write_draft(ctx: ServiceContext, path: str, result: dict[str, Any]) -> dict[str, Any]:
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "format": "draft",
        "cursor": result["cursor"],
        "records": len(result["records"]),
    }
