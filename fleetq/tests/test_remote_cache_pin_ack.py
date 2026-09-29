from __future__ import annotations

import asyncio

from fleetq.engine import fence
from fleetq.engine.controller import Controller, ControllerConfig
from fleetq.util import utcnow

from harness import Harness


class PinExecutor:
    def __init__(self, results: list[bool]):
        self.results = list(results)
        self.calls: list[tuple[str, str, int]] = []

    async def release_cache_pin(self, target: str, attempt_id: str, *, epoch: int) -> bool:
        self.calls.append((target, attempt_id, epoch))
        return self.results.pop(0) if self.results else True


def _attempt(h: Harness, *, phase="TERMINAL", artifacts="COMPLETE", state="RELEASED", backend="slurm") -> str:
    serial = h.store.run_sync(lambda c: c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    jid = h.submit(key=f"pin-{serial}-{phase}-{artifacts}-{state}-{backend}")["jobs"][0]
    now = utcnow()

    def insert(conn):
        job = conn.execute("SELECT spec_digest FROM jobs WHERE id=?", (jid,)).fetchone()
        aid = f"attempt-{jid}"
        target = f"{backend}-site"
        conn.execute("INSERT OR IGNORE INTO nodes(id,backend,enabled,config_json,updated_at) "
                     "VALUES(?,?,1,'{}',?)", (target, backend, now))
        if phase == "TERMINAL":
            conn.execute("UPDATE jobs SET phase=?, execution_outcome='COMPLETED', success=1, artifacts_state=? WHERE id=?",
                         (phase, artifacts, jid))
        else:
            conn.execute("UPDATE jobs SET phase=?, artifacts_state=? WHERE id=?", (phase, artifacts, jid))
        conn.execute(
            "INSERT INTO attempts(id,job_id,n,backend,target,epoch,state,remote_may_be_live,launch_op_id,"
            "spec_digest,created_at,ended_at,updated_at) VALUES(?,?,1,?,?,7,?,0,?,?,?, ?,?)",
            (aid, jid, backend, target, state, f"op-{jid}", job["spec_digest"], now, now, now),
        )
        return aid

    return h.store.run_sync(insert)


def test_remote_pin_release_rotates_across_backends_and_retries_unacked(tmp_path):
    h = Harness(tmp_path)
    try:
        aid1 = _attempt(h)
        aid2 = _attempt(h, backend="bare")
        aid3 = _attempt(h)
        ident = h.store.run_sync(fence.start_controller)
        slurm = PinExecutor([False, True])
        bare = PinExecutor([True])
        ctl = Controller(h.store, {"slurm": slurm, "bare": bare}, ident,
                         config=ControllerConfig(cache_pin_releases_per_tick=1), clock_ok=lambda: True)
        h.store.run_sync(lambda c: c.execute(
            "UPDATE nodes SET fence_epoch=?, reconciled_epoch=?",
            (ident.epoch, ident.epoch)))

        async def run():
            await ctl._release_remote_cache_pins()
            await ctl._release_remote_cache_pins()
            await ctl._release_remote_cache_pins()
            await ctl._release_remote_cache_pins()

        asyncio.run(run())
        assert [aid for _target, aid, _epoch in slurm.calls] == [aid1, aid3, aid1]
        assert [aid for _target, aid, _epoch in bare.calls] == [aid2]
        assert all(epoch == ident.epoch for _target, _aid, epoch in slurm.calls + bare.calls)
        acked = h.store.run_sync(lambda c: {r[0] for r in c.execute(
            "SELECT attempt_id FROM remote_cache_pin_release_acks")})
        assert acked == {aid1, aid2, aid3}
    finally:
        h.close()


def test_pin_release_skips_nonterminal_or_artifact_unresolved_attempts(tmp_path):
    h = Harness(tmp_path)
    try:
        eligible = _attempt(h, artifacts="NOT_REQUESTED")
        _attempt(h, phase="FINALIZING", artifacts="COMPLETE")
        _attempt(h, artifacts="FAILED")
        _attempt(h, artifacts="EXPIRED")
        _attempt(h, phase="PENDING", artifacts="NOT_REQUESTED")
        _attempt(h, state="RUNNING")
        _attempt(h, backend="fake")
        ident = h.store.run_sync(fence.start_controller)
        h.store.run_sync(lambda c: c.execute(
            "UPDATE nodes SET fence_epoch=?, reconciled_epoch=?",
            (ident.epoch - 1, ident.epoch - 1)))
        slurm = PinExecutor([])
        bare = PinExecutor([])
        ctl = Controller(h.store, {"slurm": slurm, "bare": bare}, ident, clock_ok=lambda: True)

        async def run():
            await ctl._release_remote_cache_pins()
            assert not slurm.calls and not bare.calls
            h.store.run_sync(lambda c: c.execute(
                "UPDATE nodes SET fence_epoch=?, reconciled_epoch=?",
                (ident.epoch, ident.epoch)))
            await ctl._release_remote_cache_pins()

        asyncio.run(run())

        assert [call[1] for call in slurm.calls + bare.calls] == [eligible]
    finally:
        h.close()
