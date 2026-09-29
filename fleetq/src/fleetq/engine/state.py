"""The sole state reducer (§11): every job and attempt state change goes here.

All functions take the owner thread's connection and run inside its current
transaction. Each externally meaningful change increments ``jobs.version`` and
writes an event in the same transaction (invariant 11). Transitions not in the
tables below raise ``InvariantViolation``, rolling the whole transaction back.

Two properties matter more than any other:

* ``remote_may_be_live`` becomes 1 *before* a launch or submission is sent
  (the durable intent), and only returns to 0 on positive evidence: a typed
  refusal, conclusive never-started proof, or confirmed release. Silence,
  elapsed time, or a failed transport call never clears it (§1.2, §4.1).
* Reservations are released only with that same evidence, so a GPU can't be
  reused while an old payload might still hold it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from ..errors import FqError, InvariantViolation
from ..util import new_id, utcnow

# ---- transition tables ---------------------------------------------------------

JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"DISPATCHING", "HELD", "BLOCKED", "TERMINAL"}),
    "HELD": frozenset({"PENDING", "BLOCKED", "TERMINAL"}),
    "BLOCKED": frozenset({"PENDING", "HELD", "TERMINAL"}),
    "DISPATCHING": frozenset({
        "PENDING", "HELD", "RUNNING", "SUBMITTED", "SUBMISSION_UNKNOWN", "RECONCILING",
        "CANCELLING", "FINALIZING", "TERMINAL", "BLOCKED",
    }),
    "SUBMISSION_UNKNOWN": frozenset({
        "SUBMITTED", "RUNNING", "RECONCILING", "CANCELLING", "FINALIZING", "TERMINAL", "PENDING",
    }),
    "SUBMITTED": frozenset({"RUNNING", "CANCELLING", "RECONCILING", "FINALIZING", "TERMINAL", "BLOCKED"}),
    "RUNNING": frozenset({"CANCELLING", "RECONCILING", "FINALIZING", "TERMINAL", "PENDING"}),
    "CANCELLING": frozenset({"TERMINAL", "RECONCILING", "FINALIZING", "CANCELLING"}),
    "RECONCILING": frozenset({
        "RUNNING", "SUBMITTED", "CANCELLING", "FINALIZING", "TERMINAL", "PENDING", "RECONCILING",
    }),
    "FINALIZING": frozenset({"TERMINAL", "FINALIZING"}),
    "TERMINAL": frozenset(),
}

ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "PLANNED": frozenset({"STAGING", "NEVER_STARTED"}),
    "STAGING": frozenset({"LAUNCHING", "SUBMITTING", "NEVER_STARTED"}),
    "LAUNCHING": frozenset({"RUNNING", "REFUSED", "START_UNKNOWN", "NEVER_STARTED", "STOPPED"}),
    "START_UNKNOWN": frozenset({"RUNNING", "STOPPED", "NEVER_STARTED", "REFUSED", "STOPPING"}),
    "SUBMITTING": frozenset({"SUBMITTED", "SUBMISSION_UNKNOWN", "NEVER_STARTED", "RUNNING", "STOPPED"}),
    "SUBMISSION_UNKNOWN": frozenset({"SUBMITTED", "RUNNING", "STOPPED", "NEVER_STARTED", "STOPPING"}),
    "SUBMITTED": frozenset({"RUNNING", "STOPPING", "STOPPED"}),
    "RUNNING": frozenset({"STOPPING", "STOPPED"}),
    "STOPPING": frozenset({"STOPPED", "STOPPING"}),
    "STOPPED": frozenset({"RELEASED"}),
    "REFUSED": frozenset(),
    "NEVER_STARTED": frozenset(),
    "RELEASED": frozenset(),
}

# States in which the payload may exist remotely. Entering one of these sets
# remote_may_be_live; only the final states clear it.
POSSIBLY_LIVE_STATES = frozenset({
    "LAUNCHING", "START_UNKNOWN", "SUBMITTING", "SUBMISSION_UNKNOWN",
    "SUBMITTED", "RUNNING", "STOPPING", "STOPPED",
})
FINAL_ATTEMPT_STATES = frozenset({"REFUSED", "NEVER_STARTED", "RELEASED"})
TERMINAL_JOB_PHASES = frozenset({"TERMINAL"})


# ---- events --------------------------------------------------------------------

def add_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    job_id: int | None = None,
    attempt_id: str | None = None,
    target: str | None = None,
    job_version: int | None = None,
    actor: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (ts, job_id, attempt_id, target, kind, job_version, actor, detail_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (utcnow(), job_id, attempt_id, target, kind, job_version, actor, json.dumps(detail or {}, sort_keys=True)),
    )


# ---- jobs ----------------------------------------------------------------------

def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise FqError("not_found", f"job {job_id} does not exist")
    return row


_JOB_MUTABLE = frozenset({
    "desired_state", "phase", "reason", "execution_outcome", "exit_code", "exit_signal",
    "artifacts_state", "success", "cancel_requested", "priority", "executions_used",
    "started_at", "ended_at", "not_before",
    "spec_json", "spec_digest", "name",          # `fq modify`, and only before any attempt
})


def update_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    event: str,
    actor: str,
    expect_version: int | None = None,
    detail: dict[str, Any] | None = None,
    **fields: Any,
) -> sqlite3.Row:
    """Apply one checked change to a job; bump its version; record the event."""
    job = get_job(conn, job_id)
    if expect_version is not None and job["version"] != expect_version:
        raise FqError(
            "version_conflict",
            f"job {job_id} is at version {job['version']}, not {expect_version}",
            details={"current_version": job["version"]},
        )
    unknown = set(fields) - _JOB_MUTABLE
    if unknown:
        raise InvariantViolation(f"not a mutable job field: {sorted(unknown)}")
    if {"spec_json", "spec_digest"} & set(fields) and (
            job["phase"] not in ("PENDING", "HELD", "BLOCKED") or active_attempt(conn, job_id) is not None):
        raise InvariantViolation(f"job {job_id}: its spec can only change before any attempt")
    new_phase = fields.get("phase", job["phase"])
    if new_phase != job["phase"] and new_phase not in JOB_TRANSITIONS[job["phase"]]:
        raise InvariantViolation(f"job {job_id}: illegal phase transition {job['phase']} -> {new_phase}")
    if new_phase == "TERMINAL" and job["phase"] != "TERMINAL":
        if possibly_live_attempt(conn, job_id) is not None:
            raise InvariantViolation(
                f"job {job_id}: cannot become TERMINAL while an attempt may still be live"
            )
    if new_phase == "HELD" and job["phase"] != "HELD" and active_attempt(conn, job_id) is not None:
        raise InvariantViolation(f"job {job_id}: cannot become HELD while an attempt is active")
    if not fields:
        return job
    version = job["version"] + 1
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE jobs SET {assignments}, version = ?, updated_at = ? WHERE id = ?",
        (*fields.values(), version, utcnow(), job_id),
    )
    add_event(conn, event, job_id=job_id, job_version=version, actor=actor, detail={**(detail or {}), **{
        k: v for k, v in fields.items() if k in ("phase", "desired_state", "reason", "execution_outcome")
    }})
    return get_job(conn, job_id)


# ---- attempts ------------------------------------------------------------------

def get_attempt(conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    if row is None:
        raise InvariantViolation(f"attempt {attempt_id} does not exist")
    return row


def possibly_live_attempt(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM attempts WHERE job_id = ? AND remote_may_be_live = 1", (job_id,)
    ).fetchone()


def active_attempt(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM attempts WHERE job_id = ? AND state NOT IN ('REFUSED','NEVER_STARTED','RELEASED')",
        (job_id,),
    ).fetchone()


def create_attempt(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    backend: str,
    target: str,
    epoch: int,
    reservations: Iterable[dict[str, Any]],
    profile_digest: str | None = None,
    actor: str = "scheduler",
) -> sqlite3.Row:
    """Plan an attempt and atomically reserve its resources (§4.4).

    The two partial unique indexes make a second concurrent attempt, or a
    double-booked GPU, a constraint failure rather than a race.
    """
    job = get_job(conn, job_id)
    if job["phase"] != "PENDING" or job["desired_state"] != "RUN":
        raise InvariantViolation(f"job {job_id} is {job['phase']}/{job['desired_state']}, not dispatchable")
    if active_attempt(conn, job_id) is not None:
        raise InvariantViolation(f"job {job_id} already has an unfinished attempt")
    n = conn.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM attempts WHERE job_id = ?", (job_id,)).fetchone()[0]
    attempt_id = new_id("att")
    now = utcnow()
    conn.execute(
        "INSERT INTO attempts (id, job_id, n, backend, target, epoch, state, remote_may_be_live,"
        " launch_op_id, spec_digest, profile_digest, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,'PLANNED',0,?,?,?,?,?)",
        (attempt_id, job_id, n, backend, target, epoch, new_id("op"), job["spec_digest"], profile_digest, now, now),
    )
    for res in reservations:
        conn.execute(
            "INSERT INTO resource_reservations (attempt_id, node_id, kind, gpu_uuid, amount, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (attempt_id, res["node_id"], res["kind"], res.get("gpu_uuid"), int(res["amount"]), now),
        )
    add_event(conn, "attempt_planned", job_id=job_id, attempt_id=attempt_id, target=target, actor=actor,
              detail={"n": n, "backend": backend})
    update_job(conn, job_id, event="dispatching", actor=actor, phase="DISPATCHING", reason=None)
    return get_attempt(conn, attempt_id)


_ATTEMPT_MUTABLE = frozenset({
    "remote_id", "boot_id", "outcome", "exit_code", "exit_signal", "started_at", "ended_at", "profile_digest",
})


def update_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    state: str | None = None,
    event: str,
    actor: str = "scheduler",
    evidence: dict[str, Any] | None = None,
    **fields: Any,
) -> sqlite3.Row:
    """Move an attempt; maintain ``remote_may_be_live`` from its state."""
    att = get_attempt(conn, attempt_id)
    unknown = set(fields) - _ATTEMPT_MUTABLE
    if unknown:
        raise InvariantViolation(f"not a mutable attempt field: {sorted(unknown)}")
    new_state = state or att["state"]
    if new_state != att["state"] and new_state not in ATTEMPT_TRANSITIONS[att["state"]]:
        raise InvariantViolation(f"attempt {attempt_id}: illegal transition {att['state']} -> {new_state}")
    if new_state in FINAL_ATTEMPT_STATES:
        live = 0
    elif new_state in POSSIBLY_LIVE_STATES:
        live = 1
    else:
        live = att["remote_may_be_live"]
    if new_state == "RELEASED" and _open_reservations(conn, attempt_id):
        raise InvariantViolation(f"attempt {attempt_id}: release reservations before RELEASED")
    ev_list = json.loads(att["evidence_json"])
    if evidence:
        ev_list.append({"ts": utcnow(), **evidence})
    sets = {"state": new_state, "remote_may_be_live": live, "evidence_json": json.dumps(ev_list), **fields}
    assignments = ", ".join(f"{name} = ?" for name in sets)
    conn.execute(
        f"UPDATE attempts SET {assignments}, updated_at = ? WHERE id = ?",
        (*sets.values(), utcnow(), attempt_id),
    )
    add_event(conn, event, job_id=att["job_id"], attempt_id=attempt_id, target=att["target"], actor=actor,
              detail={"from": att["state"], "to": new_state, **({"evidence": evidence} if evidence else {})})
    return get_attempt(conn, attempt_id)


def _open_reservations(conn: sqlite3.Connection, attempt_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM resource_reservations WHERE attempt_id = ? AND released_at IS NULL",
        (attempt_id,),
    ).fetchone()[0]


def release_reservations(conn: sqlite3.Connection, attempt_id: str, *, reason: str, actor: str = "scheduler") -> int:
    """Release an attempt's reservations. Callers must hold release evidence."""
    att = get_attempt(conn, attempt_id)
    if att["state"] not in ("REFUSED", "NEVER_STARTED", "STOPPED", "PLANNED", "STAGING"):
        raise InvariantViolation(
            f"attempt {attempt_id} is {att['state']}; reservations stay held until stop or refusal is proven"
        )
    cur = conn.execute(
        "UPDATE resource_reservations SET released_at = ? WHERE attempt_id = ? AND released_at IS NULL",
        (utcnow(), attempt_id),
    )
    if cur.rowcount:
        add_event(conn, "reservations_released", job_id=att["job_id"], attempt_id=attempt_id,
                  target=att["target"], actor=actor, detail={"count": cur.rowcount, "reason": reason})
    return cur.rowcount


