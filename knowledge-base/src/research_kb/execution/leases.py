from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from research_kb.errors import blocked, not_found, schema_validation_failed
from research_kb.storage.db import new_id, utc_now

DEFAULT_LEASE_TTL_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(UTC)


def resource_row(conn: sqlite3.Connection, project_id: str, resource_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM resources WHERE project_id = ? AND resource_id = ?",
        (project_id, resource_id),
    ).fetchone()
    if row is None:
        raise not_found("Resource does not resolve.", resource_id=resource_id)
    return row


def active_or_quarantined(conn: sqlite3.Connection, resource_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM resource_leases
        WHERE resource_id = ? AND state IN ('active', 'quarantined')
        ORDER BY generation
        """,
        (resource_id,),
    ).fetchall()


def acquire_lease(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    resource_id: str,
    execution_id: str,
    seq: int,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> dict[str, Any]:
    resource = resource_row(conn, project_id, resource_id)
    for lease in active_or_quarantined(conn, resource_id):
        if lease["owner_execution_id"] == execution_id:
            return {
                "lease_id": lease["lease_id"],
                "generation": lease["generation"],
                "state": lease["state"],
                "reused": True,
            }
    capacity_raw = resource["capacity"]
    if isinstance(capacity_raw, dict):
        capacity = int(capacity_raw.get("units", 1))
    else:
        capacity = int(capacity_raw)
    held = active_or_quarantined(conn, resource_id)
    if len(held) >= capacity:
        raise blocked(
            "No resource capacity is available; uncertain allocations stay quarantined until termination is verified.",
            resource_id=resource_id,
            capacity=capacity,
            held=len(held),
        )
    generation_row = conn.execute(
        "SELECT COALESCE(MAX(generation), 0) + 1 AS next FROM resource_leases WHERE resource_id = ?",
        (resource_id,),
    ).fetchone()
    generation = int(generation_row["next"])
    lease_id = new_id()
    expires_at = (_now() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn.execute(
        """
        INSERT INTO resource_leases
          (lease_id, project_id, resource_id, generation, owner_execution_id, state,
           acquired_at, expires_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        (lease_id, project_id, resource_id, generation, execution_id, utc_now(), expires_at),
    )
    return {"lease_id": lease_id, "generation": generation, "state": "active", "expires_at": expires_at, "reused": False}


def quarantine_expired(conn: sqlite3.Connection, project_id: str, *, reason: str = "lease expired without termination evidence") -> list[str]:
    now = utc_now()
    rows = conn.execute(
        """
        SELECT lease_id FROM resource_leases
        WHERE project_id = ? AND state = 'active' AND expires_at < ?
        """,
        (project_id, now),
    ).fetchall()
    lease_ids = [row["lease_id"] for row in rows]
    for lease_id in lease_ids:
        conn.execute(
            """
            UPDATE resource_leases SET state = 'quarantined', quarantine_reason = ?
            WHERE lease_id = ? AND state = 'active'
            """,
            (reason, lease_id),
        )
    return lease_ids


def release_lease(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    lease_id: str,
    seq: int,
    verified_termination: bool,
    reason: str = "",
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM resource_leases WHERE project_id = ? AND lease_id = ?",
        (project_id, lease_id),
    ).fetchone()
    if row is None:
        raise not_found("Lease does not resolve.", lease_id=lease_id)
    if not verified_termination:
        conn.execute(
            """
            UPDATE resource_leases SET state = 'quarantined', quarantine_reason = ?
            WHERE lease_id = ?
            """,
            (reason or "termination not verified; allocation retained in quarantine", lease_id),
        )
        return {"lease_id": lease_id, "state": "quarantined", "released": False}
    conn.execute(
        "UPDATE resource_leases SET state = 'released', released_seq = ? WHERE lease_id = ?",
        (seq, lease_id),
    )
    return {"lease_id": lease_id, "state": "released", "released": True}


def assert_generation_current(conn: sqlite3.Connection, lease_id: str, generation: int) -> None:
    row = conn.execute(
        "SELECT generation, state FROM resource_leases WHERE lease_id = ?", (lease_id,)
    ).fetchone()
    if row is None:
        raise schema_validation_failed("Lease does not resolve for fencing check.", lease_id=lease_id)
    if int(row["generation"]) != int(generation):
        raise blocked(
            "Stale lease generation rejected by fencing check.",
            lease_id=lease_id,
            submitted_generation=generation,
            current_generation=row["generation"],
        )
    if row["state"] not in ("active", "quarantined"):
        raise blocked("Lease is not in a fenced state.", lease_id=lease_id, state=row["state"])


def leases_for_execution(conn: sqlite3.Connection, project_id: str, execution_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM resource_leases WHERE project_id = ? AND owner_execution_id = ?",
        (project_id, execution_id),
    ).fetchall()
