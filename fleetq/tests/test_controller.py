"""Controller scenarios against the fake executor (§9 recovery matrix, §13.2)."""

from __future__ import annotations

import asyncio

import pytest

from fleetq.engine import state
from fleetq.engine.controller import ControllerConfig
from fleetq.errors import FqError
from fleetq.executors.fake import FakeNode

from harness import Harness


@pytest.fixture()
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def _drive(h: Harness, n: int = 6, *, restored: bool = False):
    async def go():
        ctl = h.start(restored=restored)
        await ctl.startup()
        await h.ticks(n)
    asyncio.run(go())


def test_happy_path_completes_once(h):
    jid = h.submit(key="k1")["jobs"][0]
    _drive(h)
    job = h.job(jid)
    assert job["phase"] == "TERMINAL"
    assert job["execution_outcome"] == "COMPLETED" and job["success"] == 1
    assert job["executions_used"] == 1
    assert list(h.fake.entries().values()) == [1]
    assert h.open_gpu_reservations() == []
    assert h.invariants() == []


def test_submit_replay_returns_same_job_and_conflict_on_different_body(h):
    first = h.submit(key="same")
    again = h.submit(key="same")
    assert again["jobs"] == first["jobs"] and again["idempotent_replay"] is True
    with pytest.raises(FqError) as exc:
        h.submit(key="same", resources={"gpus": 2})
    assert exc.value.code == "idempotency_conflict"


def test_lost_launch_response_is_reconciled_not_replayed(h):
    h.fake.node("n1").lose_launch_response = True
    jid = h.submit(key="k")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(1)
        att = h.attempts(jid)[0]
        assert att["state"] == "START_UNKNOWN" and att["remote_may_be_live"] == 1
        assert h.job(jid)["phase"] == "RECONCILING"
        # Uncertainty keeps the GPU held: nothing else may take it.
        assert h.open_gpu_reservations() == ["GPU-a"]
        await h.ticks(5)
    asyncio.run(go())
    assert h.job(jid)["execution_outcome"] == "COMPLETED"
    assert sum(h.fake.entries().values()) == 1
    assert len(h.attempts(jid)) == 1
    assert ("launch_unknown" in [k for k, _ in h.alerts])
    assert h.invariants() == []


def test_placement_refusal_does_not_consume_a_retry(h):
    h.fake.node("n1").refuse_gpus = {"GPU-a"}
    jid = h.submit(key="k")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(1)
        first = h.attempts(jid)[0]
        assert first["state"] == "REFUSED" and first["remote_may_be_live"] == 0
        assert h.job(jid)["executions_used"] == 0
        cooldown = h.store.run_sync(lambda c: c.execute(
            "SELECT cooldown_until FROM node_gpus WHERE uuid='GPU-a'").fetchone()[0])
        assert cooldown is not None
        await h.ticks(5)  # retries on GPU-b, which is not refused
    asyncio.run(go())
    attempts = h.attempts(jid)
    assert attempts[-1]["state"] == "RELEASED"
    assert h.job(jid)["execution_outcome"] == "COMPLETED"
    assert h.job(jid)["executions_used"] == 1
    assert h.invariants() == []


def test_one_gpu_is_never_double_booked(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], run_ticks=3)])
    j1 = h.submit(key="a")["jobs"][0]
    j2 = h.submit(key="b")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(1)
        phases = {h.job(j1)["phase"], h.job(j2)["phase"]}
        assert "PENDING" in phases and len(h.open_gpu_reservations()) == 1
        await h.ticks(12)
    asyncio.run(go())
    assert h.job(j1)["execution_outcome"] == h.job(j2)["execution_outcome"] == "COMPLETED"
    assert h.invariants() == []
    h.close()


def test_cancel_pending_job_is_immediate(h):
    jid = h.submit(key="k", control={"hold": True})["jobs"][0]
    assert h.job(jid)["phase"] == "HELD"
    h.store.run_sync(lambda c: state.update_job(c, jid, event="cancel", actor="t", desired_state="CANCEL"))
    _drive(h, 2)
    assert h.job(jid)["phase"] == "TERMINAL" and h.job(jid)["execution_outcome"] == "CANCELLED"
    assert h.fake.entries() == {}


