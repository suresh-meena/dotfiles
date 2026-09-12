from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from research_kb.errors import storage_failure, unsupported_version
from research_kb.service.context import ServiceContext
from research_kb.storage import migrations
from research_kb.storage.db import connect, new_id, utc_now, write_tx
from research_kb.version import RUNTIME_VERSION, SCHEMA_VERSION


def _copy_tree(source: Path, destination: Path) -> int:
    if not source.exists():
        return 0
    count = 0
    for item in source.rglob("*"):
        if item.is_file():
            target = destination / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            count += 1
    return count


def create_backup(ctx: ServiceContext, *, destination: str | Path | None = None, tag: str = "manual") -> dict[str, Any]:
    timestamp = utc_now().replace(":", "").replace("-", "").replace(".", "")
    dest = Path(destination) if destination else ctx.routing.backups_dir / f"backup-{timestamp}-{tag}"
    dest.mkdir(parents=True, exist_ok=True)
    db_target = dest / "research.db"
    source = connect(ctx.routing.db_path, read_only=True)
    try:
        target = sqlite3.connect(str(db_target))
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            target.close()
    finally:
        source.close()
    blob_count = _copy_tree(ctx.routing.sources_dir.parent, dest / "sources")
    extraction_count = _copy_tree(ctx.routing.extractions_dir.parent, dest / "extractions")
    policy_source = ctx.routing.state_dir / "policy.json"
    if policy_source.exists():
        shutil.copy2(policy_source, dest / "policy.json")
    manifest = {
        "backup_schema": "1.0",
        "created_at": utc_now(),
        "tag": tag,
        "project_id": ctx.project_id,
        "controller_epoch": ctx.epoch,
        "runtime_version": RUNTIME_VERSION,
        "database_schema_version": SCHEMA_VERSION,
        "migration_checksum": migrations.schema_checksum(),
        "policy_revision": ctx.policy_revision,
        "blob_files": blob_count + extraction_count,
        "note": (
            "Backups include the database, required source/extraction blobs, and policy profile. "
            "Keep an off-device encrypted copy; backup hashes must be verified periodically."
        ),
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"backup_dir": str(dest), "manifest": manifest}


def maybe_backup_after_critical(ctx: ServiceContext, operations: list[dict[str, Any]]) -> dict[str, Any] | None:
    from research_kb.domain.transition_registry import get_operation

    if not any(get_operation(operation["op"]).high_risk for operation in operations):
        return None
    return create_backup(ctx, tag="after-critical-approval")


def restore_backup(
    *,
    source: str | Path,
    destination: str | Path,
    verify: bool = True,
) -> dict[str, Any]:
    src = Path(source)
    dest = Path(destination)
    manifest_path = src / "manifest.json"
    if not (src / "research.db").exists():
        raise storage_failure(f"Backup database is missing: {src / 'research.db'}")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "research.db", dest / "research.db")
    if (src / "policy.json").exists():
        shutil.copy2(src / "policy.json", dest / "policy.json")
    _copy_tree(src / "sources", dest / "sources")
    _copy_tree(src / "extractions", dest / "extractions")
    flags = {
        "mode": "read_only_reconciliation",
        "dispatch_enabled": False,
        "auto_approval_enabled": False,
        "lease_reuse_enabled": False,
        "destructive_maintenance_enabled": False,
        "restored_at": utc_now(),
        "source_backup": str(src),
    }
    conn = connect(dest / "research.db")
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise storage_failure("Restored database failed integrity_check.", result=integrity)
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise storage_failure("Restored database has foreign key violations.", violations=len(foreign_keys))
        applied = migrations.applied_versions(conn)
        known = {migration.version for migration in migrations.discover()}
        if set(applied) - known:
            raise unsupported_version(
                "Backup schema is newer than this runtime.",
                database=list(applied),
                runtime=sorted(known),
            )
        with write_tx(conn):
            new_epoch = new_id()
            conn.execute(
                "INSERT INTO controller_meta (key, value) VALUES ('controller_epoch', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (new_epoch,),
            )
            conn.execute(
                "INSERT INTO controller_meta (key, value) VALUES ('restore_mode', 'read_only_reconciliation') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (),
            )
            conn.execute(
                "INSERT INTO controller_meta (key, value) VALUES ('dispatch_enabled', 'false') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (),
            )
            conn.execute(
                "INSERT INTO controller_meta (key, value) VALUES ('restored_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (utc_now(),),
            )
            conn.execute(
                """
                UPDATE resource_leases SET state = 'quarantined',
                  quarantine_reason = 'restored backup: physical state unknown; verify before reuse'
                WHERE state IN ('active', 'expired')
                """
            )
            conn.execute(
                """
                UPDATE dispatch_outbox SET state = 'dead_letter',
                  last_error = 'restored backup: old intents must not launch duplicate jobs'
                WHERE state IN ('pending', 'dispatched')
                """
            )
            conn.execute(
                """
                INSERT INTO commit_events
                  (project_id, actor_id, request_id, action, reason, recorded_at, policy_revision, epoch, changed_json)
                SELECT project_id,
                       (SELECT actor_id FROM actors LIMIT 1),
                       NULL, 'maintenance', 'Restore completed into read-only reconciliation mode.',
                       ?, NULL, ?, '[]'
                FROM projects
                """
                ,
                (utc_now(), new_epoch),
            )
        flags["new_controller_epoch"] = new_epoch
        if verify:
            checks = {
                "integrity": integrity,
                "foreign_keys": len(foreign_keys),
                "citations": conn.execute("SELECT COUNT(*) AS c FROM citations").fetchone()["c"],
                "revisions": conn.execute("SELECT COUNT(*) AS c FROM revisions").fetchone()["c"],
            }
        else:
            checks = {}
    finally:
        conn.close()
    (dest / "restore.json").write_text(json.dumps(flags, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = {
        "restored_to": str(dest),
        "flags": flags,
        "verification": checks,
        "operator_review_required": True,
        "note": (
            "A restored database passing integrity_check does not prove external artifacts or physical job state match it."
        ),
    }
    if manifest_path.exists():
        result["backup_manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
    return result
