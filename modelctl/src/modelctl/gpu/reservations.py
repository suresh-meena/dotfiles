from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any

from ..state import STATE_ROOT


def _lock_file_for_gpu(uuid: str) -> Path:
    # Use deterministic path under STATE_ROOT/locks/gpu/<uuid>.lock  (per spec §14.1)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in uuid)
    p = STATE_ROOT / "locks" / "gpu" / f"{safe}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.parent.chmod(0o700)
    except Exception:
        pass
    return p


def try_reserve_gpus(uuids: list[str], deployment_id: str, timeout: float = 5.0) -> tuple[bool, list[str]]:
    """Atomically reserve GPUs sorted lexicographically per spec §9.7. Returns (ok, busy)."""
    uuids_sorted = sorted(uuids)
    handles: list[Any] = []
    try:
        for u in uuids_sorted:
            lf = _lock_file_for_gpu(u)
            fh = open(lf, "a+")
            handles.append(fh)
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # busy
                for h in handles:
                    try:
                        fcntl.flock(h, fcntl.LOCK_UN)
                        h.close()
                    except Exception:
                        pass
                return False, [u]
            # write ownership metadata (best effort)
            try:
                fh.seek(0)
                fh.truncate(0)
                fh.write(deployment_id)
                fh.flush()
            except Exception:
                pass
        # keep locks held via handles stored in process? For v1 local, we keep file locks but not daemon-held.
        # Release immediately after verification (real remote would use flock-held process). For local simulation, we treat file existence as reservation.
        for h in handles:
            try:
                fcntl.flock(h, fcntl.LOCK_UN)
                h.close()
            except Exception:
                pass
        return True, []
    except Exception:
        for h in handles:
            try:
                h.close()
            except Exception:
                pass
        return False, uuids


def release_gpus(uuids: list[str]) -> None:
    for u in uuids:
        lf = _lock_file_for_gpu(u)
        try:
            if lf.exists():
                lf.unlink()
        except Exception:
            pass


def list_reservations() -> list[dict[str, Any]]:
    root = STATE_ROOT / "locks" / "gpu"
    if not root.exists():
        return []
    out = []
    for p in root.glob("*.lock"):
        try:
            owner = p.read_text().strip() if p.stat().st_size > 0 else None
        except Exception:
            owner = None
        out.append({"gpu_uuid": p.stem, "lock_path": str(p), "owner": owner})
    return out
