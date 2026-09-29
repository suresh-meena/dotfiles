"""Focused P1a persistence, restore, and desired-state race regressions."""

from __future__ import annotations

import asyncio
import sqlite3


from fleetq.db.store import Store, migrate
from fleetq import cli
from fleetq.engine import fence, state
from fleetq.executors.fake import FakeNode, FakeRemoteAttempt

from harness import Harness


def test_store_rolls_back_failed_mutation_and_remains_usable(tmp_path):
    store = Store(tmp_path / "rollback.db")
    store.open()
    try:
        def fail(conn):
            conn.execute("INSERT INTO controller_meta(key,value) VALUES ('partial','yes')")
            raise RuntimeError("injected pre-commit failure")

        try:
            store.run_sync(fail)
        except RuntimeError as exc:
            assert "injected" in str(exc)
        else:
            raise AssertionError("injected transaction failure was swallowed")

        assert store.run_sync(lambda c: c.execute(
            "SELECT value FROM controller_meta WHERE key='partial'").fetchone()) is None
        store.run_sync(lambda c: c.execute(
            "INSERT INTO controller_meta(key,value) VALUES ('after','usable')"))
        assert store.run_sync(lambda c: c.execute(
            "SELECT value FROM controller_meta WHERE key='after'").fetchone()[0]) == "usable"
    finally:
        store.close()


def test_sequential_schema_migrations_are_idempotent(tmp_path):
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    conn.execute("PRAGMA user_version=1")
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")} >= {
            "managed_slurm_snapshots", "budget_dimension_buckets"}
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    conn.close()


def test_online_backup_restores_request_tombstone_and_unresolved_attempt(tmp_path):
    (tmp_path / "source").mkdir()
    h = Harness(tmp_path / "source")
    jid = h.submit(key="durable-key")["jobs"][0]

    def create_uncertain_attempt(conn):
        attempt = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1, reservations=[])
        state.update_attempt(conn, attempt["id"], state="STAGING", event="staging")
        state.update_attempt(conn, attempt["id"], state="LAUNCHING", event="launching")
    h.store.run_sync(create_uncertain_attempt)
    backup = tmp_path / "restore.db"
    h.store.backup(backup)
    h.close()

    restored = Store(backup)
    restored.open()
    try:
        rows = restored.run_sync(lambda c: (
            c.execute("SELECT id,state,remote_may_be_live FROM attempts WHERE job_id=?", (jid,)).fetchall(),
            c.execute("SELECT COUNT(*) FROM idempotency WHERE key='durable-key'").fetchone()[0],
        ))
        assert [(r["state"], r["remote_may_be_live"]) for r in rows[0]] == [("LAUNCHING", 1)]
        assert rows[1] == 1
        ident = restored.run_sync(lambda c: fence.start_controller(c, restored_from_backup=True))
        assert ident.restored
        assert restored.run_sync(fence.restore_discovery_pending)
    finally:
        restored.close()


def test_restored_controller_completes_discovery_after_all_targets_are_fenced(tmp_path):
    h = Harness(tmp_path)
    h.fake.nodes["n2"] = FakeNode("n2", gpus=[])
    h.add_node("n2", gpus=[], enabled=False)
    h.store.run_sync(lambda c: c.execute("UPDATE nodes SET fence_epoch=1 WHERE id='n2'"))
    ctl = h.start(restored=True)

    class InlineAsyncStore:
        async def run(self, fn):
            return h.store.run_sync(fn)

    ctl.store = InlineAsyncStore()
    try:
        asyncio.run(ctl.startup())
        assert h.store.run_sync(fence.restore_discovery_pending) is False
        assert h.store.run_sync(lambda c: c.execute(
            "SELECT reconciled_epoch FROM nodes WHERE id='n1'").fetchone()[0]) == ctl.ident.epoch
        assert h.store.run_sync(lambda c: c.execute(
            "SELECT reconciled_epoch FROM nodes WHERE id='n2'").fetchone()[0]) == ctl.ident.epoch
    finally:
        h.close()


def test_restore_does_not_probe_never_fenced_disabled_target(tmp_path):
    h = Harness(tmp_path)
    h.fake.nodes["n2"] = FakeNode("n2", gpus=[])
    h.add_node("n2", gpus=[], enabled=False)
    ctl = h.start(restored=True)

    class InlineAsyncStore:
        async def run(self, fn):
            return h.store.run_sync(fn)

    ctl.store = InlineAsyncStore()
    try:
        asyncio.run(ctl.startup())
        assert ("fence", "n1") in h.fake.calls
        assert ("fence", "n2") not in h.fake.calls
        assert h.store.run_sync(fence.restore_discovery_pending) is False
    finally:
        h.close()


