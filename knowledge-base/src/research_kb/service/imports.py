from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from research_kb.domain.canonical import canonical_hash
from research_kb.errors import schema_validation_failed
from research_kb.service.context import ServiceContext
from research_kb.service.operations import apply_operations
from research_kb.service.sources import find_source_version
from research_kb.storage.db import new_id, utc_now, write_tx

IMPORT_PIPELINE_VERSION = "register_source/1"


def import_key(connector_namespace: str, external_id: str, external_version: str, options: dict[str, Any]) -> tuple[str, str]:
    pipeline_hash = canonical_hash(
        {"pipeline": IMPORT_PIPELINE_VERSION, "namespace": connector_namespace, "options": options}
    )
    return pipeline_hash, f"{connector_namespace}:{external_id}:{external_version}:{pipeline_hash[:12]}"


def existing_receipt(
    conn: sqlite3.Connection,
    project_id: str,
    connector_namespace: str,
    external_id: str,
    external_version: str,
    pipeline_hash: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM import_receipts
        WHERE project_id = ? AND connector_namespace = ? AND external_id = ?
          AND external_version = ? AND pipeline_hash = ?
        """,
        (project_id, connector_namespace, external_id, external_version, pipeline_hash),
    ).fetchone()


def run_import(
    ctx: ServiceContext,
    *,
    connector_namespace: str,
    items: list[dict[str, Any]],
    request_id: str,
    reason: str | None,
    dry_run: bool = False,
    source_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not items:
        raise schema_validation_failed("An import requires at least one item.")
    scope = source_scope or {}
    max_items = int(ctx.policy.get("limits", {}).get("max_page_size", 200))
    if len(items) > max_items:
        raise schema_validation_failed("Import batch exceeds the configured page limit.", limit=max_items)
    outcomes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    created_refs: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        external_id = str(item.get("external_id") or item.get("path") or index)
        external_version = str(item.get("version") or "unversioned")
        options = {
            "subkind": item.get("subkind", "note"),
            "capture": bool(item.get("captured_path")),
            "scope": scope,
        }
        pipeline_hash, key = import_key(connector_namespace, external_id, external_version, options)
        receipt = existing_receipt(
            ctx.conn, ctx.project_id, connector_namespace, external_id, external_version, pipeline_hash
        )
        if receipt is not None and receipt["status"] == "complete":
            outcomes.append(
                {
                    "item": key,
                    "status": "duplicate_reused",
                    "receipt_id": receipt["receipt_id"],
                    "outcomes": json.loads(receipt["outcomes_json"] or "[]"),
                }
            )
            continue
        if dry_run:
            outcomes.append({"item": key, "status": "validated", "would_capture": bool(item.get("captured_path"))})
            continue
        existing_source = find_source_version(
            ctx.conn,
            ctx.project_id,
            external_id=external_id,
            version=external_version,
        )
        if existing_source is not None and not captured_path:
            outcomes.append(
                {"item": key, "status": "duplicate_reused", "object_id": existing_source}
            )
            continue
        captured_path = item.get("captured_path")
        if captured_path and not Path(captured_path).is_file():
            failures.append({"item": key, "error": "source_file_missing", "path": captured_path})
            continue
        source_payload: dict[str, Any] = {
            "subkind": item.get("subkind", "note"),
            "title": item.get("title") or external_id,
            "author": item.get("author"),
            "external_id": external_id,
            "version": external_version,
            "locator": item.get("locator") or captured_path,
            "identity_assurance": item.get("identity_assurance", "content_sha256" if captured_path else "metadata_only"),
            "preservation": item.get("preservation", "local_copy_allowed" if captured_path else "metadata_only"),
            "supplied_by": item.get("supplied_by"),
            "extracted_by": item.get("extracted_by"),
            "author_attribution_notes": item.get("author_attribution_notes"),
            "captured_path": captured_path,
            "anchors": item.get("anchors") or [],
            "aliases": item.get("aliases") or [],
        }
        try:
            batch = apply_operations(
                ctx,
                operations=[{"op": "register_source", "payload": source_payload}],
                request_id=f"{request_id}:{index}",
                reason=reason or f"Connector import from {connector_namespace}.",
                action="import",
                record_idempotent=True,
                idempotency_operation="import_item",
                idempotency_payload_hash=canonical_hash({"key": key}),
            )
            created = batch.created[0] if batch.created else None
            if created is not None:
                created_refs.append(created)
            outcomes.append({"item": key, "status": "created", "created": created})
            _record_receipt(
                ctx,
                connector_namespace=connector_namespace,
                external_id=external_id,
                external_version=external_version,
                pipeline_hash=pipeline_hash,
                status="complete",
                cursor={"index": index + 1, "total": len(items)},
                outcomes=[outcomes[-1]],
                failures=[],
            )
        except Exception as exc:
            failures.append({"item": key, "error": type(exc).__name__, "message": str(exc)})
            _record_receipt(
                ctx,
                connector_namespace=connector_namespace,
                external_id=external_id,
                external_version=external_version,
                pipeline_hash=pipeline_hash,
                status="failed",
                cursor={"index": index, "total": len(items)},
                outcomes=[],
                failures=[failures[-1]],
            )
    status = "complete" if not failures else ("partial" if created_refs or outcomes else "failed")
    return {
        "schema_version": "1.0",
        "project_id": ctx.project_id,
        "connector_namespace": connector_namespace,
        "status": status,
        "outcomes": outcomes,
        "failures": failures,
        "created": created_refs,
        "resume_cursor": {"completed_items": len(outcomes) + len(failures), "total": len(items)},
        "note": "Importing a source is not blanket approval of extracted claims.",
    }


def _record_receipt(
    ctx: ServiceContext,
    *,
    connector_namespace: str,
    external_id: str,
    external_version: str,
    pipeline_hash: str,
    status: str,
    cursor: dict[str, Any],
    outcomes: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> str:
    conn = ctx.conn
    existing = existing_receipt(
        conn, ctx.project_id, connector_namespace, external_id, external_version, pipeline_hash
    )
    with write_tx(conn):
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM commit_events WHERE project_id = ?",
            (ctx.project_id,),
        ).fetchone()
        seq = int(seq_row["seq"])
        if existing is None:
            receipt_id = new_id()
            conn.execute(
                """
                INSERT INTO import_receipts
                  (receipt_id, project_id, connector_namespace, external_id, external_version,
                   pipeline_hash, status, cursor_json, outcomes_json, failures_json,
                   created_seq, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    ctx.project_id,
                    connector_namespace,
                    external_id,
                    external_version,
                    pipeline_hash,
                    status,
                    json.dumps(cursor, sort_keys=True),
                    json.dumps(outcomes, sort_keys=True),
                    json.dumps(failures, sort_keys=True),
                    seq,
                    utc_now(),
                    utc_now(),
                ),
            )
        else:
            receipt_id = existing["receipt_id"]
            conn.execute(
                """
                UPDATE import_receipts
                SET status = ?, cursor_json = ?, outcomes_json = ?, failures_json = ?, updated_at = ?
                WHERE receipt_id = ?
                """,
                (
                    status,
                    json.dumps(cursor, sort_keys=True),
                    json.dumps(outcomes, sort_keys=True),
                    json.dumps(failures, sort_keys=True),
                    utc_now(),
                    receipt_id,
                ),
            )
    return receipt_id


def list_receipts(ctx: ServiceContext, *, connector_namespace: str | None = None) -> list[dict[str, Any]]:
    clauses = ["project_id = ?"]
    params: list[Any] = [ctx.project_id]
    if connector_namespace:
        clauses.append("connector_namespace = ?")
        params.append(connector_namespace)
    rows = ctx.conn.execute(
        f"SELECT * FROM import_receipts WHERE {' AND '.join(clauses)} ORDER BY created_seq",
        params,
    ).fetchall()
    return [dict(row) for row in rows]
