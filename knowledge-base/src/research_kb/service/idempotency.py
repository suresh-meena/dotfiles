from __future__ import annotations

import json
import sqlite3
from typing import Any

from research_kb.domain.canonical import canonical_hash
from research_kb.errors import idempotency_conflict
from research_kb.storage.db import utc_now

_TRANSPORT_FIELDS = frozenset({"transport", "client", "session_id", "trace_id", "timestamp"})


def request_payload_hash(operation: str, payload: dict[str, Any]) -> str:
    normalized = {key: value for key, value in payload.items() if key not in _TRANSPORT_FIELDS}
    return canonical_hash({"operation": operation, "payload": normalized})


def lookup(
    conn: sqlite3.Connection, project_id: str, actor_id: str, request_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM idempotency_records
        WHERE project_id = ? AND actor_id = ? AND request_id = ?
        """,
        (project_id, actor_id, request_id),
    ).fetchone()


def replay_or_conflict(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor_id: str,
    request_id: str,
    operation: str,
    payload_hash: str,
) -> dict[str, Any] | None:
    row = lookup(conn, project_id, actor_id, request_id)
    if row is None:
        return None
    if row["operation"] != operation or row["payload_hash"] != payload_hash:
        raise idempotency_conflict()
    result = json.loads(row["result_json"])
    result["idempotent_replay"] = True
    return result


def record(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor_id: str,
    request_id: str,
    operation: str,
    payload_hash: str,
    result: dict[str, Any],
    commit_seq: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO idempotency_records
          (project_id, actor_id, request_id, operation, payload_hash, result_json, commit_seq, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            actor_id,
            request_id,
            operation,
            payload_hash,
            json.dumps(result, sort_keys=True),
            commit_seq,
            utc_now(),
        ),
    )
