"""Cancellation, hold and retry scheduling races for the P5 workflow layer."""

from __future__ import annotations

import asyncio

from fleetq.engine import state
from fleetq.executors.base import CollectManifest
from fleetq.executors.fake import FakeNode
from fleetq.util import utcnow

from harness import Harness


def test_cancel_committed_while_staging_prevents_payload_entry(tmp_path):
    """A cancel between plan/stage and launch wins at the durable boundary."""
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"])])
    job = h.submit(key="cancel-stage")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        planned = await ctl._tx(lambda c: ctl._plan(c, job))
        assert planned is not None
        h.store.run_sync(lambda c: state.update_job(
            c, job, event="cancel_requested", actor="test", desired_state="CANCEL", cancel_requested=1))
        await ctl._dispatch(planned[0])
        await ctl.tick()

    asyncio.run(go())
    assert h.fake.entries() == {}
    assert h.job(job)["phase"] == "TERMINAL"
    assert h.job(job)["execution_outcome"] == "CANCELLED"
    assert h.open_gpu_reservations() == []
    assert h.invariants() == []
    h.close()


def test_unreachable_cancel_keeps_live_attempt_and_reservation_until_recovery(tmp_path):
    node = FakeNode("n1", gpus=["GPU-a"], run_ticks=100)
    h = Harness(tmp_path, [node])
    job = h.submit(key="cancel-offline")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(3)
        assert len(h.attempts(job)) == 1 and h.fake.entries()
        node.reachable = False
        h.store.run_sync(lambda c: state.update_job(
            c, job, event="cancel_requested", actor="test", desired_state="CANCEL", cancel_requested=1))
        await h.ticks(2)
        assert h.job(job)["phase"] == "CANCELLING"
        assert h.open_gpu_reservations() == ["GPU-a"]
        assert h.fake.entries() and h.invariants() == []
        node.reachable = True
        await h.ticks(3)

    asyncio.run(go())
    assert h.job(job)["phase"] == "TERMINAL"
    assert h.job(job)["execution_outcome"] == "CANCELLED"
    assert h.open_gpu_reservations() == []
    assert len(h.attempts(job)) == 1, "cancellation never schedules a replacement execution"
    assert h.invariants() == []
    h.close()


