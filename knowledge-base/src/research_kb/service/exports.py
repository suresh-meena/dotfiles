from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_kb.domain.vocab import GENERATED_NOTICE
from research_kb.errors import schema_validation_failed
from research_kb.service.context import ServiceContext
from research_kb.storage.db import encode_snapshot_cursor, utc_now

EXPORT_SCHEMA_VERSION = "1.0"


def _snapshot(ctx: ServiceContext, as_of_cursor: str | None) -> tuple[str, int]:
    from research_kb.service.retrieval import resolve_snapshot

    seq, _ = resolve_snapshot(ctx, as_of_cursor)
    return encode_snapshot_cursor(ctx.epoch, seq), seq


def _header(ctx: ServiceContext, title: str, cursor: str) -> str:
    return (
        f"<!-- {GENERATED_NOTICE} -->\n"
        f"# {title}\n\n"
        f"- Project: `{ctx.project_id}`\n"
        f"- Snapshot cursor: `{cursor}`\n"
        f"- Export schema: `{EXPORT_SCHEMA_VERSION}`\n"
        f"- Generated at: {utc_now()}\n\n"
        "This file is a generated view. Edit records through the runtime, not this file.\n\n"
    )


def export_markdown(ctx: ServiceContext, *, as_of_cursor: str | None = None, sections: list[str] | None = None) -> dict[str, Any]:
    cursor, seq = _snapshot(ctx, as_of_cursor)
    wanted = set(sections or ["project_map", "topic_index", "work_view", "evidence_report"])
    parts: list[str] = []
    if "project_map" in wanted:
        parts.append(_project_map(ctx, seq))
    if "topic_index" in wanted:
        parts.append(_topic_index(ctx, seq))
    if "work_view" in wanted:
        parts.append(_work_view(ctx, seq))
    if "evidence_report" in wanted:
        parts.append(_evidence_report(ctx, seq))
    body = _header(ctx, "Research knowledge base export", cursor) + "\n\n".join(parts)
    return {
        "format": "markdown",
        "content": body,
        "cursor": cursor,
        "sections": sorted(wanted),
    }


def _project_map(ctx: ServiceContext, seq: int) -> str:
    conn = ctx.conn
    lines = ["## Project map", ""]
    project = conn.execute(
        """
        SELECT o.object_id, r.revision, r.title, r.state_json
        FROM objects o JOIN revisions r
          ON r.project_id = o.project_id AND r.object_id = o.object_id AND r.revision = o.current_revision
        WHERE o.project_id = ? AND o.kind = 'project'
        """,
        (ctx.project_id,),
    ).fetchall()
    for row in project:
        state = json.loads(row["state_json"] or "{}")
        lines.append(f"### {row['title']} (`{row['object_id'][:8]}@{row['revision']}`)")
        lines.append("")
        lines.append(f"- Scope: {state.get('scope') or 'unrecorded'}")
        objectives = state.get("objectives") or []
        if objectives:
            lines.append("- Objectives:")
            for objective in objectives:
                lines.append(f"  - {objective}")
        lines.append("")
    counts = conn.execute(
        """
        SELECT r.kind, r.subkind, COUNT(*) AS count
        FROM objects o JOIN revisions r
          ON r.project_id = o.project_id AND r.object_id = o.object_id AND r.revision = o.current_revision
        WHERE o.project_id = ? AND r.recorded_seq <= ?
        GROUP BY r.kind, r.subkind ORDER BY r.kind, r.subkind
        """,
        (ctx.project_id, seq),
    ).fetchall()
    lines.append("| kind | subkind | count |")
    lines.append("|---|---|---|")
    for row in counts:
        lines.append(f"| {row['kind']} | {row['subkind']} | {row['count']} |")
    lines.append("")
    return "\n".join(lines)


def _topic_index(ctx: ServiceContext, seq: int) -> str:
    conn = ctx.conn
    rows = conn.execute(
        """
        SELECT o.object_id, r.revision, r.kind, r.subkind, r.title, r.recorded_seq
        FROM objects o JOIN revisions r
          ON r.project_id = o.project_id AND r.object_id = o.object_id AND r.revision = o.current_revision
        WHERE o.project_id = ? AND r.recorded_seq <= ? AND r.record_state != 'tombstoned'
        ORDER BY r.kind, r.subkind, r.title
        """,
        (ctx.project_id, seq),
    ).fetchall()
    lines = ["## Topic index", ""]
    current = None
    for row in rows:
        group = f"{row['kind']}/{row['subkind']}"
        if group != current:
            current = group
            lines.append(f"### {group}")
            lines.append("")
        lines.append(f"- {row['title']} (`{row['object_id'][:8]}@{row['revision']}`)")
    lines.append("")
    return "\n".join(lines)


