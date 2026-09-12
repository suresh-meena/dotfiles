from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from research_kb.errors import storage_failure

LEGACY_MAPPING: list[dict[str, str]] = [
    {
        "legacy": "knowledge_items",
        "handling": "Map subkind and preserve text/attribution; separate todo/work records and link them where needed.",
    },
    {
        "legacy": "mutable experiment with spec_version",
        "handling": (
            "Recover real historical specifications from preserved snapshots when available; otherwise "
            "record an explicitly unreconstructed legacy reference and block unsupported critical use."
        ),
    },
    {
        "legacy": "scope_type/scope_id",
        "handling": "Resolve actual objects and create typed links; leave unresolved references in a draft/import report.",
    },
    {
        "legacy": "source_refs_json",
        "handling": "Convert verified references to pinned links/citations; do not invent versions or page anchors.",
    },
    {
        "legacy": "incomparable validity",
        "handling": (
            "Preserve the legacy judgment; create a target-specific comparison assessment only when its target is known."
        ),
    },
    {
        "legacy": "status fields",
        "handling": "Map through an explicit vocabulary table and report unmappable states.",
    },
    {
        "legacy": "timestamps",
        "handling": "Preserve original reported timestamps separately from new import-recording time.",
    },
    {
        "legacy": "audit diffs",
        "handling": "Import as historical evidence; do not pretend they reconstruct missing full states.",
    },
    {
        "legacy": "artifact URIs",
        "handling": "Register locations with unverified identity until manifests/checksums are established.",
    },
]


def inventory(database: str | Path) -> dict[str, Any]:
    path = Path(database)
    if not path.is_file():
        raise storage_failure(f"Legacy database not found: {path}")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        inventory_rows = []
        for table in tables:
            quoted = table.replace('"', '""')
            count = conn.execute(f'SELECT COUNT(*) AS count FROM "{quoted}"').fetchone()["count"]
            columns = [row["name"] for row in conn.execute(f'PRAGMA table_info("{quoted}")')]
            inventory_rows.append({"table": table, "rows": int(count), "columns": columns})
        return {
            "database": str(path),
            "read_only": True,
            "tables": inventory_rows,
            "mapping": LEGACY_MAPPING,
            "note": (
                "Inventory only. No v2 data is modified or imported automatically; preserve a consistent "
                "backup, then map identities explicitly. State the earliest point for which full historical "
                "reconstruction is supported."
            ),
        }
    finally:
        conn.close()
