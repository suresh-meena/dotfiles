from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from research_kb.domain.canonical import hash_bytes
from research_kb.domain.vocab import SOURCE_ANCHOR_KINDS, SOURCE_SUBKINDS
from research_kb.errors import (
    artifact_unavailable,
    not_found,
    schema_validation_failed,
)
from research_kb.ingestion import extract as extraction
from research_kb.service.context import ServiceContext
from research_kb.service.objects import Mutation, create_object
from research_kb.storage import blobs
from research_kb.storage.db import new_id, utc_now
from research_kb.storage.search import project_revision


def insert_anchor(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source_object_id: str,
    source_revision: int,
    extraction_id: str | None,
    anchor_kind: str,
    locator: dict[str, Any],
    coordinate_system: str,
    excerpt: str | None,
    status: str,
    seq: int,
) -> str:
    if anchor_kind not in SOURCE_ANCHOR_KINDS:
        raise schema_validation_failed(
            "Unknown anchor kind.", anchor_kind=anchor_kind, allowed=list(SOURCE_ANCHOR_KINDS)
        )
    anchor_id = new_id()
    conn.execute(
        """
        INSERT INTO source_anchors
          (anchor_id, project_id, source_object_id, source_revision, extraction_id, anchor_kind,
           locator_json, coordinate_system, excerpt, excerpt_hash, status, created_seq, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            anchor_id,
            project_id,
            source_object_id,
            source_revision,
            extraction_id,
            anchor_kind,
            json.dumps(locator, sort_keys=True),
            coordinate_system,
            excerpt,
            hash_bytes(excerpt.encode("utf-8")) if excerpt is not None else None,
            status,
            seq,
            utc_now(),
        ),
    )
    return anchor_id


def _register_blob(
    ctx: ServiceContext,
    *,
    digest: str,
    byte_size: int,
    media_type: str | None,
    assurance: str,
    location: str,
    location_kind: str,
    seq: int,
    role: str = "source",
) -> None:
    conn = ctx.conn
    conn.execute(
        """
        INSERT OR IGNORE INTO blob_registry (blob_hash, byte_size, media_type, assurance, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (digest, byte_size, media_type, assurance, utc_now()),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO blob_locations (blob_hash, location, location_kind, availability, last_verified_at)
        VALUES (?, ?, ?, 'available', ?)
        """,
        (digest, location, location_kind, utc_now()),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO project_blobs (project_id, blob_hash, role, registered_seq)
        VALUES (?, ?, ?, ?)
        """,
        (ctx.project_id, digest, role, seq),
    )


def resolve_import_path(ctx: ServiceContext, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise schema_validation_failed(
            "Import paths must be absolute.", path=raw_path
        )
    resolved = path.resolve()
    roots = ctx.policy.get("capture", {}).get("allowed_import_roots") or []
    if roots:
        allowed = [Path(root).resolve() for root in roots]
        if not any(resolved == root or root in resolved.parents for root in allowed):
            raise schema_validation_failed(
                "Import path is outside the approved import roots.",
                path=str(resolved),
                allowed_roots=[str(root) for root in allowed],
            )
    if not resolved.is_file():
        raise artifact_unavailable(f"Source file is not available: {resolved}")
    return resolved


def register_source(mutation: Mutation, payload: dict[str, Any]) -> dict[str, Any]:
    ctx = mutation.ctx
    conn = ctx.conn
    subkind = payload.get("subkind")
    if subkind not in SOURCE_SUBKINDS:
        raise schema_validation_failed("Unknown source subkind.", allowed=list(SOURCE_SUBKINDS))
    state = {key: value for key, value in payload.items() if key not in ("aliases", "anchors", "captured_path")}
    state.setdefault("blob_hash", None)
    state.setdefault("byte_size", None)
    state.setdefault("identity_assurance", "metadata_only")
    captured_path = payload.get("captured_path")
    stored_blob: dict[str, Any] | None = None
    if captured_path:
        path = resolve_import_path(ctx, captured_path)
        max_bytes = int(ctx.policy.get("limits", {}).get("max_import_bytes", 100_000_000))
        if mutation.dry_run:
            if not path.is_file():
                raise artifact_unavailable(f"Source file is not available: {path}")
            state["blob_hash"] = None
            state["byte_size"] = path.stat().st_size
            state["identity_assurance"] = "content_sha256"
            state["locator"] = state.get("locator") or str(path)
            state["retrieval_time"] = state.get("retrieval_time") or utc_now()
            state["import_time"] = state.get("import_time") or utc_now()
            stored_blob = {"digest": None, "size": state["byte_size"], "path": path, "source_path": path, "dry_run": True}
        else:
            digest, size, stored_path = blobs.stage_and_store(ctx.routing.sources_dir.parent, path, max_bytes=max_bytes)
            state["blob_hash"] = digest
            state["byte_size"] = size
            state["identity_assurance"] = "content_sha256"
            state["locator"] = state.get("locator") or str(path)
            state["retrieval_time"] = state.get("retrieval_time") or utc_now()
            state["import_time"] = state.get("import_time") or utc_now()
            stored_blob = {"digest": digest, "size": size, "path": stored_path, "source_path": path, "dry_run": False}
            _register_blob(
                ctx,
                digest=digest,
                byte_size=size,
                media_type=state.get("media_type"),
                assurance="content_sha256",
                location=str(stored_path),
                location_kind="local_blob",
                seq=mutation.commit_seq,
            )
    result = create_object(
        mutation,
        kind="source",
        subkind=subkind,
        title=state.get("title", ""),
        state_json=state,
        record_state="active",
        aliases=payload.get("aliases") or [],
        source_object_id=None,
    )
    object_id = result["object_id"]
    if payload.get("external_id"):
        from research_kb.storage.repo import add_alias

        add_alias(
            conn,
            project_id=ctx.project_id,
            object_id=object_id,
            alias_text=str(payload["external_id"]),
            namespace="external_id",
            seq=mutation.commit_seq,
        )
    extraction_record: dict[str, Any] | None = None
    if stored_blob is not None and not stored_blob.get("dry_run"):
        parsed = extraction.extract(stored_blob["source_path"])
        pipeline_hash = extraction.extraction_pipeline_hash(parsed.parser_name, parsed.parser_version)
        extraction_id = new_id()
        section_list: list[dict[str, Any]] = []
        extraction_digest = None
        if parsed.status in ("extracted", "ocr_uncertain") and parsed.text:
            payload_bytes = extraction.extraction_blob_bytes(parsed)
            extraction_digest, _ = blobs.store_bytes(ctx.routing.extractions_dir.parent, payload_bytes)
            section_list = extraction.sections_for_extraction(parsed)
        conn.execute(
            """
            INSERT INTO source_extractions
              (extraction_id, project_id, source_object_id, source_revision, original_blob_hash,
               extraction_blob_hash, parser_name, parser_version, pipeline_hash, status,
               omissions_json, created_seq, created_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                extraction_id,
                ctx.project_id,
                object_id,
                stored_blob["digest"],
                extraction_digest,
                parsed.parser_name,
                parsed.parser_version,
                pipeline_hash,
                parsed.status,
                json.dumps(parsed.omissions),
                mutation.commit_seq,
                utc_now(),
            ),
        )
        extraction_record = {
            "extraction_id": extraction_id,
            "blob_hash": extraction_digest,
            "pipeline_hash": pipeline_hash,
            "status": parsed.status,
            "sections": section_list,
            "parser_name": parsed.parser_name,
            "parser_version": parsed.parser_version,
        }
    anchors: list[dict[str, Any]] = []
    extraction_text = ""
    if extraction_record and extraction_record.get("blob_hash"):
        extraction_text = blobs.read_blob(
            ctx.routing.extractions_dir.parent, extraction_record["blob_hash"]
        ).decode("utf-8", errors="replace")
    for anchor in payload.get("anchors") or []:
        locator = anchor.get("locator") or {}
        coordinate_system = anchor.get("coordinate_system", "locator")
        status = anchor.get("status", "ok")
        if not locator and extraction_text and anchor.get("excerpt"):
            located = extraction.find_excerpt_anchor(extraction_text, anchor["excerpt"])
            locator = located["locator"]
            coordinate_system = located["coordinate_system"]
            status = located.get("status", status)
        anchor_id = insert_anchor(
            conn,
            project_id=ctx.project_id,
            source_object_id=object_id,
            source_revision=1,
            extraction_id=extraction_record["extraction_id"] if extraction_record else None,
            anchor_kind=anchor.get("anchor_kind", "markdown_text"),
            locator=locator,
            coordinate_system=coordinate_system,
            excerpt=anchor.get("excerpt"),
            status=status,
            seq=mutation.commit_seq,
        )
        anchors.append({"anchor_id": anchor_id})
    if extraction_record and extraction_record.get("sections"):
        sections = extraction_record["sections"]
        for section in sections[:1000]:
            anchor_id = insert_anchor(
                conn,
                project_id=ctx.project_id,
                source_object_id=object_id,
                source_revision=1,
                extraction_id=extraction_record["extraction_id"],
                anchor_kind="markdown_text",
                locator={
                    "heading_path": section.get("heading_path", []),
                    "line_start": section.get("line_start"),
                    "line_end": section.get("line_end"),
                },
                coordinate_system="line_range_1based",
                excerpt=section.get("body", "")[:2000],
                status="ok",
                seq=mutation.commit_seq,
            )
            section["anchor_id"] = anchor_id
        project_revision(
            conn,
            project_id=ctx.project_id,
            object_id=object_id,
            revision=1,
            title=state.get("title", ""),
            body_md="",
            alias_text=" ".join(payload.get("aliases") or []),
            recorded_seq=mutation.commit_seq,
            sections=sections,
            source_object_id=object_id,
        )
    result["extraction"] = (
        {
            "extraction_id": extraction_record["extraction_id"],
            "status": extraction_record["status"],
            "blob_hash": extraction_record["blob_hash"],
        }
        if extraction_record
        else None
    )
    result["anchors"] = anchors
    return result


def reproject_source(ctx: ServiceContext, object_id: str, revision: int) -> None:
    conn = ctx.conn
    row = conn.execute(
        """
        SELECT extraction_id, extraction_blob_hash, status FROM source_extractions
        WHERE project_id = ? AND source_object_id = ? AND source_revision = ?
        ORDER BY created_seq DESC LIMIT 1
        """,
        (ctx.project_id, object_id, revision),
    ).fetchone()
    revision_row = conn.execute(
        "SELECT title, body_md, record_state, state_json FROM revisions WHERE project_id = ? AND object_id = ? AND revision = ?",
        (ctx.project_id, object_id, revision),
    ).fetchone()
    if revision_row is None:
        raise not_found("Source revision does not resolve for reprojection.", object_id=object_id, revision=revision)
    from research_kb.storage.repo import aliases_for

    alias_text = " ".join(aliases_for(conn, ctx.project_id, object_id))
    sections: list[dict[str, Any]] = []
    if row is not None and row["extraction_blob_hash"]:
        text = blobs.read_blob(ctx.routing.extractions_dir.parent, row["extraction_blob_hash"]).decode(
            "utf-8", errors="replace"
        )
        parsed = extraction.Extraction(status=row["status"], text=text)
        sections = extraction.attach_line_ranges(text, extraction.chunk_markdown(text))
        for section in sections:
            anchor = conn.execute(
                """
                SELECT anchor_id FROM source_anchors
                WHERE project_id = ? AND source_object_id = ? AND source_revision = ?
                  AND json_extract(locator_json, '$.line_start') = ?
                  AND json_extract(locator_json, '$.line_end') = ?
                LIMIT 1
                """,
                (
                    ctx.project_id,
                    object_id,
                    revision,
                    section.get("line_start"),
                    section.get("line_end"),
                ),
            ).fetchone()
            section["anchor_id"] = anchor["anchor_id"] if anchor else None
    project_revision(
        conn,
        project_id=ctx.project_id,
        object_id=object_id,
        revision=revision,
        title=revision_row["title"],
        body_md=revision_row["body_md"],
        alias_text=alias_text,
        recorded_seq=ctx.latest_seq(),
        sections=sections,
        source_object_id=object_id,
    )


def find_source_version(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    external_id: str,
    version: str,
) -> str | None:
    row = conn.execute(
        """
        SELECT r.object_id FROM revisions r
        WHERE r.project_id = ?
          AND json_extract(r.state_json, '$.external_id') = ?
          AND json_extract(r.state_json, '$.version') = ?
          AND r.record_state != 'tombstoned'
        ORDER BY r.recorded_seq DESC LIMIT 1
        """,
        (project_id, external_id, version),
    ).fetchone()
    return row["object_id"] if row else None


def source_read(
    ctx: ServiceContext,
    *,
    anchor_id: str | None = None,
    source_object_id: str | None = None,
    revision: int | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    max_chars: int = 20000,
) -> dict[str, Any]:
    conn = ctx.conn
    if anchor_id:
        anchor = conn.execute(
            "SELECT * FROM source_anchors WHERE project_id = ? AND anchor_id = ?",
            (ctx.project_id, anchor_id),
        ).fetchone()
        if anchor is None:
            raise not_found("Anchor does not resolve.", anchor_id=anchor_id)
        source_object_id = anchor["source_object_id"]
        revision = anchor["source_revision"]
        locator = json.loads(anchor["locator_json"] or "{}")
        line_start = locator.get("line_start", line_start)
        line_end = locator.get("line_end", line_end)
    if not source_object_id:
        raise schema_validation_failed("source_read requires an anchor or source object.")
    extraction_row = conn.execute(
        """
        SELECT extraction_blob_hash, status FROM source_extractions
        WHERE project_id = ? AND source_object_id = ? AND source_revision = ?
        ORDER BY created_seq DESC LIMIT 1
        """,
        (ctx.project_id, source_object_id, revision or 1),
    ).fetchone()
    if extraction_row is None or not extraction_row["extraction_blob_hash"]:
        raise artifact_unavailable(
            "No frozen extraction text is available for this source.",
            source_object_id=source_object_id,
            status=extraction_row["status"] if extraction_row else "not_extracted",
        )
    text = blobs.read_blob(
        ctx.routing.extractions_dir.parent, extraction_row["extraction_blob_hash"]
    ).decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = (line_start or 1) - 1
    end = line_end if line_end is not None else min(len(lines), start + 400)
    excerpt = "\n".join(lines[start:end])
    truncated = len(excerpt) > max_chars
    if truncated:
        excerpt = excerpt[:max_chars]
    return {
        "source_object_id": source_object_id,
        "revision": revision,
        "line_start": start + 1,
        "line_end": end,
        "excerpt": excerpt,
        "truncated": truncated,
        "extraction_status": extraction_row["status"],
        "total_lines": len(lines),
    }


def find_anchors_for_object(
    conn: sqlite3.Connection, project_id: str, source_object_id: str, revision: int | None = None
) -> list[sqlite3.Row]:
    if revision is None:
        return conn.execute(
            "SELECT * FROM source_anchors WHERE project_id = ? AND source_object_id = ? ORDER BY created_seq",
            (project_id, source_object_id),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM source_anchors WHERE project_id = ? AND source_object_id = ? AND source_revision = ? ORDER BY created_seq",
        (project_id, source_object_id, revision),
    ).fetchall()
