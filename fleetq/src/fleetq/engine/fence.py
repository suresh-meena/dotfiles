"""Controller identity, epochs, and the single-controller lock (§1.3).

* ``fleet_id`` persists across restarts; it names this deployment.
* ``controller_epoch`` increments on every start and is persisted before use.
  Nodes and cluster control roots record the highest epoch they have
  accepted and refuse launch mutations from a lower one, so a stale process,
  or a controller restored from an old backup, can't launch work.
* ``process_instance_id`` is diagnostic only. It must never be used as
  identity, or a restart would orphan our own markers (ledger L111).
* One controller at a time holds an exclusive ``flock`` on the state
  directory: no active-active mode exists.
"""

from __future__ import annotations

import fcntl
import os
import stat
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..util import new_id, utcnow
from . import state

LOCK_NAME = "controller.lock"


@dataclass(frozen=True)
class ControllerIdentity:
    fleet_id: str
    epoch: int
    process_instance_id: str
    restored: bool
    restore_epoch_raise_allowed: bool


class ControllerLockHeld(RuntimeError):
    pass


class ControllerLock:
    """An exclusive, non-blocking flock held for the controller's lifetime."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise RuntimeError(f"unsafe controller lock file: {self.path}")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ControllerLockHeld(
                f"another fleetqd holds {self.path}; only one controller may run (§1.1)"
            ) from None
        except BaseException:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM controller_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO controller_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def ensure_fleet_id(conn: sqlite3.Connection) -> str:
    """This installation's persistent identity, created once; never bumps the epoch."""
    fleet_id = _meta(conn, "fleet_id")
    if fleet_id is None:
        fleet_id = new_id("fleet")
        _set_meta(conn, "fleet_id", fleet_id)
        _set_meta(conn, "created_at", utcnow())
    return fleet_id


def start_controller(conn: sqlite3.Connection, *, restored_from_backup: bool = False) -> ControllerIdentity:
    """Establish identity and a fresh epoch; persisted before anything uses it.

    On a restore (``restored_from_backup``), the database's epoch may be lower
    than one a target already accepted. Dispatch is therefore disabled
    globally (``restore_discovery_pending``) until every target is fenced
    above its highest accepted epoch and its namespace enumerated (§1.3).
    """
    fleet_id = ensure_fleet_id(conn)
    epoch = int(_meta(conn, "controller_epoch") or "0") + 1
    _set_meta(conn, "controller_epoch", str(epoch))
    _set_meta(conn, "epoch_started_at", utcnow())
    if restored_from_backup:
        _set_meta(conn, "restore_discovery_pending", "1")
        _set_meta(conn, "restore_epoch_raise_allowed", "1")
    # A new epoch invalidates every target's fence: each must be re-fenced and
    # reconciled before dispatch there resumes (§1.3, §12).
    conn.execute("UPDATE nodes SET reconciled_epoch = NULL")
    ident = ControllerIdentity(
        fleet_id=fleet_id,
        epoch=epoch,
        process_instance_id=new_id("proc"),
        restored=restored_from_backup or _meta(conn, "restore_discovery_pending") == "1",
        restore_epoch_raise_allowed=(restored_from_backup
                                     or _meta(conn, "restore_epoch_raise_allowed") == "1"),
    )
    state.add_event(conn, "controller_started", actor="controller",
                    detail={"epoch": epoch, "instance": ident.process_instance_id, "restored": ident.restored})
    return ident


def raise_epoch_above(conn: sqlite3.Connection, highest_seen: int) -> int:
    """After restore discovery: make our epoch exceed every accepted epoch."""
    current = int(_meta(conn, "controller_epoch") or "0")
    if highest_seen >= current:
        current = highest_seen + 1
        _set_meta(conn, "controller_epoch", str(current))
        state.add_event(conn, "epoch_raised", actor="controller", detail={"epoch": current, "seen": highest_seen})
    return current


def current_epoch(conn: sqlite3.Connection) -> int:
    return int(_meta(conn, "controller_epoch") or "0")


def restore_discovery_pending(conn: sqlite3.Connection) -> bool:
    return _meta(conn, "restore_discovery_pending") == "1"


def complete_restore_discovery(conn: sqlite3.Connection) -> None:
    _set_meta(conn, "restore_discovery_pending", "0")
    _set_meta(conn, "restore_epoch_raise_allowed", "0")
    state.add_event(conn, "restore_discovery_complete", actor="controller")


def mark_target_reconciled(conn: sqlite3.Connection, target: str, epoch: int) -> None:
    conn.execute(
        "UPDATE nodes SET fence_epoch = ?, reconciled_epoch = ?, updated_at = ? WHERE id = ?",
        (epoch, epoch, utcnow(), target),
    )
    state.add_event(conn, "target_reconciled", target=target, actor="controller", detail={"epoch": epoch})


def target_dispatch_ready(conn: sqlite3.Connection, target: str) -> bool:
    """A target may receive new work only once fenced at the current epoch."""
    row = conn.execute("SELECT reconciled_epoch FROM nodes WHERE id = ?", (target,)).fetchone()
    return bool(row) and row["reconciled_epoch"] == current_epoch(conn) and not restore_discovery_pending(conn)
