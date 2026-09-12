from __future__ import annotations

import json
import sqlite3
from typing import Any

from research_kb.errors import not_found
from research_kb.storage.db import new_id, utc_now


def append_intent(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    execution_id: str,
    intent: dict[str, Any],
    seq: int,
) -> str:
    outbox_id = new_id()
    conn.execute(
        """
        INSERT INTO dispatch_outbox
          (outbox_id, project_id, execution_id, intent_json, state, created_seq, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (outbox_id, project_id, execution_id, json.dumps(intent, sort_keys=True), seq, utc_now()),
    )
    return outbox_id


def fetch_intent(conn: sqlite3.Connection, project_id: str, execution_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM dispatch_outbox WHERE project_id = ? AND execution_id = ?",
        (project_id, execution_id),
    ).fetchone()
    if row is None:
        raise not_found("No dispatch intent exists for this execution.", execution_id=execution_id)
    return row


def mark_dispatched(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    execution_id: str,
    dispatched: bool,
    error: str | None = None,
) -> None:
    if dispatched:
        conn.execute(
            """
            UPDATE dispatch_outbox
            SET state = 'dispatched', attempts = attempts + 1, dispatched_at = ?
            WHERE project_id = ? AND execution_id = ? AND state IN ('pending', 'dispatched')
            """,
            (utc_now(), project_id, execution_id),
        )
    else:
        conn.execute(
            """
            UPDATE dispatch_outbox
            SET attempts = attempts + 1, last_error = ?,
                state = CASE WHEN attempts + 1 >= 5 THEN 'dead_letter' ELSE state END
            WHERE project_id = ? AND execution_id = ?
            """,
            (error, project_id, execution_id),
        )


def mark_acknowledged(conn: sqlite3.Connection, *, project_id: str, execution_id: str) -> None:
    conn.execute(
        """
        UPDATE dispatch_outbox SET state = 'acknowledged', acknowledged_at = ?
        WHERE project_id = ? AND execution_id = ?
        """,
        (utc_now(), project_id, execution_id),
    )


def pending_intents(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM dispatch_outbox
        WHERE project_id = ? AND state IN ('pending', 'dispatched')
        ORDER BY created_seq
        """,
        (project_id,),
    ).fetchall()
