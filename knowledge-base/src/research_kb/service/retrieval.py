from __future__ import annotations

import json
import sqlite3
from typing import Any

from research_kb.domain.vocab import CONTEXT_MODES
from research_kb.errors import cursor_invalid, schema_validation_failed
from research_kb.service import work
from research_kb.service.context import ServiceContext
from research_kb.service.objects import get_object_bundle
from research_kb.service.sources import source_read
from research_kb.storage import repo
from research_kb.storage.db import (
    decode_page_cursor,
    decode_snapshot_cursor,
    encode_page_cursor,
    encode_snapshot_cursor,
    utc_now,
)
from research_kb.storage.search import check_integrity, search as fts_search
from research_kb.version import SCHEMA_VERSION


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def resolve_snapshot(ctx: ServiceContext, as_of_cursor: str | None) -> tuple[int, bool]:
    if as_of_cursor is None:
        return ctx.latest_seq(), False
    epoch, seq = decode_snapshot_cursor(as_of_cursor)
    if epoch != ctx.epoch:
        from research_kb.errors import epoch_changed

        raise epoch_changed(ctx.epoch, epoch)
    return seq, True


def snapshot_block(ctx: ServiceContext, seq: int, historical: bool) -> dict[str, Any]:
    return {
        "cursor": encode_snapshot_cursor(ctx.epoch, seq),
        "historical": historical,
        "as_of_seq": seq,
    }


def resolve_focus_refs(
    ctx: ServiceContext, refs: list[Any], *, as_of_seq: int | None = None
) -> list[dict[str, Any]]:
    from research_kb.service.objects import resolve_object_token

    resolved: list[dict[str, Any]] = []
    for ref in refs:
        if isinstance(ref, str):
            object_id = resolve_object_token(ref, ctx.conn, ctx.project_id, as_of_seq=as_of_seq)
            resolved.append({"object_id": object_id, "revision": None})
        else:
            object_id = resolve_object_token(
                ref["object_id"], ctx.conn, ctx.project_id, as_of_seq=as_of_seq
            )
            resolved.append({"object_id": object_id, "revision": ref.get("revision"), **{
                key: value for key, value in ref.items() if key not in ("object_id", "revision")
            }})
    return resolved


def _effective_visible(row: sqlite3.Row, effective_at: str | None) -> bool:
    if effective_at is None:
        return True
    effective_from = row["effective_from"]
    effective_to = row["effective_to"]
    if effective_from is not None and effective_from > effective_at:
        return False
    if effective_to is not None and effective_to <= effective_at:
        return False
    return True


def record_block(
    ctx: ServiceContext,
    row: sqlite3.Row,
    *,
    role: str,
    as_of_seq: int | None = None,
    effective_at: str | None = None,
) -> dict[str, Any]:
    state = repo.parse_state(row)
    block: dict[str, Any] = {
        "ref": {
            "project_id": ctx.project_id,
            "object_id": row["object_id"],
            "revision": row["revision"],
        },
        "display_id": f"{state.get('alias') or row['object_id'][:8]}@{row['revision']}",
        "role": role,
        "title": row["title"],
        "excerpt": row["body_md"][:1200] if row["body_md"] else row["title"],
        "kind": row["kind"],
        "subkind": row["subkind"],
        "record_state": row["record_state"],
        "review_state": state.get("review_state"),
        "evidence_state": state.get("evidence_state") or (state.get("assessment") or {}).get("evidence_state"),
        "recorded_seq": row["recorded_seq"],
        "recorded_at": row["recorded_at"],
        "effective_from": row["effective_from"],
        "effective_to": row["effective_to"],
    }
    if effective_at and row["effective_from"] is None and row["effective_to"] is None:
        block["effective_unknown"] = True
    citations = ctx.conn.execute(
        "SELECT anchor_id, role FROM citations WHERE project_id = ? AND citing_object_id = ? AND citing_revision = ?",
        (ctx.project_id, row["object_id"], row["revision"]),
    ).fetchall()
    block["citation_ids"] = [citation["anchor_id"] for citation in citations]
    return block