def test_cancel_running_job_waits_for_confirmed_stop(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], run_ticks=100)])
    jid = h.submit(key="k")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(2)
        assert h.job(jid)["phase"] == "RUNNING"
        h.store.run_sync(lambda c: state.update_job(c, jid, event="cancel", actor="t", desired_state="CANCEL",
                                                     cancel_requested=1))
        await h.ticks(3)
    asyncio.run(go())
    job = h.job(jid)
    assert job["phase"] == "TERMINAL" and job["execution_outcome"] == "CANCELLED" and job["success"] == 0
    assert h.open_gpu_reservations() == []
    assert h.invariants() == []
    h.close()


def test_cancel_while_unreachable_stays_cancelling_and_holds_resources(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], run_ticks=100)])
    jid = h.submit(key="k")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(2)
        h.fake.node("n1").reachable = False
        h.store.run_sync(lambda c: state.update_job(c, jid, event="cancel", actor="t", desired_state="CANCEL"))
        await h.ticks(3)
        # A cancel request is durable desired state, not proof of cancellation (invariant 5).
        assert h.job(jid)["phase"] == "CANCELLING"
        assert h.open_gpu_reservations() == ["GPU-a"]
        h.fake.node("n1").reachable = True
        await h.ticks(3)
    asyncio.run(go())
    assert h.job(jid)["execution_outcome"] == "CANCELLED"
    assert h.open_gpu_reservations() == []
    h.close()


def test_reboot_is_node_fail_and_retries_with_proof_of_death(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], run_ticks=100)])
    jid = h.submit(key="k", control={"retry": 1})["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(2)
        h.fake.reboot("n1")
        h.fake.node("n1").run_ticks = 2
        await h.ticks(8)
    asyncio.run(go())
    attempts = h.attempts(jid)
    assert attempts[0]["outcome"] == "NODE_FAIL"
    assert len(attempts) == 2 and attempts[1]["outcome"] == "COMPLETED"
    assert all(count == 1 for count in h.fake.entries().values())
    assert h.job(jid)["execution_outcome"] == "COMPLETED"
    assert h.invariants() == []
    h.close()


def test_node_fail_without_retry_is_terminal(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], run_ticks=100)])
    jid = h.submit(key="k")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(2)
        h.fake.reboot("n1")
        await h.ticks(3)
    asyncio.run(go())
    assert h.job(jid)["execution_outcome"] == "NODE_FAIL" and h.job(jid)["success"] == 0
    assert len(h.attempts(jid)) == 1
    h.close()


def test_unreachable_is_not_death_and_escalates_after_lost_contact(tmp_path):
    cfg = ControllerConfig(tick_s=0.01, observe_interval_s=0.0, lost_contact_s=0.05, refusal_backoff_s=0,
                           stage_backoff_s=0, retry_backoff_s=0, post_job_cooldown_s=0)
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], run_ticks=100)], config=cfg)
    jid = h.submit(key="k", control={"retry": 3})["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(2)
        h.fake.node("n1").reachable = False
        await h.ticks(1)
        await asyncio.sleep(0.1)
        await h.ticks(2)
    asyncio.run(go())
    job = h.job(jid)
    assert job["phase"] == "RECONCILING" and job["reason"] == "LOST_CONTACT"
    assert len(h.attempts(jid)) == 1          # never retried on silence
    assert h.attempts(jid)[0]["remote_may_be_live"] == 1
    assert "lost_contact" in [k for k, _ in h.alerts]
    h.close()


def test_descendants_block_release(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], run_ticks=1, leave_descendants=True)])
    jid = h.submit(key="k")["jobs"][0]
    _drive(h, 4)
    # The wrapper finished but descendants remain: the GPU is not freed.
    assert h.job(jid)["phase"] != "TERMINAL"
    assert h.open_gpu_reservations() == ["GPU-a"]
    h.close()


def test_pause_stops_new_launches_but_not_cancellation(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], run_ticks=100)])
    running = h.submit(key="a")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(2)
        h.store.run_sync(lambda c: c.execute(
            "INSERT INTO controller_meta (key, value) VALUES ('dispatch','paused')"))
        queued = h.submit(key="b", resources={"gpus": 0})["jobs"][0]
        h.store.run_sync(lambda c: state.update_job(c, running, event="cancel", actor="t", desired_state="CANCEL"))
        await h.ticks(3)
        return queued
    queued = asyncio.run(go())
    assert h.job(running)["execution_outcome"] == "CANCELLED"
    assert h.job(queued)["phase"] == "PENDING"
    h.close()


