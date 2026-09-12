from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from research_kb.domain.canonical import content_hash_for_revision
from research_kb.domain.relation_registry import (
    get_predicate,
    is_acyclic,
    tracking_allowed,
    validate_endpoints,
)
from research_kb.domain.schemas import validate_record_payload
from research_kb.domain.vocab import (
    CITATION_ROLES,
    RECORD_KINDS,
    RECORD_STATES,
    SUBGENRES,
)
from research_kb.errors import (
    reference_unresolved,
    revision_conflict,
    schema_validation_failed,
)
from research_kb.service.context import ServiceContext
from research_kb.storage import repo
from research_kb.storage.db import new_id, utc_now
from research_kb.storage.search import project_revision
from research_kb.version import RECORD_SCHEMA_VERSION

from research_kb.domain.relation_registry import impact_predicates

IMPACT_INCOMING = impact_predicates("incoming")
IMPACT_OUTGOING = impact_predicates("outgoing")

_UNSET = object()


@dataclass
class Mutation:
    ctx: ServiceContext
    commit_seq: int
    recorded_at: str = ""
    request_id: str | None = None
    reason: str | None = None
    dry_run: bool = False

    @property
    def project_id(self) -> str:
        return self.ctx.project_id

    @property
    def actor_id(self) -> str:
        return self.ctx.actor_id

    @property
    def epoch(self) -> str:
        return self.ctx.epoch

    def new_id(self) -> str:
        return new_id()



def validate_subkind(kind: str, subkind: str) -> None:
    if kind not in RECORD_KINDS:
        raise schema_validation_failed("Unknown record kind.", kind=kind, allowed=list(RECORD_KINDS))
    allowed = SUBGENRES.get(kind)
    if allowed is None:
        raise schema_validation_failed("Unknown record kind.", kind=kind, allowed=sorted(SUBGENRES))
    if subkind not in allowed:
        raise schema_validation_failed(
            "Unknown subkind for this record kind.",
            kind=kind,
            subkind=subkind,
            allowed=list(allowed),
        )


_FORBIDDEN_ATTRIBUTION_FIELDS = frozenset(
    {"actor_id", "created_by", "recorded_by", "reviewed_by", "reviewer", "id", "revision"}
)
_ALLOWED_ATTRIBUTION_FIELDS = frozenset(
    {
        "original_speaker",
        "provenance_category",
        "extraction_agent",
        "source_author",
        "imported_from",
        "notes",
        "session_id",
    }
)


def _validate_attribution(attribution: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(attribution or {})
    claimed = _FORBIDDEN_ATTRIBUTION_FIELDS.intersection(data)
    if claimed:
        raise schema_validation_failed(
            "Attribution fields cannot claim authenticated identity or revision.",
            forbidden=sorted(claimed),
        )
    unknown = set(data) - _ALLOWED_ATTRIBUTION_FIELDS
    if unknown:
        raise schema_validation_failed(
            "Unknown attribution fields.",
            unknown=sorted(unknown),
            allowed=sorted(_ALLOWED_ATTRIBUTION_FIELDS),
        )
    return data


def _validate_timestamp(value: str | None, field: str) -> None:
    if value is None:
        return
    text = value.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise schema_validation_failed(f"{field} must be an ISO-8601 timestamp.", value=value) from exc


def _coerce_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise schema_validation_failed("Expected an ISO-8601 timestamp string or null.", value=repr(value))


def searchable_extra_text(state: dict[str, Any], body_md: str, title: str) -> str:
    from research_kb.domain.searchtext import semantic_text

    return semantic_text(state, body_md, title)


def _upsert_resource(
    conn: sqlite3.Connection,
    project_id: str,
    object_id: str,
    subkind: str,
    state: dict[str, Any],
) -> None:
    capacity = state.get("capacity")
    if isinstance(capacity, dict):
        capacity_value = int(capacity.get("units", 1))
    elif isinstance(capacity, int):
        capacity_value = capacity
    else:
        capacity_value = 1
    parent_ref = state.get("parent_resource_ref") or {}
    parent_id = parent_ref.get("object_id") if isinstance(parent_ref, dict) else None
    existing = conn.execute("SELECT resource_id FROM resources WHERE resource_id = ?", (object_id,)).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO resources
              (resource_id, project_id, resource_kind, machine_id, display_name, capabilities_json,
               capacity, admin_state, limitations_json, parent_resource_id, physical_identity,
               allocation_domain, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                object_id,
                project_id,
                subkind,
                state.get("machine_id", object_id),
                state.get("display_name"),
                json.dumps(state.get("capabilities") or {}, sort_keys=True),
                capacity_value,
                state.get("admin_state", "unknown"),
                json.dumps(state.get("limitations") or []),
                parent_id,
                state.get("physical_identity"),
                state.get("allocation_domain"),
                utc_now(),
            ),
        )
    else:
        conn.execute(
            """
            UPDATE resources
            SET resource_kind = ?, machine_id = ?, capabilities_json = ?, capacity = ?,
                admin_state = ?, limitations_json = ?, parent_resource_id = ?,
                physical_identity = ?, allocation_domain = ?
            WHERE resource_id = ?
            """,
            (
                subkind,
                state.get("machine_id", object_id),
                json.dumps(state.get("capabilities") or {}, sort_keys=True),
                capacity_value,
                state.get("admin_state", "unknown"),
                json.dumps(state.get("limitations") or []),
                parent_id,
                state.get("physical_identity"),
                state.get("allocation_domain"),
                object_id,
            ),
        )