def _blockers_block(ctx: ServiceContext, as_of_seq: int | None) -> list[dict[str, Any]]:
    return repo.open_blockers(ctx.conn, ctx.project_id, as_of_seq=as_of_seq)


def _review_flags_block(
    ctx: ServiceContext, object_id: str, as_of_seq: int | None
) -> list[dict[str, Any]]:
    flags = repo.open_review_flags(ctx.conn, ctx.project_id, target_object_id=object_id)
    return [
        {
            "flag_id": flag["flag_id"],
            "cause_object_id": flag["cause_object_id"],
            "cause_revision": flag["cause_revision"],
            "predicate": flag["predicate"],
            "reason": flag["reason"],
        }
        for flag in flags
        if as_of_seq is None or flag["created_seq"] <= as_of_seq
    ]


def _claim_evidence_records(
    ctx: ServiceContext, object_id: str, as_of_seq: int | None, effective_at: str | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    from research_kb.domain.evidence import dedupe_by_underlying_provenance

    conn = ctx.conn
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    claim_row = (
        repo.visible_revision(conn, ctx.project_id, object_id, as_of_seq)
        if as_of_seq is not None
        else repo.current_revision(conn, ctx.project_id, object_id)
    )
    if claim_row is None:
        return records, ["claim_not_visible_at_snapshot"]
    if not _effective_visible(claim_row, effective_at):
        missing.append("claim_not_applicable_at_effective_at")
    records.append(record_block(ctx, claim_row, role="direct", effective_at=effective_at))
    state = repo.parse_state(claim_row)
    assessment = state.get("assessment") or {}
    if not assessment:
        missing.append("no_evidence_assessment_recorded")
    elif assessment.get("review_state") != "reviewed":
        missing.append("evidence_assessment_not_reviewed")
    for item in assessment.get("missing_checks") or []:
        missing.append(f"missing_check: {item}")
    if claim_row["kind"] == "claim":
        criteria = state.get("evidence_criteria") or []
        if not criteria:
            missing.append("no_evidence_criteria_declared")
    links = repo.list_links(
        conn, project_id=ctx.project_id, object_id=object_id, as_of_seq=as_of_seq
    )
    evidence_blocks: list[dict[str, Any]] = []
    provenance_map = _provenance_identity_map(conn, ctx.project_id, links, object_id)
    for link in links:
        if link["predicate"] not in ("supports", "contradicts", "derived_from", "assumes", "resolves"):
            continue
        other_id = link["src_object_id"] if link["dst_object_id"] == object_id else link["dst_object_id"]
        other_revision = link["src_revision"] if link["dst_object_id"] == object_id else link["dst_revision"]
        if other_revision:
            other = repo.get_revision(conn, ctx.project_id, other_id, other_revision)
        else:
            other = repo.current_revision(conn, ctx.project_id, other_id)
        if not _effective_visible(other, effective_at):
            missing.append(f"evidence_not_applicable_at_effective_at: {other_id}")
            continue
        role = "counterevidence" if link["predicate"] == "contradicts" else "evidence"
        block = record_block(ctx, other, role=role, effective_at=effective_at)
        block["provenance_identity"] = provenance_map.get(
            f"{other['object_id']}:{other['revision']}"
        ) or f"record:{other['object_id']}"
        evidence_blocks.append(block)
    records.extend(dedupe_by_underlying_provenance(evidence_blocks))
    blockers = _blockers_block(ctx, as_of_seq)
    return records, missing


def _interval_matches(effective_from: str | None, effective_to: str | None, effective_at: str) -> bool:
    if effective_from is not None and effective_from > effective_at:
        return False
    if effective_to is not None and effective_to <= effective_at:
        return False
    return True


def _effective_visible_by_state(bundle: dict[str, Any], effective_at: str | None) -> bool:
    if effective_at is None:
        return True
    return _interval_matches(bundle.get("effective_from"), bundle.get("effective_to"), effective_at)


def _provenance_identity_map(conn, project_id: str, links, target_object_id: str) -> dict[str, str]:
    identities: dict[str, str] = {}
    pairs: list[tuple[str, int]] = []
    for link in links:
        if link["predicate"] not in ("supports", "contradicts", "derived_from", "assumes", "resolves"):
            continue
        other_id = link["src_object_id"] if link["dst_object_id"] == target_object_id else link["dst_object_id"]
        other_revision = link["src_revision"] if link["dst_object_id"] == target_object_id else link["dst_revision"]
        if other_revision is None:
            row = repo.object_row_or_none(conn, project_id, other_id)
            if row is None:
                continue
            other_revision = int(row["current_revision"])
        pairs.append((other_id, int(other_revision)))
    if not pairs:
        return identities
    object_ids = sorted({object_id for object_id, _ in pairs})
    placeholders = ",".join("?" for _ in object_ids)
    rows = conn.execute(
        f"""
        SELECT c.citing_object_id, c.citing_revision, a.source_object_id
        FROM citations c
        JOIN source_anchors a ON a.anchor_id = c.anchor_id
        WHERE c.project_id = ? AND c.citing_object_id IN ({placeholders})
        """,
        (project_id, *object_ids),
    ).fetchall()
    for row in rows:
        identities.setdefault(
            f"{row['citing_object_id']}:{row['citing_revision']}",
            f"source:{row['source_object_id']}",
        )
    return identities


def search_candidates(
    ctx: ServiceContext,
    *,
    query: str,
    as_of_seq: int,
    limit: int,
    kind: str | None = None,
    subkind: str | None = None,
    advanced: bool = False,
    after: tuple[float, int, str] | None = None,
    effective_at: str | None = None,
) -> dict[str, Any]:
    from research_kb.retrieval import embeddings

    health = check_integrity(ctx.conn)
    embedding_info: dict[str, Any] = {"enabled": False, "degraded": False, "reason": "disabled_by_policy"}
    if not health["healthy"]:
        return {
            "candidates": [],
            "index_health": health,
            "embedding": embedding_info,
            "warning": "INDEX_DEGRADED: use exact reads or request a rebuild.",
        }
    candidates = fts_search(
        ctx.conn,
        project_id=ctx.project_id,
        query=query,
        as_of_seq=as_of_seq,
        kind=kind,
        subkind=subkind,
        limit=limit,
        advanced=advanced,
        after=after,
        effective_at=effective_at,
    )
    provider, status = embeddings.provider_from_policy(ctx.policy)
    if status != "disabled_by_policy":
        embedding_info = {"enabled": True, "degraded": status != "local_hashing", "reason": status}
        if embedding_info["degraded"]:
            embedding_info["warning"] = f"SEMANTIC_DEGRADED: {status}; lexical results returned."
        else:
            try:
                semantic = embeddings.semantic_search(
                    ctx, query=query, as_of_seq=as_of_seq, limit=limit
                )
                k = int(ctx.policy.get("retrieval", {}).get("rrf_k", 60))
                candidates = embeddings.fuse_with_lexical(candidates, semantic, k=k)
                embedding_info["watermark"] = embeddings.watermark(ctx)
            except Exception as exc:
                embedding_info["degraded"] = True
                embedding_info["warning"] = f"SEMANTIC_DEGRADED: {type(exc).__name__}; lexical results returned."
    return {
        "candidates": candidates,
        "index_health": health,
        "embedding": embedding_info,
        "warning": None,
    }


def context(
    ctx: ServiceContext,
    *,
    mode: str,
    query: str | None = None,
    focus_refs: list[Any] | None = None,
    budget_tokens: int | None = None,
    as_of_cursor: str | None = None,
    limit: int = 40,
    effective_at: str | None = None,
) -> dict[str, Any]:
    if mode not in CONTEXT_MODES:
        raise schema_validation_failed("Unknown context mode.", allowed=list(CONTEXT_MODES))
    as_of_seq, historical = resolve_snapshot(ctx, as_of_cursor)
    budget = budget_tokens or int(
        ctx.policy.get("retrieval", {}).get("focused_budget_tokens", 8000)
        if mode != "lookup"
        else ctx.policy.get("retrieval", {}).get("orientation_budget_tokens", 3000)
    )
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    reasons: list[str] = []
    search_exhaustive = False
    evidence_expansion_complete = True
    blockers = _blockers_block(ctx, as_of_seq if historical else None)
    flags: list[dict[str, Any]] = []
    if mode == "lookup":
        for ref in resolve_focus_refs(ctx, focus_refs or []):
            bundle = get_object_bundle(
                ctx.conn,
                ctx.project_id,
                ref["object_id"],
                revision=ref.get("revision"),
                as_of_seq=as_of_seq if historical and not ref.get("revision") else None,
                include_history=False,
            )
            if not _effective_visible_by_state(bundle, effective_at):
                missing.append(f"not_applicable_at_effective_at: {ref['object_id']}")
            records.append(
                {
                    "ref": {
                        "project_id": ctx.project_id,
                        "object_id": ref["object_id"],
                        "revision": bundle["revision"],
                    },
                    "display_id": f"{ref['object_id'][:8]}@{bundle['revision']}",
                    "role": "direct",
                    "title": bundle["title"],
                    "excerpt": bundle["body_md"][:1200] or bundle["title"],
                    "kind": bundle["kind"],
                    "subkind": bundle["subkind"],
                    "record_state": bundle["record_state"],
                    "review_state": bundle["state_json"].get("review_state"),
                    "effective_from": bundle.get("effective_from"),
                    "effective_to": bundle.get("effective_to"),
                    "citations": bundle["citations"],
                    "links": bundle["links"],
                }
            )
            if (
                effective_at
                and bundle.get("effective_from") is None
                and bundle.get("effective_to") is None
            ):
                records[-1]["effective_unknown"] = True
    elif mode in ("question",):
        if not query:
            raise schema_validation_failed("A question context requires a query or explicit references.")
        result = search_candidates(
            ctx, query=query, as_of_seq=as_of_seq, limit=limit, effective_at=effective_at
        )
        if result.get("warning"):
            reasons.append(result["warning"])
            missing.append("lexical_search_unavailable")
        role_priority = {"search_hit": 0, "evidence": 1, "counterevidence": 2, "direct": 3}
        index: dict[str, int] = {}

        def upsert(block: dict[str, Any]) -> None:
            key = f"{block['ref'].get('object_id')}:{block['ref'].get('revision')}"
            if key in index:
                existing = records[index[key]]
                if role_priority.get(str(block.get("role")), 0) > role_priority.get(str(existing.get("role")), 0):
                    existing["role"] = block["role"]
                return
            index[key] = len(records)
            records.append(block)

        for candidate in result["candidates"]:
            row = repo.get_revision(ctx.conn, ctx.project_id, candidate["object_id"], candidate["revision"])
            if not _effective_visible(row, effective_at):
                continue
            upsert(record_block(ctx, row, role="search_hit", as_of_seq=as_of_seq, effective_at=effective_at))
        if result["candidates"]:
            for candidate in result["candidates"][:10]:
                for link in repo.list_links(
                    ctx.conn, project_id=ctx.project_id, object_id=candidate["object_id"], as_of_seq=as_of_seq
                ):
                    if link["dst_object_id"] != candidate["object_id"]:
                        continue
                    if link["predicate"] not in ("contradicts", "supports", "assumes", "resolves"):
                        continue
                    other = repo.current_revision(ctx.conn, ctx.project_id, link["src_object_id"])
                    if not _effective_visible(other, effective_at):
                        continue
                    role = "counterevidence" if link["predicate"] == "contradicts" else "evidence"
                    upsert(record_block(ctx, other, role=role, as_of_seq=as_of_seq, effective_at=effective_at))
        search_exhaustive = False
        missing.append("approximate_search_not_exhaustive")
    elif mode == "claim_evidence":
        refs = resolve_focus_refs(ctx, focus_refs or [])
        if not refs:
            raise schema_validation_failed("claim_evidence requires a claim/object reference.")
        for ref in refs:
            claim_records, claim_missing = _claim_evidence_records(
                ctx, ref["object_id"], as_of_seq if historical else None, effective_at
            )
            records.extend(claim_records)
            missing.extend(claim_missing)
            flags.extend(_review_flags_block(ctx, ref["object_id"], as_of_seq if historical else None))
    elif mode == "history":
        refs = resolve_focus_refs(ctx, focus_refs or [])
        if not refs:
            raise schema_validation_failed("history requires an object reference.")
        for ref in refs:
            bundle = get_object_bundle(
                ctx.conn,
                ctx.project_id,
                ref["object_id"],
                revision=ref.get("revision"),
                as_of_seq=as_of_seq,
                include_history=True,
            )
            history = bundle["history"]
            if effective_at:
                history = [
                    entry
                    for entry in history
                    if _interval_matches(
                        entry.get("effective_from"), entry.get("effective_to"), effective_at
                    )
                ]
            records.append(
                {
                    "ref": {
                        "project_id": ctx.project_id,
                        "object_id": ref["object_id"],
                        "revision": bundle["revision"],
                    },
                    "role": "historical",
                    "title": bundle["title"],
                    "excerpt": bundle["body_md"][:800],
                    "history": history,
                    "effective_at": effective_at,
                    "links": bundle["links"],
                }
            )
    elif mode == "progress":
        scope = resolve_focus_refs(ctx, focus_refs or [])
        progress = work.project_progress(
            ctx, scope_ref=scope[0] if scope else None, as_of_seq=as_of_seq if historical else None
        )
        records.append(
            {
                "ref": {"project_id": ctx.project_id, "object_id": "project", "revision": None},
                "role": "progress",
                "title": "Work progress",
                "progress": progress,
            }
        )
    elif mode == "next_work":
        recommendations = work.next_work(ctx, as_of_seq=as_of_seq if historical else None, limit=limit or 5)
        records.append(
            {
                "ref": {"project_id": ctx.project_id, "object_id": "project", "revision": None},
                "role": "recommendation",
                "title": "Next work (advisory)",
                "recommendations": recommendations,
            }
        )
    elif mode == "source_read":
        anchor_refs = focus_refs or []
        for ref in anchor_refs:
            if isinstance(ref, dict) and "anchor_id" in ref:
                records.append({"role": "source_span", **source_read(ctx, anchor_id=ref["anchor_id"])})
            elif isinstance(ref, dict) and "object_id" in ref:
                records.append(
                    {
                        "role": "source_span",
                        **source_read(
                            ctx,
                            source_object_id=ref["object_id"],
                            revision=ref.get("revision"),
                            line_start=ref.get("line_start"),
                            line_end=ref.get("line_end"),
                        ),
                    }
                )
            else:
                missing.append(f"unrecognized source_read focus: {ref!r}")
    else:
        reasons.append("mode_not_implemented")
        evidence_expansion_complete = False
    total_tokens = sum(_estimate_tokens(json.dumps(record, default=str)) for record in records)
    truncated = total_tokens > budget
    if truncated:
        reasons.append(f"context_budget_exceeded: needed about {total_tokens} tokens, budget {budget}")
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": ctx.project_id,
        "mode": mode,
        "snapshot": snapshot_block(ctx, as_of_seq, historical),
        "records": records,
        "blockers": blockers,
        "review_flags": flags,
        "missing": sorted(set(missing)),
        "completeness": {
            "exact_state_complete": not historical or True,
            "evidence_expansion_complete": evidence_expansion_complete,
            "search_exhaustive": search_exhaustive,
            "truncated": truncated,
            "reasons": reasons,
        },
        "next_cursor": None,
    }


