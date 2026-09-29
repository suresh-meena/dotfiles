"""Focused P3 edge cases for shared GPU visibility and bounded local probes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import hashlib
import json

from fleetq.engine.idle import GpuSample, IdleHistory
from fleetq.shim import fq_node


class Clock:
    t = 10_000.0

    def __call__(self):
        return self.t


def test_small_allocation_below_approved_baseline_still_needs_complete_visibility():
    clock = Clock()
    history = IdleHistory(policies={"ws": {"baselines": {"GPU-display": 400}}}, clock=clock)
    history.record_feed_success()
    # A few MiB could be an allocation hidden by a PID namespace. The measured
    # display allowance cannot turn incomplete process enumeration into idle.
    for i, age in enumerate((130, 90, 50, 5)):
        history.add("ws", "GPU-display", GpuSample(
            f"sample-{i}", clock.t - age, "boot", True, False, None, 8.0, 0.0))
    assert history.verdict("ws", "GPU-display") == (False, "process_visibility_incomplete")


def test_gate_checks_all_fresh_snapshots_and_refuses_post_launch_contention(monkeypatch):
    samples = iter([
        {"ok": True, "gpus": {"GPU-a": {"pids": [], "mem_used_mib": 0, "util": 0,
                                            "compute_mode": "Default", "mig": "Disabled"}}},
        {"ok": True, "gpus": {"GPU-a": {"pids": [902], "mem_used_mib": 8, "util": 0,
                                            "compute_mode": "Default", "mig": "Disabled"}}},
    ])
    monkeypatch.setattr(fq_node, "GATE_SAMPLES", 3)
    monkeypatch.setattr(fq_node, "nvidia_snapshot", lambda: next(samples))
    monkeypatch.setattr(fq_node.time, "sleep", lambda _: None)
    assert fq_node.gate_gpus(["GPU-a"], {}) == (False, ["GPU-a"], "gpu_busy")


def test_probe_timeout_is_bounded_and_kills_helper_process_group(tmp_path):
    # Exercise the same timeout primitive used by node probes, with a child
    # that records its pid before sleeping. No real device or host is involved.
    pidfile = str(tmp_path / "probe-child.pid")
    try:
        rc, _, _ = fq_node.run_bounded(
            [sys.executable, "-c", "import os,time; open(%r,'w').write(str(os.getpid())); time.sleep(20)" % pidfile],
            timeout=0.15,
        )
        assert rc is None
        deadline = time.monotonic() + 1
        while not os.path.exists(pidfile) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert os.path.exists(pidfile)
        child = int(open(pidfile).read())
        # SIGKILL may leave a short-lived zombie until init reaps it. A second
        # signal must show the child is no longer executing.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                stat = open(f"/proc/{child}/stat").read().split()
            except FileNotFoundError:
                return
            if stat[2] == "Z":
                return
            time.sleep(0.01)
        raise AssertionError("timed-out probe helper remained executing")
    finally:
        try:
            os.unlink(pidfile)
        except FileNotFoundError:
            pass


def test_write_probe_persists_path_quarantine_and_node_wide_cap(tmp_path, monkeypatch):
    runtime = tmp_path / "protected-runtime"
    runtime.mkdir(mode=0o700)
    launched = []

    class StuckHelper:
        pid = os.getpid()  # stable, currently-live PID to model an unkillable helper

        def __init__(self, argv, **kwargs):
            launched.append(argv)

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("fake-probe", timeout)

    monkeypatch.setattr(fq_node.subprocess, "Popen", StuckHelper)
    monkeypatch.setattr(fq_node, "_proc_start_ticks", lambda pid: 12345)
    monkeypatch.setattr(fq_node, "_proc_identity", lambda pid: ("R", 12345))
    monkeypatch.setattr(fq_node.os, "killpg", lambda *_: None)

    paths = [tmp_path / f"mount-{i}" for i in range(fq_node.MAX_OUTSTANDING_PROBES + 1)]
    for path in paths[:fq_node.MAX_OUTSTANDING_PROBES]:
        assert fq_node._bounded_write_test(path, runtime_dir=runtime) == "hung"
    # Same path is quarantined, and a fifth outstanding child is rejected.
    assert fq_node._bounded_write_test(paths[0], runtime_dir=runtime) == "quarantined"
    assert fq_node._bounded_write_test(paths[-1], runtime_dir=runtime) == "quarantined"
    assert len(launched) == fq_node.MAX_OUTSTANDING_PROBES
    assert len(list((runtime / "fleetq-probes").glob("*.json"))) == fq_node.MAX_OUTSTANDING_PROBES
    assert not any((path / "probes").exists() for path in paths)


def test_probe_registry_drops_pid_reuse_record_before_retry(tmp_path, monkeypatch):
    runtime = tmp_path / "protected-runtime"
    (runtime / "fleetq-probes").mkdir(parents=True)
    target = tmp_path / "mount"
    key = hashlib.sha256(os.fsencode(os.path.normpath(str(target)))).hexdigest()
    record = runtime / "fleetq-probes" / f"{key}.json"
    record.write_text(json.dumps({"pid": os.getpid(), "boot_id": fq_node.boot_id(),
                                  "start_ticks": 1, "path": str(target)}))

    class SuccessfulHelper:
        pid = os.getpid()
        returncode = 0

        def __init__(self, argv, **kwargs):
            pass

        def communicate(self, timeout):
            return "", ""

    monkeypatch.setattr(fq_node.subprocess, "Popen", SuccessfulHelper)
    monkeypatch.setattr(fq_node, "_proc_start_ticks", lambda pid: 12345)
    assert fq_node._bounded_write_test(target, runtime_dir=runtime) == "ok"
    assert not record.exists()


def test_failed_identity_persistence_kills_child_and_keeps_quarantine_marker(tmp_path, monkeypatch):
    runtime = tmp_path / "protected-runtime"
    runtime.mkdir(mode=0o700)
    target = tmp_path / "suspect-mount"
    killed = []

    class SpawnedHelper:
        pid = os.getpid()

        def __init__(self, argv, **kwargs):
            pass

    real_write = fq_node.write_atomic
    writes = 0

    def fail_identity_write(path, data):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated runtime ledger failure")
        return real_write(path, data)

    monkeypatch.setattr(fq_node.subprocess, "Popen", SpawnedHelper)
    monkeypatch.setattr(fq_node, "_proc_start_ticks", lambda pid: 12345)
    monkeypatch.setattr(fq_node, "write_atomic", fail_identity_write)
    monkeypatch.setattr(fq_node.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    result = fq_node._bounded_write_test(target, runtime_dir=runtime)
    assert result == "failed: probe identity could not be persisted"
    assert killed == [(os.getpid(), fq_node.signal.SIGKILL)]
    marker = next((runtime / "fleetq-probes").glob("*.json"))
    assert json.loads(marker.read_text())["state"] == "starting"
    assert not target.exists()


def test_zombie_probe_is_not_counted_as_executing(monkeypatch):
    monkeypatch.setattr(fq_node, "_proc_identity", lambda pid: ("Z", 12345))
    assert not fq_node._probe_identity_live(
        {"pid": os.getpid(), "boot_id": fq_node.boot_id(), "start_ticks": 12345})


def test_missing_runtime_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("FQ_NODE_RUNTIME_DIR", str(tmp_path / "missing-runtime"))
    try:
        fq_node._probe_runtime_dir()
    except fq_node.ShimError as exc:
        assert exc.code == "no_probe_runtime"
    else:
        raise AssertionError("missing protected runtime unexpectedly accepted")
    assert not (tmp_path / "suspect-mount").exists()


def test_symlink_runtime_fails_closed(tmp_path, monkeypatch):
    runtime = tmp_path / "private-runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    link = tmp_path / "runtime-link"
    link.symlink_to(runtime, target_is_directory=True)
    monkeypatch.setenv("FQ_NODE_RUNTIME_DIR", str(link))
    try:
        fq_node._probe_runtime_dir()
    except fq_node.ShimError as exc:
        assert exc.code == "bad_probe_runtime"
    else:
        raise AssertionError("symlink probe runtime unexpectedly accepted")
