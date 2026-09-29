"""Artifact collection: a durable state machine that never reruns compute (§6.4).

A FINALIZING job's last attempt stages its approved outputs on the node, one
pull moves them into ``<artifacts>/.partial/<attempt>/``, every file is checked
against the node's size (and hash, where the node could afford one), and the
set is published atomically under ``<artifacts>/<job>/<attempt n>/``. Transport
trouble waits and retries; a wrong byte re-pulls; a missing required output,
a refused path or an over-allowance set fails finalization, which is reported
apart from the compute outcome. Past the deadline, collection expires.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any

from ..util import utcnow
from . import state


def safe_relpath(rel: str) -> str | None:
    """A relative path that stays inside the publish root, normalized; None if it can't."""
    if not rel or "\x00" in rel or rel.startswith("/"):
        return None
    norm = posixpath.normpath(rel)
    if norm in (".", "") or norm.startswith("../") or norm == ".." or "/../" in f"/{norm}/":
        return None
    return norm


def covered(required: str, files: list[dict[str, Any]]) -> bool:
    return any(f["relpath"] == required or f["relpath"].startswith(required.rstrip("/") + "/") for f in files)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(partial: Path, files: list[dict[str, Any]]) -> list[str]:
    """Problems with the pulled set; empty means every file is exactly what the node staged."""
    problems = []
    for f in files:
        path = partial / f["slot"]
        if path.is_symlink() or not path.is_file():
            problems.append(f"{f['relpath']}: not transferred")
            continue
        if path.stat().st_size != f["size"]:
            problems.append(f"{f['relpath']}: size {path.stat().st_size} != {f['size']}")
            continue
        f["local_sha256"] = sha256_file(path)
        if f.get("sha256") and f["local_sha256"] != f["sha256"]:
            problems.append(f"{f['relpath']}: hash mismatch")
    return problems


def ensure_real_directory(path: Path, *, create: bool = False) -> None:
    """Require a directory entry, never a symlink, before artifact I/O."""
    if ".." in path.parts:
        raise ValueError(f"unsafe artifact directory: {path}")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError(f"unsafe artifact directory: {path}")


def prepare_staging(partial: Path) -> None:
    """Check both parents so .partial cannot redirect cleanup or a pull."""
    name = partial.name.removesuffix(".pub")
    if not re.fullmatch(r"att_[A-Za-z0-9_-]{1,64}", name):
        raise ValueError(f"unsafe artifact staging entry: {partial}")
    ensure_real_directory(partial.parent.parent, create=True)
    ensure_real_directory(partial.parent, create=True)


def remove_staging(path: Path) -> None:
    prepare_staging(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode):
        raise ValueError(f"unsafe artifact staging entry: {path}")
    shutil.rmtree(path)


def publish(partial: Path, final: Path, files: list[dict[str, Any]]) -> None:
    """Publish a new tree once; a replay may only reuse identical published bytes."""
    prepare_staging(partial)
    ensure_real_directory(partial)
    ensure_real_directory(final.parent.parent)
    ensure_real_directory(final.parent, create=True)
    if any(safe_relpath(f["relpath"]) != f["relpath"] or not f["slot"]
           or "/" in f["slot"] or f["slot"] in (".", "..") for f in files):
        raise ValueError("unsafe artifact publish path")
    if any((partial / f["slot"]).is_symlink() for f in files):
        raise ValueError("artifact slot is a symlink")
    if final.is_symlink():
        raise FileExistsError(f"artifact destination is a symlink: {final}")
    if final.exists():
        if not final.is_dir():
            raise FileExistsError(f"artifact destination is not a directory: {final}")
        expected = {f["relpath"] for f in files}
        present = set()
        for path in final.rglob("*"):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise FileExistsError(f"artifact destination contains an unsafe entry: {path}")
            if path.is_file():
                present.add(path.relative_to(final).as_posix())
        if present != expected or any(
            sha256_file(final / f["relpath"]) != sha256_file(partial / f["slot"]) for f in files
        ):
            raise FileExistsError(f"artifact destination conflicts with the staged output: {final}")
        remove_staging(partial)
        return
    staging = partial.with_name(partial.name + ".pub")
    remove_staging(staging)
    ensure_real_directory(staging, create=True)
    for f in files:
        dest = staging / f["relpath"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial / f["slot"], dest)
    os.replace(staging, final)
    remove_staging(partial)


def validate_manifest(files: list[dict[str, Any]], *, max_files: int, max_bytes: int) -> tuple[int, str | None]:
    """Validate an executor-provided manifest and return its byte total."""
    if not isinstance(files, list):
        return 0, "invalid_manifest"
    if len(files) > max_files:
        return 0, "too_many_files"
    total = 0
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            return 0, "invalid_manifest"
        relpath, slot, size = item.get("relpath"), item.get("slot"), item.get("size")
        if (not isinstance(relpath, str) or safe_relpath(relpath) != relpath
                or not isinstance(slot, str) or not slot or "/" in slot or slot in (".", "..")
                or isinstance(size, bool) or not isinstance(size, int) or size < 0
                or relpath in seen):
            return 0, "invalid_manifest"
        seen.add(relpath)
        total += size
        if total > max_bytes:
            return total, "too_large"
    return total, None


