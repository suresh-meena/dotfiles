import json

import pytest

from fleetq.cli import validate_fleetctl_inventory
from fleetq.config import DaemonConfig, NodeConfig, load_config
from fleetq.errors import FqError


def test_inventory_guard_checks_roles_enabled_state_and_config_home(tmp_path):
    executable = tmp_path / "fleetctl"
    output = tmp_path / "output.json"
    log = tmp_path / "argv.txt"
    executable.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$ARGV_LOG\"\ncat \"$INVENTORY\"\n"
    )
    executable.chmod(0o755)
    output.write_text(json.dumps({"targets": [
        {"name": "ws", "role": "workstation", "enabled": True},
        {"name": "login", "role": "login", "enabled": True},
    ]}))
    import os
    old_log, old_inv = os.environ.get("ARGV_LOG"), os.environ.get("INVENTORY")
    os.environ.update(ARGV_LOG=str(log), INVENTORY=str(output))
    try:
        cfg = DaemonConfig(state_dir=tmp_path / "state", fleetctl=executable,
                           fleet_config_home=tmp_path / "fleet", nodes=[
            NodeConfig(id="local", backend="bare", mode="exclusive", enabled=True, fleetctl_target="ws"),
            NodeConfig(id="cluster", backend="slurm", enabled=True, fleetctl_target="login"),
        ])
        validate_fleetctl_inventory(cfg)
        assert "--config-home " + str(tmp_path / "fleet") in log.read_text()
    finally:
        if old_log is None: os.environ.pop("ARGV_LOG", None)
        else: os.environ["ARGV_LOG"] = old_log
        if old_inv is None: os.environ.pop("INVENTORY", None)
        else: os.environ["INVENTORY"] = old_inv


@pytest.mark.parametrize("inventory", [
    {"targets": [{"name": "n", "role": "login", "enabled": True}]},
    {"targets": [{"name": "n", "role": "workstation", "enabled": False}]},
    {"targets": [{"name": "n", "role": "unknown", "enabled": True}]},
])
def test_inventory_guard_fails_closed(tmp_path, monkeypatch, inventory):
    executable = tmp_path / "fleetctl"
    executable.write_text("#!/bin/sh\nprintf '%s' '" + json.dumps(inventory) + "'\n")
    executable.chmod(0o755)
    cfg = DaemonConfig(state_dir=tmp_path / "state", fleetctl=executable,
                       nodes=[NodeConfig(id="n", backend="bare", mode="exclusive", enabled=True)])
    with pytest.raises(SystemExit):
        validate_fleetctl_inventory(cfg)


@pytest.mark.parametrize("node_text", [
    'enabled = "false"', 'login_shell = "false"', 'fleetctl_target = ""',
])
def test_quoted_node_booleans_are_rejected(tmp_path, node_text):
    path = tmp_path / "config.toml"
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\ndev_mode = true\n\n'
                    f'[[node]]\nid = "n"\nbackend = "bare"\nmode = "exclusive"\n{node_text}\n')
    with pytest.raises(FqError):
        load_config(path)


def test_quoted_dev_mode_and_duplicate_enabled_routes_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\ndev_mode = "false"\n')
    with pytest.raises(FqError):
        load_config(path)
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\ndev_mode = true\n\n'
                    '[[node]]\nid = "a"\nbackend = "bare"\nmode = "exclusive"\nenabled = true\nfleetctl_target = "shared"\n\n'
                    '[[node]]\nid = "b"\nbackend = "slurm"\nenabled = true\nfleetctl_target = "shared"\n')
    with pytest.raises(FqError):
        load_config(path)


@pytest.mark.parametrize("section,entry", [("controller", "typo_limit = 1"), ("limits", "typo_limit = 1")])
def test_unknown_safety_limit_keys_rejected(tmp_path, section, entry):
    path = tmp_path / "config.toml"
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\ndev_mode = true\n\n[{section}]\n{entry}\n')
    with pytest.raises(FqError):
        load_config(path)


