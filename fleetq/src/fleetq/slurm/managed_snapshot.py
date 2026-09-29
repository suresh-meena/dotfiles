"""Bounded, cached all-user Slurm queue snapshots owned by fleetqd.

The scheduler output is the same ``squeue --json`` document consumed by
Fleetmon. Parsing follows Fleetmon's bounded row/identifier/depth contract;
truncation or any invalid row rejects the entire snapshot.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any

from ..util import parse_utc, utcnow

MAX_SQUEUE_BYTES = 256 * 1024
MAX_SQUEUE_ROWS = 2_000
MAX_ROW_BYTES = 16 * 1024
MAX_JSON_DEPTH = 32
DEFAULT_INTERVAL_S = 120.0
DEFAULT_STALE_AFTER_S = 300.0
MIN_INTERVAL_S = 30.0
MAX_INTERVAL_S = 3600.0

# Site opting in to this feature runs this one-shot script. Fleetctl's bounded
# stdout capture is the hard output cap; a truncated envelope is never parsed.
MANAGED_SQUEUE_SCRIPT = "exec squeue --json"
# squeue has no --parsable2 (that is sacct's), and old Slurm such as 19.05 has
# no --json; -o %-codes with "|" separators work on every release. The format
# must stay identical to Fleetmon's SQUEUE_TEXT_FORMAT.
SQUEUE_TEXT_FORMAT = "|%A|%F|%K|%u|%a|%T|%P|%N|%S|%e|%l|%M||%b"
MANAGED_SQUEUE_TEXT_SCRIPT = f"exec squeue --noheader --format='{SQUEUE_TEXT_FORMAT}'"

SQUEUE_TEXT_FIELDS = (
    "cluster", "job_id", "array_job_id", "array_task_id", "user", "account", "state",
    "partition", "nodes", "start", "end", "timelimit", "elapsed", "alloc_tres", "req_tres",
)
_JOB_ID = re.compile(r"^[A-Za-z0-9_.+-]{1,256}$")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _depth(value: Any, limit: int = MAX_JSON_DEPTH) -> None:
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > limit:
            raise ValueError("squeue JSON depth exceeds limit")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _job_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("squeue row missing valid job_id")
    value = str(value)
    if not _JOB_ID.fullmatch(value) or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        raise ValueError("squeue row missing valid job_id")
    return value


def parse_squeue_json(data: str | bytes) -> list[dict[str, Any]]:
    """Strict counterpart of Fleetmon ``parse_squeue``; overflow is incomplete."""
    if isinstance(data, bytes):
        if len(data) > MAX_SQUEUE_BYTES:
            raise ValueError("squeue output exceeds limit")
        data = data.decode("utf-8")
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_SQUEUE_BYTES or "\x00" in data:
        raise ValueError("invalid or oversized squeue output")
    document = json.loads(data, parse_constant=_reject_json_constant)
    _depth(document)
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), list):
        raise ValueError("squeue JSON must contain a jobs list")
    rows = document["jobs"]
    if len(rows) > MAX_SQUEUE_ROWS:
        raise ValueError("squeue row count exceeds limit")
    checked = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("squeue row must be an object")
        row = dict(row)
        row["job_id"] = _job_id(row.get("job_id", row.get("job_id_raw")))
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_ROW_BYTES:
            raise ValueError("squeue row exceeds limit")
        checked.append(row)
    return checked


def _split_parsable(line: str) -> list[str]:
    values: list[str] = []
    field: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            field.append(char if char in {"|", "\\"} else "\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            values.append("".join(field))
            field.clear()
        else:
            field.append(char)
    if escaped:
        field.append("\\")
    values.append("".join(field))
    if len(values) == len(SQUEUE_TEXT_FIELDS) + 1 and values[-1] == "":
        values.pop()
    return values


def parse_squeue_text(data: str | bytes) -> list[dict[str, Any]]:
    """Fleetmon's ``|``-separated fallback, rejecting rather than truncating.

    squeue prints ``N/A`` for a value it does not have, which reads as absent.
    """
    if isinstance(data, bytes):
        if len(data) > MAX_SQUEUE_BYTES:
            raise ValueError("squeue output exceeds limit")
        data = data.decode("utf-8")
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_SQUEUE_BYTES:
        raise ValueError("invalid or oversized squeue output")
    lines = [line for line in data.splitlines() if line.strip()]
    if len(lines) > MAX_SQUEUE_ROWS:
        raise ValueError("squeue row count exceeds limit")
    rows = []
    for line in lines:
        if len(line.encode("utf-8")) > MAX_ROW_BYTES:
            raise ValueError("squeue row exceeds limit")
        values = _split_parsable(line)
        if len(values) != len(SQUEUE_TEXT_FIELDS) or any(
            len(value.encode("utf-8")) > MAX_ROW_BYTES for value in values
        ):
            raise ValueError("squeue row has unexpected columns")
        row = {key: (None if value in ("", "N/A") else value)
               for key, value in zip(SQUEUE_TEXT_FIELDS, values, strict=True)}
        row["job_id"] = _job_id(row["job_id"])
        rows.append(row)
    return rows


def site_snapshot_settings(config_json: str | dict[str, Any]) -> tuple[bool, float, float]:
    """Read the explicit opt-in and bounded cadence/staleness policy."""
    if isinstance(config_json, str):
        config = json.loads(config_json or "{}")
    else:
        config = config_json
    site = config.get("site") if isinstance(config, dict) else None
    site = site if isinstance(site, dict) else {}
    enabled = site.get("managed_queue_snapshot") is True
    interval = site.get("managed_queue_interval_s", DEFAULT_INTERVAL_S)
    stale_after = site.get("managed_queue_stale_after_s", DEFAULT_STALE_AFTER_S)
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or interval <= 0):
        raise ValueError("managed_queue_interval_s must be finite and positive")
    if (isinstance(stale_after, bool) or not isinstance(stale_after, (int, float))
            or not math.isfinite(stale_after) or stale_after <= 0):
        raise ValueError("managed_queue_stale_after_s must be finite and positive")
    interval = min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, float(interval)))
    stale_after = min(MAX_INTERVAL_S * 24, max(interval, float(stale_after)))
    return enabled, interval, stale_after


def record_snapshot_success(
    conn: sqlite3.Connection, site_id: str, *, jobs: list[dict[str, Any]],
    observed_at: str, output_bytes: int,
) -> None:
    if (not isinstance(jobs, list) or len(jobs) > MAX_SQUEUE_ROWS
            or isinstance(output_bytes, bool) or not isinstance(output_bytes, int)
            or not 0 <= output_bytes <= MAX_SQUEUE_BYTES):
        raise ValueError("snapshot exceeds output bounds")
    payload = json.dumps(jobs, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(payload.encode("utf-8")) > MAX_SQUEUE_BYTES:
        raise ValueError("normalized squeue snapshot exceeds limit")
    conn.execute(
        """INSERT INTO managed_slurm_snapshots
           (site_id, attempted_at, last_success_at, complete, error, jobs_json, row_count, output_bytes)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(site_id) DO UPDATE SET attempted_at=excluded.attempted_at,
             last_success_at=excluded.last_success_at, complete=1, error=NULL,
             jobs_json=excluded.jobs_json, row_count=excluded.row_count, output_bytes=excluded.output_bytes""",
        (site_id, observed_at, observed_at, 1, None, payload, len(jobs), output_bytes),
    )


def record_snapshot_failure(conn: sqlite3.Connection, site_id: str, *, attempted_at: str, error: str) -> None:
    safe_error = error if error in {
        "budget_deferred", "transport_failed", "remote_failed", "timeout", "output_truncated",
        "invalid_output", "snapshot_unavailable",
    } else "snapshot_unavailable"
    conn.execute(
        """INSERT INTO managed_slurm_snapshots
           (site_id, attempted_at, last_success_at, complete, error, jobs_json, row_count, output_bytes)
           VALUES (?,?,NULL,0,?,'[]',0,0)
           ON CONFLICT(site_id) DO UPDATE SET attempted_at=excluded.attempted_at,
             complete=0, error=excluded.error""",
        (site_id, attempted_at, safe_error),
    )


def managed_slurm_document(conn: sqlite3.Connection, *, now: str | None = None) -> dict[str, Any]:
    """Build a cached API document only; this function never performs remote I/O."""
    generated_at = now or utcnow()
    generated = parse_utc(generated_at)
    sites = []
    for node in conn.execute(
        "SELECT id, config_json FROM nodes WHERE backend='slurm' AND enabled=1 ORDER BY id"
    ):
        config = json.loads(node["config_json"] or "{}")
        settings_error = None
        try:
            enabled, _interval, stale_after = site_snapshot_settings(config)
        except ValueError:
            site = config.get("site") if isinstance(config, dict) else None
            enabled = isinstance(site, dict) and site.get("managed_queue_snapshot") is True
            stale_after = DEFAULT_STALE_AFTER_S
            settings_error = "invalid_config"
        if not enabled:
            continue
        row = conn.execute(
            "SELECT * FROM managed_slurm_snapshots WHERE site_id=?", (node["id"],)
        ).fetchone()
        if row is None:
            last_success = None
            age = None
            jobs = []
            complete = False
            error = "no_snapshot"
            observed_at = None
            output_bytes = row_count = 0
        else:
            observed_at = row["attempted_at"]
            last_success = row["last_success_at"]
            age = max(0.0, (generated - parse_utc(last_success)).total_seconds()) if last_success else None
            complete = bool(row["complete"]) and settings_error is None
            error = settings_error or row["error"]
            output_bytes, row_count = row["output_bytes"], row["row_count"]
            try:
                jobs = json.loads(row["jobs_json"], parse_constant=_reject_json_constant)
                if (not isinstance(jobs, list) or len(jobs) != row_count
                        or len(jobs) > MAX_SQUEUE_ROWS or any(not isinstance(job, dict) for job in jobs)):
                    raise ValueError("stored row count mismatch")
            except (TypeError, ValueError, json.JSONDecodeError):
                jobs, complete, error = [], False, "invalid_output"
        sites.append({
            "site_id": node["id"], "complete": complete,
            "stale": settings_error is not None or not last_success or age is None or age > stale_after,
            "observed_at": observed_at, "last_success_at": last_success, "age_s": age,
            "error": error, "row_count": row_count, "output_bytes": output_bytes, "jobs": jobs,
        })
    return {"schema": "fleetq.managed-slurm/v1", "generated_at": generated_at, "sites": sites}
