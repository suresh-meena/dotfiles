from __future__ import annotations

import fcntl
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..runtime.vllm import pid_alive
from ..state import STATE_ROOT

# Ownership model per spec §14.1/§14.2:
#  - the flock on the .lock file provides atomicity for reserve/release/steal
#    operations (short-lived, held only while mutating state);
#  - persistent reservation ownership lives in a meta file next to the lock:
#    {"deployment_id": ..., "pid": ..., "reserved_at": ...};
#  - a reservation is live while its owner pid is alive; a dead owner pid
#    makes the reservation stale and reclaimable;
#  - pid 0 marks an in-flight managed reservation (created before spawn);
#    it is not stealable by reserve(), but cleanup_stale() may reclaim it
#    once it is older than its age threshold and the flock is acquirable.


def _lock_file_for_gpu(uuid: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in uuid)
    p = STATE_ROOT / "locks" / "gpu" / f"{safe}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.parent.chmod(0o700)
    except Exception:
        pass
    return p


def _meta_file_for_gpu(uuid: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in uuid)
    return STATE_ROOT / "locks" / "gpu" / f"{safe}.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_meta(uuid: str, deployment_id: str, pid: int) -> None:
    meta = {"deployment_id": deployment_id, "pid": pid, "reserved_at": _utc_now()}
    mf = _meta_file_for_gpu(uuid)
    tmp = mf.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, sort_keys=True))
    tmp.replace(mf)


def _read_meta(uuid: str) -> dict[str, Any] | None:
    mf = _meta_file_for_gpu(uuid)
    try:
        return json.loads(mf.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        return {"deployment_id": None, "pid": None, "reserved_at": None}


def reservation_is_stale(meta: dict[str, Any] | None, in_flight_max_age_s: int = 300) -> bool:
    """A reservation is stale when its owner is provably dead, or when an
    in-flight (pid 0) reservation is older than the in-flight age threshold."""
    if not meta:
        return True
    pid = meta.get("pid")
    if pid is None:
        return True
    if pid == 0:
        reserved_at = meta.get("reserved_at")
        try:
            age = time.time() - datetime.fromisoformat(reserved_at).timestamp() if reserved_at else 0
        except Exception:
            age = 0
        return age > in_flight_max_age_s
    return not pid_alive(pid)


def try_reserve_gpus(uuids: list[str], deployment_id: str, pid: int | None = None) -> tuple[bool, list[str]]:
    """Atomically reserve GPUs sorted lexicographically per spec §9.7.

    Returns (ok, busy). A busy result lists the first GPU that could not be
    reserved. A reservation with a live owner pid is never stealable here.
    """
    owner_pid = pid if pid else 0
    uuids_sorted = sorted(uuids)
    acquired: list[tuple[str, Any]] = []
    try:
        for u in uuids_sorted:
            lf = _lock_file_for_gpu(u)
            fh = open(lf, "a+")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fh.close()
                # flock is held by a live process => genuinely busy
                for _, h in acquired:
                    try:
                        fcntl.flock(h, fcntl.LOCK_UN)
                    except Exception:
                        pass
                    h.close()
                return False, [u]
            # flock acquired: ownership is decided by meta + live pid
            meta = _read_meta(u)
            if meta and not reservation_is_stale(meta):
                # live reservation owned by someone else (pid 0 in-flight or live pid)
                try:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except Exception:
                    pass
                fh.close()
                for _, h in acquired:
                    try:
                        fcntl.flock(h, fcntl.LOCK_UN)
                    except Exception:
                        pass
                    h.close()
                return False, [u]
            _write_meta(u, deployment_id, owner_pid)
            acquired.append((u, fh))
        for _, fh in acquired:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:
                pass
            fh.close()
        return True, []
    except Exception:
        for _, fh in acquired:
            try:
                fh.close()
            except Exception:
                pass
        return False, uuids


def update_owner_pid(uuid: str, deployment_id: str, pid: int) -> bool:
    """Record the spawned server pid on an in-flight reservation."""
    if not pid or pid <= 0:
        return False
    lf = _lock_file_for_gpu(uuid)
    try:
        fh = open(lf, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        try:
            meta = _read_meta(uuid)
            if meta and meta.get("deployment_id") == deployment_id:
                _write_meta(uuid, deployment_id, pid)
                return True
            return False
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:
                pass
            fh.close()
    except Exception:
        return False


def release_for_owner(uuids: list[str], deployment_id: str) -> list[str]:
    """Release reservations owned by deployment_id. Others' reservations are
    left untouched. Returns the uuids actually released."""
    released: list[str] = []
    for u in uuids:
        lf = _lock_file_for_gpu(u)
        try:
            fh = open(lf, "a+")
        except Exception:
            continue
        try:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            meta = _read_meta(u)
            if not meta or meta.get("deployment_id") != deployment_id:
                continue
            try:
                lf.unlink()
            except FileNotFoundError:
                pass
            try:
                _meta_file_for_gpu(u).unlink()
            except FileNotFoundError:
                pass
            released.append(u)
        except Exception:
            continue
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:
                pass
            fh.close()
    return released


def list_reservations() -> list[dict[str, Any]]:
    root = STATE_ROOT / "locks" / "gpu"
    if not root.exists():
        return []
    out = []
    for p in sorted(root.glob("*.lock")):
        uuid = p.stem
        meta = _read_meta(uuid)
        out.append(
            {
                "gpu_uuid": uuid,
                "lock_path": str(p),
                "owner": meta.get("deployment_id") if meta else None,
                "pid": meta.get("pid") if meta else None,
                "reserved_at": meta.get("reserved_at") if meta else None,
                "stale": reservation_is_stale(meta),
            }
        )
    return out


def cleanup_stale(max_age_s: int = 300) -> list[dict[str, Any]]:
    """Remove reservations whose owner is dead, or in-flight reservations
    older than max_age_s. Returns the removed reservations."""
    root = STATE_ROOT / "locks" / "gpu"
    if not root.exists():
        return []
    removed: list[dict[str, Any]] = []
    for p in sorted(root.glob("*.lock")):
        uuid = p.stem
        meta = _read_meta(uuid)
        if not meta or not reservation_is_stale(meta, in_flight_max_age_s=max_age_s):
            continue
        try:
            fh = open(p, "a+")
        except Exception:
            continue
        try:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            # re-check under flock in case state changed
            meta2 = _read_meta(uuid)
            if not meta2 or not reservation_is_stale(meta2, in_flight_max_age_s=max_age_s):
                continue
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            try:
                _meta_file_for_gpu(uuid).unlink()
            except FileNotFoundError:
                pass
            removed.append(
                {
                    "gpu_uuid": uuid,
                    "owner": meta2.get("deployment_id"),
                    "pid": meta2.get("pid"),
                    "reserved_at": meta2.get("reserved_at"),
                }
            )
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:
                pass
            fh.close()
    return removed
