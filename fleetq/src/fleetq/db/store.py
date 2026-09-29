"""The database owner: one thread, one connection, one transaction at a time (§5).

Every read and write runs as a function on the owner thread inside
``BEGIN IMMEDIATE ... COMMIT``.  HTTP handlers and long-polls never hold a
transaction: they submit a function and await its result, so a slow client
can't pin a read snapshot or block the scheduler's writes.

Checkpoints, migrations and online backups also run on this thread, which is
what "keep checkpoint ownership explicit" in §5 means in practice.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import sqlite3
import stat
import threading
import tempfile
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, TypeVar

from ..errors import FqError
from . import schema

T = TypeVar("T")

MAX_QUEUE_DEPTH = 2000

# The WAL-reset fix (§5, R18): 3.51.3 and later, backported to 3.50.7 and 3.44.6.
_PATCHED_RANGES = (
    ((3, 44, 6), (3, 45, 0)),
    ((3, 50, 7), (3, 51, 0)),
    ((3, 51, 3), (999, 0, 0)),
)


def sqlite_version_tuple(text: str | None = None) -> tuple[int, int, int]:
    parts = (text or sqlite3.sqlite_version).split(".")
    nums = [int(p) for p in parts[:3]] + [0] * (3 - len(parts[:3]))
    return nums[0], nums[1], nums[2]


def sqlite_has_wal_reset_fix(text: str | None = None) -> bool:
    """True when the linked SQLite is at or past a release carrying the fix.

    A distribution can backport the fix without changing the version string, so
    False here means "not proven by version", not "proven vulnerable". The
    ``sqlite_backport_attestation`` config setting records that provenance
    explicitly instead of guessing.
    """
    version = sqlite_version_tuple(text)
    return any(low <= version < high for low, high in _PATCHED_RANGES)


def check_sqlite_gate(attestation: str | None) -> tuple[bool, str]:
    linked = sqlite3.sqlite_version
    if sqlite_has_wal_reset_fix(linked):
        return True, f"SQLite {linked} includes the WAL-reset fix"
    if attestation:
        return True, f"SQLite {linked} accepted by attestation: {attestation}"
    return False, (
        f"linked SQLite {linked} predates the WAL-reset fix (3.51.3, or backports "
        "3.50.7 / 3.44.6). Upgrade it, or set sqlite_backport_attestation to the "
        "distribution advisory proving the fix was backported."
    )


class Store:
    """Owns the connection; everything else talks to it through ``run``."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._queue: "queue.Queue[tuple[Callable[[sqlite3.Connection], Any], Future] | None]" = queue.Queue(
            maxsize=MAX_QUEUE_DEPTH
        )
        self._thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None
        self._ready = threading.Event()
        self._open_error: BaseException | None = None

    # ---- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        self._thread = threading.Thread(target=self._main, name="fleetq-db", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._open_error is not None:
            raise self._open_error

    def close(self) -> None:
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout=30)
        self._thread = None

    def _main(self) -> None:
        try:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal" and self.path != ":memory:":
                raise RuntimeError(f"could not enable WAL (got {mode!r})")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA busy_timeout = 5000")
            migrate(conn)
            self._conn = conn
        except BaseException as exc:  # surfaced to open()
            self._open_error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._queue.get()
            if item is None:
                break
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(conn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                fut.set_result(result)
            except BaseException as exc:
                fut.set_exception(exc)
        conn.close()

    # ---- execution -----------------------------------------------------------

    def submit(self, fn: Callable[[sqlite3.Connection], T]) -> "Future[T]":
        fut: Future = Future()
        try:
            self._queue.put_nowait((fn, fut))
        except queue.Full:
            raise FqError("not_ready", "database queue is full; the controller is overloaded", retry_after=2.0)
        return fut

    def run_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return self.submit(fn).result()

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.wrap_future(self.submit(fn))

    # ---- maintenance ---------------------------------------------------------

    def backup(self, dest: Path | str) -> None:
        """Publish a verified online backup without ever replacing a path."""
        dest = Path(dest).expanduser()
        parent = dest.parent
        if not parent.is_dir() or parent.is_symlink():
            raise FqError("invalid_argument", f"backup destination parent must be an existing non-symlink directory: {parent}")
        # Resolve the parent for alias checks, but never resolve the destination
        # itself: an existing symlink is an occupied destination and is refused.
        final_dest = parent.resolve() / dest.name
        if self.path != ":memory:":
            source = Path(self.path).resolve()
            if final_dest == source:
                raise FqError("invalid_argument", "backup destination aliases the live database")
        if os.path.lexists(final_dest):
            raise FqError("conflict", f"backup destination already exists: {final_dest}")

        try:
            fd, temp_name = tempfile.mkstemp(prefix=".fleetq-backup-", suffix=".tmp", dir=parent)
        except OSError as exc:
            raise FqError("unsafe_storage", f"could not create private backup temporary file in {parent}: {exc}") from exc
        os.close(fd)
        temp_path = Path(temp_name)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute("COMMIT")  # the backup API must not run inside our txn
            try:
                target = sqlite3.connect(str(temp_path))
                try:
                    conn.backup(target)
                    check = target.execute("PRAGMA quick_check").fetchone()
                    if check is None or check[0] != "ok":
                        raise RuntimeError(f"SQLite backup quick_check failed: {check[0] if check else 'no result'}")
                finally:
                    target.close()
                with temp_path.open("rb") as backed_up:
                    os.fsync(backed_up.fileno())
                # link() is atomic and fails if any file, directory, or symlink
                # appeared at the destination since the preflight check.
                os.link(temp_path, final_dest)
                temp_path.unlink()
                dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                conn.execute("BEGIN IMMEDIATE")

        try:
            self.run_sync(_do)
        except FileExistsError as exc:
            raise FqError("conflict", f"backup destination already exists: {final_dest}") from exc
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            raise FqError("unsafe_storage", f"could not create verified backup at {final_dest}: {exc}") from exc
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def migrate(conn: sqlite3.Connection) -> None:
    """Create or verify the schema. Refuses anything it doesn't understand."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }
    if version > schema.SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is newer than this fleetq ({schema.SCHEMA_VERSION}); "
            "refusing to open it (downgrades are not supported)"
        )
    if version == 0:
        if tables:
            raise RuntimeError("database has tables but no schema version; refusing to guess what it is")
        conn.executescript("BEGIN;" + schema.DDL + f"PRAGMA user_version = {schema.SCHEMA_VERSION}; COMMIT;")
        return
    if version == 1:
        conn.executescript(
            "BEGIN;"
            "CREATE TABLE managed_slurm_snapshots ("
            "site_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,"
            "attempted_at TEXT NOT NULL,last_success_at TEXT,"
            "complete INTEGER NOT NULL CHECK (complete IN (0,1)),error TEXT,"
            "jobs_json TEXT NOT NULL DEFAULT '[]' CHECK (length(jobs_json) <= 524288),"
            "row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),"
            "output_bytes INTEGER NOT NULL DEFAULT 0 CHECK (output_bytes >= 0));"
            "PRAGMA user_version = 2;COMMIT;"
        )
        version = 2
    if version == 2:
        conn.executescript(
            "BEGIN;"
            "CREATE TABLE budget_dimension_buckets ("
            "cluster TEXT NOT NULL,"
            "dimension TEXT NOT NULL CHECK (dimension IN ('sessions','bytes')),"
            "tokens REAL NOT NULL,updated_at TEXT NOT NULL,"
            "PRIMARY KEY (cluster, dimension));"
            "PRAGMA user_version = 3;COMMIT;"
        )
        version = 3
    if version == 3:
        conn.executescript(
            "BEGIN;"
            "CREATE TABLE remote_cache_pin_release_acks ("
            "attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),"
            "acknowledged_at TEXT NOT NULL,"
            "controller_epoch INTEGER NOT NULL);"
            "PRAGMA user_version = 4;COMMIT;"
        )


# ---- state-volume guard (§11) ------------------------------------------------

SENTINEL_NAME = ".fleetq-volume"


def validate_state_dir_path(state_dir: Path) -> Path:
    """Require a dedicated absolute path with no traversal or symlink component."""
    raw = os.fspath(state_dir)
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"state directory must be an absolute canonical path without '..': {path}")
    home = Path.home().resolve()
    normalized = Path(os.path.normpath(raw))
    if normalized == Path("/") or normalized == home or normalized in home.parents:
        raise RuntimeError(f"state directory must be dedicated and cannot be /, HOME, or an ancestor of HOME: {path}")
    protected = ("/etc", "/usr", "/var", "/root", "/boot", "/proc", "/sys", "/dev",
                 "/bin", "/sbin", "/lib", "/lib64", "/opt", "/run")
    if any(normalized == Path(prefix) or Path(prefix) in normalized.parents for prefix in protected):
        raise RuntimeError(f"state directory cannot be inside a protected system directory: {path}")
    if normalized.is_relative_to(home):
        home_parts = normalized.relative_to(home).parts
        if (home_parts and home_parts[0] in {".ssh", ".gnupg", ".config", ".cache"}
                or home_parts[:1] == (".local",) and (len(home_parts) < 3 or home_parts[1] != "state")):
            raise RuntimeError(f"state directory cannot be inside a HOME configuration directory: {path}")
    # lstat every existing component, including the leaf; Path.resolve() alone
    # would silently accept symlink aliases.
    current = Path("/")
    for component in normalized.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"cannot inspect state path component {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"state path contains a symlink component: {current}")
    return normalized


def check_state_volume(state_dir: Path, expected_volume_id: str | None,
                       expected_fs_uuid: str | None = None, *, require_identity: bool = False) -> None:
    """Refuse to start on the wrong or missing filesystem.

    ``RequiresMountsFor`` in a user unit is not a complete guard (§11): if the
    SSD isn't mounted, the directory may still exist on the SD card. The
    sentinel holds an id written at install time; a missing or different id
    means this is not the approved state volume, and fleetqd does not create an
    empty database here.
    """
    state_dir = validate_state_dir_path(Path(state_dir))
    if require_identity and (not expected_volume_id or not expected_fs_uuid):
        raise RuntimeError("production state needs both volume_id and state_fs_uuid in [daemon]")
    try:
        directory = state_dir.lstat()
    except OSError as exc:
        raise RuntimeError(f"state directory is unavailable: {state_dir}: {exc}") from exc
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise RuntimeError(f"state directory must be a real, owner-only directory: {state_dir}")
    sentinel = state_dir / SENTINEL_NAME
    try:
        sent = sentinel.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"state volume sentinel {sentinel} is missing: this is not the approved state "
            "volume (is the SSD mounted?). Refusing to create a new database here."
        ) from exc
    if not stat.S_ISREG(sent.st_mode) or sent.st_uid != os.getuid() or sent.st_mode & 0o077:
        raise RuntimeError(f"state volume sentinel must be an owned, private regular file: {sentinel}")
    found = sentinel.read_text().strip()
    if expected_volume_id is not None and found != expected_volume_id:
        raise RuntimeError(
            f"state volume sentinel says {found!r}, config expects {expected_volume_id!r}; refusing to start"
        )
    if expected_fs_uuid is not None:
        if not re.fullmatch(r"[0-9A-Fa-f-]{8,64}", expected_fs_uuid):
            raise RuntimeError("state_fs_uuid is not a filesystem UUID")
        device = Path("/dev/disk/by-uuid") / expected_fs_uuid
        try:
            approved_dev = device.stat().st_rdev
        except OSError as exc:
            raise RuntimeError(f"approved state filesystem UUID {expected_fs_uuid} is unavailable") from exc
        if approved_dev != directory.st_dev:
            raise RuntimeError(f"state directory {state_dir} is not on approved filesystem {expected_fs_uuid}")
    database = state_dir / "fleetq.db"
    if database.is_symlink():
        raise RuntimeError(f"database path is a symlink: {database}")
    if database.exists():
        info = database.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError(f"database must be an owned, private regular file: {database}")
