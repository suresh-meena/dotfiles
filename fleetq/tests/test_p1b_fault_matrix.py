"""Workstation dispatch crash edges using the fake systemd/shim harness.

No SSH, fleetctl, or real workstation is contacted here; the node shim and
payloads run in a temporary directory on the test host.
"""

from __future__ import annotations

import json
import subprocess
import sys

from test_fq_node import FLEET, SHIM, att, node


def test_unpublished_stage_is_inert_and_published_stage_is_preparable(node):
    a = att(901)
    stage_id = f"{a}-1-private"
    private = node.root / "inbox" / f".stage-{stage_id}"
    private.mkdir(parents=True)
    (private / "manifest.json").write_text(json.dumps({
        "attempt_id": a, "fleet_id": FLEET, "job_id": 7, "epoch": 1,
        "command": {"argv": ["true"]}, "resources": {"mem_mb": 1, "cpus": 1, "time_s": 10},
    }))
    assert node.call("prepare", "--fleet-id", FLEET, "--epoch", "1", a)["error"]["code"] == "inbox_missing"
    assert node.call("publish-stage", "--fleet-id", FLEET, "--epoch", "1", "--stage-id", stage_id)["published"]
    assert node.call("prepare", "--fleet-id", FLEET, "--epoch", "1", a)["staged"]


def test_duplicate_unit_recreation_after_completion_does_not_reenter_payload(node):
    a = att(902)
    counter = node.tmp / "entries"
    node.stage(a, argv=["/bin/sh", "-c", f"echo entered >> {counter}"])
    assert node.launch(a)["result"] == "started"
    node.wait_state(a, "stopped")
    # Remove only fake manager's dead-unit record so systemd-run accepts a
    # recreated unit name, as can happen after unit collection/recovery.
    (node.sysd / f"fq-{a}.service.json").unlink(missing_ok=True)
    # The runner's durable claim is the authority even if the unit name is free.
    replay = subprocess.run([sys.executable, str(SHIM), "--root", str(node.root), "run", a],
                            env={**node.env(), "INVOCATION_ID": "recreated-unit"},
                            capture_output=True, text=True, timeout=30)
    assert replay.returncode == 0
    assert counter.read_text().splitlines() == ["entered"]
    assert node.call("launch", "--fleet-id", FLEET, "--epoch", "1", "--shim-path", str(SHIM), a)["replay"]


def test_runner_claim_without_payload_start_is_at_most_once(node):
    a = att(903)
    counter = node.tmp / "entries-boundary"
    node.stage(a, argv=["/bin/sh", "-c", f"echo entered >> {counter}"])
    adir = node.root / "attempts" / a
    # Crash after the exclusive runner claim but before the payload-start fact.
    (adir / "facts" / "runner_entered").write_text(json.dumps({"invocation": "crashed-before-payload"}))
    replay = subprocess.run([sys.executable, str(SHIM), "--root", str(node.root), "run", a],
                            env={**node.env(), "INVOCATION_ID": "replayed"},
                            capture_output=True, text=True, timeout=30)
    assert replay.returncode == 0
    assert not counter.exists()
    assert not (adir / "facts" / "payload_started").exists()


def test_late_finalizer_cannot_replace_the_winning_result(node):
    a = att(905)
    node.stage(a, argv=["true"])
    node.launch(a)
    node.wait_state(a, "stopped")
    adir = node.root / "attempts" / a
    winner = json.loads((adir / "service_result.json").read_text())
    proc = subprocess.run([sys.executable, str(SHIM), "--root", str(node.root), "finalize", a],
                          env={**node.env(), "INVOCATION_ID": "late-matrix", "SERVICE_RESULT": "oom-kill",
                               "EXIT_CODE": "exited", "EXIT_STATUS": "137"},
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads((adir / "service_result.json").read_text()) == winner
    assert json.loads((adir / "replay" / "finalize-late-matrix.json").read_text())["service_result"] == "oom-kill"


def test_stale_epoch_cannot_cancel_or_release_and_keeps_allocation(node):
    a = att(904)
    node.stage(a, argv=["/bin/sh", "-c", "sleep 20"], gpus=["GPU-a"])
    assert node.launch(a)["result"] == "started"
    node.wait_state(a, "running")
    marker = node.root / "alloc" / "GPU-a.json"
    node.call("fence", "--fleet-id", FLEET, "--epoch", "2")
    cancel = node.call("cancel", "--fleet-id", FLEET, "--epoch", "1", a)
    release = node.call("release", "--fleet-id", FLEET, "--epoch", "1", a)
    assert cancel["reason"] == "stale_epoch"
    assert release["released"] is False and release["reason"] == "stale_epoch"
    assert node.status(a)["state"] == "running"
    assert json.loads(marker.read_text())["attempt_id"] == a
    # Current fenced owner can stop it; collection/release remains a later action.
    stopped = node.call("cancel", "--fleet-id", FLEET, "--epoch", "2", a)
    assert stopped["stopped"] is True
    node.wait_state(a, "stopped")
    assert marker.exists()
