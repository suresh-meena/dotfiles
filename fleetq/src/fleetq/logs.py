"""The numpi-side log cache (§6.4).

``fq logs`` reads only this cache; a request never causes a remote read. The
controller's collector appends bounded deltas on its own budgeted cadence.

Offsets are byte offsets into the remote stream, within a *generation*. A
remote file that shrinks (truncated, rotated, recreated) starts a new
generation from 0 and is reported as a reset, not silently spliced. Past the
per-stream cap, the oldest bytes are evicted and ``base`` moves forward; a
read below ``base`` returns an explicit gap. Data is returned as base64: the
cache holds bytes, and a chunk boundary may split a UTF-8 sequence, which the
client decodes incrementally.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

STREAMS = ("stdout", "stderr")
DEFAULT_CAP_BYTES = 64 * 1024 * 1024
MAX_READ_BYTES = 1024 * 1024
_ATTEMPT_RE = re.compile(r"^att_[A-Za-z0-9_-]{1,64}$")


def _empty_meta() -> dict[str, Any]:
    return {"generation": 0, "base": 0, "end": 0, "remote_size": None, "complete": False,
            "collected_at": None, "source_available": None, "resets": 0, "evicted": 0}


class LogCache:
    def __init__(self, root: Path, *, cap_bytes: int = DEFAULT_CAP_BYTES) -> None:
        self.root = root
        self.cap = cap_bytes

    def _paths(self, attempt_id: str, stream: str) -> tuple[Path, Path]:
        if stream not in STREAMS or not _ATTEMPT_RE.match(attempt_id):
            raise ValueError(f"bad log key {attempt_id!r}/{stream!r}")
        d = self.root / attempt_id
        return d / f"{stream}.log", d / f"{stream}.json"

    def meta(self, attempt_id: str, stream: str) -> dict[str, Any]:
        _, meta_path = self._paths(attempt_id, stream)
        try:
            return {**_empty_meta(), **json.loads(meta_path.read_text())}
        except (OSError, ValueError):
            return _empty_meta()

    def _save_meta(self, meta_path: Path, meta: dict[str, Any]) -> None:
        tmp = meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, sort_keys=True))
        os.replace(tmp, meta_path)

    def append(self, attempt_id: str, stream: str, *, offset: int, data: bytes, remote_size: int | None,
               final: bool) -> dict[str, Any]:
        """Record bytes read from ``offset`` of the remote stream. Idempotent for re-reads."""
        log_path, meta_path = self._paths(attempt_id, stream)
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        meta = self.meta(attempt_id, stream)
        if remote_size is not None and remote_size < meta["end"]:
            # The remote file shrank: a new generation, never a silent splice.
            meta.update(generation=meta["generation"] + 1, base=0, end=0, complete=False,
                        resets=meta["resets"] + 1)
            log_path.write_bytes(b"")
        if offset == meta["end"] and data:
            with open(log_path, "ab") as handle:
                handle.write(data)
            meta["end"] += len(data)
            size = meta["end"] - meta["base"]
            if size > self.cap:
                keep = self.cap // 2
                with open(log_path, "rb") as handle:
                    handle.seek(size - keep)
                    tail = handle.read()
                tmp = log_path.with_suffix(".log.tmp")
                tmp.write_bytes(tail)
                os.replace(tmp, log_path)
                meta["evicted"] += meta["end"] - keep - meta["base"]
                meta["base"] = meta["end"] - keep
        meta.update(remote_size=remote_size, collected_at=time.time(), source_available=remote_size is not None)
        if final and remote_size is not None and meta["end"] >= remote_size:
            meta["complete"] = True
        self._save_meta(meta_path, meta)
        return meta

    def mark_source_gone(self, attempt_id: str, stream: str) -> None:
        """The remote file can't be read any more; what we have is all there will be."""
        log_path, meta_path = self._paths(attempt_id, stream)
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        meta = self.meta(attempt_id, stream)
        meta.update(source_available=False, complete=True, collected_at=time.time())
        self._save_meta(meta_path, meta)

    def read(self, attempt_id: str, stream: str, *, offset: int | None, max_bytes: int,
             generation: int | None = None) -> dict[str, Any]:
        log_path, _ = self._paths(attempt_id, stream)
        meta = self.meta(attempt_id, stream)
        max_bytes = max(0, min(int(max_bytes), MAX_READ_BYTES))
        reset = generation is not None and generation != meta["generation"]
        if offset is None or reset:
            offset = meta["base"]
        gap = None
        if offset < meta["base"]:
            gap = {"from": offset, "to": meta["base"], "reason": "evicted"}
            offset = meta["base"]
        offset = min(offset, meta["end"])
        data = b""
        if max_bytes and offset < meta["end"]:
            with open(log_path, "rb") as handle:
                handle.seek(offset - meta["base"])
                data = handle.read(max_bytes)
        collected = meta["collected_at"]
        return {
            "generation": meta["generation"], "reset": reset, "offset": offset,
            "next_offset": offset + len(data), "data_b64": base64.b64encode(data).decode(),
            "gap": gap, "base": meta["base"], "end": meta["end"], "remote_size": meta["remote_size"],
            "complete": meta["complete"], "source_available": meta["source_available"],
            "collected_at": collected, "age_s": round(time.time() - collected, 3) if collected else None,
        }
