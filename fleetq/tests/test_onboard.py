"""Workstation onboarding (§10, §11): one explicitly authorized node at a time."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fleetq import onboard
from fleetq.cli import open_store
from fleetq.config import DaemonConfig, NodeConfig
from fleetq.transport.fleetctl import Fleetctl

from test_e2e_bare import FAKES, ROOT, SHIM, Fleet


@pytest.fixture(scope="module", autouse=True)
def built():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py")], check=True, capture_output=True)


@pytest.fixture()
def site(tmp_path, monkeypatch):
    fleet = Fleet(tmp_path)
    monkeypatch.setenv("FAKE_FLEET_ROOT", str(fleet.fleet_root))
    state = tmp_path / "state"
    state.mkdir()
    (state / ".fleetq-volume").write_text("test\n")

    def make(mode="exclusive", extra_nodes=()):
        node = NodeConfig(id="ws1", backend="bare", enabled=True, mode=mode, control_root=str(fleet.control_root),
                          capacity={"ram_budget_mb": 32000, "cpus": 16, "scratch_budget_mb": 50000, "job_slots": 4})
        cfg = DaemonConfig(state_dir=state, fleetctl=FAKES / "fleetctl", dev_mode=True, nodes=[node, *extra_nodes])
        return cfg, open_store(cfg)
    return fleet, make


def run(coro):
    return asyncio.run(coro)


def transport():
    return Fleetctl(FAKES / "fleetctl")


def gpus(store):
    return store.run_sync(lambda c: [dict(r) for r in c.execute(
        "SELECT uuid, model, vram_total, idx, drained, drain_reason FROM node_gpus WHERE node_id='ws1' ORDER BY uuid")])


def test_install_activates_an_immutable_release_enrolls_and_probes(site):
    fleet, make = site
    cfg, store = make()
    report = run(onboard.install(cfg, store, transport(), "ws1"))
    root = fleet.control_root
    link = root / "bin" / "fq-node"
    assert link.is_symlink() and os.readlink(link) == report["active"]
    release = (link.parent / os.readlink(link)).resolve()
    assert release.parent.parent == root / "releases" and release.read_bytes() == SHIM.read_bytes()
    enrollment = json.loads((root / "enrollment.json").read_text())
    assert enrollment["node_id"] == "ws1" and enrollment["fleet_id"] == report["enrollment"]["fleet_id"]
    assert report["probe"]["fit"] and report["probe"]["blockers"] == []
    assert [(g["uuid"], g["model"], g["vram_total"], g["idx"]) for g in gpus(store)] == [
        ("GPU-aaaa", "RTX 4090", 24564, 0), ("GPU-bbbb", "RTX 4090", 24564, 1)]
    again = run(onboard.install(cfg, store, transport(), "ws1"))
    assert again["enrollment"]["already"] is True
    store.close()


def test_an_upgrade_keeps_the_old_release_for_running_units(site, tmp_path):
    fleet, make = site
    cfg, store = make()
    first = run(onboard.install(cfg, store, transport(), "ws1"))
    newer = tmp_path / "fq-node-next"
    newer.write_bytes(SHIM.read_bytes() + b"\n# next\n")
    second = run(onboard.install(cfg, store, transport(), "ws1", shim=newer))
    assert first["active"] != second["active"]
    releases = sorted(p.name for p in (fleet.control_root / "releases").iterdir())
    assert len(releases) == 2, "the previous release stays until nothing references it"
    store.close()


def test_upgrade_accepts_legacy_private_pycache(site):
    fleet, make = site
    cfg, store = make()
    run(onboard.install(cfg, store, transport(), "ws1"))
    pycache = fleet.control_root / "pycache"
    pycache.mkdir(mode=0o700)
    (pycache / "old.pyc").write_bytes(b"legacy")
    report = run(onboard.install(cfg, store, transport(), "ws1"))
    assert report["enrollment"]["already"] is True
    assert (pycache / "old.pyc").read_bytes() == b"legacy"
    store.close()


def test_activation_refuses_a_file_that_does_not_match_its_hash(site):
    fleet, make = site
    cfg, store = make()
    rel = fleet.control_root / "releases" / "deadbeefdeadbeef"
    rel.mkdir(parents=True)
    binary = rel / "fq-node"
    binary.write_text("tampered")
    binary.chmod(0o755)
    res = run(transport().exec("ws1", ["sh", "-c", onboard.ACTIVATE_SCRIPT, "fleetq", str(fleet.control_root),
                                       "deadbeefdeadbeef", "0" * 64], timeout=30, mutation=True))
    assert res.payload()["error"] == "hash_mismatch"
    assert not (fleet.control_root / "bin" / "fq-node").exists()
    store.close()


@pytest.mark.parametrize("root", ["/", "/tmp/fq/../etc/fq", "/tmp/./fq"])
def test_install_rejects_dangerous_root_syntax_before_contact(site, root):
    fleet, make = site
    cfg, store = make()
    cfg.nodes[0].control_root = root
    with pytest.raises(onboard.OnboardError) as exc:
        run(onboard.install(cfg, store, transport(), "ws1"))
    assert exc.value.code == "no_control_root"
    assert fleet.calls() == []
    store.close()


def test_install_checks_remote_home_before_pushing(site):
    fleet, make = site
    cfg, store = make()
    # The remote HOME is the otherwise valid dedicated root.
    env_path = fleet.sandbox / "env.json"
    env = json.loads(env_path.read_text())
    env["HOME"] = str(fleet.control_root)
    env_path.write_text(json.dumps(env))
    with pytest.raises(onboard.OnboardError) as exc:
        run(onboard.install(cfg, store, transport(), "ws1"))
    assert exc.value.code == "unsafe_control_root"
    calls = fleet.calls()
    assert len(calls) == 1 and calls[0]["argv"][0] == "exec", "only the read-only precheck may run"
    assert not any(fleet.control_root.iterdir())
    store.close()


@pytest.mark.parametrize("root", ["/etc/fleetq", "/usr/local/lib/fleetq", "/var/lib/fleetq",
                                  "/opt/fleetq", "/run/fleetq"])
def test_install_rejects_protected_system_roots_before_sync(site, root):
    fleet, make = site
    cfg, store = make()
    cfg.nodes[0].control_root = root

    with pytest.raises(onboard.OnboardError) as exc:
        run(onboard.install(cfg, store, transport(), "ws1"))

    assert exc.value.code == "unsafe_control_root"
    calls = fleet.calls()
    assert len(calls) == 1 and calls[0]["argv"][0] == "exec"
    assert not any(call["argv"][0] == "sync" for call in calls)
    store.close()


def test_activation_refuses_protected_system_root_before_mutation(site):
    fleet, make = site
    cfg, store = make()
    protected = Path(f"/usr/local/lib/fleetq-test-{os.getpid()}")
    res = run(transport().exec("ws1", ["sh", "-c", onboard.ACTIVATE_SCRIPT, "fleetq",
                                       str(protected), "deadbeef", "0" * 64],
                               timeout=30, mutation=True))
    assert res.payload()["error"] == "unsafe_root"
    assert not (protected / "bin").exists()
    store.close()


def test_precheck_leaves_nonempty_home_subdirectory_untouched(site, tmp_path):
    fleet, make = site
    cfg, store = make()
    home = tmp_path / "remote-home"
    protected = home / ".ssh"
    protected.mkdir(parents=True)
    sentinel = protected / "authorized_keys"
    sentinel.write_text("keep me\n")
    env_path = fleet.sandbox / "env.json"
    env = json.loads(env_path.read_text())
    env["HOME"] = str(home)
    env_path.write_text(json.dumps(env))
    cfg.nodes[0].control_root = str(protected)

    with pytest.raises(onboard.OnboardError) as exc:
        run(onboard.install(cfg, store, transport(), "ws1"))

    assert exc.value.code == "unsafe_control_root"
    calls = fleet.calls()
    assert len(calls) == 1 and calls[0]["argv"][0] == "exec"
    assert sentinel.read_text() == "keep me\n"
    assert sorted(p.name for p in protected.iterdir()) == ["authorized_keys"]
    store.close()


@pytest.mark.parametrize("component", [".ssh", ".gnupg", ".config", ".local", ".cache"])
def test_precheck_rejects_empty_home_config_roots_without_sync(site, tmp_path, component):
    fleet, make = site
    cfg, store = make()
    home = tmp_path / "remote-home"
    protected = home / component
    protected.mkdir(parents=True)
    env_path = fleet.sandbox / "env.json"
    env = json.loads(env_path.read_text())
    env["HOME"] = str(home)
    env_path.write_text(json.dumps(env))
    cfg.nodes[0].control_root = str(protected)

    with pytest.raises(onboard.OnboardError) as exc:
        run(onboard.install(cfg, store, transport(), "ws1"))

    assert exc.value.code == "unsafe_control_root"
    calls = fleet.calls()
    assert len(calls) == 1 and calls[0]["argv"][0] == "exec"
    assert list(protected.iterdir()) == []
    store.close()


def test_install_rejects_home_ancestor_and_symlink_alias_before_push(site, tmp_path):
    fleet, make = site
    cfg, store = make()
    env_path = fleet.sandbox / "env.json"
    env = json.loads(env_path.read_text())
    env["HOME"] = str(fleet.control_root / "user")
    env_path.write_text(json.dumps(env))
    with pytest.raises(onboard.OnboardError) as exc:
        run(onboard.install(cfg, store, transport(), "ws1"))
    assert exc.value.code == "unsafe_control_root"
    assert len(fleet.calls()) == 1 and not any(fleet.control_root.iterdir())

    alias = tmp_path / "root-alias"
    alias.symlink_to(fleet.control_root, target_is_directory=True)
    cfg.nodes[0].control_root = str(alias)
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    env_path.write_text(json.dumps(env))
    with pytest.raises(onboard.OnboardError) as exc:
        run(onboard.install(cfg, store, transport(), "ws1"))
    assert exc.value.code == "unsafe_control_root"
    assert len(fleet.calls()) == 2, "the symlink alias is rejected before rsync"
    assert not any(fleet.control_root.iterdir())
    store.close()


def test_activation_refuses_home_root_before_creating_bin(site):
    fleet, make = site
    cfg, store = make()
    env_path = fleet.sandbox / "env.json"
    env = json.loads(env_path.read_text())
    env["HOME"] = str(fleet.control_root)
    env_path.write_text(json.dumps(env))
    res = run(transport().exec("ws1", ["sh", "-c", onboard.ACTIVATE_SCRIPT, "fleetq",
                                       str(fleet.control_root), "deadbeef", "0" * 64],
                               timeout=30, mutation=True))
    assert res.payload()["error"] == "unsafe_root"
    assert not (fleet.control_root / "bin").exists()
    store.close()


@pytest.mark.parametrize("component", [".ssh", ".gnupg", ".config", ".local", ".cache"])
def test_activation_refuses_empty_home_config_roots(site, tmp_path, component):
    fleet, make = site
    cfg, store = make()
    home = tmp_path / "home"
    protected = home / component
    protected.mkdir(parents=True)
    env_path = fleet.sandbox / "env.json"
    env = json.loads(env_path.read_text())
    env["HOME"] = str(home)
    env_path.write_text(json.dumps(env))

    res = run(transport().exec("ws1", ["sh", "-c", onboard.ACTIVATE_SCRIPT, "fleetq",
                                       str(protected), "deadbeef", "0" * 64],
                               timeout=30, mutation=True))
    assert res.payload()["error"] == "unsafe_root"
    assert list(protected.iterdir()) == []
    store.close()


def test_activation_refuses_unrelated_root_without_changing_it(site, tmp_path):
    fleet, make = site
    cfg, store = make()
    home = tmp_path / "home"
    protected = home / ".ssh"
    rel = protected / "releases" / "0123456789abcdef"
    rel.mkdir(parents=True)
    candidate = rel / "fq-node"
    candidate.write_bytes(SHIM.read_bytes())
    candidate.chmod(0o755)
    sentinel = protected / "authorized_keys"
    sentinel.write_text("keep me\n")
    env_path = fleet.sandbox / "env.json"
    env = json.loads(env_path.read_text())
    env["HOME"] = str(home)
    env_path.write_text(json.dumps(env))

    res = run(transport().exec("ws1", ["sh", "-c", onboard.ACTIVATE_SCRIPT, "fleetq",
                                       str(protected), "0123456789abcdef",
                                       hashlib.sha256(candidate.read_bytes()).hexdigest()],
                               timeout=30, mutation=True))
    assert res.payload()["error"] == "unsafe_root"
    assert sentinel.read_text() == "keep me\n"
    assert candidate.read_bytes() == SHIM.read_bytes()
    assert not (protected / "bin").exists()
    store.close()


def test_no_linger_blocks_the_node_and_placement_says_why(site):
    fleet, make = site
    env = {**fleet.node_env, "FAKE_LINGER": "no"}
    (fleet.sandbox / "env.json").write_text(json.dumps(env))
    cfg, store = make()
    report = run(onboard.install(cfg, store, transport(), "ws1"))
    assert not report["probe"]["fit"]
    assert any("linger" in b for b in report["probe"]["blockers"])
    probe = store.run_sync(lambda c: json.loads(c.execute(
        "SELECT last_probe_json FROM nodes WHERE id='ws1'").fetchone()[0]))
    assert probe["verdict"]["fit"] is False
    store.close()


def test_memory_enforcement_is_required_on_all_workstations(site, tmp_path):
    fleet, make = site
    uid = os.getuid()
    controllers = tmp_path / "cg" / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service" / "cgroup.controllers"
    controllers.write_text("cpu pids\n")
    cfg, store = make(mode="exclusive")
    report = run(onboard.install(cfg, store, transport(), "ws1"))
    assert not report["probe"]["fit"]
    assert any("MemoryMax" in b for b in report["probe"]["blockers"])
    store.close()
    cfg, store = make(mode="shared")
    verdict = run(onboard.probe(cfg, store, transport(), "ws1"))
    assert not verdict["fit"] and any("MemoryMax" in b for b in verdict["blockers"])
    store.close()


def test_probe_tracks_gpu_inventory_changes_without_deleting_rows(site, tmp_path):
    fleet, make = site
    cfg, store = make()
    run(onboard.install(cfg, store, transport(), "ws1"))
    store.run_sync(lambda c: c.execute(
        "UPDATE node_gpus SET drained=1, drain_reason='operator: fan noise' WHERE uuid='GPU-aaaa'"))
    (tmp_path / "nvsmi.json").write_text(json.dumps({"gpus": [
        {"uuid": "GPU-aaaa", "index": 0, "name": "RTX 4090", "total": 24564},
        {"uuid": "GPU-cccc", "index": 1, "name": "A100", "total": 81920, "mig": "Enabled"}], "apps": []}))
    run(onboard.probe(cfg, store, transport(), "ws1"))
    rows = {g["uuid"]: g for g in gpus(store)}
    assert rows["GPU-aaaa"]["drain_reason"] == "operator: fan noise", "a probe never clears an operator drain"
    assert rows["GPU-bbbb"]["drained"] == 1 and rows["GPU-bbbb"]["drain_reason"] == "missing_from_probe"
    assert rows["GPU-cccc"]["drained"] == 1 and rows["GPU-cccc"]["drain_reason"] == "mig_enabled"
    # It comes back: the probe's own drain clears.
    (tmp_path / "nvsmi.json").write_text(json.dumps({"gpus": [
        {"uuid": "GPU-bbbb", "index": 1, "name": "RTX 4090", "total": 24564}], "apps": []}))
    run(onboard.probe(cfg, store, transport(), "ws1"))
    assert {g["uuid"]: g["drained"] for g in gpus(store)}["GPU-bbbb"] == 0
    store.close()


def test_an_unreadable_gpu_inventory_blocks_but_keeps_the_last_one(site, tmp_path):
    fleet, make = site
    cfg, store = make()
    run(onboard.install(cfg, store, transport(), "ws1"))
    (tmp_path / "nvsmi.json").write_text(json.dumps({"gpus": [], "fail": True}))
    verdict = run(onboard.probe(cfg, store, transport(), "ws1"))
    assert not verdict["fit"] and any("GPU inventory unavailable" in b for b in verdict["blockers"])
    assert [g["drained"] for g in gpus(store)] == [0, 0], "unknown is not 'no GPUs'"
    store.close()


def test_clusters_are_never_installed_on(site):
    _, make = site
    cfg, store = make(extra_nodes=[NodeConfig(id="kiac", backend="slurm", control_root="/home/x/fq")])
    with pytest.raises(onboard.OnboardError) as exc:
        run(onboard.install(cfg, store, transport(), "kiac"))
    assert exc.value.code == "not_a_workstation"
    store.close()


def test_the_cli_needs_the_literal_target_matching_flag(site, tmp_path):
    fleet, make = site
    cfg, store = make()
    store.close()
    conf = tmp_path / "fleetqd.toml"
    conf.write_text(f'[daemon]\nstate_dir = "{cfg.state_dir}"\nfleetctl = "{FAKES / "fleetctl"}"\ndev_mode = true\n\n'
                    f'[[node]]\nid = "ws1"\nbackend = "bare"\nmode = "exclusive"\ncontrol_root = "{fleet.control_root}"\n')

    def fleetqd(*args):
        return subprocess.run([sys.executable, "-m", "fleetq.cli", "--config", str(conf), *args],
                              capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    for bad in ([], ["--i-authorize-target-ws2"], ["--i-authorize-target-ws1", "--i-authorize-target-ws1"]):
        res = fleetqd("node", "install", "ws1", *bad)
        assert res.returncode == 2 and "--i-authorize-target-ws1" in res.stderr
    assert fleet.calls() == [], "a refused install contacts nothing"
    assert fleetqd("node", "show", "ws1").returncode == 3
    ok = fleetqd("node", "install", "ws1", "--i-authorize-target-ws1")
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["probe"]["fit"] is True
    shown = fleetqd("node", "show", "ws1")
    assert shown.returncode == 0 and len(json.loads(shown.stdout)["gpus"]) == 2