def search(
    ctx: ServiceContext,
    *,
    query: str,
    as_of_cursor: str | None = None,
    kind: str | None = None,
    subkind: str | None = None,
    limit: int = 50,
    advanced: bool = False,
    page_cursor: str | None = None,
    effective_at: str | None = None,
) -> dict[str, Any]:
    import hashlib

    from research_kb.retrieval import embeddings

    as_of_seq, historical = resolve_snapshot(ctx, as_of_cursor)
    page_size = min(limit, int(ctx.policy.get("limits", {}).get("max_page_size", 200)))
    scope = (
        f"search:{hashlib.sha1(query.encode('utf-8')).hexdigest()[:12]}:"
        f"{kind}:{subkind}:{int(advanced)}:{as_of_seq}"
    )
    last_key = (
        decode_page_cursor(page_cursor, project_id=ctx.project_id, scope=scope)
        if page_cursor
        else None
    )
    provider, status = embeddings.provider_from_policy(ctx.policy)
    fused = status == "local_hashing"
    if fused:
        offset = 0
        if last_key and last_key.startswith("o:"):
            offset = int(last_key.split(":", 1)[1])
        result = search_candidates(
            ctx,
            query=query,
            as_of_seq=as_of_seq,
            limit=offset + page_size + 1,
            kind=kind,
            subkind=subkind,
            advanced=advanced,
            effective_at=effective_at,
        )
        all_candidates = result["candidates"]
        candidates = all_candidates[offset : offset + page_size]
        truncated = len(all_candidates) > offset + page_size
        next_page = (
            encode_page_cursor(ctx.project_id, scope, f"o:{offset + page_size}")
            if truncated
            else None
        )
        pagination_mode = "offset_with_fusion"
    else:
        after = None
        if last_key and last_key.startswith("k:"):
            _, rank, seq, object_id = last_key.split(":", 3)
            after = (float(rank), int(seq), object_id)
        result = search_candidates(
            ctx,
            query=query,
            as_of_seq=as_of_seq,
            limit=page_size + 1,
            kind=kind,
            subkind=subkind,
            advanced=advanced,
            after=after,
            effective_at=effective_at,
        )
        raw_candidates = result["candidates"]
        truncated = len(raw_candidates) > page_size
        candidates = raw_candidates[:page_size]
        next_page = None
        if truncated and candidates:
            last = candidates[-1]
            next_page = encode_page_cursor(
                ctx.project_id,
                scope,
                f"k:{last['rank']}:{last['recorded_seq']}:{last['object_id']}",
            )
        pagination_mode = "lexical_keyset"
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": ctx.project_id,
        "snapshot": snapshot_block(ctx, as_of_seq, historical),
        "query": query,
        "effective_at": effective_at,
        "candidates": candidates,
        "index_health": result["index_health"],
        "embedding": result.get("embedding"),
        "next_page_cursor": next_page,
        "pagination_mode": pagination_mode,
        "completeness": {
            "search_exhaustive": False,
            "truncated": truncated,
            "reasons": [result["warning"]] if result.get("warning") else [],
        },
    }


