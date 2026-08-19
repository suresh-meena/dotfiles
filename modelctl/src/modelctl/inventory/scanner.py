from __future__ import annotations

import hashlib
import json
import posixpath
from pathlib import Path
from typing import Any

from ..errors import ModelctlError

# Recognizable model dir markers per spec §5.4
MARKERS = {"config.json"}

def _is_model_dir_listing(files: list[str]) -> bool:
    # check presence of config.json and at least one safetensors or index
    has_config = "config.json" in files
    has_weights = any(f.endswith(".safetensors") or f == "model.safetensors.index.json" for f in files)
    has_tokenizer = any(f.startswith("tokenizer") for f in files)
    return has_config and (has_weights or has_tokenizer)


def scan_local_roots(roots: list[str]) -> list[dict[str, Any]]:
    """Local filesystem scan for demo/testing - restricted to declared roots."""
    out: list[dict[str, Any]] = []
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        for child in rp.iterdir():
            if not child.is_dir():
                continue
            try:
                files = [p.name for p in child.iterdir() if p.is_file()]
            except PermissionError:
                continue
            if _is_model_dir_listing(files):
                # fingerprint stub
                listing = []
                for p in child.iterdir():
                    if p.is_file():
                        stat = p.stat()
                        listing.append({"relative": p.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                # hash config.json if present
                cfg_hash = None
                cfg_path = child / "config.json"
                if cfg_path.exists():
                    h = hashlib.sha256()
                    h.update(cfg_path.read_bytes())
                    cfg_hash = h.hexdigest()
                idx_hash = None
                for idx_name in ("model.safetensors.index.json", "pytorch_model.bin.index.json"):
                    idx_path = child / idx_name
                    if idx_path.exists():
                        h = hashlib.sha256()
                        h.update(idx_path.read_bytes())
                        idx_hash = h.hexdigest()
                        break
                # compute fingerprint
                from .fingerprint import fingerprint_from_listing

                fp = fingerprint_from_listing(str(child), listing, cfg_hash, idx_hash)
                total_size = sum(x["size"] for x in listing)
                out.append(
                    {
                        "canonical_path": str(child),
                        "files": listing,
                        "config_hash": cfg_hash,
                        "index_hash": idx_hash,
                        "fingerprint": fp,
                        "size_bytes": total_size,
                    }
                )
    return out


def validate_path_inside_roots(path: str, roots: list[str]) -> bool:
    # canonicalize posix
    canon = posixpath.normpath(path)
    for r in roots:
        rn = posixpath.normpath(r)
        if canon == rn or canon.startswith(rn.rstrip("/") + "/"):
            return True
    return False


def remote_scan_command(roots: list[str]) -> list[str]:
    """Return argv for remote scan helper (POSIX). The actual remote invocation is via SSH transport with strict argv encoding."""
    # We emit a python snippet that walks only declared roots; no shell interpolation.
    # The SSH adapter will execute this as `python3 -c '<snippet>'` with argv vector, not shell.
    # For now return the marker for transport layer.
    return ["python3", "-c", f"import os,json,hashlib;roots={json.dumps(roots)};print(json.dumps({{'roots':roots}}))"]