def test_crash_before_send_is_never_started(tmp_path):
    h = Harness(tmp_path)
    jid = h.submit(key="k")["jobs"][0]

    async def first():
        ctl = h.start()
        await ctl.startup()
        # Plan and stage by hand, then "crash" before the launch boundary.
        placed = await ctl._tx(lambda c: ctl._plan(c, jid))
        await ctl._tx(lambda c: state.update_attempt(c, placed[0], state="STAGING", event="staging"))
    asyncio.run(first())
    _drive(h, 6)   # a new controller process
    assert h.attempts(jid)[0]["state"] == "NEVER_STARTED"
    assert h.job(jid)["execution_outcome"] == "COMPLETED"
    assert sum(h.fake.entries().values()) == 1
    assert h.invariants() == []
    h.close()


def test_cancel_after_commit_before_send_never_launches(tmp_path):
    h = Harness(tmp_path)
    jid = h.submit(key="k")["jobs"][0]

    async def first():
        ctl = h.start()
        await ctl.startup()
        placed = await ctl._tx(lambda c: ctl._plan(c, jid))
        await ctl._tx(lambda c: state.update_attempt(c, placed[0], state="STAGING", event="staging"))
        # Cancellation lands after the durable attempt commit, before any send.
        await ctl._tx(lambda c: state.update_job(c, jid, event="cancel", actor="t", desired_state="CANCEL"))

    asyncio.run(first())
    _drive(h, 4)  # recovery must honor cancel without sending the staged attempt
    assert h.job(jid)["execution_outcome"] == "CANCELLED"
    assert h.attempts(jid)[0]["state"] == "NEVER_STARTED"
    assert h.fake.entries() == {}
    assert h.open_gpu_reservations() == []
    assert h.invariants() == []
    h.close()


def test_hold_after_staging_commit_recovers_to_held_without_launch(tmp_path):
    h = Harness(tmp_path)
    jid = h.submit(key="hold-stage")["jobs"][0]

    def interrupted(conn):
        att = state.create_attempt(conn, jid, backend="fake", target="n1", epoch=1, reservations=[])
        state.update_attempt(conn, att["id"], state="STAGING", event="staging")
        state.update_job(conn, jid, event="hold", actor="test", desired_state="HOLD")

    h.store.run_sync(interrupted)
    ctl = h.start()
    try:
        h.store.run_sync(ctl._recover_interrupted)
        assert h.job(jid)["phase"] == "HELD"
        assert h.attempts(jid)[0]["state"] == "NEVER_STARTED"
        assert h.fake.entries() == {}
        assert h.open_gpu_reservations() == []
        assert h.invariants() == []
    finally:
        h.close()


@pytest.mark.parametrize("desired", ["CANCEL", "HOLD"])
def test_cancel_or_hold_during_staging_prevents_remote_launch(tmp_path, desired):
    h = Harness(tmp_path)
    jid = h.submit(key=f"stage-race-{desired}")["jobs"][0]
    ctl = h.start()
    h.store.run_sync(lambda c: c.execute(
        "UPDATE nodes SET reconciled_epoch=? WHERE id='n1'", (ctl.ident.epoch,)))
    att = h.store.run_sync(lambda c: state.create_attempt(
        c, jid, backend="fake", target="n1", epoch=ctl.ident.epoch, reservations=[]))

    class InlineAsyncStore:
        """Keep this race test deterministic under runtimes with thread wakeup issues."""
        async def run(self, fn):
            return h.store.run_sync(fn)

    ctl.store = InlineAsyncStore()

    async def exercise():
        async def gated_stage(_ctx):
            from fleetq.executors.base import StageResult
            # Simulate the control request committing while stage is in flight.
            h.store.run_sync(lambda c: state.update_job(
                c, jid, event=desired.lower(), actor="test", desired_state=desired,
                cancel_requested=int(desired == "CANCEL")))
            return StageResult(ok=True)

        h.fake.stage = gated_stage
        await asyncio.wait_for(ctl._dispatch(att["id"]), timeout=2)

    try:
        asyncio.run(exercise())
        job = h.job(jid)
        assert job["phase"] == ("TERMINAL" if desired == "CANCEL" else "HELD")
        if desired == "CANCEL":
            assert job["execution_outcome"] == "CANCELLED"
        assert h.fake.entries() == {}
        assert h.attempts(jid)[0]["state"] == "NEVER_STARTED"
        assert h.open_gpu_reservations() == []
        assert h.invariants() == []
    finally:
        h.close()


