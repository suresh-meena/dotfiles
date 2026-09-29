"""fleetqd HTTP API (``/api/v1``, §6.2).

Handlers never own a database transaction: each submits a function to the
store's owner thread. Long-polls release every transaction before waiting.
They subscribe first, then re-check, so a change between check and wait is
never lost. No read, wait, log or UI request causes a remote call (invariant 8).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import API_VERSION, __version__, auth, budget, views
from ..db.store import Store
from ..engine import admission, fence, state
from ..engine.controller import Controller
from ..errors import FqError
from ..util import utcnow
from ..views import job_envelope, job_summary

MAX_JSON_BODY = 256 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_STRING = 64 * 1024
MAX_WAIT_S = 55.0
MAX_WAITERS = 128
MAX_WAITERS_PER_TOKEN = 16
MAX_WAITERS_PER_OWNER = 64
PAGE_MAX = 500


def _bundle_storage_usage(conn: sqlite3.Connection, digest: str, owner: str, token_id: str):
    """Return committed global, owner and token bytes plus reference membership."""
    global_used = int(conn.execute("SELECT COALESCE(SUM(compressed_bytes),0) FROM bundles").fetchone()[0])
    owner_used = int(conn.execute(
        "SELECT COALESCE(SUM(b.compressed_bytes),0) FROM bundles b WHERE EXISTS ("
        "SELECT 1 FROM bundle_refs r WHERE r.digest=b.digest AND r.owner=? AND r.released_at IS NULL)",
        (owner,)).fetchone()[0])
    owner_has_ref = conn.execute(
        "SELECT 1 FROM bundle_refs WHERE digest=? AND owner=? AND released_at IS NULL LIMIT 1",
        (digest, owner)).fetchone() is not None
    row = conn.execute("SELECT compressed_bytes FROM bundles WHERE digest=?", (digest,)).fetchone()
    token_used = int(conn.execute(
        "SELECT COALESCE(SUM(b.compressed_bytes),0) FROM bundles b WHERE EXISTS ("
        "SELECT 1 FROM bundle_refs r WHERE r.digest=b.digest AND r.owner=? AND r.ref_kind='upload'"
        " AND r.ref_id=? AND r.released_at IS NULL)",
        (owner, token_id)).fetchone()[0])
    token_has_ref = conn.execute(
        "SELECT 1 FROM bundle_refs WHERE digest=? AND owner=? AND ref_kind='upload'"
        " AND ref_id=? AND released_at IS NULL LIMIT 1",
        (digest, owner, token_id)).fetchone() is not None
    owner = conn.execute("SELECT quota_json FROM principals WHERE name=?", (owner,)).fetchone()
    owner_quota = json.loads(owner["quota_json"] or "{}") if owner else {}
    return (global_used, owner_used, owner_has_ref, int(row["compressed_bytes"]) if row else None,
            token_used, token_has_ref, owner_quota)


def _orphan_bundle_bytes(conn: sqlite3.Connection, bundle_dir: Path) -> int:
    """Count validly named objects published before a crashed DB commit."""
    known = {row[0] for row in conn.execute("SELECT digest FROM bundles")}
    total = 0
    for path in bundle_dir.glob("*.tar.gz"):
        hex_digest = path.name[:-7]
        if len(hex_digest) != 64 or any(ch not in "0123456789abcdef" for ch in hex_digest):
            continue
        digest = "sha256:" + hex_digest
        if digest in known:
            continue
        try:
            st = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total


@dataclass
class Runtime:
    store: Store
    controller: Controller | None
    bundle_dir: Path
    bundle_limits: Any = None
    clock_ok: Any = lambda: False
    clock_health: Any = None
    waiters: int = 0
    waiters_by_token: dict[str, int] = field(default_factory=dict)
    waiters_by_owner: dict[str, int] = field(default_factory=dict)
    submit_times: dict[str, list[float]] = field(default_factory=dict)
    # One daemon/worker is the supported deployment. Serialize bundle PUTs
    # from quota check through DB publication so in-flight bytes cannot race
    # each other's disk reserve or logical quota calculation.
    upload_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    log_cache: Any = None
    artifact_dir: Path | None = None
    controller_task: asyncio.Task | None = None
    ui_origin: str | None = None
    ui_dev_mode: bool = False


def _error(exc: FqError) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(int(max(1, exc.retry_after)))
    return JSONResponse(exc.envelope(), status_code=exc.http_status, headers=headers)


def _dt_since(seconds: float) -> str:
    import datetime as _dt
    seconds = max(0.0, min(float(seconds), 400 * 86400))
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def create_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="fleetq", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(FqError)
    async def _fq_error(_request: Request, exc: FqError):
        return _error(exc)

    async def principal(request: Request) -> auth.Principal:
        return await rt.store.run(lambda c: auth.verify(c, _presented(request), clock_ok=rt.clock_ok()))

    def _presented(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        return header[7:] if header.lower().startswith("bearer ") else None

    async def json_body(request: Request) -> Any:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_JSON_BODY:
                raise FqError("invalid_argument", f"request body exceeds {MAX_JSON_BODY} bytes")
            body.extend(chunk)
        try:
            parsed = json.loads(body or b"{}")
        except (json.JSONDecodeError, RecursionError) as exc:
            raise FqError("invalid_argument", f"request body is not JSON: {exc}")
        if not isinstance(parsed, dict):
            raise FqError("invalid_argument", "request body must be a JSON object")
        pending = [(parsed, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > MAX_JSON_DEPTH:
                raise FqError("invalid_argument", f"JSON nesting exceeds {MAX_JSON_DEPTH} levels")
            if isinstance(value, str) and len(value) > MAX_JSON_STRING:
                raise FqError("invalid_argument", f"JSON string exceeds {MAX_JSON_STRING} characters")
            if isinstance(value, dict):
                pending.extend((item, depth + 1) for pair in value.items() for item in pair)
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
        return parsed

    def _wake() -> None:
        if rt.controller is not None:
            rt.controller.wake()
            rt.controller.hub.publish()

    # ---- health -------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz():
        def check(conn):
            targets = [r["id"] for r in conn.execute("SELECT id FROM nodes WHERE enabled = 1")]
            unreconciled = [t for t in targets if not fence.target_dispatch_ready(conn, t)]
            return {"restore_pending": fence.restore_discovery_pending(conn),
                    "enabled_targets": len(targets), "unreconciled_targets": unreconciled,
                    "dispatch": (conn.execute("SELECT value FROM controller_meta WHERE key='dispatch'").fetchone()
                                 or {"value": "on"})["value"]}
        info = await rt.store.run(check)
        task = rt.controller_task
        controller_live = rt.controller is not None and (task is None or not task.done())
        startup_complete = bool(rt.controller and getattr(rt.controller, "startup_complete", False))
        tick_at = getattr(rt.controller, "last_tick_at", None) if rt.controller else None
        tick_s = getattr(getattr(rt.controller, "config", None), "tick_s", 10.0)
        tick_fresh = tick_at is not None and time.monotonic() - tick_at <= max(30.0, tick_s * 3)
        ready = (controller_live and startup_complete and tick_fresh and not info["restore_pending"]
                 and not info["unreconciled_targets"])
        clock_healthy = bool(rt.clock_ok())
        return JSONResponse({"ok": ready, "controller_live": controller_live,
                             "controller_started": startup_complete, "controller_tick_fresh": tick_fresh,
                             "clock_healthy": clock_healthy,
                             "dispatch_ready": ready and info["dispatch"] == "on" and clock_healthy, **info},
                            status_code=200 if ready else 503)

    # ---- identity / limits ----------------------------------------------------------

    @app.get("/api/v1/whoami")
    async def whoami(request: Request):
        p = await principal(request)
        return {"schema": "fq.whoami/v1", "ok": True, "owner": p.owner, "token": p.token_id, "label": p.label,
                "kind": p.kind, "scopes": sorted(p.scopes), "allow_clusters": p.allow_clusters,
                "quota": admission.effective_quota(p)}

    @app.get("/api/v1/limits")
    async def limits(request: Request):
        await principal(request)
        lim = rt.bundle_limits
        return {"schema": "fq.limits/v1", "ok": True, "api_version": API_VERSION,
                "features": admission.FEATURES,
                "bundle": {"max_compressed": lim.max_compressed, "max_file": lim.max_file,
                           "max_members": lim.max_members, "max_expanded": lim.max_expanded,
                           "max_owner_bytes": lim.max_owner_bytes,
                           "max_global_bytes": lim.max_global_bytes} if lim else None,
                "max_wait_s": MAX_WAIT_S}

    # ---- bundles ------------------------------------------------------------------

    def _bundle_path(digest: str) -> Path:
        return rt.bundle_dir / (digest.split(":", 1)[1] + ".tar.gz")

    def _check_digest(digest: str) -> str:
        if not (digest.startswith("sha256:") and len(digest) == 71 and all(c in "0123456789abcdef" for c in digest[7:])):
            raise FqError("invalid_argument", "bundle digest must be sha256:<64 hex>")
        return digest

    @app.head("/api/v1/bundles/{digest}")
    async def bundle_head(digest: str, request: Request):
        p = await principal(request)
        digest = _check_digest(digest)
        row = await rt.store.run(lambda c: c.execute(
            "SELECT b.compressed_bytes FROM bundles b JOIN bundle_refs r ON r.digest=b.digest "
            "WHERE b.digest=? AND r.owner=? AND r.released_at IS NULL LIMIT 1",
            (digest, p.owner)).fetchone())
        try:
            file_stat = _bundle_path(digest).lstat() if row is not None else None
        except OSError:
            file_stat = None
        present = (file_stat is not None and stat.S_ISREG(file_stat.st_mode)
                   and file_stat.st_size == row["compressed_bytes"])
        return Response(status_code=200 if present else 404)

    @app.put("/api/v1/bundles/{digest}")
    async def bundle_put(digest: str, request: Request):
        """Stream to a private temp file, validate, fsync, atomically publish (§6.3)."""
        from .. import bundles

        p = await principal(request)
        p.require("submit")
        digest = _check_digest(digest)
        limits_ = rt.bundle_limits or bundles.BundleLimits()
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) < 0:
                    raise ValueError
            except ValueError as exc:
                raise FqError("invalid_argument", "Content-Length must be a non-negative integer") from exc
            if int(declared) > limits_.max_compressed:
                raise FqError("bundle_too_large", f"bundle exceeds {limits_.max_compressed} bytes compressed")

        reserve = 2 * 1024**3
        async with rt.upload_lock:
            tmp_dir = rt.bundle_dir / "tmp"
            tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp_stat = tmp_dir.lstat()
            if not stat.S_ISDIR(tmp_stat.st_mode) or tmp_stat.st_uid != os.getuid():
                raise FqError("unsafe_storage", "bundle upload temporary directory is not an owned directory")
            tmp_dir.chmod(0o700)
            tmp = tmp_dir / f"{secrets.token_hex(12)}.part"
            written = 0
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    async for chunk in request.stream():
                        written += len(chunk)
                        if written > limits_.max_compressed:
                            raise FqError("bundle_too_large", "bundle exceeds the compressed size limit")
                        fs = os.statvfs(rt.bundle_dir)
                        free = fs.f_bavail * fs.f_frsize
                        # Include filesystem-block overhead in the safety margin.
                        needed = ((len(chunk) + fs.f_frsize - 1) // fs.f_frsize) * fs.f_frsize + fs.f_frsize
                        if free < needed + reserve:
                            raise FqError("insufficient_storage", "numpi is low on disk; uploads are paused",
                                          retry_after=300)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    info = await asyncio.to_thread(bundles.bundle_validate, tmp, expected_digest=digest, limits=limits_)
                except bundles.BundleError as exc:
                    raise FqError("bundle_invalid", f"bundle rejected: {exc.message}", details={"code": exc.code})

                final = _bundle_path(digest)
                winner_info = None
                if final.exists():
                    try:
                        st = final.lstat()
                        if not stat.S_ISREG(st.st_mode):
                            raise FqError("conflict", "stored bundle object is not a regular file",
                                          hint="an administrator must repair the bundle cache")
                        winner_info = await asyncio.to_thread(bundles.bundle_validate, final,
                                                              expected_digest=digest, limits=limits_)
                    except bundles.BundleError as exc:
                        raise FqError("conflict", "stored bundle object failed integrity validation",
                                      hint="an administrator must repair the bundle cache",
                                      details={"reason": exc.code}) from exc
                candidate = winner_info or info
                (global_used, owner_used, owner_has_ref, existing_size, token_used, token_has_ref,
                 owner_quota, orphan_bytes) = await rt.store.run(
                    lambda c: (*_bundle_storage_usage(c, digest, p.owner, p.token_id),
                               _orphan_bundle_bytes(c, rt.bundle_dir)))
                global_delta = candidate.compressed_bytes if existing_size is None else max(
                    0, candidate.compressed_bytes - existing_size)
                # If the file survived a previous crash but its DB row did
                # not, it is already included in orphan_bytes.
                if existing_size is None and final.exists():
                    global_delta = 0
                owner_delta = candidate.compressed_bytes if not owner_has_ref else max(
                    0, candidate.compressed_bytes - (existing_size or 0))
                token_delta = candidate.compressed_bytes if not token_has_ref else max(
                    0, candidate.compressed_bytes - (existing_size or 0))
                owner_limit = min(int(limits_.max_owner_bytes),
                                  int(owner_quota.get("bundle_bytes", limits_.max_owner_bytes)))
                token_limit = int(admission.effective_quota(p)["bundle_bytes"])
                if token_used + token_delta > token_limit:
                    raise FqError("quota_exceeded", f"token bundle quota {token_limit} bytes would be exceeded")
                if owner_used + owner_delta > owner_limit:
                    raise FqError("quota_exceeded", f"owner bundle quota {owner_limit} bytes would be exceeded")
                if global_used + orphan_bytes + global_delta > int(limits_.max_global_bytes):
                    raise FqError("quota_exceeded",
                                  f"global bundle quota {limits_.max_global_bytes} bytes would be exceeded")

                if winner_info is None:
                    # Link, rather than replace, so another publisher can
                    # never overwrite the first valid encoding for this hash.
                    try:
                        os.link(tmp, final)
                    except FileExistsError as exc:
                        raise FqError(
                            "conflict", "bundle object appeared during publication; retry the upload"
                        ) from exc
                    tmp.unlink()
                    dir_fd = os.open(rt.bundle_dir, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)

                def record(conn):
                    # upload_lock serializes every path that adds a bundle
                    # row/ref; submissions can only refer to already-owned
                    # bundles and do not increase owner logical bytes.
                    conn.execute(
                        "INSERT INTO bundles (digest, compressed_bytes, expanded_bytes, members, format_version,"
                        " path, created_at)"
                        " VALUES (?,?,?,?,?,?,?) ON CONFLICT(digest) DO UPDATE SET"
                        " compressed_bytes=excluded.compressed_bytes,"
                        " expanded_bytes=excluded.expanded_bytes, members=excluded.members,"
                        " format_version=excluded.format_version,"
                        " path=excluded.path",
                        (digest, candidate.compressed_bytes, candidate.expanded_bytes, candidate.members,
                         candidate.format_version, str(final), utcnow()))
                    conn.execute(
                        "INSERT INTO bundle_refs (digest, owner, ref_kind, ref_id, created_at)"
                        " VALUES (?,?,'upload',?,?)"
                        " ON CONFLICT(digest, owner, ref_kind, ref_id) DO UPDATE SET released_at=NULL",
                        (digest, p.owner, p.token_id, utcnow()))
                await rt.store.run(record)
                winner_info = candidate
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        return {"schema": "fq.bundle/v1", "ok": True, "digest": digest,
                "compressed_bytes": winner_info.compressed_bytes, "expanded_bytes": winner_info.expanded_bytes,
                "members": winner_info.members}

    # ---- jobs ------------------------------------------------------------------------

    def _rate_limit(p: auth.Principal) -> None:
        limit = admission.effective_quota(p)["submits_per_minute"]
        now = time.monotonic()
        window = [t for t in rt.submit_times.get(p.token_id, []) if now - t < 60]
        if len(window) >= limit:
            raise FqError("rate_limited", f"at most {limit} submissions per minute for this token",
                          retry_after=60 - (now - window[0]))
        window.append(now)
        rt.submit_times[p.token_id] = window

    @app.post("/api/v1/jobs")
    async def submit(request: Request):
        p = await principal(request)
        body = await json_body(request)
        key = request.headers.get("idempotency-key", "")
        replay = await rt.store.run(lambda c: admission.replay(c, p, body, idempotency_key=key))
        if replay is not None:
            return JSONResponse(replay, status_code=200)

        accept, restore_pending = await rt.store.run(lambda c: (
            (c.execute("SELECT value FROM controller_meta WHERE key='accept'").fetchone() or {"value": "on"})["value"],
            fence.restore_discovery_pending(c)))
        if restore_pending:
            raise FqError("not_ready", "new jobs are paused while restored job state is being discovered",
                          retry_after=300)
        if accept != "on":
            raise FqError("draining", "fleetqd is not accepting new jobs right now", retry_after=300)
        _rate_limit(p)
        response = await rt.store.run(lambda c: admission.admit(c, p, body, idempotency_key=key))
        _wake()
        return JSONResponse(response, status_code=200 if response.get("idempotent_replay") else 201)

    @app.get("/api/v1/jobs")
    async def list_jobs(request: Request, phase: str | None = None, owner: str | None = None,
                        limit: int = 50, before: int | None = None, active: bool = False):
        p = await principal(request)
        p.require("read")
        limit = max(1, min(PAGE_MAX, limit))
        who = owner if (owner and p.has("manage_all")) else p.owner

        def q(conn):
            clauses, args = ["owner = ?"], [who]
            if phase:
                clauses.append("phase = ?")
                args.append(phase)
            if active:
                clauses.append("phase <> 'TERMINAL'")
            if before:
                clauses.append("id < ?")
                args.append(before)
            rows = conn.execute(f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
                                (*args, limit)).fetchall()
            return [job_summary(r) for r in rows]
        jobs = await rt.store.run(q)
        return {"schema": "fq.jobs/v1", "ok": True, "jobs": jobs,
                "next_before": jobs[-1]["id"] if len(jobs) == limit else None}

    def _who(p: auth.Principal, user: str | None, all_users: bool) -> str | None:
        if all_users or (user and user != p.owner):
            if not (p.has("manage_all") or p.has("read_all")):
                raise FqError("forbidden", "seeing other users' jobs needs the read_all or manage_all scope")
            return user if user else None
        return p.owner

    @app.get("/api/v1/queue")
    async def queue(request: Request, user: str | None = None, all_users: bool = False, name: str | None = None,
                    where: str | None = None, group: str | None = None, finished: bool = False, limit: int = 500):
        """squeue: a numpi read only; it never asks a node or a cluster anything."""
        p = await principal(request)
        p.require("read")
        who = _who(p, user, all_users)
        states = {s.strip().upper() for s in (request.query_params.get("state") or "").split(",") if s.strip()} or None

        def q(conn):
            order, effective = rt.controller.dispatch_view(conn) if rt.controller else ([], {})
            rows = views.queue_rows(conn, owner=who, phases=states, name_glob=name, where=where, group=group,
                                    include_finished=finished, dispatch_order=order, effective=effective,
                                    limit=max(1, min(limit, 2000)))
            return {"schema": "fq.queue/v1", "ok": True, "jobs": rows, "pending_in_line": len(order)}
        return await rt.store.run(q)

    @app.get("/api/v1/managed-slurm")
    async def managed_slurm(request: Request):
        """Return fleetqd's cached managed-Slurm snapshot; never query a site here."""
        p = await principal(request)
        p.require("read_all")
        from ..slurm.managed_snapshot import managed_slurm_document
        return await rt.store.run(managed_slurm_document)

    @app.get("/api/v1/history")
    async def history(request: Request, since_s: float = 7 * 86400, user: str | None = None,
                      all_users: bool = False, where: str | None = None, summary: bool = False, limit: int = 1000):
        """sacct: finished jobs, with wait, run time and GPU-hours."""
        p = await principal(request)
        p.require("read")
        who = _who(p, user, all_users)
        since = _dt_since(since_s)

        def q(conn):
            rows = views.history_rows(conn, owner=who, since=since, where=where, limit=max(1, min(limit, 20000)))
            body: dict[str, Any] = {"schema": "fq.history/v1", "ok": True, "since": since}
            if summary:
                body["summary"] = views.history_summary(rows)
            else:
                body["jobs"] = rows
            return body
        return await rt.store.run(q)

    async def _load_job(p: auth.Principal, job_id: int, *, manage: bool = False) -> None:
        def check(conn):
            job = state.get_job(conn, job_id)
            allowed = auth.can_manage(p, job) if manage else auth.can_read(p, job)
            if not allowed:
                # Don't reveal existence to someone who can't read it.
                if not auth.can_read(p, job):
                    raise FqError("not_found", f"job {job_id} does not exist")
                raise FqError("forbidden", f"this token may not manage job {job_id}")
        await rt.store.run(check)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: int, request: Request):
        p = await principal(request)
        await _load_job(p, job_id)
        return await rt.store.run(lambda c: job_envelope(c, state.get_job(c, job_id)))

    @app.get("/api/v1/jobs/{job_id}/logs")
    async def job_logs(job_id: int, request: Request, stream: str = "stdout", offset: int | None = None,
                       max_bytes: int = 65536, generation: int | None = None, attempt: int | None = None):
        """The numpi cache only: a log request never causes a remote read (§6.4)."""
        p = await principal(request)
        p.require("logs")
        await _load_job(p, job_id)
        if stream not in ("stdout", "stderr"):
            raise FqError("invalid_argument", "stream must be stdout or stderr")
        if offset is not None and offset < 0:
            raise FqError("invalid_argument", "offset must be >= 0")

        def pick(conn):
            if attempt is not None:
                return conn.execute("SELECT id, n, state FROM attempts WHERE job_id = ? AND n = ?",
                                    (job_id, attempt)).fetchone()
            return conn.execute("SELECT id, n, state FROM attempts WHERE job_id = ? AND started_at IS NOT NULL"
                                " ORDER BY n DESC LIMIT 1", (job_id,)).fetchone()
        row = await rt.store.run(pick)
        body: dict[str, Any] = {"schema": "fq.logs/v1", "ok": True, "job": job_id, "stream": stream}
        if row is None or rt.log_cache is None:
            if attempt is not None and row is None:
                raise FqError("not_found", f"job {job_id} has no attempt {attempt}")
            return {**body, "attempt": None, "data_b64": "", "offset": 0, "next_offset": 0, "complete": False,
                    "generation": 0, "gap": None, "reset": False, "note": "no attempt has started yet"}
        chunk = await asyncio.to_thread(rt.log_cache.read, row["id"], stream, offset=offset,
                                        max_bytes=max_bytes, generation=generation)
        return {**body, "attempt": row["n"], "attempt_state": row["state"], **chunk}

    @app.get("/api/v1/jobs/{job_id}/artifacts")
    async def job_artifacts(job_id: int, request: Request):
        p = await principal(request)
        await _load_job(p, job_id)

        def q(conn):
            job = state.get_job(conn, job_id)
            rows = conn.execute(
                "SELECT a.relpath, a.required, a.state, a.size, a.sha256, t.n AS attempt FROM artifacts a"
                " JOIN attempts t ON t.id = a.attempt_id WHERE a.job_id = ? ORDER BY t.n, a.relpath",
                (job_id,)).fetchall()
            return {"schema": "fq.artifacts/v1", "ok": True, "job": job_id, "state": job["artifacts_state"],
                    "files": [dict(r) for r in rows]}
        return await rt.store.run(q)

    @app.get("/api/v1/jobs/{job_id}/artifacts/{relpath:path}")
    async def job_artifact(job_id: int, relpath: str, request: Request, attempt: int | None = None):
        from starlette.responses import FileResponse
        p = await principal(request)
        p.require("logs")
        await _load_job(p, job_id)

        def q(conn):
            sql = ("SELECT a.local_path, a.sha256, a.size FROM artifacts a JOIN attempts t ON t.id = a.attempt_id"
                   " WHERE a.job_id = ? AND a.relpath = ? AND a.state = 'COMPLETE'")
            args: list[Any] = [job_id, relpath]
            if attempt is not None:
                sql += " AND t.n = ?"
                args.append(attempt)
            return conn.execute(sql + " ORDER BY t.n DESC LIMIT 1", args).fetchone()
        row = await rt.store.run(q)
        if row is None or rt.artifact_dir is None:
            raise FqError("not_found", f"job {job_id} has no collected artifact {relpath!r}")
        source_path = Path(row["local_path"])
        artifact_dir = rt.artifact_dir

        def stored_file() -> Path | None:
            path = source_path.resolve()
            if artifact_dir.resolve() not in path.parents or source_path.is_symlink() or not path.is_file():
                return None
            return path
        path = await asyncio.to_thread(stored_file)
        if path is None:
            raise FqError("not_found", f"artifact {relpath!r} is not in the artifact store")
        return FileResponse(path, media_type="application/octet-stream",
                            headers={"X-Fq-Sha256": row["sha256"] or "", "Cache-Control": "no-store"})

    @app.get("/api/v1/jobs/{job_id}/wait")
    async def wait_job(job_id: int, request: Request, until: str = "terminal", since_version: int = 0,
                       timeout: float = MAX_WAIT_S):
        p = await principal(request)
        await _load_job(p, job_id)
        if until not in ("terminal", "started", "change"):
            raise FqError("invalid_argument", "until must be terminal, started or change")
        return await _wait(request, p, [job_id], until=until, since_version=since_version,
                           timeout=min(max(0.0, timeout), MAX_WAIT_S), mode="all")

    @app.post("/api/v1/wait")
    async def wait_many(request: Request):
        p = await principal(request)
        body = await json_body(request)
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids or len(ids) > 1000:
            raise FqError("invalid_argument", "ids must list 1-1000 job ids")
        for job_id in ids:
            await _load_job(p, int(job_id))
        mode = body.get("mode", "all")
        if mode not in ("all", "any"):
            raise FqError("invalid_argument", "mode must be all or any")
        return await _wait(request, p, [int(i) for i in ids], until=body.get("until", "terminal"), since_version=0,
                           timeout=min(float(body.get("timeout", MAX_WAIT_S)), MAX_WAIT_S), mode=mode)

    async def _wait(request, p, ids, *, until, since_version, timeout, mode):
        if (rt.waiters >= MAX_WAITERS or rt.waiters_by_token.get(p.token_id, 0) >= MAX_WAITERS_PER_TOKEN
                or rt.waiters_by_owner.get(p.owner, 0) >= MAX_WAITERS_PER_OWNER):
            raise FqError("rate_limited", "too many concurrent waits", retry_after=5)
        rt.waiters += 1
        rt.waiters_by_token[p.token_id] = rt.waiters_by_token.get(p.token_id, 0) + 1
        rt.waiters_by_owner[p.owner] = rt.waiters_by_owner.get(p.owner, 0) + 1
        started = time.monotonic()
        try:
            while True:
                seq = rt.controller.hub.seq if rt.controller else 0     # subscribe first
                def authorized_docs(conn):
                    # A token may be revoked while a long poll is asleep.
                    fresh = auth.verify(conn, _presented(request), clock_ok=rt.clock_ok())
                    if fresh.token_id != p.token_id:
                        raise FqError("unauthorized", "wait token changed")
                    jobs = [state.get_job(conn, i) for i in ids]
                    if any(not auth.can_read(fresh, job) for job in jobs):
                        raise FqError("not_found", "job is no longer readable")
                    return [job_envelope(conn, job)["job"] for job in jobs]
                docs = await rt.store.run(authorized_docs)

                def met(doc):
                    if until == "terminal":
                        return doc["terminal"]
                    if until == "started":
                        return doc["times"]["started"] is not None or doc["terminal"]
                    return doc["version"] > since_version
                flags = [met(d) for d in docs]
                done = all(flags) if mode == "all" else any(flags)
                remaining = timeout - (time.monotonic() - started)
                if done or remaining <= 0 or rt.controller is None:
                    waited = {"until": until, "met": done, "seconds": round(time.monotonic() - started, 3)}
                    if len(ids) == 1:
                        return {"schema": "fq.job/v1", "ok": True, "job": docs[0], "waited": waited}
                    return {"schema": "fq.wait/v1", "ok": True, "jobs": docs, "waited": waited}
                await rt.controller.hub.wait_beyond(seq, remaining)          # then wait
        finally:
            rt.waiters -= 1
            rt.waiters_by_token[p.token_id] -= 1
            rt.waiters_by_owner[p.owner] -= 1

    @app.get("/api/v1/jobs/{job_id}/explain")
    async def explain(job_id: int, request: Request):
        p = await principal(request)
        await _load_job(p, job_id)

        def q(conn):
            job = state.get_job(conn, job_id)
            decisions = [dict(r) | {"detail": json.loads(r["detail_json"])} for r in conn.execute(
                "SELECT ts, decision, attempt_id, detail_json FROM placement_decisions WHERE job_id = ?"
                " ORDER BY id DESC LIMIT 10", (job_id,))]
            for d in decisions:
                d.pop("detail_json", None)
            deps = [dict(r) for r in conn.execute(
                "SELECT d.parent_id, d.type, j.phase, j.success FROM deps d JOIN jobs j ON j.id = d.parent_id"
                " WHERE d.job_id = ?", (job_id,))]
            events = [dict(r) | {"detail": json.loads(r["detail_json"])} for r in conn.execute(
                "SELECT ts, kind, attempt_id, target, actor, detail_json FROM events WHERE job_id = ?"
                " ORDER BY id DESC LIMIT 25", (job_id,))]
            for e in events:
                e.pop("detail_json", None)
            return {"schema": "fq.explain/v1", "ok": True, "job": job_id, "phase": job["phase"],
                    "reason": job["reason"], "not_before": job["not_before"], "decisions": decisions,
                    "dependencies": deps, "events": events}
        return await rt.store.run(q)

    def _expect_version(request: Request) -> int | None:
        match = request.headers.get("if-match")
        return int(match.strip('"')) if match and match.strip('"').isdigit() else None

    @app.post("/api/v1/jobs/{job_id}/{action}")
    async def act(job_id: int, action: str, request: Request):
        p = await principal(request)
        await _load_job(p, job_id, manage=True)
        expect = _expect_version(request)
        body = await json_body(request) if action == "priority" else {}

        def mutate(conn):
            job = state.get_job(conn, job_id)
            if action == "cancel":
                if job["phase"] != "TERMINAL":
                    state.update_job(conn, job_id, event="cancel_requested", actor=p.label, expect_version=expect,
                                     desired_state="CANCEL", cancel_requested=1)
            elif action == "hold":
                # v1 holds only locally unlaunched work (§4.2).
                if job["phase"] not in ("PENDING", "HELD", "BLOCKED"):
                    raise FqError("not_modifiable", f"job {job_id} is {job['phase']}; only unlaunched jobs can be held")
                state.update_job(conn, job_id, event="held", actor=p.label, expect_version=expect,
                                 desired_state="HOLD", phase="HELD")
            elif action == "release":
                if job["phase"] != "HELD":
                    raise FqError("not_modifiable", f"job {job_id} is not held")
                state.update_job(conn, job_id, event="released", actor=p.label, expect_version=expect,
                                 desired_state="RUN", phase="PENDING", reason=None)
            elif action == "priority":
                value = body.get("priority")
                if isinstance(value, bool) or not isinstance(value, int) or not -1000 <= value <= 1000:
                    raise FqError("invalid_argument", "priority must be an integer in [-1000, 1000]")
                if job["phase"] not in ("PENDING", "HELD", "BLOCKED"):
                    raise FqError("not_modifiable", "priority applies only to jobs not yet dispatched")
                state.update_job(conn, job_id, event="priority_changed", actor=p.label, expect_version=expect,
                                 priority=value)
            elif action == "top":
                # Ahead of every other waiting job of this owner (priority is capped at 1000).
                if job["phase"] not in ("PENDING", "HELD", "BLOCKED"):
                    raise FqError("not_modifiable", "top applies only to jobs not yet dispatched")
                highest = conn.execute("SELECT MAX(priority) FROM jobs WHERE owner = ? AND id <> ? AND phase IN"
                                       " ('PENDING','HELD','BLOCKED')", (job["owner"], job_id)).fetchone()[0]
                value = min(1000, max(job["priority"], (highest if highest is not None else job["priority"]) + 1))
                state.update_job(conn, job_id, event="topped", actor=p.label, expect_version=expect, priority=value)
            elif action == "requeue":
                new_id = admission.requeue_job(conn, p, job_id, actor=p.label)
                return {**job_envelope(conn, state.get_job(conn, new_id)), "requeue_of": job_id}
            else:
                raise FqError("not_found", f"unknown action {action!r}")
            return job_envelope(conn, state.get_job(conn, job_id))
        result = await rt.store.run(mutate)
        _wake()
        return result

    @app.get("/api/v1/jobs/{job_id}/shell")
    async def job_shell(job_id: int, request: Request):
        """Where and how to open `fq shell` inside a running workstation job. Contacts nothing:
        the caller's own fleetctl opens the session with the caller's own SSH access."""
        p = await principal(request)
        await _load_job(p, job_id, manage=True)
        from ..executors.bare import NODE_LAUNCHER

        def q(conn):
            job = state.get_job(conn, job_id)
            att = conn.execute("SELECT * FROM attempts WHERE job_id = ? AND state = 'RUNNING' AND remote_may_be_live = 1"
                               " ORDER BY n DESC LIMIT 1", (job_id,)).fetchone()
            if job["phase"] != "RUNNING" or att is None:
                raise FqError("not_running", f"job {job_id} is {job['phase']}; a shell needs a running job")
            if att["backend"] != "bare":
                raise FqError("invalid_argument", "shells open on workstations only, never on a cluster")
            cfg = json.loads(conn.execute("SELECT config_json FROM nodes WHERE id = ?",
                                          (att["target"],)).fetchone()["config_json"] or "{}")
            root = cfg.get("control_root")
            gpus = [r["gpu_uuid"] for r in conn.execute(
                "SELECT gpu_uuid FROM resource_reservations WHERE attempt_id = ? AND kind = 'gpu'", (att["id"],))]
            argv = [cfg.get("node_python") or "python3", "-c", NODE_LAUNCHER, f"{root}/bin/fq-node", "--root", root,
                    "shell", "--fleet-id", fence.ensure_fleet_id(conn), att["id"]]
            return {"schema": "fq.shell/v1", "ok": True, "job": job_id, "attempt": att["id"],
                    "node": att["target"], "fleetctl_target": cfg.get("fleetctl_target") or att["target"],
                    "gpus": gpus, "argv": argv}
        return await rt.store.run(q)

    @app.patch("/api/v1/jobs/{job_id}")
    async def modify(job_id: int, request: Request):
        """Change where, with what, or when a job that has not started will run (§4.2)."""
        p = await principal(request)
        await _load_job(p, job_id, manage=True)
        expect = _expect_version(request)
        patch = await json_body(request)

        def mutate(conn):
            admission.modify_job(conn, p, job_id, patch, actor=p.label, expect_version=expect)
            return job_envelope(conn, state.get_job(conn, job_id))
        result = await rt.store.run(mutate)
        _wake()
        return result

    # ---- nodes / admin ----------------------------------------------------------------

    @app.get("/api/v1/nodes")
    async def nodes(request: Request):
        p = await principal(request)
        p.require("read")

        def q(conn):
            out = []
            for row in conn.execute("SELECT * FROM nodes ORDER BY id").fetchall():
                gpus = [dict(g) for g in conn.execute(
                    "SELECT uuid, model, vram_total, idx, reserved, drained, drain_reason, cooldown_until"
                    " FROM node_gpus WHERE node_id = ? ORDER BY idx", (row["id"],))]
                held = {r[0]: r[1] for r in conn.execute(
                    "SELECT r.gpu_uuid, j.id FROM resource_reservations r JOIN attempts a ON a.id = r.attempt_id"
                    " JOIN jobs j ON j.id = a.job_id WHERE r.node_id = ? AND r.kind = 'gpu' AND r.released_at IS NULL",
                    (row["id"],))}
                for g in gpus:
                    g["fleetq_job"] = held.get(g["uuid"])
                out.append({"id": row["id"], "backend": row["backend"], "mode": row["mode"],
                            "enabled": bool(row["enabled"]), "state": row["state"],
                            "drain": {"kind": row["drain_kind"], "reason": row["drain_reason"], "by": row["drain_by"]}
                            if row["drain_kind"] else None,
                            "dispatch_ready": fence.target_dispatch_ready(conn, row["id"]), "gpus": gpus})
            return out
        return {"schema": "fq.nodes/v1", "ok": True, "nodes": await rt.store.run(q)}

    @app.post("/api/v1/nodes/{node_id}/{action}")
    async def node_action(node_id: str, action: str, request: Request):
        p = await principal(request)
        p.require("nodes")
        body = await json_body(request)

        def mutate(conn):
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if row is None:
                raise FqError("not_found", f"no node {node_id}")
            if action == "drain":
                reason = str(body.get("reason") or "manual drain")[:200]
                conn.execute("UPDATE nodes SET drain_kind='manual', drain_reason=?, drain_by=?, drain_at=?, updated_at=?"
                             " WHERE id=?", (reason, p.label, utcnow(), utcnow(), node_id))
            elif action == "resume":
                conn.execute("UPDATE nodes SET drain_kind=NULL, drain_reason=NULL, drain_by=NULL, drain_at=NULL,"
                             " updated_at=? WHERE id=?", (utcnow(), node_id))
            else:
                raise FqError("not_found", f"unknown node action {action!r}")
            state.add_event(conn, f"node_{action}", target=node_id, actor=p.label, detail=body)
            return {"ok": True}
        result = await rt.store.run(mutate)
        _wake()
        return result

    @app.post("/api/v1/admin/{switch}")
    async def admin(switch: str, request: Request):
        p = await principal(request)
        p.require("admin")
        body = await json_body(request)
        if switch not in ("dispatch", "accept"):
            raise FqError("not_found", f"unknown switch {switch!r}")
        value = body.get("value")
        allowed = {"dispatch": ("on", "paused"), "accept": ("on", "off")}[switch]
        if value not in allowed:
            raise FqError("invalid_argument", f"{switch} must be one of {allowed}")

        def mutate(conn):
            conn.execute("INSERT INTO controller_meta (key, value) VALUES (?, ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (switch, value))
            state.add_event(conn, f"admin_{switch}", actor=p.label, detail={"value": value})
        await rt.store.run(mutate)
        _wake()
        return {"ok": True, switch: value}

    @app.get("/api/v1/status")
    async def status(request: Request):
        p = await principal(request)
        p.require("read")

        def q(conn):
            phases = {r["phase"]: r["c"] for r in conn.execute("SELECT phase, COUNT(*) c FROM jobs GROUP BY phase")}
            day_ago = utcnow()[:10] + "T00:00:00.000000Z"
            result = {"schema": "fq.status/v1", "ok": True, "version": __version__, "api_versions": [API_VERSION],
                    "epoch": fence.current_epoch(conn), "restore_pending": fence.restore_discovery_pending(conn),
                    "phases": phases, "remote_calls_today": budget.usage(conn, day_ago)}
            if p.has("read_all") or p.has("admin"):
                result["remote_cost_ledger"] = budget.remote_cost_ledger(conn, day_ago)
            return result
        return await rt.store.run(q)

    # ---- budget authority (§3.5) -------------------------------------------------------

    @app.post("/api/v1/permits")
    async def permits(request: Request):
        p = await principal(request)
        p.require("permits")
        body = await json_body(request)
        if not isinstance(body, dict):
            raise FqError("invalid_argument", "request body must be an object")
        cost = body.get("cost", {})
        if not isinstance(cost, dict) or set(cost) - {"rpc", "sessions", "bytes"}:
            raise FqError("invalid_argument", "cost must be an object with rpc, sessions, or bytes values")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in cost.values()):
            raise FqError("invalid_argument", "permit cost values must be non-negative integers")
        return await rt.store.run(lambda c: budget.grant_permit(
            c, cluster=str(body.get("cluster", "")), op_class=str(body.get("op_class", "action")),
            cost=cost,
            caller=str(body.get("caller") or p.label)))

    @app.post("/api/v1/permits/{permit_id}/redeem")
    async def redeem(permit_id: str, request: Request):
        p = await principal(request)
        p.require("permits")
        body = await json_body(request)
        if not isinstance(body, dict):
            raise FqError("invalid_argument", "request body must be an object")
        cost = body.get("cost") or {}
        if not isinstance(cost, dict) or set(cost) - {"rpc", "sessions", "bytes"}:
            raise FqError("invalid_argument", "cost must be an object with rpc, sessions, or bytes values")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in cost.values()):
            raise FqError("invalid_argument", "permit cost values must be non-negative integers")
        return await rt.store.run(lambda c: budget.redeem_permit(
            c, permit_id, cluster=str(body.get("cluster", "")), op_class=str(body.get("op_class", "")),
            cost=cost))

    from .ui import install_ui
    install_ui(app, rt)
    return app