def reserve_files(conn: sqlite3.Connection, job_id: int, attempt_id: str, collect: list[dict[str, Any]],
                  files: list[dict[str, Any]], *, owner_max_bytes: int,
                  global_max_bytes: int) -> tuple[bool, str | None]:
    """Atomically reserve the manifest's bytes before pulling them locally.

    PENDING rows are durable reservations, and COMPLETE rows are retained data.
    A replay inserts no duplicate bytes; changing a file's reported size can
    only increase its reservation. The Store serializes this check and write
    in one BEGIN IMMEDIATE transaction.
    """
    job = conn.execute("SELECT j.owner,j.token_id,p.quota_json owner_quota,t.quota_json token_quota "
                       "FROM jobs j JOIN principals p ON p.name=j.owner "
                       "JOIN tokens t ON t.id=j.token_id WHERE j.id=?", (job_id,)).fetchone()
    if job is None:
        return False, "job_missing"

    def quota_cap(value: str, configured: int) -> int | None:
        try:
            quota = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(quota, dict):
            return None
        if "artifact_bytes" not in quota:
            return configured
        cap = quota["artifact_bytes"]
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            return None
        return min(configured, cap)

    owner_cap = quota_cap(job["owner_quota"], owner_max_bytes)
    token_cap = quota_cap(job["token_quota"], owner_cap) if owner_cap is not None else None
    if owner_cap is None or token_cap is None:
        return False, "invalid_artifact_quota"
    required = {c["path"]: c["required"] for c in collect}
    existing = {r["relpath"]: r["size"] for r in conn.execute(
        "SELECT relpath,size FROM artifacts WHERE attempt_id=?", (attempt_id,))}
    delta = sum(max(0, f["size"] - (existing.get(f["relpath"]) or 0)) for f in files)
    owner_used = conn.execute(
        "SELECT COALESCE(SUM(a.size),0) FROM artifacts a JOIN jobs j ON j.id=a.job_id "
        "WHERE j.owner=?", (job["owner"],)).fetchone()[0]
    token_used = conn.execute(
        "SELECT COALESCE(SUM(a.size),0) FROM artifacts a JOIN jobs j ON j.id=a.job_id "
        "WHERE j.token_id=?", (job["token_id"],)).fetchone()[0]
    global_used = conn.execute("SELECT COALESCE(SUM(size),0) FROM artifacts").fetchone()[0]
    if owner_used + delta > owner_cap:
        return False, "owner_retained_bytes"
    if token_used + delta > token_cap:
        return False, "token_retained_bytes"
    if global_used + delta > global_max_bytes:
        return False, "global_retained_bytes"
    now = utcnow()
    for f in files:
        top = next((p for p in required if f["relpath"] == p or f["relpath"].startswith(p.rstrip("/") + "/")), None)
        conn.execute("INSERT INTO artifacts (job_id,attempt_id,relpath,required,state,size,updated_at) "
                     "VALUES (?,?,?,?, 'PENDING',?,?) "
                     "ON CONFLICT(attempt_id,relpath) DO UPDATE SET "
                     "size=MAX(COALESCE(artifacts.size,0),excluded.size),updated_at=excluded.updated_at",
                     (job_id, attempt_id, f["relpath"], int(required.get(top, True)), f["size"], now))
    return True, None


def available_bytes(path: Path) -> int:
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


def mark_complete(conn: sqlite3.Connection, attempt_id: str, final: Path, files: list[dict[str, Any]]) -> None:
    now = utcnow()
    for f in files:
        conn.execute("UPDATE artifacts SET state='COMPLETE', sha256=?, local_path=?, updated_at=?"
                     " WHERE attempt_id=? AND relpath=?",
                     (f["local_sha256"], str(final / f["relpath"]), now, attempt_id, f["relpath"]))


def finish(conn: sqlite3.Connection, job_id: int, *, artifacts_state: str, reason: str | None,
           detail: dict[str, Any] | None = None) -> bool:
    """End finalization. Compute success and artifact success are reported separately."""
    job = state.get_job(conn, job_id)
    if job["phase"] != "FINALIZING":
        return False
    exec_ok = job["execution_outcome"] == "COMPLETED" and job["exit_code"] in (0, None)
    success = exec_ok and artifacts_state == "COMPLETE"
    state.update_job(conn, job_id, event=f"artifacts_{artifacts_state.lower()}", actor="controller",
                     phase="TERMINAL", artifacts_state=artifacts_state, success=int(success),
                     reason=reason, detail=detail)
    return success


def pending_jobs(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT j.id AS job_id, j.spec_json, j.ended_at, j.artifacts_state, a.id AS attempt_id, a.n, a.target,"
        " a.backend FROM jobs j JOIN attempts a ON a.job_id = j.id"
        " AND a.n = (SELECT MAX(n) FROM attempts WHERE job_id = j.id)"
        " WHERE j.phase = 'FINALIZING' ORDER BY j.ended_at LIMIT ?", (limit,)).fetchall()


def detail_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
