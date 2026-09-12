from __future__ import annotations

import json
import sqlite3
from typing import Any

from research_kb.domain.vocab import CAPABILITY_GROUPS, CHANGE_ACTIONS, ROLE_CAPABILITIES
from research_kb.errors import not_found, reference_unresolved
from research_kb.storage.db import utc_now


def parse_state(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    raw = row["state_json"] if isinstance(row, sqlite3.Row) else row.get("state_json", "{}")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def normalize_ref(project_id: str, ref: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(ref, str):
        return {"project_id": project_id, "object_id": ref, "revision": None}
    ref_project = ref.get("project_id") or project_id
    if ref_project != project_id:
        from research_kb.errors import project_mismatch

        raise project_mismatch("A reference points at another project.", project_id, str(ref_project))
    return {
        "project_id": project_id,
        "object_id": ref["object_id"],
        "revision": ref.get("revision"),
    }


def latest_seq(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS seq FROM commit_events WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return int(row["seq"])


def append_commit_event(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor_id: str,
    action: str,
    epoch: str,
    reason: str | None = None,
    request_id: str | None = None,
    policy_revision: str | None = None,
    changed: list[dict[str, Any]] | None = None,
    recorded_at: str | None = None,
) -> int:
    if action not in CHANGE_ACTIONS:
        from research_kb.errors import schema_validation_failed

        raise schema_validation_failed("Unknown commit action.", action=action, allowed=list(CHANGE_ACTIONS))
    cursor = conn.execute(
        """
        INSERT INTO commit_events
          (project_id, actor_id, request_id, action, reason, recorded_at, policy_revision, epoch, changed_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            actor_id,
            request_id,
            action,
            reason,
            recorded_at or utc_now(),
            policy_revision,
            epoch,
            json.dumps(changed or [], sort_keys=True),
        ),
    )
    return int(cursor.lastrowid or 0)


def get_actor(conn: sqlite3.Connection, actor_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM actors WHERE actor_id = ?", (actor_id,)).fetchone()
    if row is None:
        raise not_found("The authenticated actor is not registered.", actor_id=actor_id)
    return row


def capabilities_for(actor_row: sqlite3.Row) -> set[str]:
    capabilities = set(json.loads(actor_row["capabilities_json"] or "[]"))
    for role in json.loads(actor_row["roles_json"] or "[]"):
        capabilities.update(ROLE_CAPABILITIES.get(role, ()))
    return capabilities


ACTOR_KINDS = ("human", "agent", "service", "imported_source_author")


def ensure_actor(
    conn: sqlite3.Connection,
    actor_id: str,
    *,
    kind: str = "human",
    display_name: str | None = None,
    roles: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> sqlite3.Row:
    if kind not in ACTOR_KINDS:
        from research_kb.errors import schema_validation_failed

        raise schema_validation_failed("Unknown actor kind.", kind=kind, allowed=list(ACTOR_KINDS))
    for role in roles or []:
        if role not in CAPABILITY_GROUPS:
            from research_kb.errors import schema_validation_failed

            raise schema_validation_failed(
                "Unknown capability role.", role=role, allowed=list(CAPABILITY_GROUPS)
            )
    row = conn.execute("SELECT * FROM actors WHERE actor_id = ?", (actor_id,)).fetchone()
    if row is not None:
        return row
    conn.execute(
        """
        INSERT INTO actors (actor_id, kind, display_name, roles_json, capabilities_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            actor_id,
            kind,
            display_name or actor_id,
            json.dumps(roles or ["reader"]),
            json.dumps(capabilities or []),
            utc_now(),
        ),
    )
    return get_actor(conn, actor_id)


def object_row_or_none(conn: sqlite3.Connection, project_id: str, object_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM objects WHERE project_id = ? AND object_id = ?",
        (project_id, object_id),
    ).fetchone()


def get_revision(
    conn: sqlite3.Connection,
    project_id: str,
    object_id: str,
    revision: int,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM revisions WHERE project_id = ? AND object_id = ? AND revision = ?",
        (project_id, object_id, revision),
    ).fetchone()
    if row is None:
        raise not_found(
            "The requested object revision does not exist.",
            object_id=object_id,
            revision=revision,
        )
    return row


def visible_revision(
    conn: sqlite3.Connection,
    project_id: str,
    object_id: str,
    as_of_seq: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM revisions
        WHERE project_id = ? AND object_id = ? AND recorded_seq <= ?
        ORDER BY revision DESC
        LIMIT 1
        """,
        (project_id, object_id, as_of_seq),
    ).fetchone()


def current_revision(conn: sqlite3.Connection, project_id: str, object_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT r.* FROM objects o
        JOIN revisions r ON r.project_id = o.project_id AND r.object_id = o.object_id
                         AND r.revision = o.current_revision
        WHERE o.project_id = ? AND o.object_id = ?
        """,
        (project_id, object_id),
    ).fetchone()
    if row is None:
        raise not_found("The object has no current revision.", object_id=object_id)
    return row


def resolve_alias(
    conn: sqlite3.Connection, project_id: str, normalized: str, *, as_of_seq: int | None = None
) -> list[sqlite3.Row]:
    sql = """
        SELECT a.alias_text, a.namespace, a.object_id, o.kind
        FROM aliases a
        JOIN objects o ON o.project_id = a.project_id AND o.object_id = a.object_id
        WHERE a.project_id = ? AND a.alias_norm = ? AND a.retired_seq IS NULL
    """
    params: list[Any] = [project_id, normalized]
    if as_of_seq is not None:
        sql += " AND a.created_seq <= ?"
        params.append(as_of_seq)
    return conn.execute(sql, params).fetchall()


def aliases_for(conn: sqlite3.Connection, project_id: str, object_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT alias_text FROM aliases
        WHERE project_id = ? AND object_id = ? AND retired_seq IS NULL
        ORDER BY alias_norm
        """,
        (project_id, object_id),
    ).fetchall()
    return [row["alias_text"] for row in rows]


def add_alias(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    object_id: str,
    alias_text: str,
    namespace: str,
    seq: int,
) -> None:
    from research_kb.domain.canonical import normalize_alias

    conn.execute(
        """
        INSERT OR IGNORE INTO aliases (project_id, namespace, alias_text, alias_norm, object_id, created_seq)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, namespace, alias_text, normalize_alias(alias_text), object_id, seq),
    )


def resolve_ref(
    conn: sqlite3.Connection,
    project_id: str,
    ref: dict[str, Any] | str,
    *,
    as_of_seq: int | None = None,
    require_revision: bool = False,
) -> sqlite3.Row:
    normalized = normalize_ref(project_id, ref)
    object_id = normalized["object_id"]
    revision = normalized["revision"]
    object_row = object_row_or_none(conn, project_id, object_id)
    if object_row is None:
        raise reference_unresolved("Referenced object does not exist in this project.", object_id=object_id)
    if revision is not None:
        row = get_revision(conn, project_id, object_id, int(revision))
        if as_of_seq is not None and row["recorded_seq"] > as_of_seq:
            raise reference_unresolved(
                "Referenced revision was not yet visible at the requested snapshot.",
                object_id=object_id,
                revision=revision,
            )
        return row
    if require_revision:
        raise reference_unresolved(
            "This reference requires an exact positive revision.",
            object_id=object_id,
        )
    if as_of_seq is not None:
        row = visible_revision(conn, project_id, object_id, as_of_seq)
        if row is None:
            raise reference_unresolved(
                "No revision of the referenced object is visible at the requested snapshot.",
                object_id=object_id,
            )
        return row
    return current_revision(conn, project_id, object_id)


def list_links(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    object_id: str | None = None,
    predicate: str | None = None,
    direction: str | None = None,
    as_of_seq: int | None = None,
    include_retired: bool = False,
) -> list[sqlite3.Row]:
    clauses = ["l.project_id = ?"]
    params: list[Any] = [project_id]
    if object_id is not None:
        if direction == "out":
            clauses.append("l.src_object_id = ?")
            params.append(object_id)
        elif direction == "in":
            clauses.append("l.dst_object_id = ?")
            params.append(object_id)
        else:
            clauses.append("(l.src_object_id = ? OR l.dst_object_id = ?)")
            params.extend([object_id, object_id])
    if predicate:
        clauses.append("l.predicate = ?")
        params.append(predicate)
    as_of = as_of_seq if as_of_seq is not None else latest_seq(conn, project_id)
    clauses.append(
        "l.revision = (SELECT MAX(l2.revision) FROM link_revisions l2 WHERE l2.project_id = l.project_id "
        "AND l2.object_id = l.object_id AND l2.revision <= ("
        "SELECT MAX(r.revision) FROM revisions r WHERE r.project_id = l.project_id AND r.object_id = l.object_id "
        "AND r.recorded_seq <= ?))"
    )
    params.append(as_of)
    if not include_retired:
        clauses.append(
            "(SELECT r.record_state FROM revisions r WHERE r.project_id = l.project_id "
            "AND r.object_id = l.object_id AND r.revision = l.revision) != 'retired'"
        )
    sql = f"""
        SELECT l.*, r.record_state, r.recorded_seq, r.title, r.state_json
        FROM link_revisions l
        JOIN revisions r ON r.project_id = l.project_id AND r.object_id = l.object_id
                        AND r.revision = l.revision
        WHERE {' AND '.join(clauses)}
        ORDER BY l.predicate, l.object_id
    """
    return conn.execute(sql, params).fetchall()


def links_many(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    object_ids: list[str],
    predicate: str | None = None,
    direction: str | None = None,
    as_of_seq: int | None = None,
) -> dict[str, list[sqlite3.Row]]:
    if not object_ids:
        return {}
    column = "src_object_id" if direction == "out" else "dst_object_id"
    placeholders = ",".join("?" for _ in object_ids)
    clauses = [f"l.{column} IN ({placeholders})", "l.project_id = ?"]
    params: list[Any] = [*object_ids, project_id]
    if predicate:
        clauses.append("l.predicate = ?")
        params.append(predicate)
    as_of = as_of_seq if as_of_seq is not None else latest_seq(conn, project_id)
    clauses.append(
        "l.revision = (SELECT MAX(l2.revision) FROM link_revisions l2 WHERE l2.project_id = l.project_id "
        "AND l2.object_id = l.object_id AND l2.revision <= ("
        "SELECT MAX(r.revision) FROM revisions r WHERE r.project_id = l.project_id AND r.object_id = l.object_id "
        "AND r.recorded_seq <= ?))"
    )
    params.append(as_of)
    clauses.append(
        "(SELECT r.record_state FROM revisions r WHERE r.project_id = l.project_id "
        "AND r.object_id = l.object_id AND r.revision = l.revision) != 'retired'"
    )
    rows = conn.execute(
        f"""
        SELECT l.*, r.record_state, r.recorded_seq, r.title, r.state_json
        FROM link_revisions l
        JOIN revisions r ON r.project_id = l.project_id AND r.object_id = l.object_id
                        AND r.revision = l.revision
        WHERE {' AND '.join(clauses)}
        ORDER BY l.predicate, l.object_id
        """,
        params,
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row[column], []).append(row)
    return grouped


def revisions_many(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    object_ids: list[str],
    as_of_seq: int | None = None,
) -> dict[str, sqlite3.Row]:
    if not object_ids:
        return {}
    placeholders = ",".join("?" for _ in object_ids)
    as_of = as_of_seq if as_of_seq is not None else latest_seq(conn, project_id)
    rows = conn.execute(
        f"""
        SELECT r.* FROM revisions r
        WHERE r.project_id = ? AND r.object_id IN ({placeholders})
          AND r.recorded_seq <= ?
          AND r.revision = (
            SELECT MAX(r2.revision) FROM revisions r2
            WHERE r2.project_id = r.project_id AND r2.object_id = r.object_id AND r2.recorded_seq <= ?
          )
        """,
        (project_id, *object_ids, as_of, as_of),
    ).fetchall()
    return {row["object_id"]: row for row in rows}


def open_review_flags(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    target_object_id: str | None = None,
    statuses: tuple[str, ...] = ("open",),
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in statuses)
    clauses = [f"status IN ({placeholders})"]
    params: list[Any] = [project_id, *statuses]
    if target_object_id:
        clauses.append("target_object_id = ?")
        params.append(target_object_id)
    return conn.execute(
        f"SELECT * FROM review_flags WHERE project_id = ? AND {' AND '.join(clauses)} ORDER BY flag_id",
        params,
    ).fetchall()


def open_blockers(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    as_of_seq: int | None = None,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT o.object_id, r.revision, r.title, r.state_json
        FROM objects o
        JOIN revisions r ON r.project_id = o.project_id AND r.object_id = o.object_id
                        AND r.revision = o.current_revision
        WHERE o.project_id = ? AND o.kind = 'knowledge'
          AND json_extract(r.state_json, '$.subkind') IN ('issue', 'caveat')
          AND json_extract(r.state_json, '$.status') = 'open'
          AND r.record_state = 'active'
        ORDER BY o.object_id
        """,
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def revision_diff(
    conn: sqlite3.Connection,
    project_id: str,
    object_id: str,
    old_revision: int,
    new_revision: int,
) -> dict[str, Any]:
    old = conn.execute(
        "SELECT title, body_md, record_state, state_json, content_hash FROM revisions "
        "WHERE project_id = ? AND object_id = ? AND revision = ?",
        (project_id, object_id, old_revision),
    ).fetchone()
    new = conn.execute(
        "SELECT title, body_md, record_state, state_json, content_hash FROM revisions "
        "WHERE project_id = ? AND object_id = ? AND revision = ?",
        (project_id, object_id, new_revision),
    ).fetchone()
    if old is None or new is None:
        return {}
    diff: dict[str, Any] = {}
    for field in ("title", "record_state", "content_hash"):
        if old[field] != new[field]:
            diff[field] = {"from": old[field], "to": new[field]}
    if old["body_md"] != new["body_md"]:
        diff["body_md"] = {
            "from_length": len(old["body_md"]),
            "to_length": len(new["body_md"]),
            "changed": True,
        }
    if old["state_json"] != new["state_json"]:
        try:
            old_state = json.loads(old["state_json"] or "{}")
            new_state = json.loads(new["state_json"] or "{}")
            changed_keys = sorted(
                key
                for key in set(old_state) | set(new_state)
                if old_state.get(key) != new_state.get(key)
            )
        except ValueError:
            changed_keys = ["<unparseable>"]
        diff["state_json"] = {"changed_keys": changed_keys}
    return diff


def insert_object_head(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    object_id: str,
    kind: str,
    record_state: str,
    content_hash: str,
    seq: int,
    recorded_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO objects
          (project_id, object_id, kind, current_revision, record_state, created_seq, created_at,
           recorded_seq, recorded_at, content_hash)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            object_id,
            kind,
            record_state,
            seq,
            recorded_at,
            seq,
            recorded_at,
            content_hash,
        ),
    )


def insert_object_revision(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    object_id: str,
    kind: str,
    subkind: str,
    revision: int,
    title: str,
    body_md: str,
    record_state: str,
    state_json: str,
    content_hash: str,
    commit_seq: int,
    actor_id: str,
    attribution_json: str,
    recorded_at: str,
    recorded_seq: int,
    occurred_at: str | None,
    effective_from: str | None,
    effective_to: str | None,
    schema_version: str,
) -> None:
    conn.execute(
        """
        INSERT INTO revisions
          (project_id, object_id, revision, kind, subkind, schema_version, title, body_md, record_state,
           state_json, recorded_seq, recorded_at, occurred_at, effective_from, effective_to,
           content_hash, commit_seq, actor_id, attribution_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            object_id,
            revision,
            kind,
            subkind,
            schema_version,
            title,
            body_md,
            record_state,
            state_json,
            recorded_seq,
            recorded_at,
            occurred_at,
            effective_from,
            effective_to,
            content_hash,
            commit_seq,
            actor_id,
            attribution_json,
        ),
    )


def update_head(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    object_id: str,
    revision: int,
    record_state: str,
    content_hash: str,
    recorded_seq: int,
    recorded_at: str,
) -> None:
    conn.execute(
        """
        UPDATE objects
        SET current_revision = ?, record_state = ?, content_hash = ?, recorded_seq = ?, recorded_at = ?
        WHERE project_id = ? AND object_id = ?
        """,
        (revision, record_state, content_hash, recorded_seq, recorded_at, project_id, object_id),
    )