def _resolve_citation_anchor(conn: sqlite3.Connection, project_id: str, anchor_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM source_anchors WHERE project_id = ? AND anchor_id = ?",
        (project_id, anchor_id),
    ).fetchone()
    if row is None:
        raise reference_unresolved("Citation anchor does not resolve in this project.", anchor_id=anchor_id)
    return row


def create_object(
    mutation: Mutation,
    *,
    kind: str,
    subkind: str,
    title: str,
    state_json: dict[str, Any],
    body_md: str = "",
    record_state: str = "draft",
    attribution: dict[str, Any] | None = None,
    aliases: list[str] | None = None,
    citations: list[dict[str, Any]] | None = None,
    links: list[dict[str, Any]] | None = None,
    occurred_at: str | None = None,
    occurred_at_unknown: bool = True,
    effective_from: str | None = None,
    effective_to: str | None = None,
    source_object_id: str | None = None,
) -> dict[str, Any]:
    conn = mutation.ctx.conn
    validate_subkind(kind, subkind)
    if record_state not in ("draft", "active"):
        raise schema_validation_failed("New records can be draft or active; use explicit transitions otherwise.")
    full_state = dict(state_json)
    full_state.setdefault("subkind", subkind)
    if kind == "source" and not full_state.get("source_type"):
        full_state["source_type"] = subkind
    validate_record_payload(kind, subkind, full_state, body_md=body_md)
    _validate_timestamp(occurred_at, "occurred_at")
    _validate_timestamp(effective_from, "effective_from")
    _validate_timestamp(effective_to, "effective_to")
    if occurred_at_unknown and occurred_at is not None:
        raise schema_validation_failed("occurred_at and occurred_at_unknown cannot both be set.")
    if effective_from and effective_to and effective_from > effective_to:
        raise schema_validation_failed("effective_from must not be after effective_to.")
    attribution_json = _validate_attribution(attribution)
    object_id = mutation.new_id()
    citations = citations or []
    for citation in citations:
        if citation.get("role") not in CITATION_ROLES:
            raise schema_validation_failed(
                "Unknown citation role.", role=citation.get("role"), allowed=list(CITATION_ROLES)
            )
        _resolve_citation_anchor(conn, mutation.project_id, citation.get("anchor_id", ""))
    digest = content_hash_for_revision(
        kind,
        subkind,
        title,
        body_md,
        record_state,
        full_state,
        citations=[
            {"anchor_id": citation.get("anchor_id"), "role": citation.get("role")}
            for citation in citations
        ],
    )
    repo.insert_object_head(
        conn,
        project_id=mutation.project_id,
        object_id=object_id,
        kind=kind,
        record_state=record_state,
        content_hash=digest,
        seq=mutation.commit_seq,
        recorded_at=mutation.recorded_at,
    )
    repo.insert_object_revision(
        conn,
        project_id=mutation.project_id,
        object_id=object_id,
        kind=kind,
        subkind=subkind,
        revision=1,
        title=title,
        body_md=body_md,
        record_state=record_state,
        state_json=json.dumps(full_state, sort_keys=True),
        content_hash=digest,
        commit_seq=mutation.commit_seq,
        actor_id=mutation.actor_id,
        attribution_json=json.dumps(attribution_json, sort_keys=True),
        recorded_at=mutation.recorded_at,
        recorded_seq=mutation.commit_seq,
        occurred_at=occurred_at,
        effective_from=effective_from,
        effective_to=effective_to,
        schema_version=RECORD_SCHEMA_VERSION,
    )
    for alias in aliases or []:
        repo.add_alias(
            conn,
            project_id=mutation.project_id,
            object_id=object_id,
            alias_text=alias,
            namespace=subkind,
            seq=mutation.commit_seq,
        )
    if kind == "resource":
        _upsert_resource(conn, mutation.project_id, object_id, subkind, full_state)
    for citation in citations:
        conn.execute(
            """
            INSERT INTO citations
              (citation_id, project_id, citing_object_id, citing_revision, anchor_id, role, note, created_seq, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mutation.new_id(),
                mutation.project_id,
                object_id,
                1,
                citation["anchor_id"],
                citation["role"],
                citation.get("note"),
                mutation.commit_seq,
                mutation.recorded_at,
            ),
        )
    for link in links or []:
        payload = dict(link)
        direction = payload.pop("direction", "out")
        if direction == "out":
            create_link(
                mutation,
                predicate=payload.pop("predicate"),
                src_ref={"object_id": object_id, "revision": 1},
                dst_ref=payload.pop("target_ref", payload.pop("dst_ref", None)),
                **payload,
            )
        else:
            create_link(
                mutation,
                predicate=payload.pop("predicate"),
                src_ref=payload.pop("src_ref", payload.pop("target_ref", None)),
                dst_ref={"object_id": object_id, "revision": 1},
                **payload,
            )
    alias_text = " ".join(aliases or [])
    projection_digest = project_revision(
        conn,
        project_id=mutation.project_id,
        object_id=object_id,
        revision=1,
        title=title,
        body_md=body_md,
        alias_text=alias_text,
        recorded_seq=mutation.commit_seq,
        source_object_id=source_object_id,
        extra_text=searchable_extra_text(full_state, body_md, title),
    )
    _maybe_enqueue_embedding(mutation.ctx, object_id, 1, projection_digest)
    return {
        "kind": kind,
        "subkind": subkind,
        "object_id": object_id,
        "revision": 1,
        "record_state": record_state,
        "content_hash": digest,
    }


def create_bootstrap_project_object(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor_id: str,
    epoch: str,
    name: str,
    commit_seq: int = 1,
) -> str:
    from research_kb.storage.db import new_id as make_id

    object_id = make_id()
    state = {
        "subkind": "project",
        "scope": "Not yet recorded. Complete during the first-use workflow.",
        "objectives": [],
        "research_area": None,
        "domain_conventions": {},
        "current_priorities": [],
        "collaboration_roles": [],
        "policy_refs": [],
        "terminology_aliases": {},
        "comparison_rules": {},
        "completion_evidence_conventions": [],
        "workspace_identity": None,
    }
    digest = content_hash_for_revision("project", "project", name, "", "draft", state)
    repo.insert_object_head(
        conn,
        project_id=project_id,
        object_id=object_id,
        kind="project",
        record_state="draft",
        content_hash=digest,
        seq=commit_seq,
        recorded_at=utc_now(),
    )
    repo.insert_object_revision(
        conn,
        project_id=project_id,
        object_id=object_id,
        kind="project",
        subkind="project",
        revision=1,
        title=name,
        body_md="",
        record_state="draft",
        state_json=json.dumps(state, sort_keys=True),
        content_hash=digest,
        commit_seq=commit_seq,
        actor_id=actor_id,
        attribution_json="{}",
        recorded_at=utc_now(),
        recorded_seq=commit_seq,
        occurred_at=None,
        effective_from=None,
        effective_to=None,
        schema_version=RECORD_SCHEMA_VERSION,
    )
    project_revision(
        conn,
        project_id=project_id,
        object_id=object_id,
        revision=1,
        title=name,
        body_md="",
        alias_text="",
        recorded_seq=commit_seq,
        extra_text=searchable_extra_text(state, "", name),
    )
    return object_id


def _check_expected_revision(
    conn: sqlite3.Connection,
    project_id: str,
    object_id: str,
    head: sqlite3.Row,
    expected: int | None,
) -> int:
    current = int(head["current_revision"])
    if expected is None:
        raise schema_validation_failed("A mutation of an existing object requires expected_revision.")
    if int(expected) != current:
        raise revision_conflict(
            int(expected),
            current,
            repo.revision_diff(
                conn, project_id, object_id, min(int(expected), current), current
            ),
        )
    return current


def revise_object(
    mutation: Mutation,
    *,
    object_id: str,
    expected_revision: int,
    title: str | None = None,
    body_md: str | None = None,
    state_json: dict[str, Any] | None = None,
    record_state: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    reaffirm_citation_ids: list[str] | None = None,
    occurred_at: str | None | object = _UNSET,
    effective_from: str | None | object = _UNSET,
    effective_to: str | None | object = _UNSET,
    source_object_id: str | None = None,
) -> dict[str, Any]:
    conn = mutation.ctx.conn
    head = repo.object_row_or_none(conn, mutation.project_id, object_id)
    if head is None:
        raise reference_unresolved("Cannot revise a missing object.", object_id=object_id)
    current_revision = _check_expected_revision(
        conn, mutation.project_id, object_id, head, expected_revision
    )
    previous = repo.get_revision(conn, mutation.project_id, object_id, current_revision)
    new_state = dict(previous["state_json"] and json.loads(previous["state_json"]) or {})
    if state_json is not None:
        new_state = dict(state_json)
    new_state.setdefault("subkind", previous["subkind"])
    validate_record_payload(previous["kind"], previous["subkind"], new_state, body_md=body_md if body_md is not None else previous["body_md"])
    resolved_state = record_state or previous["record_state"]
    if resolved_state not in RECORD_STATES:
        raise schema_validation_failed("Unknown record state.", state=resolved_state, allowed=list(RECORD_STATES))
    resolved_title = previous["title"] if title is None else title
    resolved_body = previous["body_md"] if body_md is None else body_md
    if citations is None:
        citations = []
    if reaffirm_citation_ids:
        plus = {"assess_evidence", "accept_conclusions", "resolve_critical", "administer"}
        if not plus.intersection(mutation.ctx.capabilities()):
            from research_kb.errors import permission_denied

            raise permission_denied(
                "Carrying evidence citations to a new revision requires a reviewer capability.",
                capability="assess_evidence",
                actor=mutation.actor_id,
            )
        placeholders = ",".join("?" for _ in reaffirm_citation_ids)
        old_rows = conn.execute(
            f"""
            SELECT anchor_id, role, note FROM citations
            WHERE project_id = ? AND citing_object_id = ? AND citation_id IN ({placeholders})
            """,
            (mutation.project_id, object_id, *reaffirm_citation_ids),
        ).fetchall()
        found = len(old_rows)
        if found != len(set(reaffirm_citation_ids)):
            from research_kb.errors import reference_unresolved

            raise reference_unresolved(
                "One or more citation IDs to reaffirm do not belong to this object.",
                requested=len(set(reaffirm_citation_ids)),
                found=found,
            )
        existing = {(item.get("anchor_id"), item.get("role")) for item in citations}
        for row in old_rows:
            key = (row["anchor_id"], row["role"])
            if key in existing:
                continue
            citations.append(
                {
                    "anchor_id": row["anchor_id"],
                    "role": row["role"],
                    "note": f"reaffirmed by {mutation.actor_id}" if not row["note"] else f"{row['note']}; reaffirmed by {mutation.actor_id}",
                }
            )
    for citation in citations:
        if citation.get("role") not in CITATION_ROLES:
            raise schema_validation_failed("Unknown citation role.", role=citation.get("role"))
        _resolve_citation_anchor(conn, mutation.project_id, citation.get("anchor_id", ""))
    resolved_occurred = _coerce_optional_text(previous["occurred_at"] if occurred_at is _UNSET else occurred_at)
    resolved_from = _coerce_optional_text(previous["effective_from"] if effective_from is _UNSET else effective_from)
    resolved_to = _coerce_optional_text(previous["effective_to"] if effective_to is _UNSET else effective_to)
    _validate_timestamp(resolved_occurred, "occurred_at")
    _validate_timestamp(resolved_from, "effective_from")
    _validate_timestamp(resolved_to, "effective_to")
    digest = content_hash_for_revision(
        previous["kind"],
        previous["subkind"],
        resolved_title,
        resolved_body,
        resolved_state,
        new_state,
        citations=[
            {"anchor_id": citation.get("anchor_id"), "role": citation.get("role")}
            for citation in citations
        ],
    )
    revision = current_revision + 1
    repo.insert_object_revision(
        conn,
        project_id=mutation.project_id,
        object_id=object_id,
        kind=previous["kind"],
        subkind=previous["subkind"],
        revision=revision,
        title=resolved_title,
        body_md=resolved_body,
        record_state=resolved_state,
        state_json=json.dumps(new_state, sort_keys=True),
        content_hash=digest,
        commit_seq=mutation.commit_seq,
        actor_id=mutation.actor_id,
        attribution_json=previous["attribution_json"],
        recorded_at=mutation.recorded_at,
        recorded_seq=mutation.commit_seq,
        occurred_at=resolved_occurred,
        effective_from=resolved_from,
        effective_to=resolved_to,
        schema_version=RECORD_SCHEMA_VERSION,
    )
    repo.update_head(
        conn,
        project_id=mutation.project_id,
        object_id=object_id,
        revision=revision,
        record_state=resolved_state,
        content_hash=digest,
        recorded_seq=mutation.commit_seq,
        recorded_at=mutation.recorded_at,
    )
    conn.execute(
        "DELETE FROM citations WHERE project_id = ? AND citing_object_id = ? AND citing_revision = ?",
        (mutation.project_id, object_id, revision),
    )
    if previous["kind"] == "resource":
        _upsert_resource(conn, mutation.project_id, object_id, previous["subkind"], new_state)
    for citation in citations:
        conn.execute(
            """
            INSERT INTO citations
              (citation_id, project_id, citing_object_id, citing_revision, anchor_id, role, note, created_seq, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mutation.new_id(),
                mutation.project_id,
                object_id,
                revision,
                citation["anchor_id"],
                citation["role"],
                citation.get("note"),
                mutation.commit_seq,
                mutation.recorded_at,
            ),
        )
    aliases = repo.aliases_for(conn, mutation.project_id, object_id)
    sections = (
        _source_sections(conn, mutation.project_id, object_id, revision)
        if previous["kind"] == "source"
        else None
    )
    projection_digest = project_revision(
        conn,
        project_id=mutation.project_id,
        object_id=object_id,
        revision=revision,
        title=resolved_title,
        body_md=resolved_body,
        alias_text=" ".join(aliases),
        recorded_seq=mutation.commit_seq,
        sections=sections,
        source_object_id=source_object_id,
        extra_text=searchable_extra_text(new_state, resolved_body, resolved_title),
    )
    _maybe_enqueue_embedding(mutation.ctx, object_id, revision, projection_digest)
    if digest != previous["content_hash"]:
        propagate_impact(
            mutation,
            changed_object_id=object_id,
            changed_revision=revision,
            previous_revision=current_revision,
        )
    return {
        "kind": previous["kind"],
        "subkind": previous["subkind"],
        "object_id": object_id,
        "revision": revision,
        "previous_revision": current_revision,
        "record_state": resolved_state,
        "content_hash": digest,
    }