def test_serve_refuses_dev_mode_enabled_targets_before_lock_or_fleetctl(tmp_path, monkeypatch):
    import asyncio
    import fleetq.cli as cli

    marker = tmp_path / "called"
    executable = tmp_path / "fleetctl"
    executable.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    executable.chmod(0o755)
    cfg = DaemonConfig(state_dir=tmp_path / "state", fleetctl=executable, dev_mode=True,
                       nodes=[NodeConfig(id="n", backend="slurm", enabled=True)])

    def unexpected_lock(*args, **kwargs):
        raise AssertionError("serve acquired its lock before rejecting dev mode")

    monkeypatch.setattr(cli, "ControllerLock", unexpected_lock)
    with pytest.raises(SystemExit, match="dev_mode cannot serve enabled remote targets"):
        asyncio.run(cli.serve(cfg))
    assert not marker.exists()


def test_production_runtime_checks_target_role_before_opening_state(tmp_path, monkeypatch):
    import fleetq.cli as cli

    executable = tmp_path / "fleetctl"
    executable.write_text("#!/bin/sh\nprintf '%s' '[{\"name\":\"site\",\"role\":\"login\",\"enabled\":true}]'\n")
    executable.chmod(0o755)
    cfg = DaemonConfig(state_dir=tmp_path / "state", fleetctl=executable,
                       nodes=[NodeConfig(id="site", backend="bare", mode="exclusive", enabled=True)])
    def unexpected_open(_cfg):
        raise AssertionError("state opened before target role was checked")

    monkeypatch.setattr(cli, "open_store", unexpected_open)
    with pytest.raises(SystemExit, match="requires enabled fleetctl target"):
        cli.build_runtime(cfg)


def test_clock_provider_is_explicit_and_validated(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\nclock_provider = "unknown"\n')
    with pytest.raises(FqError, match="clock_provider"):
        load_config(path)
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\nclock_provider = ["chrony"]\n')
    with pytest.raises(FqError, match="clock_provider"):
        load_config(path)
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\nclock_provider = "chrony"\n')
    assert load_config(path).clock_provider == "chrony"
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\nclock_provider = "systemd-timesyncd"\n')
    assert load_config(path).clock_provider == "systemd-timesyncd"


def test_production_runtime_requires_clock_provider_before_opening_state(tmp_path, monkeypatch):
    import fleetq.cli as cli

    cfg = DaemonConfig(state_dir=tmp_path / "state")

    def unexpected_open(_cfg):
        raise AssertionError("state opened before clock provider was checked")

    monkeypatch.setattr(cli, "open_store", unexpected_open)
    with pytest.raises(SystemExit, match="clock_provider"):
        cli.build_runtime(cfg)


def test_production_runtime_shares_initially_unhealthy_clock_gate(tmp_path, monkeypatch):
    import asyncio
    import fleetq.cli as cli
    from fleetq.clock import ClockHealth
    from fleetq.db.store import Store

    store = Store(tmp_path / "fleetq.db")
    store.open()
    monkeypatch.setattr(cli, "open_store", lambda _cfg: store)
    health = ClockHealth(probe=lambda: True)
    seen = []
    monkeypatch.setattr(cli, "ClockHealth", lambda provider: seen.append(provider) or health)
    cfg = DaemonConfig(state_dir=tmp_path / "state", clock_provider="systemd-timesyncd")
    try:
        _, controller, runtime = cli.build_runtime(cfg, executors={})
        assert seen == [cli.ClockProvider.SYSTEMD_TIMESYNCD]
        assert runtime.clock_health is health
        assert controller.clock_ok() is False
        assert runtime.clock_ok() is False
        assert asyncio.run(health.refresh()) is True
        assert controller.clock_ok() is True
        assert runtime.clock_ok() is True
    finally:
        store.close()
