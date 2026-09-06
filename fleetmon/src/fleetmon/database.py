"""Single-writer SQLite storage for bounded snapshots."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
# This project has not shipped a migration runner yet.  Existing databases
# therefore need to prove that they have exactly the schema this code knows
# how to use; silently applying CREATE TABLE IF NOT EXISTS is not sufficient
# when a pre-release database has the same user_version but different columns.
_REQUIRED_COLUMNS = {
    "schema_migrations": {"version", "applied_at"},
    "hosts": {
        "target",
        "role",
        "protocol",
        "state",
        "helper_path",
        "helper_version",
        "last_captured",
        "last_received",
        "last_success",
        "last_error",
        "backoff",
        "updated_at",
    },
    "polls": {"poll_id", "target", "started_at", "ended_at", "outcome", "error"},
    "host_samples": {
        "poll_id",
        "target",
        "captured_at",
        "received_at",
        "cpu_busy",
        "load1",
        "load5",
        "load15",
        "ram_total",
        "ram_used",
        "root_total",
        "root_free",
        "capture_skew_seconds",
        "collection_duration_seconds",
        "partial",
        "boot_id",
        "observation_duration_seconds",
        "visible_processes",
        "emitted_processes",
        "permission_denied",
        "counters_truncated",
        "limits_truncated",
        "nvml_supported",
        "nvml_error",
        "psutil_error",
    },
    "gpu_samples": {
        "poll_id",
        "uuid",
        "idx",
        "model",
        "utilization",
        "vram_total",
        "vram_used",
        "temperature_c",
        "power_watts",
        "compute_process_count",
        "supported",
        "error",
        "mig_detected",
        "instance_supported",
    },
    "user_samples": {
        "poll_id",
        "target",
        "uid",
        "username",
        "cpu_cores",
        "rss",
        "process_count",
        "gpu_process_count",
        "vram",
    },
    "current_processes": {
        "target",
        "pid",
        "create_time",
        "name",
        "executable",
        "uid",
        "username",
        "cpu_cores",
        "rss",
        "gpu_uuid",
        "gpu_index",
        "vram",
        "poll_id",
    },
    "current_process_allocations": {
        "target",
        "pid",
        "position",
        "gpu_uuid",
        "gpu_index",
        "vram_bytes",
    },
    "slurm_jobs": {
        "cluster",
        "job_id",
        "array_task_id",
        "step_id",
        "state",
        "payload",
        "updated_at",
    },
    "slurm_poll_state": {"target", "watermark", "state", "error", "updated_at"},
}
MAX_STORED_SLURM_JOBS = 2_000
MAX_STORED_GPU_ALLOCATIONS = 8
MAX_IDENTIFIER_BYTES = 256
MAX_CHART_POINTS = 2_000
MAX_CHART_GPUS = 16
MAX_SPARK_POINTS = 60
MAX_SPARK_HOSTS = 256
MAX_GPU_WINDOW_TARGETS = 256
MAX_GPU_ROWS_PER_POLL = 64
MAX_DOWNSAMPLE_KEEP = 2
MAX_DOWNSAMPLE_BATCH = 500
SAFE_POLL_ERRORS = {
    "timeout",
    "output_overflow",
    "transport",
    "invalid_json",
    "invalid_schema",
    "version_mismatch",
}
TERMINAL_SLURM_STATE_PREFIXES = (
    "COMPLETED",
    "CANCELLED",
    "FAILED",
    "TIMEOUT",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
    "REVOKED",
    "SPECIAL_EXIT",
)


def _slurm_state(value: Any) -> str | None:
    """Extract a bounded state name from Slurm's varying JSON shapes."""

    if isinstance(value, str):
        return value[:1024] or None
    if isinstance(value, dict):
        for key in ("name", "state", "current", "value"):
            state = _slurm_state(value.get(key))
            if state:
                return state
        return None
    if isinstance(value, (list, tuple)):
        # Some Slurm versions expose state as a one-item list.  Use the first
        # meaningful item so terminal prefixes remain queryable in SQLite.
        for item in value:
            state = _slurm_state(item)
            if state:
                return state
    return None


def _validate_identifier(value: Any, label: str) -> str:
    """Reject an untrusted identifier instead of truncating it into a collision."""
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"invalid {label}") from exc
    if len(encoded) > MAX_IDENTIFIER_BYTES or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise ValueError(f"invalid {label}")
    return value


