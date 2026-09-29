from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fleetq.cli import cmd_init, open_store
from fleetq.config import DaemonConfig
from fleetq.db.store import check_state_volume, validate_state_dir_path


@pytest.mark.parametrize("unsafe", ["/", "home", "home_parent", "traversal"])
def test_state_paths_must_be_dedicated(monkeypatch, tmp_path, unsafe):
    home = tmp_path / "home" / "user"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    if unsafe == "/":
        candidate = Path("/")
    elif unsafe == "home":
        candidate = home
    elif unsafe == "home_parent":
        candidate = home.parent
    else:
        candidate = tmp_path / "valid" / ".." / "escape"
    with pytest.raises(RuntimeError):
        validate_state_dir_path(candidate)


def test_state_path_rejects_symlinked_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        validate_state_dir_path(alias / "nested" / "state")


def test_valid_nested_state_path_is_accepted(tmp_path):
    path = tmp_path / "nested" / "state"
    assert validate_state_dir_path(path) == path


@pytest.mark.parametrize("candidate", ["/etc/fleetq", "/usr/local/fleetq", "/var/lib/fleetq", "/run/fleetq"])
def test_state_path_rejects_protected_system_subtrees(candidate):
    with pytest.raises(RuntimeError, match="protected system"):
        validate_state_dir_path(Path(candidate))


def test_state_path_rejects_home_config_but_allows_xdg_state(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for subpath in (".ssh/fleetq", ".gnupg/fleetq", ".config/fleetq", ".cache/fleetq",
                    ".local/share/fleetq"):
        with pytest.raises(RuntimeError, match="HOME configuration"):
            validate_state_dir_path(home / subpath)
    valid = home / ".local" / "state" / "fleetq"
    assert validate_state_dir_path(valid) == valid


def test_init_rejects_home_before_creating_anything(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    cfg = SimpleNamespace(state_dir=home)
    with pytest.raises(SystemExit, match="dedicated"):
        cmd_init(cfg, SimpleNamespace(volume_id="unused"))
    assert list(home.iterdir()) == []


def test_open_store_rejects_unsafe_state_before_creating_bundle(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    cfg = DaemonConfig(state_dir=home, fleetctl=tmp_path / "fleetctl", dev_mode=True)
    with pytest.raises(SystemExit, match="dedicated"):
        open_store(cfg)
    assert not cfg.bundle_dir.exists()


def test_volume_guard_rejects_root_and_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for path in (Path("/"), home, home.parent):
        with pytest.raises(RuntimeError, match="dedicated"):
            check_state_volume(path, None, None)
