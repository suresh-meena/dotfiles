from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from research_kb.domain.canonical import canonical_hash
from research_kb.errors import schema_validation_failed
from research_kb.version import FTS_TOKENIZER, PROJECTION_VERSION

_TOKEN_SPLIT = re.compile(r"\s+")


def escape_fts_query(query: str, *, advanced: bool = False) -> str:
    text = query.strip()
    if not text:
        raise schema_validation_failed("A search query must not be empty.")
    if advanced:
        _validate_advanced(text)
        return text
    tokens = [token for token in _TOKEN_SPLIT.split(text) if token]
    quoted = []
    for token in tokens:
        safe = token.replace('"', '""')
        quoted.append(f'"{safe}"')
    return " AND ".join(quoted)


def _validate_advanced(text: str) -> None:
    if len(text) > 500:
        raise schema_validation_failed("Advanced FTS queries are limited to 500 characters.")
    forbidden = (";" , "--", "/*", "*/")
    for marker in forbidden:
        if marker in text:
            raise schema_validation_failed("Disallowed sequence in advanced FTS query.", marker=marker)


def projection_hash(title: str, body: str, alias_text: str, sections: list[dict[str, Any]]) -> str:
    return canonical_hash(
        {
            "projection_version": PROJECTION_VERSION,
            "title": title,
            "body": body,
            "alias_text": alias_text,
            "sections": sections,
        }
    )


def project_revision(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    object_id: str,
    revision: int,
    title: str,
    body_md: str,
    alias_text: str,
    recorded_seq: int,
    sections: list[dict[str, Any]] | None = None,
    source_object_id: str | None = None,
    extra_text: str = "",
) -> str:
    digest = projection_hash(title, body_md, alias_text, sections or [])
    searchable_body = body_md
    if extra_text:
        searchable_body = f"{body_md}\n{extra_text}" if body_md else extra_text
    conn.execute(
        "DELETE FROM search_documents WHERE project_id = ? AND object_id = ? AND revision = ?",
        (project_id, object_id, revision),
    )
    conn.execute(
        """
        INSERT INTO search_documents
          (project_id, object_id, revision, doc_role, section_key, title, body, alias_text,
           anchor_id, source_object_id, recorded_seq, projection_version, projection_hash)
        VALUES (?, ?, ?, 'record', '', ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            project_id,
            object_id,
            revision,
            title,
            searchable_body,
            alias_text,
            source_object_id,
            recorded_seq,
            PROJECTION_VERSION,
            digest,
        ),
    )
    for section in sections or []:
        conn.execute(
            """
            INSERT INTO search_documents
              (project_id, object_id, revision, doc_role, section_key, title, body, alias_text,
               anchor_id, source_object_id, recorded_seq, projection_version, projection_hash)
            VALUES (?, ?, ?, 'section', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                object_id,
                revision,
                str(section.get("section_key", ""))[:200],
                str(section.get("title", "")),
                str(section.get("body", "")),
                str(section.get("alias_text", "")),
                section.get("anchor_id"),
                source_object_id,
                recorded_seq,
                PROJECTION_VERSION,
                digest,
            ),
        )
    return digest


def rebuild(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO search_fts(search_fts) VALUES('rebuild')")


def rebuild_projections(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    section_resolver: Any = None,
) -> dict[str, Any]:
    from research_kb.domain.searchtext import semantic_text

    revisions = conn.execute(
        """
        SELECT r.project_id, r.object_id, r.revision, r.kind, r.title, r.body_md, r.state_json, r.recorded_seq
        FROM revisions r WHERE r.project_id = ? ORDER BY r.object_id, r.revision
        """,
        (project_id,),
    ).fetchall()
    aliases: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT object_id, alias_text FROM aliases WHERE project_id = ? AND retired_seq IS NULL",
        (project_id,),
    ):
        aliases.setdefault(row["object_id"], []).append(row["alias_text"])
    conn.execute("DELETE FROM search_documents WHERE project_id = ?", (project_id,))
    sections_count = 0
    for row in revisions:
        state: dict[str, Any] = {}
        try:
            state = json.loads(row["state_json"] or "{}")
        except ValueError:
            state = {}
        sections: list[dict[str, Any]] = []
        if section_resolver is not None and row["kind"] == "source":
            sections = section_resolver(project_id, row["object_id"], row["revision"]) or []
            sections_count += len(sections)
        project_revision(
            conn,
            project_id=project_id,
            object_id=row["object_id"],
            revision=row["revision"],
            title=row["title"],
            body_md=row["body_md"],
            alias_text=" ".join(aliases.get(row["object_id"], [])),
            recorded_seq=row["recorded_seq"],
            sections=sections,
            extra_text=semantic_text(state, row["body_md"], row["title"]),
        )
    rebuild(conn)
    return {
        "revisions_projected": len(revisions),
        "sections_projected": sections_count,
        "documents": conn.execute(
            "SELECT COUNT(*) AS c FROM search_documents WHERE project_id = ?", (project_id,)
        ).fetchone()["c"],
    }


