"""Bounded Slurm inspection command construction and parsers.

The hub deliberately exposes only the two read-only scheduler queries used by
the MVP. Keeping command construction here makes it difficult for an
inventory value or a future web/API value to turn into an arbitrary command.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .state import check_json_depth, reject_json_constant

MAX_SCHEDULER_ROWS = 2_000
MAX_SCHEDULER_OUTPUT_BYTES = 1024 * 1024
MAX_TARGET_LENGTH = 128
MAX_JOB_ID_LENGTH = 256
MAX_FIELD_LENGTH = 16 * 1024
MAX_SCHEDULER_JSON_DEPTH = 32


def _check_json_depth(value: Any) -> None:
    check_json_depth(
        value,
        limit=MAX_SCHEDULER_JSON_DEPTH,
        depth_message="squeue JSON depth exceeds limit",
    )


SACCT_FORMAT = (
    "Cluster,JobIDRaw,ArrayJobID,ArrayTaskID,User,Account,State,Partition,"
    "NodeList,Start,End,Timelimit,Elapsed,AllocTRES,ReqTRES"
)
SACCT_COMPAT_FORMAT = (
    "Cluster,JobID,User,Account,State,Partition,"
    "NodeList,Start,End,Timelimit,Elapsed,AllocTRES,ReqTRES"
)
SQUEUE_TEXT_FORMAT = (
    "Cluster,JobIDRaw,ArrayJobID,ArrayTaskID,UserName,Account,State,Partition,"
    "NodeList,StartTime,EndTime,TimeLimit,Elapsed,AllocTRES,ReqTRES"
)


def squeue_argv() -> list[str]:
    """Return the only JSON queue command Fleetmon is permitted to run."""

    return ["squeue", "--json"]


def squeue_text_argv() -> list[str]:
    """Return the tested, bounded text fallback for old Slurm installations."""

    return ["squeue", "--noheader", "--parsable2", "--Format", SQUEUE_TEXT_FORMAT]


def sacct_argv(
    start: str, end: str, scheduler_tz: str = "UTC", compat: bool = False
) -> list[str]:
    """Build the fixed accounting query for one bounded time window.

    Times are naive ISO timestamps in the scheduler's local timezone:
    Slurm 22.05 ``sacct`` rejects ``+00:00``/``Z`` suffixes and epoch
    values, and interprets naive times where ``sacct`` runs. The timezone
    is explicit configuration, never guessed; callers convert aware UTC
    windows with :func:`sacct_local_time`. ``compat`` selects the field
    set for Slurm releases without the ArrayJobID/ArrayTaskID fields.
    """

    if not isinstance(scheduler_tz, str) or not scheduler_tz:
        raise ValueError("invalid scheduler timezone")
    zone = scheduler_zone(scheduler_tz)
    start_local = _naive_scheduler_time(start, "start", zone)
    end_local = _naive_scheduler_time(end, "end", zone)
    if start_local > end_local:
        raise ValueError("sacct start must not be after end")
    if end_local - start_local > timedelta(hours=24):
        raise ValueError("sacct window must not exceed 24 hours")
    return [
        "sacct",
        "-X",
        "-S",
        start,
        "-E",
        end,
        "--parsable2",
        "--noheader",
        "--allocations",
        "-o",
        SACCT_COMPAT_FORMAT if compat else SACCT_FORMAT,
    ]


def scheduler_zone(scheduler_tz: str) -> ZoneInfo:
    if not isinstance(scheduler_tz, str) or not scheduler_tz or len(scheduler_tz) > 64:
        raise ValueError("invalid scheduler timezone")
    try:
        return ZoneInfo(scheduler_tz)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"invalid scheduler timezone: {scheduler_tz}") from exc


def sacct_local_time(moment: datetime, scheduler_tz: str) -> str:
    """Convert an aware UTC instant to a naive scheduler-local timestamp."""

    if moment.tzinfo is None:
        raise ValueError("sacct window times must include timezone")
    return moment.astimezone(scheduler_zone(scheduler_tz)).strftime("%Y-%m-%dT%H:%M:%S")


def _naive_scheduler_time(value: str, label: str, zone: ZoneInfo) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"invalid {label}")
    if any(ord(char) < 0x20 or char == "\x7f" for char in value):
        raise ValueError(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc
    if parsed.tzinfo is not None:
        raise ValueError(f"{label} must be a naive scheduler-local timestamp")
    return parsed.replace(tzinfo=zone)


def _target(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TARGET_LENGTH:
        raise ValueError("invalid scheduler target")
    if value.startswith("-") or any(
        char.isspace() or ord(char) < 0x20 or char == "\x7f" for char in value
    ):
        raise ValueError("invalid scheduler target")
    return value


def fleetctl_slurm_argv(
    target: str, command: list[str], scheduler_tz: str = "UTC"
) -> list[str]:
    """Wrap one approved scheduler command in explicit fleetctl admin mode."""

    target = _target(target)
    if not isinstance(command, list) or any(not isinstance(x, str) for x in command):
        raise ValueError("invalid scheduler command")
    approved = command == squeue_argv() or command == squeue_text_argv()
    if command and command[0] == "sacct" and len(command) == 11:
        try:
            approved = command in (
                sacct_argv(command[3], command[5], scheduler_tz, compat=False),
                sacct_argv(command[3], command[5], scheduler_tz, compat=True),
            )
        except ValueError:
            approved = False
    if not approved:
        raise ValueError("scheduler command is not approved")
    return ["exec", "--admin", "--target", target, "--", *command]


def _bounded_text(data: str | bytes, label: str) -> str:
    if isinstance(data, bytes):
        if len(data) > MAX_SCHEDULER_OUTPUT_BYTES:
            raise ValueError(f"{label} output exceeds limit")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid {label} UTF-8") from exc
    elif isinstance(data, str):
        if len(data.encode("utf-8")) > MAX_SCHEDULER_OUTPUT_BYTES:
            raise ValueError(f"{label} output exceeds limit")
        text = data
    else:
        raise ValueError(f"{label} output must be text or bytes")
    if "\x00" in text:
        raise ValueError(f"invalid {label} output")
    return text


def _max_rows(max_rows: int) -> int:
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        raise ValueError("max_rows must be an integer")
    if not 1 <= max_rows <= MAX_SCHEDULER_ROWS:
        raise ValueError(f"max_rows must be 1..{MAX_SCHEDULER_ROWS}")
    return max_rows


def _valid_job_id(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return 0 < value <= 2**63 - 1
    if isinstance(value, str):
        return (
            bool(value)
            and len(value) <= MAX_JOB_ID_LENGTH
            and not any(ord(char) < 0x20 or char == "\x7f" for char in value)
        )
    return False


def _validate_squeue_row(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("squeue row must be object")
    job_id = row.get("job_id", row.get("job_id_raw"))
    if not _valid_job_id(job_id):
        raise ValueError("squeue row missing valid job_id")
    try:
        encoded = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("squeue row is not JSON-safe") from exc
    if len(encoded.encode("utf-8")) > MAX_FIELD_LENGTH:
        raise ValueError("squeue row exceeds limit")
    return row


def parse_squeue(
    data: str | bytes | dict[str, Any], max_rows: int = MAX_SCHEDULER_ROWS
) -> list[dict[str, Any]]:
    """Parse and bound ``squeue --json`` output."""

    max_rows = _max_rows(max_rows)
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(
                _bounded_text(data, "squeue"),
                parse_constant=reject_json_constant,
            )
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("invalid squeue JSON") from exc
        _check_json_depth(data)
    if not isinstance(data, dict):
        raise ValueError("squeue JSON must be object")
    rows = data.get("jobs", [])
    if not isinstance(rows, list):
        raise ValueError("squeue jobs must be list")
    return [_validate_squeue_row(row) for row in rows[:max_rows]]


SACCT_FIELDS = (
    "cluster",
    "job_id",
    "array_job_id",
    "array_task_id",
    "user",
    "account",
    "state",
    "partition",
    "nodes",
    "start",
    "end",
    "timelimit",
    "elapsed",
    "alloc_tres",
    "req_tres",
)
SACCT_COMPAT_FIELDS = (
    "cluster",
    "job_id",
    "user",
    "account",
    "state",
    "partition",
    "nodes",
    "start",
    "end",
    "timelimit",
    "elapsed",
    "alloc_tres",
    "req_tres",
)


def _split_sacct_line(line: str, field_count: int) -> list[str]:
    """Split a parsable2 row, honoring escaped delimiters."""

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
    if len(values) == field_count + 1 and values[-1] == "":
        values.pop()
    return values


def parse_sacct(
    data: str | bytes,
    max_rows: int = MAX_SCHEDULER_ROWS,
    fields: tuple[str, ...] = SACCT_FIELDS,
) -> list[dict[str, str | None]]:
    """Parse fixed-column ``sacct --parsable2 --noheader`` output safely."""

    if fields not in (SACCT_FIELDS, SACCT_COMPAT_FIELDS):
        raise ValueError("unsupported sacct field set")
    max_rows = _max_rows(max_rows)
    text = _bounded_text(data, "sacct")
    result: list[dict[str, str | None]] = []
    for line in text.splitlines()[: max_rows + 1]:
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_FIELD_LENGTH:
            raise ValueError("sacct row exceeds limit")
        vals = _split_sacct_line(line, len(fields))
        if len(vals) != len(fields):
            raise ValueError("sacct row has unexpected column count")
        if any(len(value.encode("utf-8")) > MAX_FIELD_LENGTH for value in vals):
            raise ValueError("sacct field exceeds limit")
        row = {key: (value or None) for key, value in zip(fields, vals, strict=True)}
        if not _valid_job_id(row["job_id"]):
            raise ValueError("sacct row missing valid job_id")
        result.append(row)
        if len(result) >= max_rows:
            break
    return result


def parse_squeue_text(
    data: str | bytes, max_rows: int = MAX_SCHEDULER_ROWS
) -> list[dict[str, str | None]]:
    """Parse the fixed-column fallback emitted by :func:`squeue_text_argv`."""

    return parse_sacct(data, max_rows=max_rows)
