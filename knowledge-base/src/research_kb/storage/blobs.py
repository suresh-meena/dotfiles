from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from research_kb.errors import artifact_unavailable, storage_failure

HASH_PREFIX = "sha256"


def blob_path(root: Path, digest: str) -> Path:
    return root / HASH_PREFIX / digest[:2] / digest


def store_bytes(root: Path, payload: bytes, *, verify: bool = True) -> tuple[str, Path]:
    digest = hashlib.sha256(payload).hexdigest()
    if verify and hashlib.sha256(payload).hexdigest() != digest:
        raise storage_failure("Blob hash verification failed after compute.")
    destination = blob_path(root, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = destination.stat().st_size
        if existing != len(payload):
            raise storage_failure("Existing blob content does not match the announced hash.", path=str(destination))
        return digest, destination
    fd, tmp_name = tempfile.mkstemp(prefix=".stage-", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, destination)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return digest, destination


def stage_and_store(root: Path, source_path: Path, *, max_bytes: int | None = None) -> tuple[str, int, Path]:
    source = Path(source_path)
    if not source.is_file():
        raise artifact_unavailable(f"Source file is not available: {source}")
    digest = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(prefix=".stage-", dir=str(root / HASH_PREFIX))
    try:
        with os.fdopen(fd, "wb") as out_handle, source.open("rb") as in_handle:
            while True:
                chunk = in_handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise storage_failure("Source payload exceeds the configured import size limit.")
                digest.update(chunk)
                out_handle.write(chunk)
            out_handle.flush()
            os.fsync(out_handle.fileno())
        hexdigest = digest.hexdigest()
        destination = blob_path(root, hexdigest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            os.unlink(tmp_name)
            return hexdigest, size, destination
        os.replace(tmp_name, destination)
        return hexdigest, size, destination
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def read_blob(root: Path, digest: str) -> bytes:
    path = blob_path(root, digest)
    if not path.is_file():
        raise artifact_unavailable(f"Blob is not available locally: {digest}")
    return path.read_bytes()


def verify_blob(root: Path, digest: str) -> bool:
    path = blob_path(root, digest)
    if not path.is_file():
        return False
    computed = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            computed.update(chunk)
    return computed.hexdigest() == digest


def gc_plan(
    conn: sqlite3.Connection,
    *,
    root: Path,
    grace_seconds: int = 86400,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    referenced: set[str] = set()
    for row in conn.execute("SELECT blob_hash FROM blob_registry"):
        referenced.add(row["blob_hash"])
    for row in conn.execute("SELECT original_blob_hash AS h FROM source_extractions WHERE original_blob_hash IS NOT NULL"):
        referenced.add(row["h"])
    for row in conn.execute("SELECT extraction_blob_hash AS h FROM source_extractions WHERE extraction_blob_hash IS NOT NULL"):
        referenced.add(row["h"])
    now = time.time()
    plan: list[dict[str, Any]] = []
    base = root / HASH_PREFIX
    if not base.is_dir():
        return plan
    for prefix_dir in sorted(base.iterdir()):
        if not prefix_dir.is_dir():
            continue
        for candidate in sorted(prefix_dir.iterdir()):
            if candidate.name.startswith(".stage-"):
                continue
            if len(candidate.name) != 64:
                continue
            if candidate.name in referenced:
                continue
            age = now - candidate.stat().st_mtime
            if age < grace_seconds:
                continue
            plan.append({"blob": candidate.name, "path": str(candidate), "age_seconds": int(age)})
            if not dry_run:
                candidate.unlink()
    return plan
