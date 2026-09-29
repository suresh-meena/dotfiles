"""Artifact retained-byte reservations and local disk reserve gates."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from fleetq.db.store import Store
from fleetq.engine import artifacts, fence
from fleetq.engine.controller import Controller, ControllerConfig
from fleetq.executors.base import CollectManifest
from fleetq.util import utcnow


def _job(conn, owner: str, job_id: int, attempt_id: str, *, token_id: str | None = None,
         token_quota: dict | None = None) -> None:
    now = utcnow()
    spec = {"collect": [{"path": "out", "required": True}], "workdir": {"subdir": "."}}
    token_id = token_id or f"tok_{owner}"
    conn.execute("INSERT OR IGNORE INTO principals(name,created_at) VALUES(?,?)", (owner, now))
    conn.execute("INSERT OR IGNORE INTO tokens(id,owner,kind,label,secret_sha256,scopes,quota_json,created_at) "
                 "VALUES(?,?,'human','test','x','[]',?,?)", (token_id, owner, json.dumps(token_quota or {}), now))
    conn.execute("INSERT INTO jobs(id,owner,token_id,name,spec_json,spec_digest,desired_state,phase,"
                 "execution_outcome,exit_code,artifacts_state,submitted_at,ended_at,updated_at) "
                 "VALUES(?,?,?,'j',?,?,'RUN','FINALIZING','COMPLETED',0,'PENDING',?,?,?)",
                 (job_id, owner, token_id, json.dumps(spec), f"sha256:{job_id:064x}", now, now, now))
    conn.execute("INSERT INTO attempts(id,job_id,n,backend,target,epoch,state,remote_may_be_live,launch_op_id,"
                 "spec_digest,created_at,updated_at) VALUES(?,?,1,'fake','n1',1,'RELEASED',0,?,?,?,?)",
                 (attempt_id, job_id, f"op-{attempt_id}", f"sha256:{job_id:064x}", now, now))


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "state.db")
    value.open()
    value.run_sync(lambda c: c.execute(
        "INSERT INTO principals(name,created_at) VALUES('alice',?)", (utcnow(),)))
    value.run_sync(lambda c: c.execute(
        "INSERT INTO tokens(id,owner,kind,label,secret_sha256,scopes,created_at) "
        "VALUES('tok','alice','human','test','x','[]',?)", (utcnow(),)))
    yield value
    value.close()


def _files(size: int, *, relpath: str = "out/file") -> list[dict]:
    return [{"slot": "000000", "relpath": relpath, "size": size}]


def test_concurrent_reservations_cannot_overcommit_owner_or_global_caps(store):
    store.run_sync(lambda c: (_job(c, "alice", 1, "att_one"), _job(c, "alice", 2, "att_two")))

    def reserve(attempt: str, job: int):
        return store.run_sync(lambda c: artifacts.reserve_files(
            c, job, attempt, [{"path": "out", "required": True}], _files(7),
            owner_max_bytes=10, global_max_bytes=10))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve, "att_one", 1), pool.submit(reserve, "att_two", 2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results, key=lambda result: result[0]) == [(False, "owner_retained_bytes"), (True, None)]
    assert store.run_sync(lambda c: c.execute("SELECT SUM(size) FROM artifacts").fetchone()[0]) == 7


def test_global_cap_applies_across_different_owners(store):
    store.run_sync(lambda c: (_job(c, "alice", 1, "att_one"), _job(c, "bob", 2, "att_two")))
    assert store.run_sync(lambda c: artifacts.reserve_files(
        c, 1, "att_one", [{"path": "out", "required": True}], _files(7),
        owner_max_bytes=10, global_max_bytes=10)) == (True, None)
    assert store.run_sync(lambda c: artifacts.reserve_files(
        c, 2, "att_two", [{"path": "out", "required": True}], _files(7),
        owner_max_bytes=10, global_max_bytes=10)) == (False, "global_retained_bytes")


def test_token_cap_isolated_between_tokens_with_same_owner(store):
    store.run_sync(lambda c: (_job(c, "alice", 1, "att_agent_one", token_id="tok_agent",
                                  token_quota={"artifact_bytes": 5}),
                              _job(c, "alice", 2, "att_agent_two", token_id="tok_agent"),
                              _job(c, "alice", 3, "att_human", token_id="tok_human")))
    def reserve(job_id, attempt_id, size):
        return store.run_sync(lambda c: artifacts.reserve_files(
            c, job_id, attempt_id, [{"path": "out", "required": True}], _files(size),
            owner_max_bytes=20, global_max_bytes=30))

    assert reserve(1, "att_agent_one", 5) == (True, None)
    assert reserve(2, "att_agent_two", 1) == (False, "token_retained_bytes")
    # A second token owned by Alice has its own byte budget, while still
    # contributing to Alice's shared owner cap.
    assert reserve(3, "att_human", 4) == (True, None)
    assert store.run_sync(lambda c: c.execute("SELECT SUM(size) FROM artifacts").fetchone()[0]) == 9


@pytest.mark.parametrize("files,cap,error", [
    (_files(1) * 2, 1, "too_many_files"),
    (_files(-1), 10, "invalid_manifest"),
    (_files(11), 10, "too_large"),
])
def test_staged_manifest_count_and_sizes_are_bounded(files, cap, error):
    assert artifacts.validate_manifest(files, max_files=1, max_bytes=cap)[1] == error


def test_pending_reservation_is_idempotent_and_survives_controller_recreation(store):
    store.run_sync(lambda c: (_job(c, "alice", 1, "att_one"), _job(c, "alice", 2, "att_two")))
    reserve = lambda attempt, job, size: store.run_sync(lambda c: artifacts.reserve_files(
        c, job, attempt, [{"path": "out", "required": True}], _files(size),
        owner_max_bytes=10, global_max_bytes=20))
    assert reserve("att_one", 1, 7) == (True, None)
    # A restart and replay of the same manifest keep one durable reservation.
    assert reserve("att_one", 1, 7) == (True, None)
    assert store.run_sync(lambda c: c.execute("SELECT SUM(size) FROM artifacts").fetchone()[0]) == 7
    assert reserve("att_two", 2, 4) == (False, "owner_retained_bytes")


class PullSpy:
    backend = "fake"

    def __init__(self, size: int):
        self.size = size
        self.pulls = 0

    async def collect_stage(self, *_args, **_kwargs):
        return CollectManifest(ok=True, files=_files(self.size))

    async def collect_pull(self, _target, _attempt, local):
        self.pulls += 1
        (local / "000000").write_bytes(b"x" * self.size)
        return True, None

    async def collect_clean(self, *_args):
        return True


def _controller(store, tmp_path, spy, *, owner_cap=10, global_cap=20, reserve=1):
    ident = store.run_sync(lambda c: fence.start_controller(c))
    config = ControllerConfig(collect_max_bytes=10, collect_max_files=10,
                              artifact_owner_max_bytes=owner_cap, artifact_global_max_bytes=global_cap,
                              artifact_free_reserve_bytes=reserve)
    root = tmp_path / "artifacts"
    class AsyncStore:
        async def run(self, fn):
            return store.run_sync(fn)

    return Controller(AsyncStore(), {"fake": spy}, ident, config=config, artifact_dir=root, clock_ok=lambda: True), root


def test_quota_denial_is_visible_and_never_starts_pull(store, tmp_path, monkeypatch):
    store.run_sync(lambda c: (_job(c, "alice", 1, "att_existing"), _job(c, "alice", 2, "att_new")))
    store.run_sync(lambda c: c.execute(
        "INSERT INTO artifacts(job_id,attempt_id,relpath,required,state,size,updated_at) "
        "VALUES(1,'att_existing','out/old',1,'COMPLETE',9,?)", (utcnow(),)))
    spy = PullSpy(2)
    controller, _ = _controller(store, tmp_path, spy, owner_cap=10)
    monkeypatch.setattr(artifacts, "available_bytes", lambda _path: 1024 * 1024 * 1024)
    row = store.run_sync(lambda c: c.execute(
        "SELECT j.id AS job_id,j.spec_json,j.ended_at,j.artifacts_state,a.id AS attempt_id,a.n,a.target,a.backend "
        "FROM jobs j JOIN attempts a ON a.job_id=j.id WHERE a.id='att_new'").fetchone())
    asyncio.run(controller._collect_job_artifacts(row))
    assert spy.pulls == 0
    job = store.run_sync(lambda c: c.execute("SELECT phase,execution_outcome,artifacts_state,reason "
                                              "FROM jobs WHERE id=2").fetchone())
    assert tuple(job[:3]) == ("FINALIZING", "COMPLETED", "RETRY_WAIT")
    assert "owner_retained_bytes" in job["reason"]
    assert store.run_sync(lambda c: c.execute(
        "SELECT COUNT(*) FROM artifacts WHERE attempt_id='att_new'").fetchone()[0]) == 0


def test_physical_reserve_defers_without_losing_compute_or_reservation(store, tmp_path, monkeypatch):
    store.run_sync(lambda c: _job(c, "alice", 1, "att_disk"))
    spy = PullSpy(5)
    controller, _ = _controller(store, tmp_path, spy, reserve=6)
    monkeypatch.setattr(artifacts, "available_bytes", lambda _path: 10)
    row = store.run_sync(lambda c: c.execute(
        "SELECT j.id AS job_id,j.spec_json,j.ended_at,j.artifacts_state,a.id AS attempt_id,a.n,a.target,a.backend "
        "FROM jobs j JOIN attempts a ON a.job_id=j.id WHERE a.id='att_disk'").fetchone())
    asyncio.run(controller._collect_job_artifacts(row))
    assert spy.pulls == 0
    job = store.run_sync(lambda c: c.execute("SELECT phase,execution_outcome,artifacts_state,reason "
                                              "FROM jobs WHERE id=1").fetchone())
    assert tuple(job[:3]) == ("FINALIZING", "COMPLETED", "RETRY_WAIT")
    assert "filesystem reserve" in job["reason"]
    assert store.run_sync(lambda c: c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]) == 0


def test_symlinked_partial_parent_defers_without_deleting_outside_files(store, tmp_path):
    store.run_sync(lambda c: _job(c, "alice", 1, "att_symlink"))
    spy = PullSpy(1)
    controller, root = _controller(store, tmp_path, spy)
    root.mkdir()
    outside = tmp_path / "important"
    outside.mkdir()
    for name in ("att_symlink", "att_symlink.pub"):
        entry = outside / name
        entry.mkdir()
        (entry / "keep.txt").write_text("keep")
    (root / ".partial").symlink_to(outside, target_is_directory=True)
    row = store.run_sync(lambda c: c.execute(
        "SELECT j.id AS job_id,j.spec_json,j.ended_at,j.artifacts_state,a.id AS attempt_id,a.n,a.target,a.backend "
        "FROM jobs j JOIN attempts a ON a.job_id=j.id WHERE a.id='att_symlink'").fetchone())
    asyncio.run(controller._collect_job_artifacts(row))
    assert spy.pulls == 0
    assert all((outside / name / "keep.txt").read_text() == "keep"
               for name in ("att_symlink", "att_symlink.pub"))
    job = store.run_sync(lambda c: c.execute("SELECT artifacts_state,reason FROM jobs WHERE id=1").fetchone())
    assert job["artifacts_state"] == "RETRY_WAIT" and "unsafe artifact staging path" in job["reason"]
