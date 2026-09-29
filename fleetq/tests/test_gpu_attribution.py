"""Shared-node process attribution remains conservative after launch."""

from __future__ import annotations

import pytest

from fleetq.executors.base import ObserveResult

from harness import Harness


def _controller(tmp_path):
    h = Harness(tmp_path, mode="shared")
    h.store.run_sync(lambda c: c.execute("UPDATE nodes SET backend='bare' WHERE id='n1'"))
    return h, h.start()


def _observe(processes, *, complete=True, boot="boot-a"):
    return ObserveResult(
        reachable=True,
        boot_id=boot,
        node_facts={"gpu_processes": {
            "complete": complete,
            "boot_id": boot,
            "gpus": [
                {"uuid": "GPU-a", "complete": True, "processes": processes},
                {"uuid": "GPU-b", "complete": True, "processes": []},
            ],
        }},
    )


def _process(*, attribution, attempt=None):
    return {"pid": 123, "boot_id": "boot-a", "process_start_ticks": 456,
            "cgroup_path": "/user.slice/example", "attribution": attribution,
            "fleetq_attempt_id": attempt}


def _apply(h, ctl, observation):
    h.store.run_sync(lambda c: ctl._apply_gpu_process_facts(c, "n1", observation))


def _drain(h, uuid="GPU-a"):
    return h.store.run_sync(lambda c: tuple(c.execute(
        "SELECT drained, drain_reason FROM node_gpus WHERE node_id='n1' AND uuid=?", (uuid,)
    ).fetchone()))


def test_foreign_process_on_unallocated_gpu_drains_and_alerts(tmp_path):
    h, ctl = _controller(tmp_path)
    try:
        _apply(h, ctl, _observe([_process(attribution="foreign")]))
        assert _drain(h) == (1, "gpu_process_unattributed")
        assert [kind for kind, _ in h.alerts] == ["gpu_escape"]
        assert h.alerts[0][1]["incident"].endswith("123:boot-a:456:foreign_process_on_unallocated_gpu")
        _apply(h, ctl, _observe([]))
        assert _drain(h) == (0, None)
    finally:
        h.close()


def test_incomplete_or_reused_identity_fails_closed(tmp_path):
    h, ctl = _controller(tmp_path)
    try:
        _apply(h, ctl, _observe([], complete=False))
        assert _drain(h) == (1, "gpu_attribution_unknown")
        assert _drain(h, "GPU-b") == (1, "gpu_attribution_unknown")
        _apply(h, ctl, _observe([_process(attribution="ownership_unknown")]))
        assert _drain(h) == (1, "gpu_attribution_unknown")
        assert any(kind == "gpu_attribution_unknown" for kind, _ in h.alerts)
    finally:
        h.close()


@pytest.mark.parametrize("start_ticks", [None, 0, -1, True, "456"])
def test_process_without_valid_start_time_fails_closed(tmp_path, start_ticks):
    h, ctl = _controller(tmp_path)
    try:
        process = _process(attribution="foreign")
        process["process_start_ticks"] = start_ticks
        _apply(h, ctl, _observe([process]))
        assert _drain(h) == (1, "gpu_attribution_unknown")
        assert [kind for kind, _ in h.alerts] == ["gpu_attribution_unknown"]
    finally:
        h.close()


def test_clean_observation_does_not_clear_manual_drain(tmp_path):
    h, ctl = _controller(tmp_path)
    try:
        h.store.run_sync(lambda c: c.execute(
            "UPDATE node_gpus SET drained=1, drain_reason='manual' WHERE node_id='n1' AND uuid='GPU-a'"))
        _apply(h, ctl, _observe([]))
        assert _drain(h) == (1, "manual")
    finally:
        h.close()


def test_post_launch_foreign_process_is_drained_and_never_killed(tmp_path):
    h, ctl = _controller(tmp_path)
    try:
        # The allocation belongs to fleetq, but a labmate appears after launch.
        job = h.submit(key="live-gpu", resources={"gpus": 1})["jobs"][0]
        def reserve(c):
            c.execute(
                "INSERT INTO attempts (id,job_id,n,backend,target,epoch,state,remote_may_be_live,launch_op_id,"
                "spec_digest,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("att_live", job, 1, "bare", "n1", 1, "RUNNING", 1, "op_live", "sha256:x", "[]", "now", "now"))
            c.execute(
                "INSERT INTO resource_reservations(attempt_id,node_id,kind,gpu_uuid,amount,created_at) "
                "VALUES('att_live','n1','gpu','GPU-a',1,'now')")
        h.store.run_sync(reserve)
        assert h.open_gpu_reservations() == ["GPU-a"]
        process = _process(attribution="foreign")
        _apply(h, ctl, _observe([process]))
        assert _drain(h) == (1, "gpu_foreign_process")
        assert [kind for kind, _ in h.alerts] == ["foreign_process"]
        assert h.alerts[0][1]["reason"] == "foreign_process_on_allocated_gpu"
        # Attribution is observation-only; the process record is untouched.
        assert process["pid"] == 123 and process["process_start_ticks"] == 456
    finally:
        h.close()


def test_pid_reuse_changes_incident_identity(tmp_path):
    h, ctl = _controller(tmp_path)
    try:
        first = _process(attribution="foreign")
        _apply(h, ctl, _observe([first]))
        h.store.run_sync(lambda c: c.execute(
            "UPDATE node_gpus SET drained=0, drain_reason=NULL WHERE node_id='n1' AND uuid='GPU-a'"))
        reused = _process(attribution="foreign")
        reused["process_start_ticks"] = 999  # same numeric PID, a different process incarnation
        _apply(h, ctl, _observe([reused]))
        incidents = [payload["incident"] for kind, payload in h.alerts if kind == "gpu_escape"]
        assert len(incidents) == 2 and incidents[0] != incidents[1]
        assert incidents[0].startswith("n1:GPU-a:123:boot-a:456:")
        assert incidents[1].startswith("n1:GPU-a:123:boot-a:999:")
    finally:
        h.close()
