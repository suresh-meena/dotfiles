from pathlib import Path

import pytest

import fleetmon.config as config_module
from fleetmon.config import (
    ConfigError,
    HubConfig,
    ensure_backup_filesystem,
    load_config,
)


def test_defaults_are_safe() -> None:
    config = HubConfig.defaults().validate()
    assert config.poll_interval_seconds == 60
    assert config.scheduler_interval_seconds == 60
    assert config.history_interval_seconds == 60
    assert config.ssh_concurrency == 2
    assert config.fleetctl_path.is_absolute()


def test_two_second_direct_polling_is_configurable() -> None:
    defaults = HubConfig.defaults()
    config = HubConfig(**{**defaults.__dict__, "poll_interval_seconds": 2})
    assert config.validate().poll_interval_seconds == 2
    unsafe = HubConfig(**{**defaults.__dict__, "poll_interval_seconds": 1.5})
    with pytest.raises(ConfigError):
        unsafe.validate()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scheduler_interval_seconds", 30),
        ("scheduler_interval_seconds", 0),
        ("history_interval_seconds", 30),
        ("history_interval_seconds", 0),
    ],
)
def test_scheduler_and_history_cadences_keep_conservative_floors(
    field: str, value: object
) -> None:
    defaults = HubConfig.defaults()
    unsafe = HubConfig(**{**defaults.__dict__, field: value})
    with pytest.raises(ConfigError):
        unsafe.validate()


def test_scheduler_and_history_cadences_accept_sixty_seconds() -> None:
    defaults = HubConfig.defaults()
    config = HubConfig(
        **{
            **defaults.__dict__,
            "poll_interval_seconds": 2,
            "scheduler_interval_seconds": 120,
            "history_interval_seconds": 60,
        }
    )
    assert config.validate().scheduler_interval_seconds == 120


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("poll_interval_seconds", 1),
        ("launch_interval_seconds", 0),
        ("ssh_concurrency", 9),
        ("remote_timeout_seconds", 21),
        ("snapshot_stdout_bytes", 300_000),
    ],
)
def test_safety_limits_cannot_be_relaxed(field: str, value: object) -> None:
    defaults = HubConfig.defaults()
    unsafe = HubConfig(**{**defaults.__dict__, field: value})
    with pytest.raises(ConfigError):
        unsafe.validate()


def test_non_loopback_requires_authentication() -> None:
    defaults = HubConfig.defaults()
    unsafe = HubConfig(**{**defaults.__dict__, "bind_host": "0.0.0.0"})
    with pytest.raises(ConfigError, match="FLEETMON_AUTH_TOKEN"):
        unsafe.validate()


def test_load_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[polling]\ninterval_seconds = 60\nsurprise = true\n")
    with pytest.raises(ConfigError, match="unknown polling keys"):
        load_config(path)


def test_load_valid_config(tmp_path: Path) -> None:
    fleetctl = tmp_path / "fleetctl"
    fleetctl.write_text("#!/bin/sh\nexit 0\n")
    fleetctl.chmod(0o700)
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[hub]
fleetctl_path = "{fleetctl}"
state_dir = "/tmp/fleetmon-test-state"

[polling]
enabled = false
disabled_targets = ["example"]
interval_seconds = 2
scheduler_interval_seconds = 300
history_interval_seconds = 120