def _source_sections(
    conn: sqlite3.Connection, project_id: str, object_id: str, revision: int
) -> list[dict[str, Any]] | None:
    row = conn.execute(
        """
        SELECT extraction_id, extraction_blob_hash, status FROM source_extractions
        WHERE project_id = ? AND source_object_id = ? AND source_revision = ?
        ORDER BY created_seq DESC LIMIT 1
        """,
        (project_id, object_id, revision),
    ).fetchone()
    if row is None or not row["extraction_blob_hash"]:
        return None
    return [{"section_key": f"extraction:{row['extraction_id']}", "title": "Extraction", "body": "", "anchor_id": None}]


def would_create_cycle(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    predicate: str,
    src_object_id: str,
    dst_object_id: str,
) -> bool:
    if not is_acyclic(predicate):
        return False
    seen: set[str] = set()
    frontier = [dst_object_id]
    while frontier:
        current = frontier.pop()
        if current == src_object_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        rows = conn.execute(
            """
            SELECT dst_object_id FROM link_revisions
            WHERE project_id = ? AND src_object_id = ? AND predicate = ?
            """,
            (project_id, current, predicate),
        ).fetchall()
        frontier.extend(row["dst_object_id"] for row in rows)
    return False


def create_link(
    mutation: Mutation,
    *,
    predicate: str,
    src_ref: dict[str, Any] | str | None,
    dst_ref: dict[str, Any] | str | None,
    pin_mode: str | None = None,
    qualifiers: dict[str, Any] | None = None,
    rationale: str | None = None,
    review_state: str | None = None,
    applicability: dict[str, Any] | str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    conn = mutation.ctx.conn
    if src_ref is None or dst_ref is None:
        raise schema_validation_failed("A link requires both source and destination references.")
    spec = get_predicate(predicate)
    src = repo.normalize_ref(mutation.project_id, src_ref)
    dst = repo.normalize_ref(mutation.project_id, dst_ref)
    src_row = repo.resolve_ref(
        conn,
        mutation.project_id,
        {"object_id": src["object_id"], "revision": src["revision"]},
    )
    dst_row = repo.resolve_ref(
        conn,
        mutation.project_id,
        {"object_id": dst["object_id"], "revision": dst["revision"]},
    )
    validate_endpoints(predicate, src_row["kind"], src_row["subkind"], dst_row["kind"], dst_row["subkind"])
    if pin_mode is None:
        pin_mode = "pinned"
    if pin_mode not in ("pinned", "tracking"):
        raise schema_validation_failed("pin_mode must be 'pinned' or 'tracking'.")
    if pin_mode == "tracking" and not tracking_allowed(predicate):
        raise schema_validation_failed(f"Predicate '{predicate}' requires pinned endpoints.")
    if pin_mode == "tracking" and qualifiers is None:
        qualifiers = {}
    if predicate == "blocks" and isinstance(qualifiers, dict) and "applies_when" in qualifiers:
        from research_kb.domain.blocking_rules import validate_rule

        validate_rule(qualifiers["applies_when"])
    src_revision = src_row["revision"] if pin_mode == "pinned" else src["revision"]
    dst_revision = dst_row["revision"] if pin_mode == "pinned" else dst["revision"]
    if pin_mode == "pinned" and (src_revision is None or dst_revision is None):
        src_revision = src_revision or src_row["revision"]
        dst_revision = dst_revision or dst_row["revision"]
    if would_create_cycle(
        conn,
        project_id=mutation.project_id,
        predicate=predicate,
        src_object_id=src["object_id"],
        dst_object_id=dst["object_id"],
    ):
        raise schema_validation_failed(
            "This relationship would create a cycle in an acyclic predicate.",
            predicate=predicate,
            src=src["object_id"],
            dst=dst["object_id"],
        )
    if spec.review_required:
        from research_kb.errors import permission_denied

        capabilities = mutation.ctx.capabilities()
        if "reviewer" not in capabilities and "resolve_critical" not in capabilities:
            raise permission_denied(
                f"Predicate '{predicate}' requires a reviewer capability.",
                capability="reviewer",
                actor=mutation.actor_id,
            )
        if review_state != "reviewed":
            raise schema_validation_failed(f"Predicate '{predicate}' must be created as reviewed.")
    state = {
        "subkind": "link",
        "predicate": predicate,
        "qualifiers": qualifiers or {},
        "rationale": rationale,
        "review_state": review_state or "unreviewed",
        "applicability": applicability,
    }
    validate_record_payload("link", "link", state)
    resolved_title = title or f"{predicate}: {src['object_id']} -> {dst['object_id']}"
    digest = content_hash_for_revision(
        "link",
        "link",
        resolved_title,
        "",
        "active",
        state,
        endpoints={
            "src": {"object_id": src["object_id"], "revision": src_revision},
            "dst": {"object_id": dst["object_id"], "revision": dst_revision},
            "pin_mode": pin_mode,
        },
    )
    link_object_id = mutation.new_id()
    repo.insert_object_head(
        conn,
        project_id=mutation.project_id,
        object_id=link_object_id,
        kind="link",
        record_state="active",
        content_hash=digest,
        seq=mutation.commit_seq,
        recorded_at=mutation.recorded_at,
    )
    repo.insert_object_revision(
        conn,
        project_id=mutation.project_id,
        object_id=link_object_id,
        kind="link",
        subkind="link",
        revision=1,
        title=resolved_title,
        body_md="",
        record_state="active",
        state_json=json.dumps(state, sort_keys=True),
        content_hash=digest,
        commit_seq=mutation.commit_seq,
        actor_id=mutation.actor_id,
        attribution_json="{}",
        recorded_at=mutation.recorded_at,
        recorded_seq=mutation.commit_seq,
        occurred_at=None,
        effective_from=None,
        effective_to=None,
        schema_version=RECORD_SCHEMA_VERSION,
    )
    conn.execute(
        """
        INSERT INTO link_revisions
          (project_id, object_id, revision, predicate, src_project_id, src_object_id, src_revision,
           dst_project_id, dst_object_id, dst_revision, pin_mode, qualifiers_json)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mutation.project_id,
            link_object_id,
            predicate,
            mutation.project_id,
            src["object_id"],
            src_revision,
            mutation.project_id,
            dst["object_id"],
            dst_revision,
            pin_mode,
            json.dumps(qualifiers or {}, sort_keys=True),
        ),
    )
    project_revision(
        conn,
        project_id=mutation.project_id,
        object_id=link_object_id,
        revision=1,
        title=resolved_title,
        body_md=rationale or "",
        alias_text="",
        recorded_seq=mutation.commit_seq,
    )
    return {
        "kind": "link",
        "subkind": "link",
        "object_id": link_object_id,
        "revision": 1,
        "predicate": predicate,
        "src_object_id": src["object_id"],
        "dst_object_id": dst["object_id"],
    }


def propagate_impact(
    mutation: Mutation,
    *,
    changed_object_id: str,
    changed_revision: int,
    previous_revision: int,
    max_nodes: int = 500,
) -> list[dict[str, Any]]:
    conn = mutation.ctx.conn
    flags: list[dict[str, Any]] = []
    visited: set[str] = set()
    frontiers: list[tuple[str, str]] = []
    for row in conn.execute(
        f"""
        SELECT * FROM link_revisions
        WHERE project_id = ? AND dst_object_id = ? AND predicate IN ({','.join('?' for _ in IMPACT_INCOMING)})
        """,
        (mutation.project_id, changed_object_id, *IMPACT_INCOMING),
    ).fetchall():
        frontiers.append((row["src_object_id"], row["predicate"]))
    for row in conn.execute(
        f"""
        SELECT * FROM link_revisions
        WHERE project_id = ? AND src_object_id = ? AND predicate IN ({','.join('?' for _ in IMPACT_OUTGOING)})
        """,
        (mutation.project_id, changed_object_id, *IMPACT_OUTGOING),
    ).fetchall():
        frontiers.append((row["dst_object_id"], row["predicate"]))
    count = 0
    while frontiers:
        target_object_id, predicate = frontiers.pop(0)
        if target_object_id in visited:
            continue
        visited.add(target_object_id)
        count += 1
        if count > max_nodes:
            conn.execute(
                """
                INSERT INTO review_flags
                  (project_id, target_project_id, target_object_id, target_revision, cause_project_id,
                   cause_object_id, cause_revision, predicate, reason, created_seq, created_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mutation.project_id,
                    mutation.project_id,
                    changed_object_id,
                    mutation.project_id,
                    changed_object_id,
                    changed_revision,
                    predicate,
                    "Impact traversal truncated at the configured node limit; analysis incomplete.",
                    mutation.commit_seq,
                    mutation.recorded_at,
                ),
            )
            break
        target = repo.object_row_or_none(conn, mutation.project_id, target_object_id)
        if target is None:
            continue
        conn.execute(
            """
            INSERT INTO review_flags
              (project_id, target_project_id, target_object_id, target_revision, cause_project_id,
               cause_object_id, cause_revision, predicate, reason, created_seq, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mutation.project_id,
                mutation.project_id,
                target_object_id,
                target["current_revision"],
                mutation.project_id,
                changed_object_id,
                changed_revision,
                predicate,
                "An upstream record changed; review current applicability and freshness.",
                mutation.commit_seq,
                mutation.recorded_at,
            ),
        )
        flags.append(
            {
                "target_object_id": target_object_id,
                "cause_object_id": changed_object_id,
                "cause_revision": changed_revision,
                "predicate": predicate,
            }
        )
        for row in conn.execute(
            f"""
            SELECT * FROM link_revisions
            WHERE project_id = ? AND dst_object_id = ? AND predicate IN ({','.join('?' for _ in IMPACT_INCOMING)})
            """,
            (mutation.project_id, target_object_id, *IMPACT_INCOMING),
        ).fetchall():
            frontiers.append((row["src_object_id"], row["predicate"]))
    return flags


def acknowledge_flag(
    mutation: Mutation,
    *,
    flag_id: int,
    rationale: str,
    review_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn = mutation.ctx.conn
    row = conn.execute(
        "SELECT * FROM review_flags WHERE project_id = ? AND flag_id = ?",
        (mutation.project_id, flag_id),
    ).fetchone()
    if row is None:
        raise reference_unresolved("Review flag does not resolve.", flag_id=flag_id)
    review_object_id = None
    review_revision = None
    if review_ref:
        resolved = repo.resolve_ref(conn, mutation.project_id, review_ref, require_revision=True)
        review_object_id = resolved["object_id"]
        review_revision = resolved["revision"]
    conn.execute(
        """
        UPDATE review_flags
        SET status = 'acknowledged', acknowledged_by = ?, acknowledged_seq = ?, rationale = ?,
            review_ref_object_id = ?, review_ref_revision = ?
        WHERE project_id = ? AND flag_id = ?
        """,
        (
            mutation.actor_id,
            mutation.commit_seq,
            rationale,
            review_object_id,
            review_revision,
            mutation.project_id,
            flag_id,
        ),
    )
    return {"flag_id": flag_id, "status": "acknowledged"}


def _maybe_enqueue_embedding(ctx: Any, object_id: str, revision: int, projection_digest: str) -> None:
    from research_kb.retrieval import embeddings

    if ctx.policy.get("retrieval", {}).get("embeddings", False):
        embeddings.enqueue_for_revision(ctx, object_id, revision, projection_digest)


def get_object_bundle(
    conn: sqlite3.Connection,
    project_id: str,
    object_id: str,
    *,
    revision: int | None = None,
    as_of_seq: int | None = None,
    include_history: bool = False,
) -> dict[str, Any]:
    if revision is not None:
        row = repo.get_revision(conn, project_id, object_id, revision)
    elif as_of_seq is not None:
        row = repo.visible_revision(conn, project_id, object_id, as_of_seq)
        if row is None:
            raise reference_unresolved("Object was not visible at this snapshot.", object_id=object_id)
    else:
        row = repo.current_revision(conn, project_id, object_id)
    bundle: dict[str, Any] = {
        "object_id": object_id,
        "kind": row["kind"],
        "subkind": row["subkind"],
        "revision": row["revision"],
        "title": row["title"],
        "body_md": row["body_md"],
        "record_state": row["record_state"],
        "state_json": repo.parse_state(row),
        "recorded_seq": row["recorded_seq"],
        "recorded_at": row["recorded_at"],
        "occurred_at": row["occurred_at"],
        "effective_from": row["effective_from"],
        "effective_to": row["effective_to"],
        "content_hash": row["content_hash"],
        "attribution": json.loads(row["attribution_json"] or "{}"),
        "aliases": repo.aliases_for(conn, project_id, object_id),
    }
    citations = conn.execute(
        """
        SELECT c.citation_id, c.anchor_id, c.role, c.note, a.source_object_id, a.source_revision,
               a.anchor_kind, a.locator_json, a.excerpt, a.excerpt_hash, a.status AS anchor_status
        FROM citations c
        JOIN source_anchors a ON a.anchor_id = c.anchor_id
        WHERE c.project_id = ? AND c.citing_object_id = ? AND c.citing_revision = ?
        ORDER BY c.created_seq
        """,
        (project_id, object_id, row["revision"]),
    ).fetchall()
    bundle["citations"] = [
        {
            **dict(citation),
            "locator": json.loads(citation["locator_json"] or "{}"),
        }
        for citation in citations
    ]
    links = repo.list_links(conn, project_id=project_id, object_id=object_id, as_of_seq=as_of_seq)
    bundle["links"] = [
        {
            "link_object_id": link["object_id"],
            "link_revision": link["revision"],
            "predicate": link["predicate"],
            "src_object_id": link["src_object_id"],
            "src_revision": link["src_revision"],
            "dst_object_id": link["dst_object_id"],
            "dst_revision": link["dst_revision"],
            "pin_mode": link["pin_mode"],
            "qualifiers": json.loads(link["qualifiers_json"] or "{}"),
            "record_state": link["record_state"],
            "title": link["title"],
        }
        for link in links
    ]
    if include_history:
        if as_of_seq is not None:
            history = conn.execute(
                """
                SELECT revision, title, record_state, recorded_seq, recorded_at, content_hash
                FROM revisions WHERE project_id = ? AND object_id = ? AND recorded_seq <= ?
                ORDER BY revision
                """,
                (project_id, object_id, as_of_seq),
            ).fetchall()
        else:
            history = conn.execute(
                """
                SELECT revision, title, record_state, recorded_seq, recorded_at, content_hash
                FROM revisions WHERE project_id = ? AND object_id = ?
                ORDER BY revision
                """,
                (project_id, object_id),
            ).fetchall()
        bundle["history"] = [dict(item) for item in history]
    if row["kind"] == "source":
        from research_kb.service.sources import find_anchors_for_object

        anchors = find_anchors_for_object(conn, project_id, object_id, row["revision"])
        bundle["anchors"] = [
            {
                "anchor_id": anchor["anchor_id"],
                "anchor_kind": anchor["anchor_kind"],
                "coordinate_system": anchor["coordinate_system"],
                "locator": json.loads(anchor["locator_json"] or "{}"),
                "excerpt": anchor["excerpt"],
                "status": anchor["status"],
            }
            for anchor in anchors
        ]
    flags = repo.open_review_flags(conn, project_id, target_object_id=object_id)
    bundle["open_review_flags"] = [dict(flag) for flag in flags]
    return bundle


def find_object_by_alias_or_id(
    conn: sqlite3.Connection, project_id: str, token: str, *, as_of_seq: int | None = None
) -> dict[str, Any]:
    from research_kb.domain.canonical import normalize_alias

    candidate = repo.object_row_or_none(conn, project_id, token)
    if candidate is not None:
        return {"status": "unique", "matches": [token]}
    matches = repo.resolve_alias(conn, project_id, normalize_alias(token), as_of_seq=as_of_seq)
    if not matches:
        raise reference_unresolved("No object or alias resolves to this token.", token=token)
    object_ids = sorted({match["object_id"] for match in matches})
    if len(object_ids) == 1:
        return {"status": "unique", "matches": object_ids}
    return {"status": "ambiguous", "matches": object_ids}


def resolve_object_token(token: str, conn: sqlite3.Connection, project_id: str, *, as_of_seq: int | None = None) -> str:
    match = find_object_by_alias_or_id(conn, project_id, token, as_of_seq=as_of_seq)
    if match["status"] != "unique":
        raise schema_validation_failed(
            "Ambiguous alias; resolve it explicitly before use.",
            token=token,
            candidates=match["matches"],
        )
    return match["matches"][0]


def changed_refs_since(
    conn: sqlite3.Connection, project_id: str, after_seq: int, until_seq: int, limit: int = 200
) -> tuple[list[dict[str, Any]], bool]:
    rows = conn.execute(
        """
        SELECT seq, action, actor_id, reason, recorded_at, changed_json, request_id, policy_revision
        FROM commit_events
        WHERE project_id = ? AND seq > ? AND seq <= ?
        ORDER BY seq
        LIMIT ?
        """,
        (project_id, after_seq, until_seq, limit + 1),
    ).fetchall()
    truncated = len(rows) > limit
    events = []
    for row in rows[:limit]:
        events.append(
            {
                "seq": row["seq"],
                "action": row["action"],
                "actor_id": row["actor_id"],
                "reason": row["reason"],
                "recorded_at": row["recorded_at"],
                "request_id": row["request_id"],
                "policy_revision": row["policy_revision"],
                "changed": json.loads(row["changed_json"] or "[]"),
            }
        )
    return events, truncated