def _work_view(ctx: ServiceContext, seq: int) -> str:
    from research_kb.service.work import project_progress

    progress = project_progress(ctx, as_of_seq=seq)
    lines = ["## Work view", "", f"Counts: `{json.dumps(progress['counts'], sort_keys=True)}`", ""]
    lines.append(f"Criteria satisfied: {progress['criteria']['numerator']}/{progress['criteria']['denominator']}")
    lines.append("")
    lines.append("| work | state | criteria |")
    lines.append("|---|---|---|")
    for item in progress["items"]:
        lines.append(f"| {item['title']} | {item['work_state']} | {item['satisfied']}/{item['criteria']} |")
    lines.append("")
    return "\n".join(lines)


def _evidence_report(ctx: ServiceContext, seq: int) -> str:
    conn = ctx.conn
    rows = conn.execute(
        """
        SELECT o.object_id, r.revision, r.kind, r.subkind, r.title, r.state_json
        FROM objects o JOIN revisions r
          ON r.project_id = o.project_id AND r.object_id = o.object_id AND r.revision = o.current_revision
        WHERE o.project_id = ? AND r.recorded_seq <= ? AND o.kind IN ('claim', 'knowledge')
          AND r.record_state = 'active'
        ORDER BY r.kind, o.object_id
        """,
        (ctx.project_id, seq),
    ).fetchall()
    lines = ["## Evidence report", ""]
    for row in rows:
        state = json.loads(row["state_json"] or "{}")
        assessment = state.get("assessment") or {}
        lines.append(f"### {row['title']} (`{row['object_id'][:8]}@{row['revision']}`)")
        lines.append("")
        lines.append(f"- Kind: {row['kind']}/{row['subkind']}")
        lines.append(f"- Evidence state: {assessment.get('evidence_state') or state.get('evidence_state') or 'untested'}")
        lines.append(f"- Review state: {assessment.get('review_state') or state.get('review_state') or 'unreviewed'}")
        if assessment.get("rationale"):
            lines.append(f"- Assessment rationale: {assessment['rationale']}")
        for check in assessment.get("missing_checks") or []:
            lines.append(f"- Missing check: {check}")
        citations = conn.execute(
            """
            SELECT c.role, a.anchor_kind, a.locator_json, a.excerpt
            FROM citations c JOIN source_anchors a ON a.anchor_id = c.anchor_id
            WHERE c.project_id = ? AND c.citing_object_id = ? AND c.citing_revision = ?
            """,
            (ctx.project_id, row["object_id"], row["revision"]),
        ).fetchall()
        for citation in citations:
            locator = json.loads(citation["locator_json"] or "{}")
            lines.append(f"- Citation ({citation['role']}, {citation['anchor_kind']}): {locator}")
        lines.append("")
    return "\n".join(lines)


def _missing_blobs(ctx: ServiceContext) -> list[dict[str, Any]]:
    from research_kb.storage import blobs

    missing: list[dict[str, Any]] = []
    for row in ctx.conn.execute("SELECT blob_hash, byte_size FROM blob_registry ORDER BY blob_hash"):
        digest = row["blob_hash"]
        source_present = blobs.blob_path(ctx.routing.sources_dir.parent, digest).exists()
        extraction_present = blobs.blob_path(ctx.routing.extractions_dir.parent, digest).exists()
        if not source_present and not extraction_present:
            missing.append({"blob": digest, "byte_size": row["byte_size"], "status": "missing"})
    return missing