# ---- invariant audit (tests and doctor) ---------------------------------------

def invariant_violations(conn: sqlite3.Connection) -> list[str]:
    """Return every §1.2 invariant the current database breaks (ideally none)."""
    problems: list[str] = []
    for row in conn.execute(
        "SELECT job_id, COUNT(*) c FROM attempts WHERE remote_may_be_live = 1 GROUP BY job_id HAVING c > 1"
    ):
        problems.append(f"job {row['job_id']} has {row['c']} possibly-live attempts")
    for row in conn.execute(
        "SELECT node_id, gpu_uuid, COUNT(*) c FROM resource_reservations"
        " WHERE kind='gpu' AND released_at IS NULL GROUP BY node_id, gpu_uuid HAVING c > 1"
    ):
        problems.append(f"GPU {row['gpu_uuid']} on {row['node_id']} reserved {row['c']} times")
    for row in conn.execute(
        "SELECT j.id FROM jobs j JOIN attempts a ON a.job_id = j.id"
        " WHERE j.phase = 'TERMINAL' AND a.remote_may_be_live = 1"
    ):
        problems.append(f"job {row['id']} is TERMINAL with a possibly-live attempt")
    for row in conn.execute(
        "SELECT a.id FROM attempts a JOIN resource_reservations r ON r.attempt_id = a.id"
        " WHERE a.state IN ('RELEASED') AND r.released_at IS NULL"
    ):
        problems.append(f"attempt {row['id']} is RELEASED but still holds reservations")
    return problems
