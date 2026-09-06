"""Small local operational state and singleton lock helpers."""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import IO, Any

ERROR_CODES = {
    "timeout",
    "output_overflow",
    "transport",
    "invalid_json",
    "invalid_schema",
    "version_mismatch",
    "polling_disabled",
    "disk_low",
}
MAX_STATE_BYTES = 1024 * 1024
MAX_TARGET_ENTRIES = 1024
LAST_UPDATED_KEY = "_updated"
ACTIVE_KEYS = ("last_success", "helper_path")


def reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def check_json_depth(
    value: Any,
    *,
    limit: int,
    depth_message: str,
    nonfinite_message: str | None = None,
    depth: int = 0,
) -> None:
    if depth > limit:
        raise ValueError(depth_message)
    if isinstance(value, dict):
        for item in value.values():
            check_json_depth(
                item,
                limit=limit,
                depth_message=depth_message,
                nonfinite_message=nonfinite_message,
                depth=depth + 1,
            )
    elif isinstance(value, (list, tuple)):
        for item in value:
            check_json_depth(
                item,
                limit=limit,
                depth_message=depth_message,
                nonfinite_message=nonfinite_message,
                depth=depth + 1,
            )
    elif (
        isinstance(value, float)
        and not math.isfinite(value)
        and nonfinite_message is not None
    ):
        raise ValueError(nonfinite_message)


def error_code(value: str | BaseException) -> str:
    text = str(value).lower()
    if "timeout" in text or "timed" in text:
        return "timeout"
    if "overflow" in text or "too large" in text:
        return "output_overflow"
    if "json" in text or "decode" in text:
        return "invalid_json"
    if "schema" in text or "protocol" in text:
        return "invalid_schema"
    if "version" in text:
        return "version_mismatch"
    return "transport"


def sanitize_error(value: str | BaseException) -> str:
    """Map exceptions to a small code; never retain stdout, stderr, or paths."""

    code = error_code(value)
    return code if code in ERROR_CODES else "transport"


class OperationalState:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        if self.path.is_symlink():
            raise PermissionError("state path must not be a symlink")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = os.lstat(self.path.parent)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise NotADirectoryError(self.path.parent)
        if parent_stat.st_mode & 0o077:
            raise PermissionError("state directory must not be group/world accessible")
        self.data: dict[str, Any] = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                return {}
            value = json.loads(
                self.path.read_text(encoding="utf-8"),
                parse_constant=reject_json_constant,
            )
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, RecursionError):
            return {}

    def save(self) -> None:
        self._prune_targets()
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    self.data,
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
            # Persist the atomic rename itself so a power loss cannot leave
            # the old state file selected after the new contents were fsynced.
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name)

    def update_target(self, target: str, **values: Any) -> None:
        targets = self.data.setdefault("targets", {})
        entry = targets.setdefault(target, {})
        entry.update(values)
        entry[LAST_UPDATED_KEY] = time.time()
        self.save()

    def target(self, target: str) -> dict[str, Any]:
        targets = self.data.get("targets", {})
        if not isinstance(targets, dict):
            return {}
        entry = targets.get(target, {})
        return dict(entry) if isinstance(entry, dict) else {}

    def _prune_targets(self) -> None:
        """Bound stored targets and their encoded size before every write.

        Churned or retired entries grow without limit, so drop the
        least-recently-updated prunable entries first (active targets keep a
        ``last_success`` or ``helper_path`` and are protected), then the
        oldest entries overall only if the count cap still cannot be met.
        """

        targets = self.data.get("targets")
        if not isinstance(targets, dict):
            return
        prunable = {
            name
            for name, entry in targets.items()
            if isinstance(entry, dict)
            and not any(entry.get(key) for key in ACTIVE_KEYS)
        }
        if len(targets) > MAX_TARGET_ENTRIES:
            order = sorted(
                targets,
                key=lambda name: (
                    name not in prunable,
                    _updated_at(targets.get(name)),
                    name,
                ),
            )
            for name in order[: len(targets) - MAX_TARGET_ENTRIES]:
                del targets[name]
                prunable.discard(name)
        while _encoded_size(self.data) > MAX_STATE_BYTES:
            candidates = sorted(
                prunable,
                key=lambda name: (_updated_at(targets.get(name)), name),
            )
            if not candidates:
                break
            del targets[candidates[0]]
            prunable.discard(candidates[0])


def _updated_at(entry: Any) -> float:
    if isinstance(entry, dict):
        value = entry.get(LAST_UPDATED_KEY)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _encoded_size(data: dict[str, Any]) -> int:
    try:
        return len(
            json.dumps(
                data, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        return MAX_STATE_BYTES + 1


class HubLock:
    """Advisory singleton lock held for the lifetime of the hub."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        if self.path.is_symlink():
            raise PermissionError("hub lock path must not be a symlink")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = os.lstat(self.path.parent)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise NotADirectoryError(self.path.parent)
        if parent_stat.st_mode & 0o077:
            raise PermissionError("lock directory must not be group/world accessible")
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        handle = os.fdopen(descriptor, "a+", encoding="ascii")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("hub is already running") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
        # Keep the inode in place. Unlinking after unlock permits a race where
        # another process locks the old inode while a third locks a new file.