def test_restored_controller_keeps_global_gate_if_target_is_unreachable(tmp_path):
    h = Harness(tmp_path)
    h.fake.node("n1").reachable = False
    ctl = h.start(restored=True)

    class InlineAsyncStore:
        async def run(self, fn):
            return h.store.run_sync(fn)

    ctl.store = InlineAsyncStore()
    try:
        asyncio.run(ctl.startup())
        assert h.store.run_sync(fence.restore_discovery_pending) is True
        assert h.store.run_sync(lambda c: c.execute(
            "SELECT state FROM nodes WHERE id='n1'").fetchone()[0]) == "UNREACHABLE"
    finally:
        h.close()


def test_restored_controller_keeps_global_gate_for_remote_orphan(tmp_path):
    h = Harness(tmp_path)
    h.fake.node("n1").attempts["att_orphan"] = FakeRemoteAttempt("att_orphan", epoch=1)
    ctl = h.start(restored=True)

    class InlineAsyncStore:
        async def run(self, fn):
            return h.store.run_sync(fn)

    ctl.store = InlineAsyncStore()
    try:
        asyncio.run(ctl.startup())
        assert h.store.run_sync(fence.restore_discovery_pending)
        assert h.store.run_sync(lambda c: c.execute(
            "SELECT state FROM nodes WHERE id='n1'").fetchone()[0]) == "QUARANTINED"
        assert h.store.run_sync(lambda c: c.execute(
            "SELECT COUNT(*) FROM events WHERE kind='orphans_discovered'").fetchone()[0]) == 1
    finally:
        h.close()


def test_restore_raises_epoch_and_refences_all_targets(tmp_path):
    h = Harness(tmp_path)
    h.fake.nodes["n2"] = FakeNode("n2", gpus=[], highest_epoch=5)
    h.add_node("n2", gpus=[], enabled=False)
    h.store.run_sync(lambda c: c.execute(
        "UPDATE nodes SET fence_epoch=1, state='QUARANTINED' WHERE id='n2'"))
    ctl = h.start(restored=True)

    class InlineAsyncStore:
        async def run(self, fn):
            return h.store.run_sync(fn)

    ctl.store = InlineAsyncStore()
    try:
        asyncio.run(ctl.startup())
        assert ctl.ident.epoch == 6
        assert h.fake.node("n1").highest_epoch == 6
        assert h.fake.node("n2").highest_epoch == 6
        assert h.store.run_sync(fence.current_epoch) == 6
        assert h.store.run_sync(fence.restore_discovery_pending) is True
        assert h.store.run_sync(lambda c: {
            row[0] for row in c.execute("SELECT reconciled_epoch FROM nodes")
        }) == {6}
        assert h.store.run_sync(lambda c: c.execute(
            "SELECT state FROM nodes WHERE id='n2'").fetchone()[0]) == "QUARANTINED"
        assert h.store.run_sync(lambda c: c.execute(
            "SELECT COUNT(*) FROM events WHERE kind='epoch_raised'").fetchone()[0]) == 1
        assert h.fake.entries() == {}
    finally:
        h.close()


def test_restore_epoch_retry_survives_controller_restart(tmp_path):
    h = Harness(tmp_path)
    h.fake.nodes["n2"] = FakeNode("n2", gpus=[], highest_epoch=5)
    h.add_node("n2", gpus=[], enabled=False)
    h.store.run_sync(lambda c: c.execute("UPDATE nodes SET fence_epoch=1 WHERE id='n2'"))
    h.fake.node("n1").reachable = False
    first = h.start(restored=True)

    class InlineAsyncStore:
        async def run(self, fn):
            return h.store.run_sync(fn)

    first.store = InlineAsyncStore()
    try:
        asyncio.run(first.startup())
        assert first.ident.epoch == 6
        assert h.store.run_sync(fence.restore_discovery_pending)
        assert h.fake.node("n2").highest_epoch == 6
        h.close()

        h2 = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a", "GPU-b"]),
                                FakeNode("n2", gpus=[], highest_epoch=6)])
        h2.store.run_sync(lambda c: c.execute("UPDATE nodes SET enabled=0 WHERE id='n2'"))
        second = h2.start()
        class InlineAsyncStore2:
            async def run(self, fn):
                return h2.store.run_sync(fn)
        second.store = InlineAsyncStore2()
        asyncio.run(second.startup())
        assert second.ident.restored
        assert second.ident.epoch == 7
        assert h2.fake.node("n1").highest_epoch == 7
        assert h2.fake.node("n2").highest_epoch == 7
        assert h2.store.run_sync(fence.restore_discovery_pending) is False
    finally:
        h.close()
        if "h2" in locals():
            h2.close()


def test_serve_restore_flag_reaches_runtime_builder(monkeypatch):
    seen = {}

    async def fake_serve(cfg, *, restored_from_backup=False):
        seen["cfg"] = cfg
        seen["restored"] = restored_from_backup

    config = object()
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "serve", fake_serve)
    assert cli.main(["serve", "--restored-from-backup"]) == 0
    assert seen == {"cfg": config, "restored": True}
