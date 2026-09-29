"""Scheduling policy under contention (§4.4)."""

from __future__ import annotations

import asyncio

from fleetq.engine.controller import ControllerConfig
from fleetq.executors.fake import FakeNode

from harness import Harness


def scenario(tmp_path, starve_after_s: float, ticks: int = 40):
    """A 4-GPU job behind a steady stream of 1-GPU jobs on one 4-GPU node."""
    gpus = [f"GPU-{i}" for i in range(4)]
    cfg = ControllerConfig(tick_s=0.01, observe_interval_s=0.0, refusal_backoff_s=0, stage_backoff_s=0,
                           retry_backoff_s=0, post_job_cooldown_s=0, starve_after_s=starve_after_s)
    h = Harness(tmp_path, [FakeNode("n1", gpus=gpus, run_ticks=3)], config=cfg)
    h.add_node("n1", gpus=gpus, capacity={"job_slots": 8})
    for i in range(3):
        h.submit(key=f"small-{i}")
    big = h.submit(key="big", resources={"gpus": 4})["jobs"][0]
    started_at = None

    async def go():
        nonlocal started_at
        ctl = h.start()
        await ctl.startup()
        for t in range(ticks):
            h.submit(key=f"stream-{t}")                  # one more 1-GPU job every tick
            await h.ticks(1)
            await asyncio.sleep(0.02)                    # let the starvation clock pass in real time
            if started_at is None and h.attempts(big):
                started_at = t
    asyncio.run(go())
    reason = h.job(big)["reason"]
    violations = h.invariants()
    h.close()
    return started_at, reason, violations


def test_without_holds_a_big_job_starves_behind_small_ones(tmp_path):
    started, reason, violations = scenario(tmp_path, starve_after_s=0)
    assert started is None, "greedy placement never lets 4 GPUs be free at once"
    assert "no_placeable_gpus" in (reason or "") and violations == []