def _validate_identity(value: Any, label: str) -> str:
    """Coerce a scheduler-provided id (string or integer) and validate it."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"invalid {label}")
    return _validate_identifier(str(value), label)


def observation_slots(
    observations: list[dict[str, Any]] | None,
) -> dict[str, list[dict[str, Any] | None]]:
    """Shape newest-first observations into per-GPU classification slots.

    Every UUID seen in any observation gets one newest-first slot list; an
    observation without that GPU contributes a slot with ``gpu=None`` so the
    classification chain breaks instead of borrowing a historic row.
    """

    uuids: set[str] = set()
    for observation in observations or []:
        uuids.update(observation.get("gpus", {}))
    slots: dict[str, list[dict[str, Any] | None]] = {}
    for uuid in uuids:
        per_uuid: list[dict[str, Any] | None] = []
        for observation in observations or []:
            slot = {
                "poll_id": observation.get("poll_id"),
                "received_at": observation.get("received_at"),
                "boot_id": observation.get("boot_id"),
                "nvml_supported": observation.get("nvml_supported"),
                "nvml_error": observation.get("nvml_error"),
                "gpu": observation.get("gpus", {}).get(uuid),
            }
            per_uuid.append(slot)
        slots[uuid] = per_uuid
    return slots


def _is_finite_interval(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hosts (
    target TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    protocol TEXT NOT NULL,
    state TEXT NOT NULL,
    helper_path TEXT,
    helper_version TEXT,
    last_captured TEXT,
    last_received REAL,
    last_success REAL,
    last_error TEXT,
    backoff REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS polls (
    poll_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    outcome TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS polls_target_time ON polls(target, started_at);
CREATE INDEX IF NOT EXISTS polls_outcome_time ON polls(outcome, started_at);
CREATE INDEX IF NOT EXISTS polls_ended_time ON polls(ended_at);
CREATE TABLE IF NOT EXISTS host_samples (
    poll_id TEXT PRIMARY KEY REFERENCES polls(poll_id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    received_at REAL NOT NULL,
    cpu_busy REAL,
    load1 REAL,
    load5 REAL,
    load15 REAL,
    ram_total INTEGER,
    ram_used INTEGER,
    root_total INTEGER,
    root_free INTEGER,
    capture_skew_seconds REAL NOT NULL,
    collection_duration_seconds REAL,
    partial INTEGER NOT NULL DEFAULT 0,
    boot_id TEXT,
    observation_duration_seconds REAL,
    visible_processes INTEGER NOT NULL DEFAULT 0,
    emitted_processes INTEGER NOT NULL DEFAULT 0,
    permission_denied INTEGER NOT NULL DEFAULT 0,
    counters_truncated INTEGER NOT NULL DEFAULT 0,
    limits_truncated INTEGER NOT NULL DEFAULT 0,
    nvml_supported INTEGER,
    nvml_error TEXT,
    psutil_error TEXT
);
CREATE INDEX IF NOT EXISTS host_samples_target_time
    ON host_samples(target, received_at);
CREATE TABLE IF NOT EXISTS gpu_samples (
    poll_id TEXT NOT NULL REFERENCES polls(poll_id) ON DELETE CASCADE,
    uuid TEXT NOT NULL,
    idx INTEGER NOT NULL,
    model TEXT,
    utilization REAL,
    vram_total INTEGER,
    vram_used INTEGER,
    temperature_c REAL,
    power_watts REAL,
    compute_process_count INTEGER NOT NULL,
    supported INTEGER NOT NULL,
    error TEXT,
    mig_detected INTEGER NOT NULL,
    instance_supported INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(poll_id, uuid)
);
CREATE INDEX IF NOT EXISTS gpu_uuid_time ON gpu_samples(uuid, poll_id);
CREATE TABLE IF NOT EXISTS user_samples (
    poll_id TEXT NOT NULL REFERENCES polls(poll_id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    uid INTEGER,
    username TEXT,
    cpu_cores REAL,
    rss INTEGER,
    process_count INTEGER NOT NULL,
    gpu_process_count INTEGER,
    vram INTEGER
);
CREATE INDEX IF NOT EXISTS users_target_uid_time
    ON user_samples(target, uid, poll_id);
CREATE INDEX IF NOT EXISTS users_poll_id ON user_samples(poll_id);
CREATE TABLE IF NOT EXISTS current_processes (
    target TEXT NOT NULL,
    pid INTEGER NOT NULL,
    create_time REAL,
    name TEXT,
    executable TEXT,
    uid INTEGER,
    username TEXT,
    cpu_cores REAL,
    rss INTEGER,
    gpu_uuid TEXT,
    gpu_index INTEGER,
    vram INTEGER,
    poll_id TEXT REFERENCES polls(poll_id) ON DELETE CASCADE,
    PRIMARY KEY(target, pid)
);
CREATE INDEX IF NOT EXISTS current_processes_poll
    ON current_processes(poll_id);
CREATE TABLE IF NOT EXISTS current_process_allocations (
    target TEXT NOT NULL,
    pid INTEGER NOT NULL,
    position INTEGER NOT NULL,
    gpu_uuid TEXT NOT NULL,
    gpu_index INTEGER NOT NULL,
    vram_bytes INTEGER,
    FOREIGN KEY(target, pid)
        REFERENCES current_processes(target, pid) ON DELETE CASCADE,
    PRIMARY KEY(target, pid, position)
);
CREATE TABLE IF NOT EXISTS slurm_jobs (
    cluster TEXT NOT NULL,
    job_id TEXT NOT NULL,
    array_task_id TEXT NOT NULL DEFAULT '',
    step_id TEXT NOT NULL DEFAULT '',
    state TEXT,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(cluster, job_id, array_task_id, step_id)
);
CREATE INDEX IF NOT EXISTS slurm_jobs_updated_time
    ON slurm_jobs(updated_at);
CREATE TABLE IF NOT EXISTS slurm_poll_state (
    target TEXT PRIMARY KEY,
    watermark TEXT,
    state TEXT NOT NULL,
    error TEXT,
    updated_at REAL NOT NULL
);
"""


