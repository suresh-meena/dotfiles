from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from research_kb.errors import storage_failure
from research_kb.storage import blobs


def verify_backup(directory: str | Path, *, sample_blobs: int = 20) -> dict[str, Any]:
    root = Path(directory)
    findings: list[dict[str, Any]] = []
    db_path = root / "research.db"
    manifest_path = root / "manifest.json"
    if not db_path.is_file():
        raise storage_failure(f"Backup database missing: {db_path}")
    if not manifest_path.is_file():
        findings.append({"check": "manifest", "status": "warning", "message": "manifest.json is missing."})
        manifest = {}
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        findings.append({"check": "manifest", "status": "ok", "message": "Backup manifest present."})
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        findings.append(
            {
                "check": "integrity",
                "status": "ok" if integrity == "ok" else "error",
                "message": str(integrity),
            }
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        findings.append(
            {
                "check": "foreign_keys",
                "status": "ok" if not violations else "error",
                "message": f"{len(violations)} violations",
            }
        )
        revisions = conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        citations = conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
        findings.append(
            {
                "check": "counts",
                "status": "ok",
                "message": "Revision and citation counts read.",
                "revisions": int(revisions),
                "citations": int(citations),
            }
        )
    finally:
        conn.close()
    checked = 0
    missing = 0
    for tree in (root / "sources", root / "extractions"):
        for candidate in sorted(tree.rglob("*"))[:sample_blobs]:
            if not candidate.is_file() or len(candidate.name) != 64:
                continue
            checked += 1
            if not blobs.verify_blob(tree, candidate.name):
                missing += 1
    findings.append(
        {
            "check": "blob_hashes",
            "status": "ok" if missing == 0 else "error",
            "message": f"{checked} sampled blob(s) verified; {missing} mismatch(es).",
        }
    )
    errors = [finding for finding in findings if finding["status"] == "error"]
    return {
        "backup_dir": str(root),
        "manifest": manifest,
        "findings": findings,
        "ok": not errors,
        "note": (
            "A passing verification proves the copied database and sampled blobs are consistent. It does "
            "not prove external artifacts at referenced locations still exist."
        ),
    }