def test_crash_after_send_is_adopted_not_rerun(tmp_path):
    h = Harness(tmp_path)
    jid = h.submit(key="k")["jobs"][0]

    async def first():
        ctl = h.start()
        await ctl.startup()
        placed = await ctl._tx(lambda c: ctl._plan(c, jid))
        aid = placed[0]
        await ctl._tx(lambda c: state.update_attempt(c, aid, state="STAGING", event="s"))
        ctx = await h.store.run(lambda c: ctl._context(c, state.get_attempt(c, aid)))
        await h.fake.stage(ctx)
        await ctl._tx(lambda c: state.update_attempt(c, aid, state="LAUNCHING", event="l"))
        await h.fake.launch(ctx)  # it executed; the controller died before recording it
    asyncio.run(first())
    _drive(h, 6)
    atts = h.attempts(jid)
    assert len(atts) == 1 and atts[0]["state"] == "RELEASED"
    assert sum(h.fake.entries().values()) == 1
    assert h.job(jid)["execution_outcome"] == "COMPLETED"
    h.close()


def test_stale_epoch_disables_dispatch(tmp_path):
    h = Harness(tmp_path)
    h.fake.node("n1").highest_epoch = 999     # a newer controller fenced it
    jid = h.submit(key="k")["jobs"][0]
    _drive(h, 3)
    assert h.job(jid)["phase"] == "PENDING"
    assert h.fake.entries() == {}
    assert "fence_conflict" in [k for k, _ in h.alerts]
    assert h.store.run_sync(lambda c: int(c.execute(
        "SELECT value FROM controller_meta WHERE key='controller_epoch'").fetchone()[0])) == 1
    h.close()


def test_orphan_remote_attempt_quarantines_target(tmp_path):
    h = Harness(tmp_path)
    from fleetq.executors.fake import FakeRemoteAttempt
    h.fake.node("n1").attempts["att_unknown"] = FakeRemoteAttempt("att_unknown", epoch=1, entered=True)
    jid = h.submit(key="k")["jobs"][0]
    _drive(h, 3)
    assert h.job(jid)["phase"] == "PENDING"
    node_state = h.store.run_sync(lambda c: c.execute("SELECT state FROM nodes WHERE id='n1'").fetchone()[0])
    assert node_state == "QUARANTINED"
    accepted_epoch = h.store.run_sync(lambda c: c.execute(
        "SELECT fence_epoch FROM nodes WHERE id='n1'").fetchone()[0])
    assert accepted_epoch == 1
    h.close()


def test_dependency_afterok_blocks_when_parent_fails(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], exit_code=1, outcome="FAILED")])
    parent = h.submit(key="p")["jobs"][0]
    child = h.submit(key="c", control={"after": [{"job": parent, "type": "afterok"}]})["jobs"][0]
    _drive(h, 8)
    assert h.job(parent)["execution_outcome"] == "FAILED"
    assert h.job(child)["phase"] == "BLOCKED" and h.job(child)["reason"] == "DependencyNeverSatisfied"
    assert h.attempts(child) == []
    h.close()


def test_dependency_afterany_runs_after_failure(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], exit_code=1, outcome="FAILED")])
    parent = h.submit(key="p")["jobs"][0]
    child = h.submit(key="c", resources={"gpus": 0}, control={"after": [{"job": parent, "type": "afterany"}]})["jobs"][0]
    _drive(h, 10)
    assert h.job(child)["phase"] == "TERMINAL"
    assert len(h.attempts(child)) == 1
    h.close()


def test_each_creates_one_job_per_destination(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"]), FakeNode("n2", gpus=["GPU-x"])])
    resp = h.submit(key="g", placement={"each": ["n1", "n2"]})
    assert resp["group"]["size"] == 2 and len(resp["jobs"]) == 2
    _drive(h, 8)
    targets = sorted(h.attempts(j)[0]["target"] for j in resp["jobs"])
    assert targets == ["n1", "n2"]
    h.close()


def test_agent_gpu_quota_is_enforced_at_placement(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=[f"GPU-{i}" for i in range(8)], run_ticks=50)])
    h.add_node("n1", gpus=[f"GPU-{i}" for i in range(8)], capacity={"job_slots": 8, "max_vram_mb": 24000})
    jobs = [h.submit(key=f"k{i}", token=h.agent_token, resources={"gpus": 2})["jobs"][0] for i in range(3)]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(3)
    asyncio.run(go())
    running = [j for j in jobs if h.job(j)["phase"] in ("RUNNING", "DISPATCHING")]
    assert len(running) == 2                       # 4-GPU agent quota: two 2-GPU jobs
    assert any("gpu_quota" in (h.job(j)["reason"] or "") for j in jobs)
    h.close()