def test_concurrent_cpu_jobs_cannot_overcommit_declared_ram_budget(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=[], run_ticks=100)])
    h.add_node("n1", gpus=[], capacity={"ram_budget_mb": 6000, "job_slots": 2})
    first = h.submit(key="ram-a", resources={"gpus": 0, "mem_mb": 4000})["jobs"][0]
    second = h.submit(key="ram-b", resources={"gpus": 0, "mem_mb": 4000})["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(6)
    asyncio.run(go())
    try:
        assert h.job(first)["phase"] == "RUNNING"
        assert not h.attempts(second)
        assert "insufficient_ram" in h.job(second)["reason"]
        committed = h.store.run_sync(lambda c: c.execute(
            "SELECT SUM(amount) FROM resource_reservations "
            "WHERE node_id='n1' AND kind='ram' AND released_at IS NULL").fetchone()[0])
        assert committed == 4000
        assert h.invariants() == []
    finally:
        h.close()


def test_a_starving_job_gets_its_node_drained_and_starts(tmp_path):
    started, _, violations = scenario(tmp_path, starve_after_s=0.05)
    assert started is not None, "the node was held until all four GPUs were free for the big job"
    assert violations == []


def _spill_harness(tmp_path, after_s=60):
    cfg = ControllerConfig(tick_s=0.01, observe_interval_s=0.0, refusal_backoff_s=0, stage_backoff_s=0,
                           retry_backoff_s=0, post_job_cooldown_s=0)
    h = Harness(tmp_path, [FakeNode("home", gpus=["GPU-h"], run_ticks=100), FakeNode("away", gpus=["GPU-x"])],
                config=cfg)
    busy = h.submit(key="busy", placement={"on": ["home"]})["jobs"][0]
    job = h.submit(key="spiller", placement={"on": ["home"], "spill": {"after_s": after_s, "to": ["away"]}})["jobs"][0]
    return h, busy, job


def _age(h, job_id, seconds):
    import datetime as dt
    then = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    h.store.run_sync(lambda c: c.execute("UPDATE jobs SET submitted_at = ? WHERE id = ?", (then, job_id)))


def test_a_job_spills_only_after_its_deadline_and_says_when(tmp_path):
    h, busy, job = _spill_harness(tmp_path)

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(3)
        assert h.attempts(busy) and not h.attempts(job), "home is busy and the deadline hasn't passed"
        decision = h.store.run_sync(lambda c: c.execute(
            "SELECT detail_json FROM placement_decisions WHERE job_id = ? AND decision = 'no_placement'"
            " ORDER BY id DESC LIMIT 1", (job,)).fetchone()[0])
        assert '"spill_not_yet"' in decision and '"in_s"' in decision
        _age(h, job, 61)
        await h.ticks(3)
        (att,) = h.attempts(job)
        assert att["target"] == "away"
    asyncio.run(go())
    assert h.invariants() == []
    h.close()


def test_owned_machines_still_win_once_spilling_is_allowed(tmp_path):
    h, busy, job = _spill_harness(tmp_path)
    _age(h, job, 3600)
    h.store.run_sync(lambda c: c.execute("DELETE FROM jobs WHERE id = ?", (busy,)))   # home is free again

    async def go():
        ctl = h.start()
        await ctl.startup()
        await h.ticks(3)
    asyncio.run(go())
    (att,) = h.attempts(job)
    assert att["target"] == "home"
    h.close()


def test_spilling_to_a_cluster_needs_the_tokens_permission(tmp_path):
    import json
    import pytest
    from fleetq import auth
    from fleetq.errors import FqError
    from fleetq.util import utcnow
    h = Harness(tmp_path, [FakeNode("home", gpus=["GPU-h"])])
    h.store.run_sync(lambda c: c.execute(
        "INSERT INTO nodes (id, backend, enabled, config_json, updated_at) VALUES (?,?,?,?,?)",
        ("kiac", "slurm", 1, json.dumps({"site": {"queues": {"a100": {}}}}), utcnow())))
    with pytest.raises(FqError) as exc:
        h.submit(key="s1", placement={"on": ["home"], "spill": {"after_s": 3600, "to": ["kiac:a100"]}})
    assert exc.value.code == "cluster_not_allowed"
    _, cl = h.store.run_sync(lambda c: auth.create_token(c, owner="suresh", kind="human", label="c",
                                                         allow_clusters=True))
    ok = h.submit(key="s2", token=cl, placement={"on": ["home"], "spill": {"after_s": 3600, "to": ["kiac:a100"]}})
    assert ok["jobs"]
    with pytest.raises(FqError):
        h.submit(key="s3", token=cl, placement={"on": ["home"], "spill": {"after_s": 5, "to": ["kiac"]}})
    h.close()


def test_a_timed_out_job_resumes_on_the_machine_holding_its_checkpoint(tmp_path):
    cfg = ControllerConfig(tick_s=0.01, observe_interval_s=0.0, refusal_backoff_s=0, stage_backoff_s=0,
                           retry_backoff_s=0, post_job_cooldown_s=0)
    late = FakeNode("z-late", gpus=["GPU-z"], run_ticks=2, outcome="TIMEOUT", exit_code=None)
    h = Harness(tmp_path, [late, FakeNode("b-early", gpus=["GPU-b"])], config=cfg)
    drain = "UPDATE nodes SET drain_kind = ?, drain_reason = ? WHERE id = 'b-early'"
    h.store.run_sync(lambda c: c.execute(drain, ("manual", "test")))
    job = h.submit(key="ckpt", placement={"on": ["b-early", "z-late"]},
                   control={"retry": 1, "retry_on": ["timeout"], "warn_signal": "USR1"})["jobs"][0]

    async def go():
        ctl = h.start()
        await ctl.startup()
        while not h.attempts(job):
            await h.ticks(1)
        assert h.attempts(job)[0]["target"] == "z-late"
        # b-early is free now, and anti-thrash plus the tie-break would both pick it.
        h.store.run_sync(lambda c: c.execute(drain, (None, None)))
        while h.attempts(job)[0]["outcome"] != "TIMEOUT":
            await h.ticks(1)
        late.outcome, late.exit_code = "COMPLETED", 0      # the resumed run finishes
        await h.ticks(10)
    asyncio.run(go())
    attempts = h.attempts(job)
    assert attempts[0]["outcome"] == "TIMEOUT" and len(attempts) == 2
    assert attempts[1]["target"] == "z-late", "the retry went back to its checkpoint"
    h.close()