def changes(
    ctx: ServiceContext,
    *,
    after: str | None,
    page_cursor: str | None = None,
    limit: int = 50,
    acknowledge: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    if after is None and page_cursor is None:
        raise cursor_invalid("changes requires --after or a page cursor.")
    if page_cursor:
        last_key = decode_page_cursor(
            page_cursor, project_id=ctx.project_id, scope=f"changes:{ctx.actor_id}"
        )
        upper_seq = int(last_key.split(":")[0])
        after_seq = int(last_key.split(":")[1])
    else:
        if after is None:
            raise cursor_invalid("changes requires --after or a page cursor.")
        after_epoch, after_seq = decode_snapshot_cursor(after)
        if after_epoch != ctx.epoch:
            from research_kb.errors import epoch_changed

            raise epoch_changed(ctx.epoch, after_epoch)
        upper_seq = ctx.latest_seq()
    from research_kb.service.objects import changed_refs_since

    events, truncated = changed_refs_since(ctx.conn, ctx.project_id, after_seq, upper_seq, limit=limit)
    next_page = None
    if truncated and events:
        next_page = encode_page_cursor(
            ctx.project_id,
            f"changes:{ctx.actor_id}",
            f"{upper_seq}:{events[-1]['seq']}",
        )
    if acknowledge and events:
        consumed = events[-1]["seq"]
        ctx.conn.execute(
            """
            INSERT INTO session_cursors (project_id, principal, session_id, acknowledged_seq, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, principal, session_id) DO UPDATE SET
              acknowledged_seq = MAX(acknowledged_seq, excluded.acknowledged_seq),
              updated_at = excluded.updated_at
            """,
            (ctx.project_id, ctx.actor_id, session_id or ctx.session_id or "default", consumed, utc_now()),
        )
    impacts = ctx.conn.execute(
        """
        SELECT flag_id, target_object_id, target_revision, cause_object_id, cause_revision, predicate, reason, status
        FROM review_flags WHERE project_id = ? AND created_seq > ? AND created_seq <= ?
        ORDER BY flag_id
        """,
        (ctx.project_id, after_seq, upper_seq),
    ).fetchall()
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": ctx.project_id,
        "snapshot": snapshot_block(ctx, upper_seq, False),
        "after_seq": after_seq,
        "events": events,
        "impacts": [dict(flag) for flag in impacts],
        "next_page_cursor": next_page,
        "acknowledged_seq": events[-1]["seq"] if acknowledge and events else None,
        "completeness": {
            "truncated": truncated,
            "reasons": ["page_limit_reached"] if truncated else [],
        },
    }


