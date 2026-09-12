from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from dataclasses import dataclass
from typing import Any, Protocol

from research_kb.errors import capability_unavailable
from research_kb.storage.db import utc_now


@dataclass(frozen=True)
class EmbeddingIdentity:
    provider: str
    model_identifier: str
    model_revision: str
    dimensions: int
    normalization: str
    chunker_version: str = "record-v1"


class EmbeddingProvider(Protocol):
    identity: EmbeddingIdentity

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class DisabledProvider:
    identity = EmbeddingIdentity("disabled", "none", "0", 1, "none")

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise capability_unavailable(
            "No embedding provider is enabled.",
            hint="External embeddings require explicit policy authorization; exact and FTS reads remain available.",
        )


class LocalHashingProvider:
    identity = EmbeddingIdentity(
        provider="local_hashing",
        model_identifier="local-hashing",
        model_revision="1",
        dimensions=256,
        normalization="l2",
    )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_embed_text(text, self.identity.dimensions) for text in texts]


def _embed_text(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    tokens = [token for token in text.lower().split() if token]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def provider_from_policy(policy: dict[str, Any]) -> tuple[EmbeddingProvider, str]:
    retrieval = policy.get("retrieval", {})
    if not retrieval.get("embeddings", False):
        return DisabledProvider(), "disabled_by_policy"
    name = retrieval.get("embedding_provider", "local_hashing")
    if name == "local_hashing":
        return LocalHashingProvider(), "local_hashing"
    return DisabledProvider(), f"provider_not_installed:{name}"


def encode_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_vector(payload: bytes) -> list[float]:
    count = len(payload) // 4
    return list(struct.unpack(f"<{count}f", payload))


def _record_text(conn: sqlite3.Connection, project_id: str, object_id: str, revision: int) -> str:
    row = conn.execute(
        """
        SELECT title, body FROM search_documents
        WHERE project_id = ? AND object_id = ? AND revision = ? AND doc_role = 'record'
        LIMIT 1
        """,
        (project_id, object_id, revision),
    ).fetchone()
    if row is None:
        return ""
    return f"{row['title']}\n{row['body']}"


def enqueue_for_revision(ctx: Any, object_id: str, revision: int, projection_hash: str) -> None:
    provider, _status = provider_from_policy(ctx.policy)
    if isinstance(provider, DisabledProvider):
        return
    identity = provider.identity
    ctx.conn.execute(
        """
        INSERT OR IGNORE INTO embedding_outbox
          (project_id, object_id, revision, projection_hash, state, provider, model_identifier,
           created_seq, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            ctx.project_id,
            object_id,
            revision,
            projection_hash,
            identity.provider,
            identity.model_identifier,
            ctx.latest_seq(),
            utc_now(),
            utc_now(),
        ),
    )


def drain(ctx: Any, *, limit: int = 100) -> dict[str, Any]:
    provider, status = provider_from_policy(ctx.policy)
    if isinstance(provider, DisabledProvider):
        raise capability_unavailable(
            "Embedding drain requires an enabled provider.", reason=status
        )
    identity = provider.identity
    jobs = ctx.conn.execute(
        """
        SELECT * FROM embedding_outbox
        WHERE project_id = ? AND state IN ('pending', 'failed')
        ORDER BY job_id LIMIT ?
        """,
        (ctx.project_id, limit),
    ).fetchall()
    done = 0
    failed = 0
    for job in jobs:
        text = _record_text(ctx.conn, ctx.project_id, job["object_id"], job["revision"])
        if not text:
            ctx.conn.execute(
                "UPDATE embedding_outbox SET state = 'dead_letter', attempts = attempts + 1, "
                "last_error = 'no projection text', updated_at = ? WHERE job_id = ?",
                (utc_now(), job["job_id"]),
            )
            failed += 1
            continue
        try:
            vector = provider.embed([text])[0]
            ctx.conn.execute(
                """
                INSERT OR REPLACE INTO embedding_cache
                  (project_id, object_id, revision, projection_hash, model_identifier, model_revision,
                   dimensions, normalization, chunker_version, vector, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ctx.project_id,
                    job["object_id"],
                    job["revision"],
                    job["projection_hash"],
                    identity.model_identifier,
                    identity.model_revision,
                    identity.dimensions,
                    identity.normalization,
                    identity.chunker_version,
                    encode_vector(vector),
                    utc_now(),
                ),
            )
            ctx.conn.execute(
                "UPDATE embedding_outbox SET state = 'done', attempts = attempts + 1, updated_at = ? "
                "WHERE job_id = ?",
                (utc_now(), job["job_id"]),
            )
            done += 1
        except Exception as exc:
            attempts = job["attempts"] + 1
            state = "dead_letter" if attempts >= 5 else "failed"
            ctx.conn.execute(
                "UPDATE embedding_outbox SET state = ?, attempts = ?, last_error = ?, updated_at = ? "
                "WHERE job_id = ?",
                (state, attempts, f"{type(exc).__name__}: {exc}", utc_now(), job["job_id"]),
            )
            failed += 1
    ctx.conn.execute(
        """
        INSERT INTO embedding_watermark (project_id, embedded_seq, provider, model_identifier, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_id) DO UPDATE SET
          embedded_seq = MAX(embedded_seq, excluded.embedded_seq),
          provider = excluded.provider,
          model_identifier = excluded.model_identifier,
          updated_at = excluded.updated_at
        """,
        (ctx.project_id, ctx.latest_seq(), identity.provider, identity.model_identifier, utc_now()),
    )
    return {
        "provider": identity.provider,
        "identity": {
            "model_identifier": identity.model_identifier,
            "model_revision": identity.model_revision,
            "dimensions": identity.dimensions,
            "normalization": identity.normalization,
            "chunker_version": identity.chunker_version,
        },
        "processed": len(jobs),
        "embedded": done,
        "failed": failed,
    }


def watermark(ctx: Any) -> dict[str, Any]:
    row = ctx.conn.execute(
        "SELECT embedded_seq, provider, model_identifier, updated_at FROM embedding_watermark WHERE project_id = ?",
        (ctx.project_id,),
    ).fetchone()
    if row is None:
        return {"embedded_seq": 0, "provider": "disabled", "model_identifier": "none", "degraded": True}
    return {
        "embedded_seq": row["embedded_seq"],
        "provider": row["provider"],
        "model_identifier": row["model_identifier"],
        "updated_at": row["updated_at"],
        "degraded": row["embedded_seq"] < ctx.latest_seq(),
    }


def semantic_search(
    ctx: Any,
    *,
    query: str,
    as_of_seq: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    provider, status = provider_from_policy(ctx.policy)
    if isinstance(provider, DisabledProvider):
        raise capability_unavailable("Semantic retrieval is disabled.", reason=status)
    identity = provider.identity
    query_vector = provider.embed([query])[0]
    rows = ctx.conn.execute(
        """
        SELECT c.object_id, c.revision, c.vector, r.title, r.kind, r.subkind, r.recorded_seq
        FROM embedding_cache c
        JOIN revisions r ON r.project_id = c.project_id AND r.object_id = c.object_id
                        AND r.revision = c.revision
        WHERE c.project_id = ?
          AND c.model_identifier = ? AND c.model_revision = ? AND c.dimensions = ?
          AND c.normalization = ? AND c.chunker_version = ?
          AND r.recorded_seq <= ?
        """,
        (
            ctx.project_id,
            identity.model_identifier,
            identity.model_revision,
            identity.dimensions,
            identity.normalization,
            identity.chunker_version,
            as_of_seq,
        ),
    ).fetchall()
    scored: list[dict[str, Any]] = []
    for row in rows:
        vector = decode_vector(row["vector"])
        score = sum(left * right for left, right in zip(query_vector, vector))
        scored.append(
            {
                "object_id": row["object_id"],
                "revision": row["revision"],
                "title": row["title"],
                "kind": row["kind"],
                "subkind": row["subkind"],
                "recorded_seq": row["recorded_seq"],
                "semantic_score": score,
            }
        )
    scored.sort(key=lambda item: item["semantic_score"], reverse=True)
    return scored[:limit]


def reciprocal_rank_fusion(
    result_lists: list[list[dict[str, Any]]],
    *,
    k: int = 60,
    key_fields: tuple[str, ...] = ("object_id", "revision"),
) -> list[dict[str, Any]]:
    fused: dict[tuple[Any, ...], dict[str, Any]] = {}
    scores: dict[tuple[Any, ...], float] = {}
    for results in result_lists:
        for rank, item in enumerate(results):
            key = tuple(item.get(field) for field in key_fields)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            fused.setdefault(key, dict(item))
    ordered = sorted(fused.values(), key=lambda item: scores[tuple(item.get(f) for f in key_fields)], reverse=True)
    for item in ordered:
        item["fusion_score"] = scores[tuple(item.get(f) for f in key_fields)]
    return ordered


def fuse_with_lexical(
    lexical: list[dict[str, Any]], semantic: list[dict[str, Any]], *, k: int = 60
) -> list[dict[str, Any]]:
    return reciprocal_rank_fusion([lexical, semantic], k=k)
