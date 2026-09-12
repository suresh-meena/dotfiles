from __future__ import annotations

import json
import sqlite3
from typing import Any

from research_kb.domain.canonical import canonical_hash
from research_kb.errors import not_found
from research_kb.service.objects import Mutation, revise_object
from research_kb.storage import repo
from research_kb.storage.db import new_id, utc_now


def record_receipt(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    execution_id: str,
    phase: str,
    payload: dict[str, Any],
    external_ref: str | None,
    generation: int | None,
    seq: int,
) -> tuple[str, bool]:
    dedup_key = f"{execution_id}:{phase}:{external_ref}:{canonical_hash(payload)}"
    existing = conn.execute(
        "SELECT receipt_id FROM executor_receipts WHERE project_id = ? AND dedup_key = ?",
        (project_id, dedup_key),
    ).fetchone()
    if existing is not None:
        return existing["receipt_id"], True
    receipt_id = new_id()
    conn.execute(
        """
        INSERT INTO executor_receipts
          (receipt_id, project_id, execution_id, phase, payload_json, external_ref, generation,
           receipt_time, dedup_key, recorded_seq)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt_id,
            project_id,
            execution_id,
            phase,
            json.dumps(payload, sort_keys=True),
            external_ref,
            generation,
            utc_now(),
            dedup_key,
            seq,
        ),
    )
    return receipt_id, False


def observe(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    subject_kind: str,
    subject_id: str,
    status: str,
    detail: dict[str, Any],
    worker_clock: str | None = None,
    worker_boot_id: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO runtime_observations
          (project_id, subject_kind, subject_id, status, detail_json, observed_at, freshness_seq, receipt_time,
           worker_clock, worker_boot_id)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            project_id,
            subject_kind,
            subject_id,
            status,
            json.dumps(detail, sort_keys=True),
            utc_now(),
            utc_now(),
            worker_clock,
            worker_boot_id,
        ),
    )
    return int(cursor.lastrowid or 0)


def reconcile_receipt(
    mutation: Mutation,
    *,
    execution_id: str,
    phase: str,
    payload: dict[str, Any],
    external_ref: str | None,
    generation: int | None,
) -> dict[str, Any]:
    conn = mutation.ctx.conn
    attempt = conn.execute(
        "SELECT * FROM run_attempts WHERE project_id = ? AND execution_id = ?",
        (mutation.project_id, execution_id),
    ).fetchone()
    if attempt is None:
        raise not_found("No run attempt is registered for this execution ID.", execution_id=execution_id)
    receipt_id, duplicate = record_receipt(
        conn,
        project_id=mutation.project_id,
        execution_id=execution_id,
        phase=phase,
        payload=payload,
        external_ref=external_ref,
        generation=generation,
        seq=mutation.commit_seq,
    )
    from research_kb.execution import leases

    if generation is not None:
        for lease in leases.leases_for_execution(conn, mutation.project_id, execution_id):
            if lease["state"] in ("active", "quarantined"):
                leases.assert_generation_current(conn, lease["lease_id"], generation)
    boot_mismatch = False
    boot_quarantined: set[str] = set()
    for lease in leases.leases_for_execution(conn, mutation.project_id, execution_id):
        if lease["state"] in ("active", "quarantined") and _compare_boot_id(conn, lease, payload) == "stale_owner":
            boot_mismatch = True
            boot_quarantined.add(lease["lease_id"])
            leases.release_lease(
                conn,
                project_id=mutation.project_id,
                lease_id=lease["lease_id"],
                seq=mutation.commit_seq,
                verified_termination=False,
                reason="worker rebooted; old job identity does not match",
            )
    pid_mismatch = _pid_identity_mismatch(conn, mutation.project_id, execution_id, payload)
    external_ownership = bool(payload.get("ownership") == "external" or payload.get("imported") is True)
    observation_status = {
        "accepted": "accepted",
        "rejected": "rejected",
        "running": "running",
        "terminal": str(payload.get("status", "completed")),
        "ambiguous": "contact_unknown",
    }.get(phase, phase)
    observe(
        conn,
        project_id=mutation.project_id,
        subject_kind="run_attempt",
        subject_id=execution_id,
        status="identity_mismatch" if (boot_mismatch or pid_mismatch) else observation_status,
        detail=payload,
        worker_clock=payload.get("worker_clock"),
        worker_boot_id=payload.get("worker_boot_id"),
    )
    from research_kb.execution import outbox

    if phase in ("accepted", "running", "terminal"):
        outbox.mark_acknowledged(conn, project_id=mutation.project_id, execution_id=execution_id)
    if external_ownership:
        return {
            "execution_id": execution_id,
            "phase": phase,
            "receipt_id": receipt_id,
            "duplicate_receipt": duplicate,
            "observed_only": True,
            "ownership": "external",
            "updated": None,
            "note": (
                "Imported job owned outside the controller: observed and recorded, but the runtime did "
                "not stop, adopt, or release its resources."
            ),
        }
    if pid_mismatch:
        return {
            "execution_id": execution_id,
            "phase": phase,
            "code": "EXECUTION_AMBIGUOUS",
            "receipt_id": receipt_id,
            "identity_mismatch": True,
            "updated": None,
            "recovery": (
                "PID start time/boot identity does not match the acknowledged launch; quarantine holds "
                "until reliable process evidence resolves it."
            ),
        }
    if duplicate:
        return {
            "execution_id": execution_id,
            "phase": phase,
            "duplicate_receipt": True,
            "receipt_id": receipt_id,
            "updated": None,
        }
    run = repo.current_revision(conn, mutation.project_id, attempt["run_object_id"])
    state = repo.parse_state(run)
    if phase == "ambiguous":
        state["status"] = state.get("status", "running")
        state["reconciliation"] = "ambiguous"
        updated = revise_object(
            mutation,
            object_id=attempt["run_object_id"],
            expected_revision=run["revision"],
            state_json=state,
        )
        return {
            "execution_id": execution_id,
            "phase": phase,
            "code": "EXECUTION_AMBIGUOUS",
            "receipt_id": receipt_id,
            "updated": updated,
            "recovery": "Reconcile the same execution ID; do not launch another attempt.",
        }
    from research_kb.domain.vocab import RUN_STATUSES, RUN_TRANSITIONS

    status_map = {
        "accepted": "starting",
        "running": "running",
        "rejected": "crashed",
        "terminal": payload.get("status", "completed"),
    }
    new_status = status_map.get(phase, state.get("status"))
    if new_status not in RUN_STATUSES:
        new_status = state.get("status")
    current_status = state.get("status")
    if new_status and current_status and new_status != current_status:
        terminal_states = ("completed", "crashed", "cancelled", "lost")
        allowed = set(RUN_TRANSITIONS.get(current_status, ()))
        terminal_allowed = new_status in terminal_states and current_status not in terminal_states
        if new_status not in allowed and not terminal_allowed:
            from research_kb.errors import schema_validation_failed

            raise schema_validation_failed(
                "Illegal execution-attempt status transition.",
                from_status=current_status,
                to_status=new_status,
            )
    state["status"] = new_status
    if payload.get("validity"):
        state["validity"] = payload["validity"]
    if payload.get("validity_reason"):
        state["validity_reason"] = payload["validity_reason"]
    if payload.get("partial_outputs"):
        state["partial_outputs"] = list(state.get("partial_outputs") or []) + payload["partial_outputs"]
    if payload.get("result_schema") is not None:
        state["result_schema"] = payload["result_schema"]
    if payload.get("checksums") is not None:
        state["output_checksums"] = payload["checksums"]
    updated = revise_object(
        mutation,
        object_id=attempt["run_object_id"],
        expected_revision=run["revision"],
        state_json=state,
    )
    released = []
    terminal = phase == "terminal" or new_status in ("completed", "crashed", "cancelled", "lost")
    if terminal:
        verified = bool(payload.get("termination_verified", False))
        for lease in leases.leases_for_execution(conn, mutation.project_id, execution_id):
            if lease["state"] in ("active", "quarantined") and lease["lease_id"] not in boot_quarantined:
                result = leases.release_lease(
                    conn,
                    project_id=mutation.project_id,
                    lease_id=lease["lease_id"],
                    seq=mutation.commit_seq,
                    verified_termination=verified,
                    reason=payload.get("termination_note", ""),
                )
                released.append(result)
    return {
        "execution_id": execution_id,
        "phase": phase,
        "receipt_id": receipt_id,
        "updated": updated,
        "resource_actions": released,
        "partial_outputs_preserved": bool(state.get("partial_outputs")),
    }


def _compare_boot_id(conn: sqlite3.Connection, lease: sqlite3.Row, payload: dict[str, Any]) -> str:
    previous = conn.execute(
        """
        SELECT worker_boot_id FROM runtime_observations
        WHERE project_id = ? AND subject_kind = 'run_attempt' AND subject_id = ?
        ORDER BY observation_id DESC LIMIT 1
        """,
        (lease["project_id"], lease["owner_execution_id"]),
    ).fetchone()
    if previous and previous["worker_boot_id"] and payload.get("worker_boot_id"):
        if previous["worker_boot_id"] != payload["worker_boot_id"]:
            return "stale_owner"
    return "match"


def _pid_identity_mismatch(
    conn: sqlite3.Connection, project_id: str, execution_id: str, payload: dict[str, Any]
) -> bool:
    reported_start = payload.get("pid_start_time")
    if reported_start is None:
        return False
    row = conn.execute(
        """
        SELECT payload_json FROM executor_receipts
        WHERE project_id = ? AND execution_id = ? AND phase = 'accepted'
        ORDER BY recorded_seq DESC, rowid DESC LIMIT 1
        """,
        (project_id, execution_id),
    ).fetchone()
    if row is None:
        return False
    accepted = json.loads(row["payload_json"] or "{}")
    acknowledged_start = accepted.get("pid_start_time")
    if acknowledged_start is None:
        return False
    return str(acknowledged_start) != str(reported_start)