def test_cancelled_retryable_failure_does_not_create_retry_attempt(tmp_path):
    node = FakeNode("n1", gpus=["GPU-a"], run_ticks=100, outcome="TIMEOUT", exit_code=None)
    h = Harness(tmp_path, [node])
    job = h.submit(key="cancel-retry", control={"retry": 3, "retry_on": ["timeout"]})["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(3)
        assert h.job(job)["phase"] == "RUNNING"
        h.store.run_sync(lambda c: state.update_job(
            c, job, event="cancel_requested", actor="test", desired_state="CANCEL", cancel_requested=1))
        await h.ticks(3)
        assert h.job(job)["phase"] == "TERMINAL"
        assert h.job(job)["execution_outcome"] == "CANCELLED"
        assert len(h.attempts(job)) == 1

    asyncio.run(go())
    assert h.fake.entries() and len(h.fake.entries()) == 1
    assert h.invariants() == []
    h.close()


def test_cancelling_one_throttled_array_member_does_not_cancel_siblings(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a", "GPU-b"], run_ticks=2)])
    h.add_node("n1", gpus=["GPU-a", "GPU-b"], capacity={"job_slots": 2})
    jobs = h.submit(key="array-cancel", control={"array": {"indices": [0, 1, 2], "throttle": 1}})["jobs"]
    cancelled = jobs[0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        h.store.run_sync(lambda c: state.update_job(
            c, cancelled, event="cancel_requested", actor="test", desired_state="CANCEL", cancel_requested=1))
        for _ in range(50):
            await h.ticks(1)
            if all(h.job(j)["phase"] == "TERMINAL" for j in jobs):
                break

    asyncio.run(go())
    assert h.job(cancelled)["execution_outcome"] == "CANCELLED"
    assert all(h.job(j)["phase"] == "TERMINAL" for j in jobs)
    assert all(h.job(j)["success"] == 1 for j in jobs[1:])
    assert len(h.fake.entries()) == 2
    assert h.invariants() == []
    h.close()


def test_cancel_arriving_during_launch_rpc_stops_the_single_entered_attempt(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"], run_ticks=100)])
    job = h.submit(key="cancel-inflight-launch")["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        entered = asyncio.Event()
        reply = asyncio.Event()
        launch = h.fake.launch

        async def delayed_reply(ctx):
            result = await launch(ctx)  # remote payload entry happens before the response is delayed
            entered.set()
            await reply.wait()
            return result

        h.fake.launch = delayed_reply
        await ctl._place_and_dispatch()
        await asyncio.wait_for(entered.wait(), timeout=5)
        h.store.run_sync(lambda c: state.update_job(
            c, job, event="cancel_requested", actor="test", desired_state="CANCEL", cancel_requested=1))
        await ctl.tick()  # the in-flight launch remains owned; cancel is retried next tick
        assert h.job(job)["phase"] != "TERMINAL"
        assert h.open_gpu_reservations() == ["GPU-a"]
        reply.set()
        await ctl.drain()
        await ctl.tick()

    asyncio.run(go())
    assert len(h.attempts(job)) == 1
    assert len(h.fake.entries()) == 1
    assert h.job(job)["phase"] == "TERMINAL"
    assert h.job(job)["execution_outcome"] == "CANCELLED"
    assert h.open_gpu_reservations() == []
    assert h.invariants() == []
    h.close()


def test_dependencies_wait_through_retry_and_required_artifact_failure(tmp_path):
    node = FakeNode("n1", gpus=["GPU-a"], run_ticks=1, outcome="TIMEOUT", exit_code=None)
    h = Harness(tmp_path, [node])
    digest = "sha256:" + "a" * 64
    now = utcnow()
    h.store.run_sync(lambda c: (
        c.execute("INSERT INTO bundles VALUES (?,?,?,?,?,?,?)",
                  (digest, 0, 0, 0, 1, str(tmp_path / "unused.tar.gz"), now)),
        c.execute("INSERT INTO bundle_refs (digest, owner, ref_kind, ref_id, created_at)"
                  " VALUES (?, 'suresh', 'upload', 'test', ?)", (digest, now))))
    parent = h.submit(key="retry-collect", workdir={"bundle": digest}, collect=["out"],
                      control={"retry": 1, "retry_on": ["timeout"]})["jobs"][0]
    afterok = h.submit(key="afterok", control={"after": [{"job": parent, "type": "afterok"}]})["jobs"][0]
    afternotok = h.submit(key="afternotok", control={"after": [{"job": parent, "type": "afternotok"}]})["jobs"][0]
    afterany = h.submit(key="afterany", control={"after": [{"job": parent, "type": "afterany"}]})["jobs"][0]

    async def go():
        ctl = h.start()
        ctl.artifact_dir = tmp_path / "artifacts"
        # A complete but empty manifest fails the required `out` path without
        # involving remote storage or creating an artifact file.
        async def collect_stage(*_args, **_kwargs):
            return CollectManifest(ok=True)

        h.fake.collect_stage = collect_stage
        await ctl.startup()
        for _ in range(20):
            await h.ticks(1)
            attempts = h.attempts(parent)
            if len(attempts) == 2:
                node.outcome = "COMPLETED"
                node.exit_code = 0
                break
        assert len(h.attempts(parent)) == 2
        # Dependencies cannot cross the retry or artifact-finalization gap.
        for _ in range(20):
            await h.ticks(1)
            if h.job(parent)["phase"] == "TERMINAL":
                break
            assert all(not h.attempts(j) for j in (afterok, afternotok, afterany))
        assert h.job(parent)["phase"] == "TERMINAL"
        for _ in range(12):
            await h.ticks(1)
            if all(h.job(j)["phase"] in ("TERMINAL", "BLOCKED") for j in (afterok, afternotok, afterany)):
                break

    asyncio.run(go())
    assert h.job(parent)["execution_outcome"] == "COMPLETED"
    assert h.job(parent)["artifacts_state"] == "FAILED" and h.job(parent)["success"] == 0
    assert h.job(afterok)["phase"] == "BLOCKED" and h.attempts(afterok) == []
    assert h.job(afternotok)["phase"] == "TERMINAL" and len(h.attempts(afternotok)) == 1
    assert h.job(afterany)["phase"] == "TERMINAL" and len(h.attempts(afterany)) == 1
    assert h.invariants() == []
    h.close()
