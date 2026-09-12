from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from research_kb.errors import not_found, permission_denied, schema_validation_failed
from research_kb.storage.db import new_id, utc_now
from research_kb.storage.repo import append_commit_event, capabilities_for, get_actor

DEFAULT_APPROVAL_TTL_SECONDS = 3600


def issue_approval(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor_id: str,
    epoch: str,
    proposal_hash: str,
    expected_versions: dict[str, Any],
    policy_revision: str,
    capability: str,
    ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
) -> dict[str, Any]:
    actor = get_actor(conn, actor_id)
    if capability not in capabilities_for(actor):
        raise permission_denied(
            "The principal cannot issue this approval.",
            capability=capability,
            actor=actor_id,
        )
    token = new_id()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    approval_id = new_id()
    issued_at = utc_now()
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    seq = append_commit_event(
        conn,
        project_id=project_id,
        actor_id=actor_id,
        action="approval",
        epoch=epoch,
        changed=[{"proposal_hash": proposal_hash, "approval_id": approval_id}],
    )
    conn.execute(
        """
        INSERT INTO approvals
          (approval_id, project_id, proposal_hash, expected_versions_json, policy_revision,
           approver_actor_id, capability, epoch, issued_seq, issued_at, expires_at, token_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            approval_id,
            project_id,
            proposal_hash,
            json.dumps(expected_versions, sort_keys=True),
            policy_revision,
            actor_id,
            capability,
            epoch,
            seq,
            issued_at,
            expires_at,
            token_hash,
        ),
    )
    return {
        "approval_id": approval_id,
        "approval_token": token,
        "proposal_hash": proposal_hash,
        "capability": capability,
        "expires_at": expires_at,
    }


def fetch_approval(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    approval_id: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = conn.execute(
            "SELECT * FROM approvals WHERE project_id = ? AND token_hash = ?",
            (project_id, token_hash),
        ).fetchone()
    elif approval_id:
        row = conn.execute(
            "SELECT * FROM approvals WHERE project_id = ? AND approval_id = ?",
            (project_id, approval_id),
        ).fetchone()
    else:
        raise schema_validation_failed("An approval ID or token is required.")
    if row is None:
        raise not_found("The approval does not resolve.", approval_id=approval_id)
    approval = dict(row)
    approval["expected_versions"] = json.loads(approval.pop("expected_versions_json") or "{}")
    return approval


def consume_approval(conn: sqlite3.Connection, approval_id: str, seq: int) -> None:
    conn.execute(
        "UPDATE approvals SET consumed_seq = ? WHERE approval_id = ? AND consumed_seq IS NULL",
        (seq, approval_id),
    )
