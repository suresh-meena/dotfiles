"""Small shared helpers: identifiers, digests, time, canonical JSON."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any


def utcnow() -> str:
    """UTC wall time as RFC 3339 with a Z suffix, for persistence and display.

    Wall time is only ever stored or shown. Elapsed-time decisions inside one
    process use ``monotonic()`` (§4.6), because wall time can jump.
    """
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(text: str) -> _dt.datetime:
    return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc)


def monotonic() -> float:
    return time.monotonic()


def new_id(prefix: str) -> str:
    """A random, unguessable identifier such as ``att_3f9c…``."""
    return f"{prefix}_{secrets.token_hex(12)}"


def canonical_json(value: Any) -> str:
    """JSON with sorted keys and no whitespace: the input to every digest."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write-temp, fsync, rename, fsync the directory (§2.6)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
