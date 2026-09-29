"""The built fq-node artifact against fake systemd/nvidia-smi, with real payloads (§2.3–2.8)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "build" / "fq-node"
FAKES = ROOT / "tests" / "fakes" / "bin"
FLEET = "fleet_test"

GPU_IDLE = {"uuid": "GPU-a", "index": 0, "total": 24564, "used": 0, "util": 0}
GPU_B = {"uuid": "GPU-b", "index": 1, "total": 24564, "used": 0, "util": 0}


@pytest.fixture(scope="session", autouse=True)
def built():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py")], check=True, capture_output=True)


class Node:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "fqroot"
        self.sysd = tmp / "systemd"
        self.cg = tmp / "cgroup"
        self.run_dir = tmp / "run"
        for d in (self.sysd, self.cg, self.run_dir):
            d.mkdir()
        self.run_dir.chmod(0o700)
        self.nvsmi = tmp / "nvsmi.json"
        self.set_gpus([GPU_IDLE, GPU_B])
        self.meminfo = tmp / "meminfo"
        self.set_mem(64000)
        self.boot = "boot-1"

    def set_gpus(self, gpus, apps=(), **flags):
        self.nvsmi.write_text(json.dumps({"gpus": list(gpus), "apps": list(apps), **flags}))

    def set_mem(self, mib: int) -> None:
        self.meminfo.write_text(f"MemTotal: 131072000 kB\nMemAvailable: {mib * 1024} kB\n")

    def env(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{FAKES}:{os.environ['PATH']}", "FQ_NODE_PATH": f"{FAKES}:/usr/bin:/bin",
                "FAKE_SYSTEMD_DIR": str(self.sysd),
                "FQ_NODE_CGROUP_ROOT": str(self.cg), "FQ_NODE_RUNTIME_DIR": str(self.run_dir),
                "FAKE_NVSMI_FILE": str(self.nvsmi), "FQ_NODE_MEMINFO": str(self.meminfo),
                "FQ_NODE_BOOT_ID": self.boot, "FQ_NODE_GATE_INTERVAL": "0.01", "FQ_NODE_NVSMI_TIMEOUT": "1"}

    def call(self, *args: str, extra_env: dict | None = None) -> dict:
        proc = subprocess.run([sys.executable, str(SHIM), "--root", str(self.root), *args], capture_output=True,
                              text=True, env={**self.env(), **(extra_env or {})}, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def enroll(self) -> None:
        assert self.call("enroll", "--fleet-id", FLEET, "--node-id", "n1")["ok"]
        assert self.call("fence", "--fleet-id", FLEET, "--epoch", "1")["accepted"]

    def stage(self, attempt: str, *, argv: list[str], gpus=(), mem=1000, time_s=60, epoch=1, **extra) -> None:
        stage_id = f"{attempt}-{epoch}-test"
        inbox = self.root / "inbox" / f".stage-{stage_id}"
        inbox.mkdir(parents=True)
        work = self.tmp / f"work-{attempt}"
        work.mkdir()
        manifest = {"attempt_id": attempt, "fleet_id": FLEET, "job_id": 7, "epoch": epoch, "spec_digest": "sha256:x",
                    "command": {"argv": argv}, "gpus": list(gpus), "in_place": str(work),
                    "resources": {"mem_mb": mem, "cpus": 2, "time_s": time_s}, "kill_grace_s": 2,
                    "mem_headroom_mb": 100, **extra}
        (inbox / "manifest.json").write_text(json.dumps(manifest))
        assert self.call("publish-stage", "--fleet-id", FLEET, "--epoch", str(epoch),
                         "--stage-id", stage_id)["published"]
        assert self.call("prepare", "--fleet-id", FLEET, "--epoch", str(epoch), attempt)["staged"]

    def launch(self, attempt: str, epoch: int = 1) -> dict:
        return self.call("launch", "--fleet-id", FLEET, "--epoch", str(epoch), "--shim-path", str(SHIM), attempt)

    def status(self, attempt: str) -> dict:
        return self.call("status", "--fleet-id", FLEET, attempt)["attempts"][attempt]

    def wait_state(self, attempt: str, wanted: str, timeout: float = 15) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.status(attempt)
            if st["state"] == wanted:
                return st
            time.sleep(0.1)
        raise AssertionError(f"{attempt} never reached {wanted}: {self.status(attempt)}")


@pytest.fixture()
def node(tmp_path):
    n = Node(tmp_path)
    n.enroll()
    return n


def att(i: int) -> str:
    return f"att_{i:024x}"


def test_enroll_and_fence_reject_stale_epoch_and_other_fleet(node):
    assert node.call("fence", "--fleet-id", FLEET, "--epoch", "1")["accepted"] is False
    assert node.call("fence", "--fleet-id", FLEET, "--epoch", "5")["accepted"] is True
    other = node.call("fence", "--fleet-id", "another", "--epoch", "9")
    assert other["ok"] is False and other["error"]["code"] == "fleet_mismatch"


@pytest.mark.parametrize("unsafe", ["home", "parent_traversal", "symlink_alias", "nonempty"])
def test_enroll_rejects_unsafe_control_roots(tmp_path, unsafe):
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    if unsafe == "home":
        root = home
    elif unsafe == "parent_traversal":
        root = tmp_path / "target" / ".." / "target"
    elif unsafe == "symlink_alias":
        root = tmp_path / "alias"
        root.symlink_to(target, target_is_directory=True)
    else:
        root = tmp_path / "nonempty"
        root.mkdir()
        (root / "keep").write_text("preserve")
    proc = subprocess.run([sys.executable, str(SHIM), "--root", str(root), "enroll",
                           "--fleet-id", FLEET, "--node-id", "n1"], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0
    reply = json.loads(proc.stdout)
    assert reply["ok"] is False and reply["error"]["code"] == "unsafe_root"
    if unsafe == "nonempty":
        assert (root / "keep").read_text() == "preserve"


@pytest.mark.parametrize("component", [".ssh", ".gnupg", ".config", ".local", ".cache"])
def test_enroll_refuses_empty_home_config_roots_without_mutation(tmp_path, component):
    home = tmp_path / "home"
    root = home / component
    root.mkdir(parents=True)
    proc = subprocess.run([sys.executable, str(SHIM), "--root", str(root), "enroll",
                           "--fleet-id", FLEET, "--node-id", "n1"], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0
    reply = json.loads(proc.stdout)
    assert reply["ok"] is False and reply["error"]["code"] == "unsafe_root"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("root", ["/etc/fleetq", "/usr/local/lib/fleetq", "/var/lib/fleetq",
                                  "/opt/fleetq", "/run/fleetq"])
def test_enroll_refuses_protected_system_roots_before_mutation(root):
    proc = subprocess.run([sys.executable, str(SHIM), "--root", root, "enroll",
                           "--fleet-id", FLEET, "--node-id", "n1"], capture_output=True, text=True,
                          env={**os.environ, "HOME": "/tmp/fq-test-home"})
    assert proc.returncode == 0
    reply = json.loads(proc.stdout)
    assert reply["ok"] is False and reply["error"]["code"] == "unsafe_root"


def test_require_enrolled_refuses_protected_system_roots_without_reading_enrollment():
    from fleetq.shim.fq_node import ControlRoot, ShimError

    with pytest.raises(ShimError) as exc:
        ControlRoot("/etc/fleetq").require_enrolled()
    assert exc.value.code == "unsafe_root"


@pytest.mark.parametrize("digest", ["sha256:../../important", "sha256:ABC", {"sha256": "x"}])
def test_prepare_rejects_malformed_bundle_digest_before_touching_cache_or_outside(node, digest):
    a = att(90)
    stage_id = f"{a}-1-bad-digest"
    incoming = node.root / "inbox" / f".stage-{stage_id}"
    incoming.mkdir()
    (incoming / "manifest.json").write_text(json.dumps({
        "attempt_id": a, "fleet_id": FLEET, "epoch": 1, "bundle_digest": digest,
    }))
    (incoming / "bundle.tar.gz").write_bytes(b"untrusted bundle")
    outside = node.tmp / "important.tar.gz"
    outside.write_bytes(b"keep")
    assert node.call("publish-stage", "--fleet-id", FLEET, "--epoch", "1",
                     "--stage-id", stage_id)["published"]

    reply = node.call("prepare", "--fleet-id", FLEET, "--epoch", "1", a)
    assert reply["ok"] is False
    assert reply["error"]["code"] == "bad_digest"
    assert outside.read_bytes() == b"keep"
    assert (node.root / "inbox" / a / "bundle.tar.gz").read_bytes() == b"untrusted bundle"
    assert not (node.root / "cache" / "ABC.tar.gz").exists()


def test_collect_clean_rejects_symlink_attempt_directory(node):
    a = att(91)
    outside = node.tmp / "outside"
    outbox = outside / "outbox"
    outbox.mkdir(parents=True)
    marker = outbox / "keep"
    marker.write_text("keep")
    (node.root / "attempts" / a).symlink_to(outside, target_is_directory=True)

    reply = node.call("collect-clean", "--fleet-id", FLEET, "--epoch", "1", a)
    assert reply["ok"] is False
    assert reply["error"]["code"] == "unsafe_attempt_dir"
    assert marker.read_text() == "keep"


def test_launch_runs_payload_once_and_records_completion(node):
    a = att(1)
    node.stage(a, argv=[sys.executable, "-c", "print('hello')"], gpus=["GPU-a"])
    r = node.launch(a)
    assert r["result"] == "started"
    st = node.wait_state(a, "stopped")
    assert st["outcome"] == "COMPLETED" and st["exit_code"] == 0 and st["cgroup_empty"] is True
    adir = node.root / "attempts" / a
    assert (adir / "stdout.log").read_text().strip() == "hello"
    assert json.loads((node.root / "alloc" / "GPU-a.json").read_text())["attempt_id"] == a
    assert node.call("release", "--fleet-id", FLEET, "--epoch", "1", a)["released"] is True
    assert not (node.root / "alloc" / "GPU-a.json").exists()


def test_remote_cache_gc_is_explicit_two_pass_and_respects_attempt_pins(node):
    digest = "a" * 64
    cache = node.root / "cache" / f"{digest}.tar.gz"
    cache.write_bytes(b"cached bundle")
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(cache, (old, old))
    attempt = att(91)
    adir = node.root / "attempts" / attempt
    adir.mkdir()
    (adir / "facts").mkdir()
    (adir / "facts" / "start_requested").write_text(json.dumps({"ts": time.time(), "boot_id": "boot-1"}))
    manifest = {"attempt_id": attempt, "fleet_id": FLEET, "epoch": 1, "spec_digest": "spec-91",
                "bundle_digest": "sha256:" + digest}
    (adir / "manifest.json").write_text(json.dumps(manifest))

    pinned = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1")
    assert pinned["mode"] == "inspect" and pinned["eligible"] == []
    assert cache.exists()

    # Allocation release is not enough: artifact collection has its own durable pin release.
    (adir / "facts" / "released").write_text(json.dumps({"ts": old - 1, "boot_id": "boot-1"}))
    still_pinned = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1")
    assert still_pinned["eligible"] == [] and cache.exists()
    marker = {"ts": old - 1, "boot_id": "boot-1", "attempt_id": attempt, "fleet_id": FLEET,
              "epoch": 1, "bundle_digest": "sha256:" + digest, "spec_digest": "spec-91"}
    (adir / "facts" / "cache.released").write_text(json.dumps(marker))
    inspected = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1")
    assert inspected["eligible"][0]["digest"] == "sha256:" + digest
    assert cache.exists()
    purged = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1", "--purge", "sha256:" + digest)
    assert purged["removed"][0]["digest"] == "sha256:" + digest
    assert not cache.exists()


def test_remote_cache_gc_rejects_wrong_fence_or_fleet(node):
    r = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "2")
    assert r["ok"] is False and r["error"]["code"] == "future_epoch"
    r = node.call("cache-gc", "--fleet-id", "other", "--epoch", "1")
    assert r["ok"] is False and r["error"]["code"] == "fleet_mismatch"


def _old_cache(node, digest):
    path = node.root / "cache" / f"{digest}.tar.gz"
    path.write_bytes(b"cached bundle")
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(path, (old, old))
    return path, old


def test_remote_cache_gc_inbox_manifest_pins_digest(node):
    digest = "b" * 64
    cache, _old = _old_cache(node, digest)
    inbox = node.root / "inbox" / att(92)
    inbox.mkdir()
    (inbox / "manifest.json").write_text(json.dumps({"bundle_digest": "sha256:" + digest}))
    result = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1")
    assert result["eligible"] == [] and result["blocked"] == []
    assert cache.exists()


def test_remote_cache_gc_malformed_attempt_blocks_every_purge(node):
    first, _ = _old_cache(node, "c" * 64)
    second, _ = _old_cache(node, "d" * 64)
    adir = node.root / "attempts" / att(93)
    adir.mkdir()
    (adir / "manifest.json").write_text("{")
    r = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1", "--purge", "sha256:" + "c" * 64)
    assert r["removed"] == []
    assert any(item["reason"] == "attempt_manifest_unreadable" for item in r["blocked"])
    assert first.exists() and second.exists()


def test_remote_cache_gc_release_timestamp_restarts_grace_and_requires_exact_digest(node):
    digest = "e" * 64
    cache, _old = _old_cache(node, digest)
    adir = node.root / "attempts" / att(94)
    (adir / "facts").mkdir(parents=True)
    attempt = att(94)
    manifest = {"attempt_id": attempt, "fleet_id": FLEET, "epoch": 1, "spec_digest": "spec-94",
                "bundle_digest": "sha256:" + digest}
    (adir / "manifest.json").write_text(json.dumps(manifest))
    (adir / "facts" / "released").write_text(json.dumps({"ts": time.time(), "boot_id": "boot-1"}))
    (adir / "facts" / "cache.released").write_text(json.dumps({
        "ts": time.time(), "boot_id": "boot-1", "attempt_id": attempt, "fleet_id": FLEET, "epoch": 1,
        "bundle_digest": "sha256:" + digest, "spec_digest": "spec-94"}))
    result = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1")
    assert result["eligible"] == []
    bad = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1", "--purge", "sha256:" + "E" * 64)
    assert bad["ok"] is False and bad["error"]["code"] == "bad_digest"
    assert cache.exists()


def test_cache_release_requires_exact_fence_and_allocation_release(node):
    digest = "1" * 64
    attempt = att(95)
    adir = node.root / "attempts" / attempt
    (adir / "facts").mkdir(parents=True)
    (adir / "manifest.json").write_text(json.dumps({"attempt_id": attempt, "fleet_id": FLEET, "epoch": 1,
                                                       "spec_digest": "spec-95", "bundle_digest": "sha256:" + digest}))
    no_allocation_release = node.call("cache-release", "--fleet-id", FLEET, "--epoch", "1", attempt)
    assert no_allocation_release["released"] is False
    (adir / "facts" / "released").write_text(json.dumps({"ts": time.time(), "boot_id": "boot-1"}))
    stale = node.call("cache-release", "--fleet-id", FLEET, "--epoch", "2", attempt)
    assert stale["ok"] is False and stale["error"]["code"] == "future_epoch"
    accepted = node.call("cache-release", "--fleet-id", FLEET, "--epoch", "1", attempt)
    assert accepted["released"] is True
    again = node.call("cache-release", "--fleet-id", FLEET, "--epoch", "1", attempt)
    assert again["released"] is True and again["already"] is True
    assert (adir / "facts" / "cache.released").exists()


def test_cache_release_refuses_live_attempt_and_malformed_marker_blocks_gc(node):
    digest = "2" * 64
    attempt = att(96)
    cache, _ = _old_cache(node, digest)
    adir = node.root / "attempts" / attempt
    (adir / "facts").mkdir(parents=True)
    manifest = {"attempt_id": attempt, "fleet_id": FLEET, "epoch": 1, "spec_digest": "spec-96",
                "bundle_digest": "sha256:" + digest}
    (adir / "manifest.json").write_text(json.dumps(manifest))
    (adir / "facts" / "released").write_text(json.dumps({"ts": time.time(), "boot_id": "boot-1"}))
    (adir / "facts" / "start_requested").write_text(json.dumps({"ts": time.time(), "boot_id": "boot-1"}))
    (adir / "facts" / "runner_entered").write_text(json.dumps({"ts": time.time(), "boot_id": "boot-1"}))
    (node.sysd / f"fq-{attempt}.service.json").write_text(json.dumps({"ActiveState": "active"}))
    live = node.call("cache-release", "--fleet-id", FLEET, "--epoch", "1", attempt)
    assert live["released"] is False and not (adir / "facts" / "cache.released").exists()
    (adir / "facts" / "cache.released").write_text("{")
    gc = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1")
    assert gc["eligible"] == [] and any(b["reason"] == "cache_release_marker_invalid" for b in gc["blocked"])
    assert cache.exists()


def test_cache_release_accepts_prior_attempt_after_restart_and_retry_at_newer_fence(node):
    digest = "3" * 64
    attempt = att(97)
    adir = node.root / "attempts" / attempt
    (adir / "facts").mkdir(parents=True)
    (adir / "manifest.json").write_text(json.dumps({
        "attempt_id": attempt, "fleet_id": FLEET, "epoch": 1, "spec_digest": "spec-97",
        "bundle_digest": "sha256:" + digest}))
    (adir / "facts" / "released").write_text(json.dumps({"ts": time.time(), "boot_id": "boot-1"}))

    assert node.call("fence", "--fleet-id", FLEET, "--epoch", "2")["accepted"]
    first = node.call("cache-release", "--fleet-id", FLEET, "--epoch", "2", attempt)
    assert first["released"] is True and first.get("already") is not True
    assert json.loads((adir / "facts" / "cache.released").read_text())["epoch"] == 2

    assert node.call("fence", "--fleet-id", FLEET, "--epoch", "3")["accepted"]
    retry = node.call("cache-release", "--fleet-id", FLEET, "--epoch", "3", attempt)
    assert retry["released"] is True and retry["already"] is True
    gc = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "3")
    assert gc["eligible"] == [] and gc["blocked"] == []


def test_remote_cache_gc_refuses_symlink_cache_leaf_and_cache_root(node):
    digest = "f" * 64
    outside = node.tmp / "outside.tar.gz"
    outside.write_bytes(b"keep")
    leaf = node.root / "cache" / f"{digest}.tar.gz"
    leaf.symlink_to(outside)
    result = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1", "--purge", "sha256:" + digest)
    assert result["removed"] == [] and result["blocked"][0]["reason"] == "cache_entry_not_owned_regular_file"
    assert outside.read_bytes() == b"keep"

    real_cache = node.root / "cache-real"
    (node.root / "cache").rename(real_cache)
    (node.root / "cache").symlink_to(real_cache, target_is_directory=True)
    result = node.call("cache-gc", "--fleet-id", FLEET, "--epoch", "1")
    assert result["ok"] is True and result["eligible"] == []
    assert result["blocked"][0]["reason"] == "cache_not_private_owned_directory"


def test_uuid_pinning_and_no_slurm_env(node):
    a = att(2)
    dump = node.tmp / "env.json"
    node.stage(a, argv=[sys.executable, "-c",
                        f"import json,os; json.dump(dict(os.environ), open({str(dump)!r},'w'))"],
               gpus=["GPU-b"], env={"MYVAR": "1", "SLURM_JOB_ID": "forged"})
    node.launch(a)
    node.wait_state(a, "stopped")
    env = json.loads(dump.read_text())
    assert env["CUDA_VISIBLE_DEVICES"] == "GPU-b" and env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert env["MYVAR"] == "1" and "SLURM_JOB_ID" not in env
    assert env["FQ_ATTEMPT_ID"] == a


def test_replayed_launch_and_replayed_runner_never_reenter(node):
    a = att(3)
    counter = node.tmp / "count"
    node.stage(a, argv=["/bin/sh", "-c", f"echo x >> {counter}; sleep 1"])
    assert node.launch(a)["result"] == "started"
    again = node.launch(a)
    assert again["result"] == "started" and again.get("replay") is True
    # Simulate systemd re-running the unit's runner directly (e.g. a restart).
    subprocess.run([sys.executable, str(SHIM), "--root", str(node.root), "run", a], env=node.env(), timeout=30)
    node.wait_state(a, "stopped")
    assert counter.read_text().count("x") == 1
    assert any((node.root / "attempts" / a / "replay").iterdir())


def test_late_conflicting_finalizer_is_preserved_without_overwriting_winner(node):
    a = att(22)
    node.stage(a, argv=[sys.executable, "-c", "pass"])
    node.launch(a)
    node.wait_state(a, "stopped")

    adir = node.root / "attempts" / a
    winner = json.loads((adir / "service_result.json").read_text())
    assert winner["service_result"] == "success"

    # Model a delayed ExecStopPost from a second systemd invocation reporting
    # contradictory evidence after the winning invocation already finalized.
    proc = subprocess.run([sys.executable, str(SHIM), "--root", str(node.root), "finalize", a],
                          capture_output=True, text=True,
                          env={**node.env(), "INVOCATION_ID": "late-conflict",
                               "SERVICE_RESULT": "oom-kill", "EXIT_CODE": "exited", "EXIT_STATUS": "137"},
                          timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads((adir / "service_result.json").read_text()) == winner
    preserved = list((adir / "replay").glob("finalize-late-conflict.json"))
    assert len(preserved) == 1
    assert json.loads(preserved[0].read_text())["service_result"] == "oom-kill"


def test_nonzero_exit_is_failed_with_code(node):
    a = att(4)
    node.stage(a, argv=["/bin/sh", "-c", "exit 3"])
    node.launch(a)
    st = node.wait_state(a, "stopped")
    assert st["outcome"] == "FAILED" and st["exit_code"] == 3


def test_walltime_is_timeout(node):
    a = att(5)
    node.stage(a, argv=["/bin/sh", "-c", "sleep 30"], time_s=1)
    node.launch(a)
    st = node.wait_state(a, "stopped", timeout=20)
    assert st["outcome"] == "TIMEOUT"


def test_oom_from_systemd_outranks_payload_result(node):
    a = att(6)
    node.stage(a, argv=["/bin/sh", "-c", "exit 0"])
    node.call("launch", "--fleet-id", FLEET, "--epoch", "1", "--shim-path", str(SHIM), a,
              extra_env={"FAKE_SYSTEMD_OOM": "1"})
    st = node.wait_state(a, "stopped")
    assert st["outcome"] == "OUT_OF_MEMORY"


def test_gate_refuses_busy_gpu_with_foreign_process(node):
    node.set_gpus([{**GPU_IDLE, "used": 900, "util": 0}, GPU_B], apps=[{"uuid": "GPU-a", "pid": 999, "mem": 900}])
    a = att(7)
    node.stage(a, argv=["true"], gpus=["GPU-a"])
    r = node.launch(a)
    assert r["result"] == "placement_refused" and r["gpus"] == ["GPU-a"]
    assert not (node.root / "attempts" / a / "facts" / "runner_entered").exists()


def test_gate_refuses_hidden_memory_without_visible_process(node):
    # Memory held with no visible process (another PID namespace): not idle (§2.2).
    node.set_gpus([{**GPU_IDLE, "used": 20000}, GPU_B])
    a = att(8)
    node.stage(a, argv=["true"], gpus=["GPU-a"])
    assert node.launch(a)["result"] == "placement_refused"


def test_measured_baseline_allows_display_memory(node):
    node.set_gpus([{**GPU_IDLE, "used": 350}, GPU_B])
    a = att(9)
    node.stage(a, argv=["true"], gpus=["GPU-a"], gpu_policy={"baselines": {"GPU-a": 400}})
    assert node.launch(a)["result"] == "started"


def test_unknown_gpu_telemetry_is_never_idle(node):
    a = att(10)
    node.stage(a, argv=["true"], gpus=["GPU-a"])
    node.set_gpus([GPU_IDLE], fail=True)
    assert node.launch(a)["reason"] == "nvidia_smi_failed"
    node.set_gpus([GPU_IDLE], hang=True)
    t0 = time.time()
    r = node.launch(a)
    assert r["result"] == "placement_refused" and r["reason"] == "nvidia_smi_hung"
    assert time.time() - t0 < 10     # a wedged driver never hangs the shim


def test_insufficient_physical_memory_refuses(node):
    node.set_mem(500)
    a = att(11)
    node.stage(a, argv=["true"], mem=1000)
    r = node.launch(a)
    assert r["result"] == "placement_refused" and r["reason"] == "insufficient_physical_memory"


def test_gpu_marker_of_live_attempt_blocks_second_attempt(node):
    a, b = att(12), att(13)
    node.stage(a, argv=["/bin/sh", "-c", "sleep 5"], gpus=["GPU-a"])
    node.launch(a)
    node.stage(b, argv=["true"], gpus=["GPU-a"])
    r = node.launch(b)
    assert r["result"] == "placement_refused" and r["reason"] == "gpu_allocated_by_fleetq"
    node.call("cancel", "--fleet-id", FLEET, "--epoch", "1", a)


def test_cancel_before_entry_is_never_started(node):
    a = att(14)
    counter = node.tmp / "ran"
    node.stage(a, argv=["/bin/sh", "-c", f"touch {counter}"])
    r = node.call("cancel", "--fleet-id", FLEET, "--epoch", "1", a)
    assert r["stopped"] is True and r["never_started"] is True
    assert node.launch(a)["result"] == "never_started"
    assert node.status(a)["state"] == "refused"
    assert not counter.exists()


def test_stale_controller_cancel_does_not_create_tombstone(node):
    node.call("fence", "--fleet-id", FLEET, "--epoch", "2")
    a = att(140)
    r = node.call("cancel", "--fleet-id", FLEET, "--epoch", "1", a)
    assert r["reason"] == "stale_epoch" and r["highest_epoch_seen"] == 2
    assert not (node.root / "attempts" / a).exists()


def test_future_controller_cancel_does_not_create_tombstone(node):
    a = att(141)
    r = node.call("cancel", "--fleet-id", FLEET, "--epoch", "3", a)
    assert r["reason"] == "future_epoch" and r["highest_epoch_seen"] == 1
    assert not (node.root / "attempts" / a).exists()


def test_cancel_running_confirms_stop(node):
    a = att(15)
    node.stage(a, argv=["/bin/sh", "-c", "sleep 60"])
    node.launch(a)
    node.wait_state(a, "running")
    r = node.call("cancel", "--fleet-id", FLEET, "--epoch", "1", a)
    assert r["stopped"] is True and r["never_started"] is False
    st = node.wait_state(a, "stopped")
    assert st["outcome"] == "CANCELLED"


def test_stale_epoch_launch_is_refused(node):
    node.call("fence", "--fleet-id", FLEET, "--epoch", "3")
    a = att(16)
    inbox = node.root / "inbox" / a
    inbox.mkdir(parents=True)
    (inbox / "manifest.json").write_text(json.dumps({
        "attempt_id": a, "fleet_id": FLEET, "job_id": 7, "epoch": 2, "command": {"argv": ["true"]},
        "resources": {"mem_mb": 1, "cpus": 1, "time_s": 1}}))
    refused = node.call("prepare", "--fleet-id", FLEET, "--epoch", "2", a)  # a stale manifest is refused
    assert refused["ok"] is False and refused["error"]["code"] == "stale_epoch"
    b = att(17)
    node.stage(b, argv=["true"], epoch=3)
    assert node.launch(b, epoch=2)["reason"] == "stale_epoch"


def test_delayed_old_stage_cannot_replace_new_epoch_inbox(node):
    a = att(55)
    # An old rsync finishes after the new controller's fence. Its private upload
    # path is inert until publication, which must reject the old epoch.
    node.call("fence", "--fleet-id", FLEET, "--epoch", "2")
    stage_id = f"{a}-1-delayed"
    delayed = node.root / "inbox" / f".stage-{stage_id}"
    delayed.mkdir()
    old_manifest = {"attempt_id": a, "fleet_id": FLEET, "epoch": 1}
    (delayed / "manifest.json").write_text(json.dumps(old_manifest))
    current = node.root / "inbox" / a
    current.mkdir()
    (current / "manifest.json").write_text(json.dumps({"attempt_id": a, "fleet_id": FLEET, "epoch": 2}))

    refused = node.call("publish-stage", "--fleet-id", FLEET, "--epoch", "1", "--stage-id", stage_id)
    assert refused["ok"] is False and refused["error"]["code"] == "stale_epoch"
    assert json.loads((current / "manifest.json").read_text())["epoch"] == 2
    assert node.call("prepare", "--fleet-id", FLEET, "--epoch", "1", a)["error"]["code"] == "stale_epoch"


def test_prepare_refuses_orphaned_code_tree_without_staged_fact(node):
    a = att(56)
    inbox = node.root / "inbox" / a
    inbox.mkdir()
    (inbox / "manifest.json").write_text(json.dumps({
        "attempt_id": a, "fleet_id": FLEET, "epoch": 1, "spec_digest": "sha256:new",
        "command": {"argv": ["true"]},
        "resources": {"mem_mb": 1, "cpus": 1, "time_s": 1},
    }))
    orphan = node.root / "attempts" / a / "code"
    orphan.mkdir(parents=True)
    (orphan / "from-old-prepare").write_text("partial")

    refused = node.call("prepare", "--fleet-id", FLEET, "--epoch", "1", a)
    assert refused["ok"] is False and refused["error"]["code"] == "partial_stage"
    assert (orphan / "from-old-prepare").read_text() == "partial"


def test_future_epoch_launch_is_refused(node):
    a = att(171)
    node.stage(a, argv=["true"], epoch=1)
    refused = node.launch(a, epoch=2)
    assert refused["reason"] == "future_epoch" and refused["highest_epoch_seen"] == 1
    assert node.status(a)["state"] == "staged"


def test_stale_release_preserves_gpu_marker(node):
    a = att(172)
    node.stage(a, argv=["true"], gpus=["GPU-a"])
    marker = node.root / "alloc" / "GPU-a.json"
    marker.write_text(json.dumps({"attempt_id": a, "epoch": 1}))
    node.call("fence", "--fleet-id", FLEET, "--epoch", "2")
    result = node.call("release", "--fleet-id", FLEET, "--epoch", "1", a)
    assert result["released"] is False and result["reason"] == "stale_epoch"
    assert json.loads(marker.read_text())["attempt_id"] == a


def test_future_release_preserves_gpu_marker(node):
    a = att(173)
    node.stage(a, argv=["true"], gpus=["GPU-a"])
    marker = node.root / "alloc" / "GPU-a.json"
    marker.write_text(json.dumps({"attempt_id": a, "epoch": 1}))
    result = node.call("release", "--fleet-id", FLEET, "--epoch", "2", a)
    assert result["released"] is False and result["reason"] == "future_epoch"
    assert json.loads(marker.read_text())["attempt_id"] == a


def test_reboot_changes_boot_id_to_node_fail(node):
    a = att(18)
    node.stage(a, argv=["/bin/sh", "-c", "sleep 60"])
    node.launch(a)
    node.wait_state(a, "running")
    # The machine "reboots": new boot id, and the old unit state is gone.
    node.call("cancel", "--fleet-id", FLEET, "--epoch", "1", a)
    for f in (node.root / "attempts" / a).glob("result.json"):
        f.unlink()
    for f in (node.root / "attempts" / a).glob("service_result.json"):
        f.unlink()
    (node.root / "attempts" / a / "facts" / "cancel").unlink()
    node.boot = "boot-2"
    st = node.status(a)
    assert st["state"] == "stopped" and st["outcome"] == "NODE_FAIL" and st["boot_changed"] is True


def test_systemd_refusal_is_never_started(node):
    a = att(19)
    node.stage(a, argv=["true"])
    r = node.call("launch", "--fleet-id", FLEET, "--epoch", "1", "--shim-path", str(SHIM), a,
                  extra_env={"FAKE_SYSTEMD_REFUSE": "1"})
    assert r["result"] == "never_started" and r["reason"] == "systemd_run_failed"


def test_setup_preserves_argv_boundaries(node):
    a = att(20)
    out = node.tmp / "args.json"
    node.stage(a, argv=[sys.executable, "-c", f"import json,sys; json.dump(sys.argv[1:], open({str(out)!r},'w'))",
                        "a b", "$(whoami)", "c;d"], setup="export FOO=1")
    node.launch(a)
    node.wait_state(a, "stopped")
    assert json.loads(out.read_text()) == ["a b", "$(whoami)", "c;d"]


def test_logs_are_offset_readable(node):
    a = att(21)
    node.stage(a, argv=["/bin/sh", "-c", "printf 'line1\\nline2\\n'"])
    node.launch(a)
    node.wait_state(a, "stopped")
    import base64
    first = node.call("logs", "--fleet-id", FLEET, a, "--max-bytes", "6")
    assert base64.b64decode(first["data_b64"]) == b"line1\n" and first["size"] == 12
    rest = node.call("logs", "--fleet-id", FLEET, a, "--offset", "6")
    assert base64.b64decode(rest["data_b64"]) == b"line2\n"


def test_probe_reports_capabilities(node):
    facts = node.call("probe")
    assert facts["linger"] is True and facts["user_runtime"] == "ok"
    assert facts["gpus"]["ok"] is True and "GPU-a" in facts["gpus"]["gpus"]
    assert facts["control_root_writable"] == "ok"
    assert (node.run_dir / "fleetq-probes").is_dir()
    assert not list((node.root / "probes").glob("*.json")), "probe ledger stays off the path being checked"


def test_probe_write_test_fails_closed_without_protected_runtime(node):
    facts = node.call("probe", extra_env={"FQ_NODE_RUNTIME_DIR": str(node.tmp / "missing-runtime")})
    assert facts["control_root_writable"] == "no_probe_runtime"
    assert not list(node.root.glob(".probe-*"))
