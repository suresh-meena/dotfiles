import asyncio
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

from fleetq import cli


class FakeStore:
    def __init__(self, *, reconciled=3, epoch=3, state="ACTIVE", fleet_id="fleet-test"):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
          CREATE TABLE controller_meta (key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE nodes (id TEXT PRIMARY KEY, reconciled_epoch INTEGER, state TEXT);
        """)
        if fleet_id is not None:
            self.conn.execute("INSERT INTO controller_meta VALUES ('fleet_id',?)", (fleet_id,))
        self.conn.execute("INSERT INTO controller_meta VALUES ('controller_epoch',?)", (str(epoch),))
        self.conn.execute("INSERT INTO controller_meta VALUES ('restore_discovery_pending','0')")
        self.conn.execute("INSERT INTO nodes VALUES ('cluster',?,?)", (reconciled, state))
        self.conn.commit()

    def run_sync(self, callback):
        return callback(self.conn)

    def close(self):
        self.conn.close()


class FakeSlurmExecutor:
    def __init__(self):
        self.fleet_id = ""
        self.calls = []

    async def cache_gc(self, target, *, epoch, purge=None):
        self.calls.append((target, epoch, purge, self.fleet_id))
        return {"ok": True, "mode": "inspect", "eligible": [], "blocked": []}


def test_operator_cli_routes_one_slurm_target_with_current_epoch_and_purge(monkeypatch, capsys, tmp_path):
    store = FakeStore()
    executor = FakeSlurmExecutor()
    cfg = SimpleNamespace(state_dir=tmp_path, nodes=[SimpleNamespace(id="cluster", backend="slurm")])
    monkeypatch.setattr(cli, "open_store", lambda _cfg: store)
    monkeypatch.setattr(cli, "build_executors", lambda _cfg, _store: {"slurm": executor})

    rc = cli.cmd_slurm_cache_gc(cfg, SimpleNamespace(target="cluster", purge="sha256:" + "a" * 64,
                                                     authorized=True))

    assert rc == 0
    assert executor.calls == [("cluster", 3, "sha256:" + "a" * 64, "fleet-test")]
    assert '"mode": "inspect"' in capsys.readouterr().out


def test_operator_cli_refuses_unconfigured_or_unfenced_target(monkeypatch, capsys, tmp_path):
    store = FakeStore(reconciled=2)
    executor = FakeSlurmExecutor()
    cfg = SimpleNamespace(state_dir=tmp_path, nodes=[SimpleNamespace(id="cluster", backend="slurm")])
    monkeypatch.setattr(cli, "open_store", lambda _cfg: store)
    monkeypatch.setattr(cli, "build_executors", lambda _cfg, _store: {"slurm": executor})

    rc = cli.cmd_slurm_cache_gc(cfg, SimpleNamespace(target="cluster", purge=None, authorized=False))

    assert rc == 2
    assert executor.calls == []
    assert "not reconciled" in capsys.readouterr().err


def test_operator_cli_requires_literal_target_authorization_for_purge(monkeypatch, capsys, tmp_path):
    store = FakeStore()
    executor = FakeSlurmExecutor()
    cfg = SimpleNamespace(state_dir=tmp_path, nodes=[SimpleNamespace(id="cluster", backend="slurm")])
    monkeypatch.setattr(cli, "open_store", lambda _cfg: store)
    monkeypatch.setattr(cli, "build_executors", lambda _cfg, _store: {"slurm": executor})

    rc = cli.cmd_slurm_cache_gc(cfg, SimpleNamespace(target="cluster", purge="sha256:" + "a" * 64,
                                                     authorized=False))

    assert rc == 2
    assert executor.calls == []
    assert "--i-authorize-target-cluster" in capsys.readouterr().err


def test_operator_cli_refuses_quarantined_target(monkeypatch, capsys, tmp_path):
    store = FakeStore(state="QUARANTINED")
    executor = FakeSlurmExecutor()
    cfg = SimpleNamespace(state_dir=tmp_path, nodes=[SimpleNamespace(id="cluster", backend="slurm")])
    monkeypatch.setattr(cli, "open_store", lambda _cfg: store)
    monkeypatch.setattr(cli, "build_executors", lambda _cfg, _store: {"slurm": executor})

    rc = cli.cmd_slurm_cache_gc(cfg, SimpleNamespace(target="cluster", purge=None, authorized=False))

    assert rc == 2
    assert executor.calls == []


def test_operator_cli_holds_controller_lock_before_opening_store(monkeypatch, tmp_path):
    order = []

    class Lock:
        def __init__(self, _state_dir):
            pass

        def acquire(self):
            order.append("lock")

        def release(self):
            order.append("release")

    store = FakeStore()
    executor = FakeSlurmExecutor()
    cfg = SimpleNamespace(state_dir=tmp_path, nodes=[SimpleNamespace(id="cluster", backend="slurm")])
    def open_store(_cfg):
        order.append("store")
        return store
    monkeypatch.setattr(cli, "ControllerLock", Lock)
    monkeypatch.setattr(cli, "open_store", open_store)
    monkeypatch.setattr(cli, "build_executors", lambda _cfg, _store: {"slurm": executor})

    rc = cli.cmd_slurm_cache_gc(cfg, SimpleNamespace(target="cluster", purge=None, authorized=False))

    assert rc == 0
    assert order == ["lock", "store", "release"]


def test_operator_cli_refuses_missing_durable_identity(monkeypatch, capsys, tmp_path):
    store = FakeStore(fleet_id=None)
    executor = FakeSlurmExecutor()
    cfg = SimpleNamespace(state_dir=tmp_path, nodes=[SimpleNamespace(id="cluster", backend="slurm")])
    monkeypatch.setattr(cli, "ControllerLock", lambda _state_dir: SimpleNamespace(acquire=lambda: None, release=lambda: None))
    monkeypatch.setattr(cli, "open_store", lambda _cfg: store)
    monkeypatch.setattr(cli, "build_executors", lambda _cfg, _store: {"slurm": executor})

    rc = cli.cmd_slurm_cache_gc(cfg, SimpleNamespace(target="cluster", purge=None, authorized=False))

    assert rc == 2
    assert executor.calls == []
    assert "not reconciled" in capsys.readouterr().err


def test_maintenance_commands_validate_home_before_creating_lock(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    cfg = SimpleNamespace(state_dir=home, bundle_dir=home / "bundles",
                          nodes=[SimpleNamespace(id="cluster", backend="slurm")])
    monkeypatch.setattr(cli, "open_store", lambda _cfg: pytest.fail("unsafe state dir reached open_store"))

    assert cli.cmd_gc(cfg, SimpleNamespace(apply=False, json=True)) == 2
    assert cli.cmd_slurm_cache_gc(cfg, SimpleNamespace(target="cluster", purge=None, authorized=False)) == 2
    with pytest.raises(SystemExit, match="fleetqd serve"):
        asyncio.run(cli.serve(cfg))

    assert list(home.iterdir()) == []
    assert "dedicated" in capsys.readouterr().err


def test_maintenance_commands_validate_root_before_lock_construction(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    cfg = SimpleNamespace(state_dir=Path("/"), bundle_dir=Path("/tmp/bundles"),
                          nodes=[SimpleNamespace(id="cluster", backend="slurm")])
    monkeypatch.setattr(cli, "ControllerLock", lambda *_: pytest.fail("unsafe state dir reached lock"))
    monkeypatch.setattr(cli, "open_store", lambda _cfg: pytest.fail("unsafe state dir reached open_store"))

    assert cli.cmd_gc(cfg, SimpleNamespace(apply=False, json=True)) == 2
    assert cli.cmd_slurm_cache_gc(cfg, SimpleNamespace(target="cluster", purge=None, authorized=False)) == 2
    with pytest.raises(SystemExit, match="fleetqd serve"):
        asyncio.run(cli.serve(cfg))
    assert "dedicated" in capsys.readouterr().err
