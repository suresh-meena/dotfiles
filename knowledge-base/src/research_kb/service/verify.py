from __future__ import annotations

import hashlib
from typing import Any

from research_kb.service.context import ServiceContext
from research_kb.storage import blobs, migrations
from research_kb.storage.search import check_integrity


def _finding(check: str, status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"check": check, "status": status, "message": message, "details": details}


def verify(
    ctx: ServiceContext,
    *,
    checks: list[str] | None = None,
    scope_ref: str | None = None,
    sample: int = 50,
) -> dict[str, Any]:
    wanted = set(checks or ["integrity", "references", "citations", "readiness", "index", "provenance"])
    findings: list[dict[str, Any]] = []
    conn = ctx.conn
    if "integrity" in wanted:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        findings.append(
            _finding("integrity", "ok" if integrity == "ok" else "error", str(integrity))
        )
        try:
            migrations.verify_checksums(conn)
            findings.append(_finding("migrations", "ok", "Applied migration checksums match."))
        except Exception as exc:
            findings.append(_finding("migrations", "error", str(exc)))
    if "references" in wanted:
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            for violation in violations[:sample]:
                findings.append(
                    _finding(
                        "foreign_keys",
                        "error",
                        "Foreign key violation.",
                        table=violation[0],
                        rowid=violation[1],
                    )
                )
        else:
            findings.append(_finding("foreign_keys", "ok", "No foreign key violations."))
        dangling = conn.execute(
            """
            SELECT COUNT(*) AS count FROM link_revisions l
            WHERE l.pin_mode = 'pinned' AND (l.src_revision IS NULL OR l.dst_revision IS NULL)
            """
        ).fetchone()["count"]
        findings.append(
            _finding(
                "pinned_links",
                "ok" if dangling == 0 else "error",
                "Pinned links must carry both endpoint revisions.",
                violations=dangling,
            )
        )
    if "citations" in wanted:
        scope_clause = ""
        params: list[Any] = [ctx.project_id]
        if scope_ref:
            scope_clause = " AND c.citing_object_id = ?"
            params.append(scope_ref)
        rows = conn.execute(
            f"""
            SELECT c.citation_id, c.citing_object_id, c.citing_revision, a.anchor_id, a.excerpt,
                   a.excerpt_hash, a.source_object_id, a.source_revision, a.status
            FROM citations c JOIN source_anchors a ON a.anchor_id = c.anchor_id
            WHERE c.project_id = ?{scope_clause}
            ORDER BY c.citation_id LIMIT ?
            """,
            [*params, sample],
        ).fetchall()
        broken = 0
        for row in rows:
            source = conn.execute(
                "SELECT 1 FROM revisions WHERE project_id = ? AND object_id = ? AND revision = ?",
                (ctx.project_id, row["source_object_id"], row["source_revision"]),
            ).fetchone()
            if source is None:
                broken += 1
                findings.append(
                    _finding(
                        "citation_resolution",
                        "error",
                        "Citation anchor points at a nonexistent source revision.",
                        citation_id=row["citation_id"],
                    )
                )
            if row["excerpt"] is not None and row["excerpt_hash"]:
                computed = hashlib.sha256(row["excerpt"].encode("utf-8")).hexdigest()
                if computed != row["excerpt_hash"]:
                    broken += 1
                    findings.append(
                        _finding(
                            "citation_excerpt_hash",
                            "error",
                            "Excerpt hash does not match the retained excerpt.",
                            anchor_id=row["anchor_id"],
                        )
                    )
        if broken == 0:
            findings.append(
                _finding(
                    "citation_resolution",
                    "ok",
                    "Sampled citations resolve to source revisions with intact excerpt hashes.",
                    sampled=len(rows),
                )
            )
    if "readiness" in wanted:
        from research_kb.service.readiness import evaluate_target, readiness_summary

        if scope_ref:
            detail = evaluate_target(ctx, scope_ref)
            findings.append(
                _finding(
                    "readiness",
                    "ok" if detail["ready"] else "warning",
                    "Target readiness under recorded criteria, evidence, and blockers.",
                    **detail,
                )
            )
        else:
            summary = readiness_summary(ctx)
            findings.append(
                _finding(
                    "readiness",
                    "warning" if summary["not_ready"] else "ok",
                    "Claim readiness summary; this check never clears blockers or reviews automatically.",
                    **summary,
                )
            )
    if "index" in wanted:
        health = check_integrity(conn)
        findings.append(
            _finding(
                "search_index",
                "ok" if health["healthy"] else "error",
                "FTS projection consistency.",
                **health,
            )
        )
    if "provenance" in wanted:
        incomplete = conn.execute(
            """
            SELECT COUNT(*) AS count FROM run_attempts
            WHERE project_id = ? AND (manifest_json IS NULL OR manifest_json = '{}')
            """,
            (ctx.project_id,),
        ).fetchone()["count"]
        findings.append(
            _finding(
                "execution_provenance",
                "ok" if incomplete == 0 else "warning",
                "Run attempts with empty manifests.",
                incomplete=incomplete,
            )
        )
    if "blobs" in wanted:
        missing = 0
        checked = 0
        for row in conn.execute(
            "SELECT blob_hash FROM blob_registry ORDER BY blob_hash LIMIT ?", (sample,)
        ):
            checked += 1
            digest = row["blob_hash"]
            source_path = blobs.blob_path(ctx.routing.sources_dir.parent, digest)
            extraction_path = blobs.blob_path(ctx.routing.extractions_dir.parent, digest)
            if not source_path.exists() and not extraction_path.exists():
                missing += 1
                findings.append(
                    _finding("blob_availability", "warning", "Blob bytes are not present locally.", blob=digest)
                )
        findings.append(
            _finding(
                "blob_inventory",
                "ok" if missing == 0 else "warning",
                "Sampled blob presence (metadata-only registrations are expected to be absent).",
                checked=checked,
                missing=missing,
            )
        )
    errors = [finding for finding in findings if finding["status"] == "error"]
    warnings = [finding for finding in findings if finding["status"] == "warning"]
    return {
        "schema_version": "1.0",
        "project_id": ctx.project_id,
        "scope": scope_ref,
        "findings": findings,
        "summary": {
            "errors": len(errors),
            "warnings": len(warnings),
            "ok": len(findings) - len(errors) - len(warnings),
            "no_silent_fixes": True,
        },
    }
