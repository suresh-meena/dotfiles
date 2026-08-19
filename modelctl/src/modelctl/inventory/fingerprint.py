from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def fingerprint_from_listing(canonical_path: str, files: list[dict[str, Any]], config_hash: str | None = None, index_hash: str | None = None) -> str:
    """Compute artifact fingerprint per spec §5.5 without copying model data."""
    # files: list of {relative, size, mtime_ns}
    sorted_files = sorted(files, key=lambda x: x["relative"])
    payload = {
        "canonical_path": canonical_path,
        "files": sorted_files,
        "config_hash": config_hash,
        "index_hash": index_hash,
    }
    j = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(j.encode()).hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