def test_unsatisfiable_is_refused_at_admission(h):
    with pytest.raises(FqError) as exc:
        h.submit(key="big", resources={"gpus": 8})
    assert exc.value.code == "unsatisfiable"


def test_reserved_env_is_rejected(h):
    with pytest.raises(FqError) as exc:
        h.submit(key="env", env={"CUDA_VISIBLE_DEVICES": "0"})
    assert exc.value.code == "invalid_argument"


def test_agent_cannot_use_clusters_without_authorization(h):
    h.store.run_sync(lambda c: c.execute(
        "INSERT INTO nodes (id, backend, enabled, config_json, updated_at) VALUES ('kiac','slurm',1,'{}','x')"))
    with pytest.raises(FqError) as exc:
        h.submit(key="c", token=h.agent_token, placement={"on": ["kiac"]})
    assert exc.value.code == "cluster_not_allowed"


def _feed(history, node, uuid, now, *, procs=0, n=4):
    from fleetq.engine.idle import GpuSample
    for i in range(n):
        history.add(node, uuid, GpuSample(f"{uuid}-{now}-{i}", now - (n - 1 - i) * 40, "b1", True, True, procs, 0.0, 0.0))
    history.record_feed_success(now)


def test_shared_node_waits_for_proven_idle_history(tmp_path):
    import time as _t
    from fleetq.engine.idle import IdleHistory
    h = Harness(tmp_path, [FakeNode("s1", gpus=["GPU-s"])], mode="shared")
    history = IdleHistory()
    jid = h.submit(key="k", placement={"on": ["s1"]})["jobs"][0]

    async def go():
        ctl = h.start()
        ctl.shared_capacity = history
        await ctl.startup()
        await h.ticks(2)
        assert h.job(jid)["phase"] == "PENDING"            # no history: never launched blindly
        assert h.attempts(jid) == []
        _feed(history, "s1", "GPU-s", _t.time(), procs=1)
        await h.ticks(2)
        assert h.attempts(jid) == []                       # a foreign process in the window
        history.reset()
        _feed(history, "s1", "GPU-s", _t.time())
        await h.ticks(4)
    asyncio.run(go())
    assert h.job(jid)["execution_outcome"] == "COMPLETED"
    h.close()


def test_exclusive_preferred_over_shared(tmp_path):
    import time as _t
    from fleetq.engine.idle import IdleHistory
    h = Harness(tmp_path, [FakeNode("shared1", gpus=["GPU-s"]), FakeNode("excl1", gpus=["GPU-e"])])
    h.add_node("shared1", gpus=["GPU-s"], mode="shared")
    history = IdleHistory()
    _feed(history, "shared1", "GPU-s", _t.time())
    jid = h.submit(key="k", placement={"on": ["shared1", "excl1"]})["jobs"][0]

    async def go():
        ctl = h.start()
        ctl.shared_capacity = history
        await ctl.startup()
        await h.ticks(3)
    asyncio.run(go())
    assert h.attempts(jid)[0]["target"] == "excl1"          # spare labmates' machine
    h.close()


def test_dispatch_failure_before_launch_releases_and_requeues(h):
    jid = h.submit(key="k")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        real_context = ctl._context
        failures = []

        def fail_once(conn, att):
            if not failures:
                failures.append(att["id"])
                raise OSError("disk I/O error")
            return real_context(conn, att)
        ctl._context = fail_once
        await h.ticks(1)
        first = h.attempts(jid)[0]
        assert first["state"] == "NEVER_STARTED" and first["remote_may_be_live"] == 0
        assert h.open_gpu_reservations() == []
        assert h.job(jid)["phase"] == "PENDING"
        await h.ticks(5)
    asyncio.run(go())
    assert h.job(jid)["execution_outcome"] == "COMPLETED"
    assert h.job(jid)["executions_used"] == 1
    assert h.invariants() == []


def test_why_a_launch_never_started_is_kept_on_the_attempt(h):
    from fleetq.executors.base import LaunchKind, LaunchResult
    jid = h.submit(key="k")["jobs"][0]

    async def rejected(_ctx):
        return LaunchResult(LaunchKind.NEVER_STARTED, reason="sbatch_rejected", permanent=True,
                            detail={"stderr": "sbatch: error: invalid partition specified: gpu"})
    h.fake.launch = rejected
    _drive(h, 2)
    assert "invalid partition specified" in h.attempts(jid)[0]["evidence_json"]
    assert h.job(jid)["phase"] == "BLOCKED"
