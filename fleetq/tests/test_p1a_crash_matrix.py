"""P1a local crash/send boundary matrix (§13.2).

These tests use the production Store/Controller reducer and in-memory executor;
no transport process or remote target is involved.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from fleetq.db.store import Store, migrate
from fleetq.engine import fence, state
from fleetq.executors.base import AttemptContext, AttemptObservation, LaunchKind, LaunchResult, ObserveResult

from harness import Harness


class InlineAsyncStore:
    def __init__(self, store):
        self.store = store

    async def run(self, fn):
        return self.store.run_sync(fn)


def counts(h: Harness, job_id: int) -> tuple[int, int]:
    return h.store.run_sync(lambda c: (
        c.execute("SELECT COUNT(*) FROM attempts WHERE job_id=?", (job_id,)).fetchone()[0],
        c.execute("SELECT COUNT(*) FROM resource_reservations r JOIN attempts a ON a.id=r.attempt_id "
                  "WHERE a.job_id=? AND r.released_at IS NULL", (job_id,)).fetchone()[0],
    ))


def test_commit_boundary_rollback_and_committed_reopen(tmp_path):
    db = tmp_path / "boundary.db"
    store = Store(db)
    store.open()
    try:
        def fail_before_commit(conn):
            conn.execute("INSERT INTO controller_meta(key,value) VALUES('boundary','partial')")
            raise RuntimeError("crash before commit")
        try:
            store.run_sync(fail_before_commit)
        except RuntimeError:
            pass
        else:
            raise AssertionError("injected failure swallowed")
        assert store.run_sync(lambda c: c.execute(
            "SELECT 1 FROM controller_meta WHERE key='boundary'").fetchone()) is None
        store.run_sync(lambda c: c.execute(
            "INSERT INTO controller_meta(key,value) VALUES('boundary','committed')"))
    finally:
        store.close()
    reopened = Store(db)
    reopened.open()
    try:
        assert reopened.run_sync(lambda c: c.execute(
            "SELECT value FROM controller_meta WHERE key='boundary'").fetchone()[0]) == "committed"
    finally:
        reopened.close()


def test_interrupted_staging_recovers_unsent_and_releases_all_holds(tmp_path):
    h = Harness(tmp_path)
    jid = h.submit(key="stage-crash")['jobs'][0]
    def stage_crash(conn):
        att = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1,
                                   reservations=[{"node_id":"n1", "kind":"gpu", "gpu_uuid":"GPU-a", "amount":1}])
        state.update_attempt(conn, att["id"], state="STAGING", event="staging")
        return att["id"]
    aid = h.store.run_sync(stage_crash)
    ctl = h.start()
    ctl.store = InlineAsyncStore(h.store)
    try:
        asyncio.run(ctl.startup())
        att = h.store.run_sync(lambda c: state.get_attempt(c, aid))
        assert att["state"] == "NEVER_STARTED" and att["remote_may_be_live"] == 0
        assert counts(h, jid) == (1, 0)
        assert h.fake.entries() == {}
        assert h.invariants() == []
    finally:
        h.close()


def test_uncertain_launch_holds_reservation_and_observations_are_monotonic(tmp_path):
    h = Harness(tmp_path)
    jid = h.submit(key="launch-unknown")['jobs'][0]
    def prepare(conn):
        att = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1,
                                   reservations=[{"node_id":"n1", "kind":"gpu", "gpu_uuid":"GPU-a", "amount":1}])
        state.update_attempt(conn, att["id"], state="STAGING", event="staging")
        state.update_attempt(conn, att["id"], state="LAUNCHING", event="launching")
        return att["id"]
    aid = h.store.run_sync(prepare)
    ctl = h.start()
    ctl.store = InlineAsyncStore(h.store)
    try:
        h.fake.node("n1").lose_launch_response = True
        send_result = asyncio.run(h.fake.launch(AttemptContext(
            aid, jid, 1, "n1", 1, "fleet", {}, "digest", f"launch-{aid}")))
        assert send_result.kind is LaunchKind.UNKNOWN
        assert h.fake.entries() == {aid: 1}
        asyncio.run(ctl._apply_launch(aid, send_result))
        att = h.store.run_sync(lambda c: state.get_attempt(c, aid))
        assert att["state"] == "START_UNKNOWN" and att["remote_may_be_live"] == 1
        assert counts(h, jid) == (1, 1)
        run_obs = ObserveResult(True, {aid: AttemptObservation(aid, "running", payload_entered=True)})
        stop_obs = ObserveResult(True, {aid: AttemptObservation(aid, "stopped", payload_entered=True,
                                                                 outcome="COMPLETED", exit_code=0,
                                                                 cgroup_empty=True)})
        asyncio.run(ctl._apply_observations("n1", run_obs))
        asyncio.run(ctl._apply_observations("n1", stop_obs))
        asyncio.run(ctl._apply_observations("n1", run_obs))  # delayed duplicate
        att = h.store.run_sync(lambda c: state.get_attempt(c, aid))
        assert att["state"] in ("STOPPED", "RELEASED")
        assert att["outcome"] == "COMPLETED"
        assert h.fake.entries() == {aid: 1}
        assert counts(h, jid)[0] == 1
        assert h.invariants() == []
    finally:
        h.close()


def test_late_launch_reply_cannot_overwrite_observed_stop(tmp_path):
    h = Harness(tmp_path)
    jid = h.submit(key="late-reply")['jobs'][0]
    def prepare(conn):
        att = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1, reservations=[])
        state.update_attempt(conn, att["id"], state="STAGING", event="staging")
        state.update_attempt(conn, att["id"], state="LAUNCHING", event="launching")
        return att["id"]
    aid = h.store.run_sync(prepare)
    ctl = h.start()
    ctl.store = InlineAsyncStore(h.store)
    try:
        asyncio.run(ctl._apply_observations("n1", ObserveResult(True, {aid: AttemptObservation(
            aid, "stopped", payload_entered=True, outcome="COMPLETED", exit_code=0, cgroup_empty=True)})))
        asyncio.run(ctl._apply_launch(aid, LaunchResult(LaunchKind.STARTED, remote_id="late")))
        att = h.store.run_sync(lambda c: state.get_attempt(c, aid))
        assert att["state"] in ("STOPPED", "RELEASED")
        assert att["outcome"] == "COMPLETED"
        assert counts(h, jid)[0] == 1
        assert h.invariants() == []
    finally:
        h.close()


@pytest.mark.parametrize("observation", ["running", "stopped"])
@pytest.mark.parametrize("late_kind", [LaunchKind.NEVER_STARTED, LaunchKind.PLACEMENT_REFUSED])
def test_late_nonstart_reply_cannot_override_observed_execution(tmp_path, observation, late_kind):
    h = Harness(tmp_path)
    jid = h.submit(key=f"late-nonstart-{observation}-{late_kind.value}")['jobs'][0]
    def prepare(conn):
        att = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1,
                                   reservations=[{"node_id":"n1", "kind":"gpu", "gpu_uuid":"GPU-a", "amount":1}])
        state.update_attempt(conn, att["id"], state="STAGING", event="staging")
        state.update_attempt(conn, att["id"], state="LAUNCHING", event="launching")
        return att["id"]
    aid = h.store.run_sync(prepare)
    ctl = h.start()
    ctl.store = InlineAsyncStore(h.store)
    try:
        payload_entered = observation in ("running", "stopped")
        evidence = AttemptObservation(aid, observation, payload_entered=payload_entered,
                                      outcome="COMPLETED" if observation == "stopped" else None,
                                      exit_code=0 if observation == "stopped" else None,
                                      cgroup_empty=True if observation == "stopped" else None)
        asyncio.run(ctl._apply_observations("n1", ObserveResult(True, {aid: evidence})))
        expected = "RUNNING" if observation == "running" else "RELEASED"
        before = h.store.run_sync(lambda c: state.get_attempt(c, aid))
        assert before["state"] == expected
        late = LaunchResult(late_kind, reason="delayed conflicting reply", cooldown_gpus=["GPU-a"])
        asyncio.run(ctl._apply_launch(aid, late))
        after = h.store.run_sync(lambda c: state.get_attempt(c, aid))
        assert after["state"] == expected
        if observation == "running":
            assert counts(h, jid) == (1, 1)
        else:
            assert after["outcome"] == "COMPLETED"
            assert counts(h, jid) == (1, 0)
        assert h.invariants() == []
    finally:
        h.close()


def test_migration_and_online_backup_preserve_uncertainty_and_tombstone(tmp_path):
    legacy = sqlite3.connect(tmp_path / "legacy.db")
    legacy.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    legacy.execute("PRAGMA user_version=1")
    migrate(legacy)
    assert legacy.execute("PRAGMA user_version").fetchone()[0] == 4
    migrate(legacy)
    assert legacy.execute("PRAGMA user_version").fetchone()[0] == 4
    legacy.close()

    (tmp_path / "source").mkdir()
    h = Harness(tmp_path / "source")
    jid = h.submit(key="backup-matrix")['jobs'][0]
    def uncertain(conn):
        att = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1, reservations=[])
        state.update_attempt(conn, att["id"], state="STAGING", event="staging")
        state.update_attempt(conn, att["id"], state="LAUNCHING", event="launching")
    h.store.run_sync(uncertain)
    dest = tmp_path / "backup.db"
    h.store.backup(dest)
    h.close()
    restored = Store(dest)
    restored.open()
    try:
        assert restored.run_sync(lambda c: c.execute(
            "SELECT COUNT(*) FROM idempotency WHERE key='backup-matrix'").fetchone()[0]) == 1
        row = restored.run_sync(lambda c: c.execute(
            "SELECT state,remote_may_be_live FROM attempts WHERE job_id=?", (jid,)).fetchone())
        assert (row["state"], row["remote_may_be_live"]) == ("LAUNCHING", 1)
        restored.run_sync(lambda c: fence.start_controller(c, restored_from_backup=True))
        assert restored.run_sync(fence.restore_discovery_pending)
    finally:
        restored.close()