def export_jsonl(ctx: ServiceContext, *, as_of_cursor: str | None = None) -> dict[str, Any]:
    cursor, seq = _snapshot(ctx, as_of_cursor)
    conn = ctx.conn
    lines: list[str] = []
    omissions: list[dict[str, Any]] = []
    meta = {
        "type": "export_manifest",
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "project_id": ctx.project_id,
        "cursor": cursor,
        "controller_epoch": ctx.epoch,
        "runtime_version": __import__("research_kb.version", fromlist=["RUNTIME_VERSION"]).RUNTIME_VERSION,
        "generated_at": utc_now(),
        "notice": GENERATED_NOTICE,
    }
    lines.append(json.dumps(meta, sort_keys=True))
    for row in conn.execute(
        "SELECT * FROM revisions WHERE project_id = ? AND recorded_seq <= ? ORDER BY object_id, revision",
        (ctx.project_id, seq),
    ):
        lines.append(json.dumps({"type": "revision", **dict(row)}, sort_keys=True))
    for row in conn.execute(
        """
        SELECT l.* FROM link_revisions l
        JOIN revisions r ON r.project_id = l.project_id AND r.object_id = l.object_id AND r.revision = l.revision
        WHERE l.project_id = ? AND r.recorded_seq <= ?
        ORDER BY l.object_id
        """,
        (ctx.project_id, seq),
    ):
        lines.append(json.dumps({"type": "link_revision", **dict(row)}, sort_keys=True))
    for row in conn.execute(
        """
        SELECT c.* FROM citations c
        JOIN revisions r ON r.project_id = c.project_id AND r.object_id = c.citing_object_id
                        AND r.revision = c.citing_revision
        WHERE c.project_id = ? AND r.recorded_seq <= ?
        ORDER BY c.citation_id
        """,
        (ctx.project_id, seq),
    ):
        lines.append(json.dumps({"type": "citation", **dict(row)}, sort_keys=True))
    for row in conn.execute(
        "SELECT * FROM source_extractions WHERE project_id = ? AND created_seq <= ? ORDER BY extraction_id",
        (ctx.project_id, seq),
    ):
        payload = dict(row)
        if payload.get("extraction_blob_hash"):
            omissions.append({"extraction_id": payload["extraction_id"], "blob_omitted": True})
            payload["extraction_blob_hash_omitted"] = True
        lines.append(json.dumps({"type": "source_extraction", **payload}, sort_keys=True))
    for row in conn.execute(
        "SELECT * FROM source_anchors WHERE project_id = ? AND created_seq <= ? ORDER BY anchor_id",
        (ctx.project_id, seq),
    ):
        lines.append(json.dumps({"type": "source_anchor", **dict(row)}, sort_keys=True))
    for row in conn.execute(
        "SELECT * FROM policy_revisions WHERE project_id = ? AND accepted_seq <= ? ORDER BY accepted_seq",
        (ctx.project_id, seq),
    ):
        lines.append(json.dumps({"type": "policy_revision", **dict(row)}, sort_keys=True))
    for row in conn.execute(
        "SELECT proposal_id, proposal_hash, status, policy_revision, created_at FROM proposals WHERE project_id = ? AND created_seq <= ?",
        (ctx.project_id, seq),
    ):
        lines.append(json.dumps({"type": "proposal", **dict(row)}, sort_keys=True))
    for row in conn.execute(
        "SELECT attempt_id, run_object_id, run_revision, slot_id, attempt_no, execution_id, manifest_hash FROM run_attempts WHERE project_id = ? AND created_seq <= ?",
        (ctx.project_id, seq),
    ):
        lines.append(json.dumps({"type": "run_attempt", **dict(row)}, sort_keys=True))
    for row in conn.execute("SELECT blob_hash, byte_size, media_type, assurance FROM blob_registry ORDER BY blob_hash"):
        lines.append(json.dumps({"type": "blob_registry", **dict(row)}, sort_keys=True))
    manifest = {
        "type": "export_omissions",
        "omitted_blob_payloads": omissions,
        "missing_registered_blobs": _missing_blobs(ctx),
        "note": "Markdown alone is not a complete backup; JSONL omits large blob bytes but lists identities.",
    }
    lines.append(json.dumps(manifest, sort_keys=True))
    return {
        "format": "jsonl",
        "content": "\n".join(lines) + "\n",
        "cursor": cursor,
        "omissions": omissions,
    }


def export_rocrate(ctx: ServiceContext, *, as_of_cursor: str | None = None) -> dict[str, Any]:
    cursor, seq = _snapshot(ctx, as_of_cursor)
    conn = ctx.conn
    entities: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT o.object_id, r.revision, r.kind, r.subkind, r.title, r.content_hash
        FROM objects o JOIN revisions r
          ON r.project_id = o.project_id AND r.object_id = o.object_id AND r.revision = o.current_revision
        WHERE o.project_id = ? AND r.recorded_seq <= ?
        ORDER BY o.object_id
        """,
        (ctx.project_id, seq),
    ):
        entities.append(
            {
                "@id": f"#/{row['kind']}/{row['object_id']}",
                "@type": "CreativeWork",
                "name": row["title"],
                "identifier": row["object_id"],
                "version": row["revision"],
                "sha256": row["content_hash"],
                "additionalType": f"{row['kind']}/{row['subkind']}",
            }
        )
    return {
        "@context": "https://w3id.org/ro/crate/1.2/context",
        "@graph": [
            {
                "@id": "ro-crate-metadata.json",
                "@type": "CreativeWork",
                "about": {"@id": "./"},
                "conformsTo": {"@id": "https://w3id.org/ro/crate/1.2"},
            },
            {
                "@id": "./",
                "@type": "Dataset",
                "name": "Research KB export",
                "identifier": ctx.project_id,
                "hasPart": [{"@id": entity["@id"]} for entity in entities],
            },
            *entities,
        ],
        "cursor": cursor,
        "notice": GENERATED_NOTICE,
    }


def write_export(ctx: ServiceContext, destination: str | Path, result: dict[str, Any]) -> dict[str, Any]:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    if result["format"] == "markdown":
        path.write_text(result["content"], encoding="utf-8")
    elif result["format"] == "jsonl":
        path.write_text(result["content"], encoding="utf-8")
    elif result["format"] == "rocrate":
        path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        raise schema_validation_failed("Unknown export format.", format=result["format"])
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "format": result["format"],
        "cursor": result["cursor"],
    }