[retention]
days = 14
disk_reserve_bytes = 134217728
""".strip()
        + "\n"
    )
    config = load_config(path)
    assert config.polling_enabled is False
    assert config.disabled_targets == ("example",)
    assert config.poll_interval_seconds == 2
    assert config.scheduler_interval_seconds == 300
    assert config.history_interval_seconds == 120
    assert config.retention_days == 14


def test_load_rejects_missing_fleetctl(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[hub]\nfleetctl_path = "{tmp_path / "missing"}"\n')
    with pytest.raises(ConfigError, match="executable"):
        load_config(path)


def test_load_rejects_string_boolean(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[polling]\nenabled = "false"\n')
    with pytest.raises(ConfigError, match="must be a boolean"):
        load_config(path)


def test_non_loopback_token_comes_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[hub]\nbind_host = "0.0.0.0"\n')
    monkeypatch.setenv("FLEETMON_AUTH_TOKEN", "x" * 32)
    config = load_config(path)
    assert config.auth_token == "x" * 32


def test_non_finite_interval_is_rejected() -> None:
    defaults = HubConfig.defaults()
    unsafe = HubConfig(**{**defaults.__dict__, "poll_interval_seconds": float("nan")})
    with pytest.raises(ConfigError, match="finite"):
        unsafe.validate()


def test_other_loopback_addresses_do_not_require_auth() -> None:
    defaults = HubConfig.defaults()
    config = HubConfig(**{**defaults.__dict__, "bind_host": "127.0.0.2"})
    assert config.validate().bind_host == "127.0.0.2"


def test_invalid_toml_is_reported_as_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[hub\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_overflowing_interval_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[polling]\ninterval_seconds = 1e10000\n")
    with pytest.raises(ConfigError, match="finite"):
        load_config(path)


def test_state_and_database_require_a_dedicated_shared_directory(
    tmp_path: Path,
) -> None:
    defaults = HubConfig.defaults()
    with pytest.raises(ConfigError, match="dedicated"):
        HubConfig(
            **{
                **defaults.__dict__,
                "state_dir": Path("/tmp"),
                "database_path": Path("/tmp/fleet.db"),
            }
        ).validate()
    with pytest.raises(ConfigError, match="directly inside"):
        HubConfig(
            **{
                **defaults.__dict__,
                "state_dir": tmp_path / "state",
                "database_path": tmp_path / "elsewhere" / "fleet.db",
            }
        ).validate()


def test_backup_dir_relative_path_is_refused(tmp_path: Path) -> None:
    defaults = HubConfig.defaults()
    unsafe = HubConfig(
        **{
            **defaults.__dict__,
            "state_dir": tmp_path / "state",
            "database_path": tmp_path / "state" / "fleet.db",
            "backup_dir": Path("backups"),
        }
    )
    with pytest.raises(ConfigError, match="absolute"):
        unsafe.validate()


def test_backup_dir_on_the_same_filesystem_is_refused(tmp_path: Path) -> None:
    defaults = HubConfig.defaults()
    (tmp_path / "state").mkdir()
    (tmp_path / "backups").mkdir()
    unsafe = HubConfig(
        **{
            **defaults.__dict__,
            "state_dir": tmp_path / "state",
            "database_path": tmp_path / "state" / "fleet.db",
            "backup_dir": tmp_path / "backups",
        }
    )
    with pytest.raises(ConfigError, match="separate filesystem"):
        ensure_backup_filesystem(unsafe)


def test_backup_dir_on_another_filesystem_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config_module,
        "_stat_dev",
        lambda path: 2 if "backups" in str(path) else 1,
    )
    defaults = HubConfig.defaults()
    config = HubConfig(
        **{
            **defaults.__dict__,
            "state_dir": tmp_path / "state",
            "database_path": tmp_path / "state" / "fleet.db",
            "backup_dir": tmp_path / "backups",
        }
    )
    assert config.validate().backup_dir == tmp_path / "backups"


def test_load_config_parses_backup_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleetctl = tmp_path / "fleetctl"
    fleetctl.write_text("#!/bin/sh\nexit 0\n")
    fleetctl.chmod(0o700)
    backup = tmp_path / "backups"
    backup.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    path = tmp_path / "config.toml"
    path.write_text(
        f'[hub]\nfleetctl_path = "{fleetctl}"\n'
        f'state_dir = "{state}"\n'
        f'backup_dir = "{backup}"\n'
    )
    monkeypatch.setattr(
        config_module,
        "_stat_dev",
        lambda target: 2 if target == backup else 1,
    )
    config = load_config(path)
    assert config.backup_dir == backup


def _write_token_file(path: Path, mode: int) -> Path:
    path.write_text("t" * 48 + "\n")
    path.chmod(mode)
    return path


def test_token_file_is_read_from_owner_only_file(tmp_path: Path) -> None:
    token_file = _write_token_file(tmp_path / "token", 0o600)
    assert config_module._read_token_file(str(token_file)) == "t" * 48


def test_token_file_refuses_group_or_world_readable(tmp_path: Path) -> None:
    for mode in (0o644, 0o640, 0o666):
        token_file = _write_token_file(tmp_path / f"token-{mode:o}", mode)
        with pytest.raises(ConfigError, match="group or world"):
            config_module._read_token_file(str(token_file))


def test_token_file_refuses_missing_or_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="existing file"):
        config_module._read_token_file(str(tmp_path / "missing"))
    with pytest.raises(ConfigError, match="absolute"):
        config_module._read_token_file("relative/token")


def test_token_file_and_environment_variable_are_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = _write_token_file(tmp_path / "token", 0o600)
    monkeypatch.setenv("FLEETMON_AUTH_TOKEN", "x" * 40)
    monkeypatch.setenv("FLEETMON_AUTH_TOKEN_FILE", str(token_file))
    with pytest.raises(ConfigError, match="only one"):
        load_config(tmp_path / "missing-config.toml")


def test_config_error_never_contains_the_token_value(tmp_path: Path) -> None:
    token = "s" * 48
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    token_file.chmod(0o644)
    with pytest.raises(ConfigError) as excinfo:
        config_module._read_token_file(str(token_file))
    assert token not in str(excinfo.value)


def test_load_config_reads_token_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleetctl = tmp_path / "fleetctl"
    fleetctl.write_text("#!/bin/sh\nexit 0\n")
    fleetctl.chmod(0o700)
    token_file = tmp_path / "token"
    token_file.write_text("y" * 40 + "\n")
    token_file.chmod(0o600)
    path = tmp_path / "config.toml"
    path.write_text(f'[hub]\nfleetctl_path = "{fleetctl}"\n')
    monkeypatch.setenv("FLEETMON_AUTH_TOKEN_FILE", str(token_file))
    config = load_config(path)
    assert config.auth_token == "y" * 40


def test_mesh_vpn_bind_needs_no_token() -> None:
    defaults = HubConfig.defaults()
    tailscale = HubConfig(**{**defaults.__dict__, "bind_host": "100.103.185.102"})
    tailscale.validate()
    lan = HubConfig(**{**defaults.__dict__, "bind_host": "10.0.0.5"})
    with pytest.raises(ConfigError, match="FLEETMON_AUTH_TOKEN"):
        lan.validate()