def status(ctx: ServiceContext, *, scope_ref: dict[str, Any] | None = None) -> dict[str, Any]:
    conn = ctx.conn
    as_of_seq = ctx.latest_seq()
    goals = conn.execute(
        """
        SELECT o.object_id, r.revision, r.title, r.state_json
        FROM objects o JOIN revisions r
          ON r.project_id = o.project_id AND r.object_id = o.object_id AND r.revision = o.current_revision
        WHERE o.project_id = ? AND o.kind = 'work'
          AND json_extract(r.state_json, '$.subkind') IN ('goal', 'milestone')
          AND r.record_state != 'retired'
        ORDER BY o.object_id
        """,
        (ctx.project_id,),
    ).fetchall()
    current_work = conn.execute(
        """
        SELECT o.object_id, r.revision, r.title, r.state_json
        FROM objects o JOIN revisions r
          ON r.project_id = o.project_id AND r.object_id = o.object_id AND r.revision = o.current_revision
        WHERE o.project_id = ? AND o.kind = 'work'
          AND json_extract(r.state_json, '$.work_state') IN ('in_progress', 'in_review')
          AND r.record_state != 'retired'
        ORDER BY o.object_id
        """,
        (ctx.project_id,),
    ).fetchall()
    reviews = conn.execute(
        """
        SELECT o.object_id, r.revision, r.title,
               json_extract(r.state_json, '$.evidence_state') AS evidence_state,
               json_extract(r.state_json, '$.review_state') AS review_state
        FROM objects o JOIN revisions r
          ON r.project_id = o.project_id AND r.object_id = o.object_id AND r.revision = o.current_revision
        WHERE o.project_id = ? AND o.kind IN ('claim', 'knowledge')
          AND json_extract(r.state_json, '$.evidence_state') IN ('contested', 'refuted')
          AND r.record_state = 'active'
        """,
        (ctx.project_id,),
    ).fetchall()
    execution_enabled = bool(ctx.policy.get("execution", {}).get("enabled", False))
    execution_summary = None
    if execution_enabled:
        execution_summary = {
            "prepared": conn.execute(
                "SELECT COUNT(*) AS count FROM dispatch_outbox WHERE project_id = ? AND state = 'pending'",
                (ctx.project_id,),
            ).fetchone()["count"],
            "leases": conn.execute(
                "SELECT COUNT(*) AS count FROM resource_leases WHERE project_id = ? AND state IN ('active', 'quarantined')",
                (ctx.project_id,),
            ).fetchone()["count"],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": ctx.project_id,
        "snapshot": snapshot_block(ctx, as_of_seq, False),
        "goals": [dict(row) for row in goals],
        "current_work": [dict(row) for row in current_work],
        "blockers": repo.open_blockers(conn, ctx.project_id, as_of_seq=as_of_seq),
        "evidence_reviews": [dict(row) for row in reviews],
        "review_flags": [
            dict(flag)
            for flag in repo.open_review_flags(conn, ctx.project_id)
            if flag["created_seq"] <= as_of_seq
        ],
        "execution": execution_summary,
    }