def check_integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    documents = conn.execute("SELECT COUNT(*) AS count FROM search_documents").fetchone()["count"]
    indexed = conn.execute("SELECT COUNT(*) AS count FROM search_fts").fetchone()["count"]
    healthy = True
    detail = ""
    try:
        conn.execute("INSERT INTO search_fts(search_fts) VALUES('integrity-check')")
    except sqlite3.DatabaseError as exc:
        healthy = False
        detail = str(exc)
    return {
        "documents": int(documents),
        "indexed": int(indexed),
        "counts_match": int(documents) == int(indexed),
        "healthy": healthy and int(documents) == int(indexed),
        "detail": detail,
        "tokenizer": FTS_TOKENIZER,
    }


def _fts_rank_expression() -> str:
    return "bm25(search_fts, 8.0, 1.0, 3.0)"


def search(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    query: str,
    as_of_seq: int,
    kind: str | None = None,
    subkind: str | None = None,
    record_state: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
    advanced: bool = False,
    after: tuple[float, int, str] | None = None,
    effective_at: str | None = None,
) -> list[dict[str, Any]]:
    match = escape_fts_query(query, advanced=advanced)
    clauses = ["search_fts MATCH ?", "d.project_id = ?", "d.recorded_seq <= ?"]
    params: list[Any] = [match, project_id, as_of_seq]
    if effective_at:
        clauses.append("(r.effective_from IS NULL OR r.effective_from <= ?)")
        clauses.append("(r.effective_to IS NULL OR r.effective_to > ?)")
        params.extend([effective_at, effective_at])
    if kind:
        clauses.append("r.kind = ?")
        params.append(kind)
    if subkind:
        clauses.append("r.subkind = ?")
        params.append(subkind)
    if record_state:
        clauses.append("r.record_state = ?")
        params.append(record_state)
    if review_state:
        clauses.append("json_extract(r.state_json, '$.review_state') = ?")
        params.append(review_state)
    outer_clauses: list[str] = []
    if after is not None:
        after_rank, after_seq, after_object = after
        outer_clauses.append(
            "(rank > ? OR (rank = ? AND (recorded_seq < ? OR (recorded_seq = ? AND object_id > ?))))"
        )
        params.extend([after_rank, after_rank, after_seq, after_seq, after_object])
    params.append(limit)
    outer_where = f"WHERE {' AND '.join(outer_clauses)}" if outer_clauses else ""
    sql = f"""
        SELECT * FROM (
            SELECT d.doc_id, d.object_id, d.revision, d.doc_role, d.section_key, d.title,
                   d.anchor_id, d.recorded_seq, r.kind, r.subkind, r.record_state,
                   {_fts_rank_expression()} AS rank
            FROM search_fts
            JOIN search_documents d ON d.doc_id = search_fts.rowid
            JOIN revisions r ON r.project_id = d.project_id
                            AND r.object_id = d.object_id
                            AND r.revision = d.revision
            WHERE {' AND '.join(clauses)}
        )
        {outer_where}
        ORDER BY rank ASC, recorded_seq DESC, object_id ASC
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        candidate = dict(row)
        key = f"{candidate['object_id']}:{candidate['revision']}"
        if key not in best or candidate["rank"] < best[key]["rank"]:
            best[key] = candidate
    ordered = sorted(best.values(), key=lambda item: (item["rank"], -int(item["recorded_seq"])))
    return ordered
