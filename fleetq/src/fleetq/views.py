"""JSON documents returned by the API and printed by the client (``fq.*/v1``).

Fields are only ever added within v1. ``job.terminal`` and ``job.success`` are
the fields agents should branch on, so nobody has to interpret the phase enum.
``remote_may_be_live`` is reported honestly: true means the scheduler cannot
yet prove the remote execution is over (§6.2).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def job_document(conn: sqlite3.Connection, job: sqlite3.Row) -> dict[str, Any]:
    attempts = conn.execute("SELECT * FROM attempts WHERE job_id = ? ORDER BY n", (job["id"],)).fetchall()
    latest = attempts[-1] if attempts else None
    token = conn.execute("SELECT id, label, kind FROM tokens WHERE id = ?", (job["token_id"],)).fetchone()
    spec = json.loads(job["spec_json"])
    gpus: list[str] = []
    queue = None
    if latest is not None:
        gpus = [r["gpu_uuid"] for r in conn.execute(
            "SELECT gpu_uuid FROM resource_reservations WHERE attempt_id = ? AND kind = 'gpu' ORDER BY gpu_uuid",
            (latest["id"],))]
        placed = conn.execute(
            "SELECT detail_json FROM placement_decisions WHERE attempt_id = ? AND decision = 'placed'",
            (latest["id"],)).fetchone()
        if placed:
            queue = json.loads(placed["detail_json"]).get("queue")
    terminal = job["phase"] == "TERMINAL"
    exec_success = None
    if job["execution_outcome"] is not None:
        exec_success = job["execution_outcome"] == "COMPLETED" and job["exit_code"] in (0, None)
    required = [c for c in spec.get("collect", []) if c.get("required", True)]
    return {
        "id": job["id"],
        "name": job["name"],
        "owner": job["owner"],
        "submitter": {"token": token["id"], "label": token["label"], "kind": token["kind"]} if token else None,
        "desired_state": job["desired_state"],
        "phase": job["phase"],
        "reason": job["reason"],
        "terminal": terminal,
        "success": (bool(job["success"]) if job["success"] is not None else None) if terminal else None,
        "remote_may_be_live": any(a["remote_may_be_live"] for a in attempts),
        "cancel_requested": bool(job["cancel_requested"]),
        "execution": {
            "outcome": job["execution_outcome"],
            "success": exec_success,
            "exit": {"code": job["exit_code"], "signal": job["exit_signal"]},
        },
        "artifacts": {
            "state": job["artifacts_state"],
            "required": len(required),
            "required_satisfied": job["artifacts_state"] == "COMPLETE" if required else True,
        },
        "placement": {
            "backend": latest["backend"] if latest else None,
            "target": latest["target"] if latest else None,
            "gpus": gpus,
            "queue": queue,
            "remote_id": latest["remote_id"] if latest else None,
        },
        "resources": spec["resources"],
        "times": {"submitted": job["submitted_at"], "started": job["started_at"], "ended": job["ended_at"]},
        "attempt": latest["n"] if latest else 0,
        "attempts": len(attempts),
        "executions_used": job["executions_used"],
        "group": job["group_id"],
        "array_index": job["array_index"],
        "priority": job["priority"],
        "version": job["version"],
    }


def job_envelope(conn: sqlite3.Connection, job: sqlite3.Row, **extra: Any) -> dict[str, Any]:
    return {"schema": "fq.job/v1", "ok": True, "job": job_document(conn, job), **extra}


def job_summary(job: sqlite3.Row) -> dict[str, Any]:
    return {"id": job["id"], "name": job["name"], "owner": job["owner"], "phase": job["phase"],
            "reason": job["reason"], "desired_state": job["desired_state"],
            "outcome": job["execution_outcome"], "priority": job["priority"], "version": job["version"],
            "submitted": job["submitted_at"], "group_id": job["group_id"], "array_index": job["array_index"],
            "not_before": job["not_before"]}


# ---- queue and history views (squeue / sacct) --------------------------------------------

# Two-letter states for tables, squeue-style. TERMINAL shows the outcome instead.
SHORT_STATE = {
    "PENDING": "PD", "HELD": "HD", "BLOCKED": "BL", "DISPATCHING": "DS", "SUBMISSION_UNKNOWN": "SU",
    "SUBMITTED": "SB", "RUNNING": "R", "CANCELLING": "CG", "RECONCILING": "RC", "FINALIZING": "FN",
}
SHORT_OUTCOME = {
    "COMPLETED": "CD", "FAILED": "F", "CANCELLED": "CA", "TIMEOUT": "TO", "OUT_OF_MEMORY": "OOM",
    "NODE_FAIL": "NF", "PREEMPTED": "PR", "UNKNOWN_EXIT": "UX",
}
_ACTIVE_ORDER = {"RUNNING": 0, "CANCELLING": 0, "FINALIZING": 0, "RECONCILING": 0, "SUBMITTED": 1,
                 "SUBMISSION_UNKNOWN": 1, "DISPATCHING": 1, "PENDING": 2, "HELD": 3, "BLOCKED": 3, "TERMINAL": 4}


def short_state(phase: str, outcome: str | None) -> str:
    if phase == "TERMINAL":
        return SHORT_OUTCOME.get(outcome or "", "?")
    return SHORT_STATE.get(phase, phase[:2])


def _seconds(a: str | None, b: str | None) -> float | None:
    from .util import parse_utc
    if not a or not b:
        return None
    return max(0.0, (parse_utc(b) - parse_utc(a)).total_seconds())


_LATEST = ("LEFT JOIN attempts a ON a.job_id = j.id AND a.n = (SELECT MAX(n) FROM attempts WHERE job_id = j.id)")


def _row(conn: sqlite3.Connection, r: sqlite3.Row, now: str) -> dict[str, Any]:
    spec = json.loads(r["spec_json"])
    placed = {}
    if r["attempt_id"]:
        d = conn.execute("SELECT detail_json FROM placement_decisions WHERE attempt_id = ? AND decision = 'placed'",
                         (r["attempt_id"],)).fetchone()
        placed = json.loads(d["detail_json"]) if d else {}
    gpu_ids = [g["gpu_uuid"] for g in conn.execute(
        "SELECT gpu_uuid FROM resource_reservations WHERE attempt_id = ? AND kind = 'gpu' ORDER BY gpu_uuid",
        (r["attempt_id"],))] if r["attempt_id"] else []
    where = r["target"]
    if where and placed.get("queue"):
        where = f"{where}:{placed['queue']}"
    started = r["job_started"] or r["att_started"]
    end = r["ended_at"] if r["phase"] == "TERMINAL" else now
    return {
        "id": r["id"], "name": r["name"], "owner": r["owner"], "phase": r["phase"],
        "st": short_state(r["phase"], r["execution_outcome"]), "outcome": r["execution_outcome"],
        "exit_code": r["exit_code"], "where": where, "backend": r["backend"],
        "gpus": spec["resources"]["gpus"], "gpu_ids": gpu_ids,
        "submitted": r["submitted_at"], "started": started, "ended": r["ended_at"],
        "elapsed_s": _seconds(started, end) if started else None,
        "wait_s": _seconds(r["submitted_at"], started) if started else None,
        "limit_s": (placed.get("resources") or {}).get("time_s") or spec["resources"]["time_s"],
        "priority": r["priority"], "reason": r["reason"], "remote_id": r["remote_id"],
        "attempt": r["n"] or 0, "group_id": r["group_id"], "array_index": r["array_index"],
        "not_before": r["not_before"], "position": None, "effective_priority": None,
    }


def queue_rows(conn: sqlite3.Connection, *, owner: str | None, phases: set[str] | None = None,
               name_glob: str | None = None, where: str | None = None, group: str | None = None,
               include_finished: bool = False, dispatch_order: list[int] | None = None,
               effective: dict[int, int] | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """squeue: who, where, how long, and -- for waiting jobs -- their place in line."""
    from .util import utcnow
    now = utcnow()
    clauses, args = [], []
    if owner is not None:
        clauses.append("j.owner = ?")
        args.append(owner)
    if not include_finished:
        clauses.append("j.phase <> 'TERMINAL'")
    if name_glob:
        clauses.append("j.name GLOB ?")
        args.append(name_glob)
    if where:
        clauses.append("a.target = ?")
        args.append(where)
    if group:
        clauses.append("j.group_id = ?")
        args.append(group)
    sql = ("SELECT j.*, j.started_at AS job_started, a.id AS attempt_id, a.target, a.backend, a.remote_id, a.n,"
           f" a.started_at AS att_started FROM jobs j {_LATEST}"
           + (" WHERE " + " AND ".join(clauses) if clauses else "")
           + " ORDER BY j.id DESC LIMIT ?")
    rows = [_row(conn, r, now) for r in conn.execute(sql, (*args, limit)).fetchall()]
    if phases:
        rows = [r for r in rows if r["phase"] in phases or r["st"] in phases]
    position = {job_id: i + 1 for i, job_id in enumerate(dispatch_order or [])}
    for r in rows:
        r["position"] = position.get(r["id"])
        r["effective_priority"] = (effective or {}).get(r["id"], r["priority"])
    rows.sort(key=lambda r: (_ACTIVE_ORDER.get(r["phase"], 5), r["position"] or 10 ** 9,
                             r["started"] or "", -r["id"] if r["phase"] == "TERMINAL" else r["id"]))
    return rows


def history_rows(conn: sqlite3.Connection, *, owner: str | None, since: str, where: str | None = None,
                 limit: int = 1000) -> list[dict[str, Any]]:
    """sacct: finished jobs since a time, with wait, run time and GPU-hours across every attempt."""
    rows = queue_rows(conn, owner=owner, phases={"TERMINAL"}, where=where, include_finished=True, limit=limit * 4)
    out = []
    for r in rows:
        if not r["ended"] or r["ended"] < since:
            continue
        run_s = sum(_seconds(a["started_at"], a["ended_at"]) or 0.0 for a in conn.execute(
            "SELECT started_at, ended_at FROM attempts WHERE job_id = ?", (r["id"],)))
        out.append({**r, "run_s": run_s, "gpu_hours": round(r["gpus"] * run_s / 3600, 4)})
    return out[:limit]


def history_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["where"] or "(never placed)"
        s = per.setdefault(key, {"where": key, "jobs": 0, "completed": 0, "failed": 0, "cancelled": 0,
                                 "gpu_hours": 0.0, "waits": []})
        s["jobs"] += 1
        if r["outcome"] == "COMPLETED" and r["exit_code"] in (0, None):
            s["completed"] += 1
        elif r["outcome"] == "CANCELLED":
            s["cancelled"] += 1
        else:
            s["failed"] += 1
        s["gpu_hours"] += r["gpu_hours"]
        if r["wait_s"] is not None:
            s["waits"].append(r["wait_s"])
    table = []
    for s in sorted(per.values(), key=lambda s: -s["gpu_hours"]):
        waits = s.pop("waits")
        s["mean_wait_s"] = round(sum(waits) / len(waits), 1) if waits else None
        s["gpu_hours"] = round(s["gpu_hours"], 3)
        table.append(s)
    total = {k: sum(s[k] for s in table) for k in ("jobs", "completed", "failed", "cancelled")}
    total["gpu_hours"] = round(sum(s["gpu_hours"] for s in table), 3)
    return {"by_where": table, "total": total}