class DatabaseVersionError(RuntimeError):
    """Raised instead of guessing how to open an unversioned/newer database."""


class Database:
    """A small serialized SQLite interface.

    All mutation methods share one connection and lock. This preserves the
    one-writer invariant while allowing FastAPI's worker thread to read safely.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        if self.path.is_symlink():
            raise PermissionError("database path must not be a symlink")
        existed = self.path.exists() and self.path.stat().st_size > 0
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = os.lstat(self.path.parent)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise NotADirectoryError(self.path.parent)
        if parent_stat.st_mode & 0o077:
            raise PermissionError(
                "database directory must not be group/world accessible"
            )
        if not self.path.exists():
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.close(descriptor)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            self.conn.close()
            raise DatabaseVersionError("database schema is newer than this Fleetmon")
        if existed and version == 0:
            tables = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchall()
            if tables:
                self.conn.close()
                raise DatabaseVersionError(
                    "refusing to modify an unversioned Fleetmon database"
                )
        if existed and version not in (0, SCHEMA_VERSION):
            self.conn.close()
            raise DatabaseVersionError(
                f"database schema version {version} is older than this "
                f"Fleetmon ({SCHEMA_VERSION}); refusing to migrate"
            )
        if existed and version == SCHEMA_VERSION:
            try:
                self._validate_existing_schema()
            except DatabaseVersionError:
                self.conn.close()
                raise
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, time.time()),
        )
        self._secure_files()

    def _validate_existing_schema(self) -> None:
        """Reject a pre-release DB that only claims to be current.

        There is no migration path in this pre-release.  In particular, using
        ``CREATE TABLE IF NOT EXISTS`` here could leave an old table with a
        missing column and make the first write fail after partial startup.
        Fail closed before enabling the writer instead.
        """

        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not set(_REQUIRED_COLUMNS).issubset(tables):
            raise DatabaseVersionError("database schema does not match Fleetmon")
        for table, required in _REQUIRED_COLUMNS.items():
            columns = {
                row[1]
                for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not required.issubset(columns):
                raise DatabaseVersionError("database schema does not match Fleetmon")
        migration_version = self.conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        if migration_version != SCHEMA_VERSION:
            raise DatabaseVersionError("database schema migration metadata mismatch")

    def _secure_files(self) -> None:
        for path in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            with suppress(OSError):
                os.chmod(path, 0o600)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def backup(self, destination: str | os.PathLike[str]) -> None:
        """Write an online backup to a new file; never overwrite an existing one."""
        dest = Path(destination)
        if dest.exists():
            raise FileExistsError(f"backup destination already exists: {dest}")
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = os.lstat(dest.parent)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise NotADirectoryError(dest.parent)
        if parent_stat.st_mode & 0o077:
            raise PermissionError("backup directory must not be group/world accessible")
        with self._lock:
            target_conn = sqlite3.connect(dest)
            try:
                self.conn.backup(target_conn)
                target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                target_conn.close()
        os.chmod(dest, 0o600)

    def upsert_host(
        self,
        target: str,
        role: str,
        protocol: str,
        state: str = "unknown",
        helper_path: str | None = None,
    ) -> None:
        _validate_identifier(target, "target")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO hosts(
                    target, role, protocol, state, helper_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(target) DO UPDATE SET
                    role=excluded.role,
                    protocol=excluded.protocol,
                    state=excluded.state,
                    helper_path=COALESCE(excluded.helper_path, hosts.helper_path),
                    updated_at=excluded.updated_at
                """,
                (target, role, protocol, state, helper_path, time.time()),
            )

    def record_host_failure(
        self,
        target: str,
        role: str,
        protocol: str,
        state: str,
        error: str,
        backoff: float,
    ) -> None:
        safe_error = error if error in SAFE_POLL_ERRORS else "transport"
        self.upsert_host(target, role, protocol, state)
        with self._lock:
            self.conn.execute(
                """
                UPDATE hosts SET last_error=?, backoff=?, updated_at=?
                WHERE target=?
                """,
                (safe_error, backoff, time.time(), target),
            )

    def hosts(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM hosts ORDER BY target")

    def overview(self, limit: int = 1000) -> list[sqlite3.Row]:
        """Return host metadata with one bounded latest sample per host."""

        limit = min(max(int(limit), 1), 1000)
        return self.query(
            """
            SELECT h.*,
                   l.poll_id AS sample_poll_id,
                   l.captured_at AS sample_captured_at,
                   l.received_at AS sample_received_at,
                   l.cpu_busy,
                   l.load1,
                   l.load5,
                   l.load15,
                   l.ram_total,
                   l.ram_used,
                   l.root_total,
                   l.root_free,
                   l.capture_skew_seconds,
                   l.collection_duration_seconds,
                   l.partial AS sample_partial,
                   l.boot_id AS sample_boot_id,
                   l.observation_duration_seconds
                       AS sample_observation_duration_seconds,
                   l.visible_processes AS sample_visible_processes,
                   l.emitted_processes AS sample_emitted_processes,
                   l.permission_denied AS sample_permission_denied,
                   l.counters_truncated AS sample_counters_truncated,
                   l.limits_truncated AS sample_limits_truncated,
                   l.nvml_supported AS sample_nvml_supported,
                   l.nvml_error AS sample_nvml_error,
                   l.psutil_error AS sample_psutil_error,
                   (SELECT COUNT(*) FROM gpu_samples AS g
                    WHERE g.poll_id=l.poll_id) AS gpu_count,
                   (SELECT MAX(g.utilization) FROM gpu_samples AS g
                    WHERE g.poll_id=l.poll_id) AS gpu_utilization,
                   (SELECT SUM(g.vram_used) FROM gpu_samples AS g
                    WHERE g.poll_id=l.poll_id) AS gpu_vram_used,
                   (SELECT SUM(g.vram_total) FROM gpu_samples AS g
                    WHERE g.poll_id=l.poll_id) AS gpu_vram_total,
                   (SELECT COUNT(*) FROM user_samples AS u
                    WHERE u.poll_id=l.poll_id) AS visible_users
            FROM hosts AS h
            LEFT JOIN host_samples AS l ON l.poll_id=(
                SELECT hs.poll_id FROM host_samples AS hs
                WHERE hs.target=h.target
                ORDER BY hs.received_at DESC, hs.poll_id DESC LIMIT 1
            )
            ORDER BY h.target
            LIMIT ?
            """,
            (limit,),
        )

    def sparklines(self, points: int = MAX_SPARK_POINTS) -> dict[str, Any]:
        """Return one bounded overview response with a recent CPU series per host.

        Every host contributes at most ``points`` recent non-null ``cpu_busy``
        values in ascending time order, read through the target/time index so
        one dashboard refresh stays bounded even with full retention.
        """

        points = min(max(int(points), 1), MAX_SPARK_POINTS)
        targets = [
            row["target"]
            for row in self.query(
                "SELECT target FROM hosts ORDER BY target LIMIT ?",
                (MAX_SPARK_HOSTS,),
            )
        ]
        series: list[dict[str, Any]] = []
        for target in targets:
            rows = self.query(
                """
                SELECT received_at, cpu_busy FROM host_samples
                WHERE target=? AND cpu_busy IS NOT NULL
                ORDER BY received_at DESC LIMIT ?
                """,
                (target, points),
            )
            series.append(
                {
                    "target": target,
                    "points": [
                        [row["received_at"], row["cpu_busy"]] for row in reversed(rows)
                    ],
                }
            )
        return {"bounded": True, "points": points, "series": series}

    @staticmethod
    def _range_epoch(value: str | None) -> float | None:
        if value is None:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    def host(
        self,
        target: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 2000,
        **_: Any,
    ) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM hosts WHERE target=?", (target,))
        if not rows:
            return None
        limit = min(max(int(limit), 1), 2000)
        predicates = ["target=?"]
        arguments: list[Any] = [target]
        if start is not None:
            predicates.append("received_at>=?")
            arguments.append(self._range_epoch(start))
        if end is not None:
            predicates.append("received_at<=?")
            arguments.append(self._range_epoch(end))
        arguments.append(limit)
        result = dict(rows[0])
        result["items"] = [
            dict(row)
            for row in self.query(
                f"""
                SELECT * FROM host_samples WHERE {" AND ".join(predicates)}
                ORDER BY received_at DESC LIMIT ?
                """,
                arguments,
            )
        ]
        result["processes"] = [dict(row) for row in self.current_processes(target)]
        allocations: dict[int, list[dict[str, Any]]] = {}
        for row in self.query(
            """
            SELECT pid, position, gpu_uuid, gpu_index, vram_bytes
            FROM current_process_allocations
            WHERE target=? ORDER BY pid, position
            """,
            (target,),
        ):
            allocation = dict(row)
            allocations.setdefault(allocation.pop("pid"), []).append(allocation)
        for process in result["processes"]:
            process["gpu_allocations"] = allocations.get(process["pid"], [])
        if result["items"]:
            latest_poll_id = result["items"][0]["poll_id"]
            result["gpus"] = [
                dict(row)
                for row in self.query(
                    "SELECT * FROM gpu_samples WHERE poll_id=? ORDER BY idx LIMIT 32",
                    (latest_poll_id,),
                )
            ]
            result["users"] = [
                dict(row)
                for row in self.query(
                    """
                    SELECT * FROM user_samples WHERE poll_id=?
                    ORDER BY cpu_cores DESC, rss DESC LIMIT 128
                    """,
                    (latest_poll_id,),
                )
            ]
        else:
            result["gpus"] = []
            result["users"] = []
        return result

    def current_processes(self, target: str) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT * FROM current_processes
            WHERE target=? ORDER BY cpu_cores DESC, rss DESC
            """,
            (target,),
        )

    @staticmethod
    def _used_fraction(numerator: Any, denominator: Any) -> float | None:
        if not isinstance(numerator, (int, float)) or isinstance(numerator, bool):
            return None
        if not isinstance(denominator, (int, float)) or isinstance(denominator, bool):
            return None
        if denominator <= 0:
            return None
        return round(numerator / denominator, 4)

    def host_charts(
        self,
        target: str,
        *,
        start: str | None = None,
        end: str | None = None,
        points: int = MAX_CHART_POINTS,
        **_: Any,
    ) -> dict[str, Any]:
        """Return one whole chart group for a target, never one series per request.

        Every series is capped at ``points`` and GPU series are capped at
        ``MAX_CHART_GPUS``, so one bounded response covers CPU, RAM, disk, and
        per-GPU views together.
        """

        points = min(max(int(points), 1), MAX_CHART_POINTS)
        predicates = ["hs.target=?"]
        arguments: list[Any] = [target]
        if start is not None:
            predicates.append("hs.received_at>=?")
            arguments.append(self._range_epoch(start))
        if end is not None:
            predicates.append("hs.received_at<=?")
            arguments.append(self._range_epoch(end))
        arguments.append(points)
        where = " AND ".join(predicates)
        rows = [
            dict(row)
            for row in self.query(
                f"""
                SELECT received_at, cpu_busy, ram_total, ram_used,
                       root_total, root_free
                FROM host_samples AS hs WHERE {where}
                ORDER BY received_at DESC LIMIT ?
                """,
                arguments,
            )
        ]
        rows.reverse()
        series: list[dict[str, Any]] = []

        def add_series(
            chart: str, label: str, sampled: list[dict[str, Any]], key: str
        ) -> None:
            chart_points = []
            for row in sampled:
                value = row.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    chart_points.append([row["received_at"], value])
            series.append(
                {
                    "chart": chart,
                    "label": label,
                    "unit": "percent",
                    "points": chart_points,
                }
            )

        add_series("cpu", "cpu used", rows, "cpu_busy")
        ram_points = []
        disk_points = []
        for row in rows:
            ram = self._used_fraction(row["ram_used"], row["ram_total"])
            if ram is not None:
                ram_points.append([row["received_at"], ram])
            root_used = None
            if isinstance(row["root_total"], (int, float)) and isinstance(
                row["root_free"], (int, float)
            ):
                root_used = self._used_fraction(
                    row["root_total"] - row["root_free"], row["root_total"]
                )
            if root_used is not None:
                disk_points.append([row["received_at"], root_used])
        series.append(
            {
                "chart": "ram",
                "label": "ram used",
                "unit": "percent",
                "points": ram_points,
            }
        )
        series.append(
            {
                "chart": "disk",
                "label": "root disk used",
                "unit": "percent",
                "points": disk_points,
            }
        )
        gpu_rows = self.query(
            f"""
            SELECT g.uuid, g.idx, g.model, g.utilization, g.vram_used, g.vram_total,
                   hs.received_at
            FROM gpu_samples AS g
            JOIN host_samples AS hs ON hs.poll_id=g.poll_id
            WHERE {where}
            ORDER BY hs.received_at DESC LIMIT ?
            """,
            arguments,
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in gpu_rows:
            grouped.setdefault(
                row["uuid"], {"idx": row["idx"], "model": row["model"], "rows": []}
            )["rows"].append(dict(row))
        # Grouping preserves receive order, so the capped set covers the most
        # recent GPUs first (uuid order is insertion order on Python 3.7+).
        for info in list(grouped.values())[:MAX_CHART_GPUS]:
            sampled = list(reversed(info["rows"]))
            label = f"gpu{info['idx']}"
            if isinstance(info["model"], str) and info["model"]:
                label += f" {info['model']}"
            utilization_points = []
            vram_points = []
            for row in sampled:
                if isinstance(row["utilization"], (int, float)) and not isinstance(
                    row["utilization"], bool
                ):
                    utilization_points.append([row["received_at"], row["utilization"]])
                vram = self._used_fraction(row["vram_used"], row["vram_total"])
                if vram is not None:
                    vram_points.append([row["received_at"], vram])
            series.append(
                {
                    "chart": "gpu_util",
                    "label": label,
                    "unit": "percent",
                    "points": utilization_points,
                }
            )
            series.append(
                {
                    "chart": "gpu_vram",
                    "label": label,
                    "unit": "percent",
                    "points": vram_points,
                }
            )
        return {"series": series}

    def jobs(
        self,
        active_only: bool = False,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        predicates: list[str] = []
        arguments: list[Any] = []
        if active_only:
            terminal_predicate = " OR ".join(
                "state LIKE ?" for _ in TERMINAL_SLURM_STATE_PREFIXES
            )
            predicates.append(f"(state IS NULL OR NOT ({terminal_predicate}))")
            arguments.extend(f"{prefix}%" for prefix in TERMINAL_SLURM_STATE_PREFIXES)
        if start is not None:
            predicates.append("updated_at>=?")
            arguments.append(self._range_epoch(start))
        if end is not None:
            predicates.append("updated_at<=?")
            arguments.append(self._range_epoch(end))
        where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
        arguments.append(min(max(int(limit), 1), 500))
        return self.query(
            f"SELECT * FROM slurm_jobs {where} ORDER BY updated_at DESC LIMIT ?",
            arguments,
        )

    def set_slurm_state(
        self,
        target: str,
        watermark: str | None,
        state: str,
        error: str | None = None,
    ) -> None:
        safe_error = error if error in SAFE_POLL_ERRORS else None
        _validate_identifier(target, "target")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO slurm_poll_state VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target) DO UPDATE SET
                    watermark=excluded.watermark,
                    state=excluded.state,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (target, watermark, state, safe_error, time.time()),
            )

    def snapshot(
        self,
        poll_id: str,
        target: str,
        sample: dict[str, Any],
        received_at: float | None = None,
        *,
        started_at: float | None = None,
    ) -> None:
        if not poll_id or not target:
            raise ValueError("poll_id and target required")
        _validate_identifier(poll_id, "poll_id")
        _validate_identifier(target, "target")
        received = time.time() if received_at is None else received_at
        started = received if started_at is None else started_at
        cpu = sample["cpu"]
        memory = sample["memory"]
        disk = sample["disk"]
        visibility = sample.get("visibility") or {}
        limits = sample.get("limits") or {}
        capabilities = sample.get("capabilities") or {}
        captured_epoch = datetime.fromisoformat(
            sample["captured_at"].replace("Z", "+00:00")
        ).timestamp()
        capture_skew = received - captured_epoch

        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    "INSERT INTO polls VALUES (?, ?, ?, ?, ?, ?)",
                    (poll_id, target, started, received, "ok", None),
                )
                cursor.execute(
                    """
                    INSERT INTO host_samples (
                        poll_id, target, captured_at, received_at,
                        cpu_busy, load1, load5, load15, ram_total, ram_used,
                        root_total, root_free, capture_skew_seconds,
                        collection_duration_seconds, partial,
                        boot_id, observation_duration_seconds,
                        visible_processes, emitted_processes, permission_denied,
                        counters_truncated, limits_truncated,
                        nvml_supported, nvml_error, psutil_error
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        poll_id,
                        target,
                        sample["captured_at"],
                        received,
                        cpu.get("busy_fraction"),
                        cpu.get("load_1m"),
                        cpu.get("load_5m"),
                        cpu.get("load_15m"),
                        memory.get("total_bytes"),
                        memory.get("used_bytes"),
                        disk.get("total_bytes"),
                        disk.get("free_bytes"),
                        capture_skew,
                        sample.get("collection_duration_seconds"),
                        int(sample["status"] == "partial"),
                        sample.get("boot_id"),
                        sample.get("observation_duration_seconds"),
                        int(visibility.get("processes_visible") or 0),
                        int(visibility.get("processes_emitted") or 0),
                        int(visibility.get("permission_denied") or 0),
                        int(bool(visibility.get("counters_truncated", False))),
                        int(bool(limits.get("truncated", False))),
                        capabilities.get("nvml_supported"),
                        capabilities.get("nvml_error"),
                        capabilities.get("psutil_error"),
                    ),
                )
                for gpu in sample["gpus"]:
                    cursor.execute(
                        """
                        INSERT INTO gpu_samples VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            poll_id,
                            gpu["uuid"],
                            gpu["index"],
                            gpu.get("model"),
                            gpu.get("utilization_fraction"),
                            gpu.get("vram_total_bytes"),
                            gpu.get("vram_used_bytes"),
                            gpu.get("temperature_c"),
                            gpu.get("power_watts"),
                            gpu.get("compute_process_count") or 0,
                            int(bool(gpu.get("supported", False))),
                            gpu.get("error"),
                            int(bool(gpu.get("mig_detected", False))),
                            int(bool(gpu.get("instance_supported", False))),
                        ),
                    )
                for user in sample["users"]:
                    cursor.execute(
                        """
                        INSERT INTO user_samples VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            poll_id,
                            target,
                            user.get("uid"),
                            user.get("username"),
                            user["cpu_cores"],
                            user["rss_bytes"],
                            user["process_count"],
                            user["gpu_process_count"],
                            user["vram_bytes"],
                        ),
                    )
                cursor.execute(
                    "DELETE FROM current_processes WHERE target=?", (target,)
                )
                for process in sample["processes"]:
                    cursor.execute(
                        """
                        INSERT INTO current_processes VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            target,
                            process["pid"],
                            process.get("create_time"),
                            process.get("name"),
                            process.get("executable"),
                            process.get("uid"),
                            process.get("username"),
                            process.get("cpu_cores"),
                            process.get("rss_bytes"),
                            process.get("gpu_uuid"),
                            process.get("gpu_index"),
                            process.get("vram_bytes"),
                            poll_id,
                        ),
                    )
                    for position, allocation in enumerate(
                        (process.get("gpu_allocations") or [])[
                            :MAX_STORED_GPU_ALLOCATIONS
                        ]
                    ):
                        cursor.execute(
                            """
                            INSERT INTO current_process_allocations VALUES (
                                ?, ?, ?, ?, ?, ?
                            )
                            """,
                            (
                                target,
                                process["pid"],
                                position,
                                allocation.get("gpu_uuid"),
                                allocation.get("gpu_index"),
                                allocation.get("vram_bytes"),
                            ),
                        )
                state = "partial" if sample["status"] == "partial" else "live"
                cursor.execute(
                    """
                    UPDATE hosts SET
                        state=?,
                        helper_version=?,
                        last_captured=?,
                        last_received=?,
                        last_success=?,
                        last_error=NULL,
                        backoff=0,
                        updated_at=?
                    WHERE target=?
                    """,
                    (
                        state,
                        sample["helper_version"],
                        sample["captured_at"],
                        received,
                        received,
                        received,
                        target,
                    ),
                )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise

    def record_error(
        self,
        poll_id: str,
        target: str,
        error: str,
        received_at: float | None = None,
        *,
        started_at: float | None = None,
    ) -> None:
        safe_error = error if error in SAFE_POLL_ERRORS else "transport"
        _validate_identifier(poll_id, "poll_id")
        _validate_identifier(target, "target")
        ended = time.time() if received_at is None else received_at
        started = ended if started_at is None else started_at
        with self._lock:
            self.conn.execute(
                "INSERT INTO polls VALUES (?, ?, ?, ?, ?, ?)",
                (poll_id, target, started, ended, "error", safe_error),
            )

    def upsert_slurm_jobs(
        self, cluster: str, jobs: list[dict[str, Any]], updated_at: float | None = None
    ) -> None:
        if not isinstance(jobs, list):
            raise ValueError("jobs must be a list")
        _validate_identifier(cluster, "cluster")
        now = time.time() if updated_at is None else updated_at
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                for job in jobs[:MAX_STORED_SLURM_JOBS]:
                    if not isinstance(job, dict):
                        continue
                    raw_job_id = job.get("job_id") or job.get("job_id_raw") or ""
                    job_id = _validate_identity(raw_job_id, "job_id")
                    if not job_id:
                        continue
                    array_task = _validate_identity(
                        job.get("array_task_id") or "", "array_task_id"
                    )
                    step_id = _validate_identity(job.get("step_id") or "", "step_id")
                    row_cluster = job.get("cluster")
                    if not isinstance(row_cluster, str) or not row_cluster:
                        row_cluster = cluster
                    _validate_identifier(row_cluster, "cluster")
                    state_value = job.get("state") or job.get("job_state")
                    state = _slurm_state(state_value) or ""
                    payload = json.dumps(
                        job,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    )
                    if len(payload.encode("utf-8")) > 65_536:
                        payload = json.dumps(
                            {
                                "array_task_id": array_task[:256],
                                "job_id": (
                                    raw_job_id[:256]
                                    if isinstance(raw_job_id, str)
                                    else raw_job_id
                                ),
                                "payload_truncated": True,
                                "state": state[:1024],
                                "step_id": step_id[:256],
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    cursor.execute(
                        """
                        INSERT INTO slurm_jobs VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(cluster, job_id, array_task_id, step_id)
                        DO UPDATE SET
                            state=excluded.state,
                            payload=excluded.payload,
                            updated_at=excluded.updated_at
                        """,
                        (
                            row_cluster,
                            job_id,
                            array_task,
                            step_id,
                            state,
                            payload,
                            now,
                        ),
                    )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise

    def _latest_observations(self, target: str, count: int) -> list[dict[str, Any]]:
        """Return up to ``count`` newest distinct observations for a target.

        Each observation is newest-first and carries the host receipt context
        needed for trustworthy classification plus the poll's GPU rows keyed
        by UUID. Only committed host samples count as observations, so an
        error poll never fabricates one. Ordering uses the indexed
        ``started_at``; with at most one in-flight poll per target its order
        equals ``ended_at`` order without a per-query sort.
        """

        observations: list[dict[str, Any]] = []
        for row in self.query(
            """
            SELECT p.poll_id, COALESCE(h.received_at, p.ended_at) AS received_at,
                   h.boot_id, h.nvml_supported, h.nvml_error
            FROM polls p LEFT JOIN host_samples h ON h.poll_id=p.poll_id
            WHERE p.target=?
            ORDER BY p.started_at DESC, p.poll_id DESC LIMIT ?
            """,
            (target, count),
        ):
            observation = dict(row)
            observation["gpus"] = {}
            observations.append(observation)
        for observation in observations:
            for gpu in self.query(
                "SELECT * FROM gpu_samples WHERE poll_id=? ORDER BY idx LIMIT ?",
                (observation["poll_id"], MAX_GPU_ROWS_PER_POLL),
            ):
                observation["gpus"][gpu["uuid"]] = dict(gpu)
        return observations

    def gpu_window(
        self, *, max_targets: int = MAX_GPU_WINDOW_TARGETS
    ) -> dict[str, list[dict[str, Any]]]:
        """Return the latest two observations per known target, bounded.

        Used by read paths that need one classification window for the whole
        fleet (idle-GPU listing, overview summary). One bounded target list,
        then indexed per-target queries.
        """

        max_targets = min(max(int(max_targets), 1), MAX_GPU_WINDOW_TARGETS)
        targets = [
            row["target"]
            for row in self.query(
                "SELECT target FROM hosts ORDER BY target LIMIT ?", (max_targets,)
            )
        ]
        return {target: self._latest_observations(target, 2) for target in targets}

    def gpu_recent(
        self, target: str, per_gpu: int = 2
    ) -> dict[str, list[dict[str, Any] | None]]:
        """Return newest-first observation slots per GPU UUID for a target.

        The window covers exactly the latest ``per_gpu`` distinct host
        observations, never older polls: a GPU row missing from one of them
        yields a slot with ``gpu=None`` so a historic row can never be
        selected across a missing sample. UUIDs seen in either of the two
        latest observations are included.
        """

        if per_gpu < 1 or per_gpu > 8:
            raise ValueError("per_gpu must be 1..8")
        observations = self._latest_observations(target, per_gpu)
        return observation_slots(observations)

    def downsample_history(
        self,
        target: str,
        interval_seconds: float,
        *,
        keep: int = MAX_DOWNSAMPLE_KEEP,
        batch: int = MAX_DOWNSAMPLE_BATCH,
    ) -> int:
        """Thin stored history beyond the newest ``keep`` observations.

        Fast polling must not multiply stored rows: the newest ``keep``
        successful observations always survive (classification needs the
        latest two), while older ones are kept only when spaced at least
        ``interval_seconds`` apart. Victims are deleted as whole polls so
        child samples cascade; error polls and hosts metadata are untouched.
        Work is bounded to one ``batch`` of polls per call.
        """

        if not _is_finite_interval(interval_seconds) or interval_seconds < 0:
            raise ValueError("interval_seconds must be a finite non-negative number")
        if keep < 1 or keep > 8:
            raise ValueError("keep must be 1..8")
        if batch < 1 or batch > MAX_DOWNSAMPLE_BATCH:
            raise ValueError("downsample batch must be 1..500")
        _validate_identifier(target, "target")
        rows = self.query(
            """
            SELECT poll_id, ended_at AS received_at FROM polls
            WHERE target=? AND outcome='ok'
            ORDER BY started_at DESC, poll_id DESC LIMIT ?
            """,
            (target, batch),
        )
        victims: list[str] = []
        seen_buckets: set[int] = set()
        for position, row in enumerate(rows):
            received = row["received_at"]
            bucket = int(received // max(interval_seconds, 1e-6))
            if position >= keep and bucket in seen_buckets:
                victims.append(row["poll_id"])
            seen_buckets.add(bucket)
        if not victims:
            return 0
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.executemany(
                    "DELETE FROM polls WHERE poll_id=?", [(pid,) for pid in victims]
                )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise
        return len(victims)

    def retain(self, before: float, batch: int = 1000) -> int:
        if batch < 1 or batch > 10_000:
            raise ValueError("retention batch must be 1..10000")
        total = 0
        while True:
            with self._lock:
                cursor = self.conn.cursor()
                try:
                    cursor.execute("BEGIN IMMEDIATE")
                    cursor.execute(
                        """
                        DELETE FROM polls WHERE rowid IN (
                            SELECT rowid FROM polls
                            WHERE ended_at < ? LIMIT ?
                        )
                        """,
                        (before, batch),
                    )
                    count = cursor.rowcount
                    terminal_predicate = " OR ".join(
                        "state LIKE ?" for _ in TERMINAL_SLURM_STATE_PREFIXES
                    )
                    cursor.execute(
                        f"""
                        DELETE FROM slurm_jobs
                        WHERE updated_at < ? AND ({terminal_predicate})
                        """,
                        (before,)
                        + tuple(
                            f"{prefix}%" for prefix in TERMINAL_SLURM_STATE_PREFIXES
                        ),
                    )
                    cursor.execute("COMMIT")
                except Exception:
                    cursor.execute("ROLLBACK")
                    raise
            total += count
            if count < batch:
                break
        return total

    def checkpoint(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._secure_files()

    def query(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchall()
