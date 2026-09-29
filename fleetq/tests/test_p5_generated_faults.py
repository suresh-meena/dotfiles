"""Bounded deterministic fault scenarios for P5 workflow invariants."""

from __future__ import annotations

import asyncio
import random

from fleetq.engine import state
from fleetq.executors.fake import FakeNode

from harness import Harness

ACTIVE = {"DISPATCHING", "RUNNING", "SUBMITTED", "SUBMISSION_UNKNOWN",
          "RECONCILING", "CANCELLING", "FINALIZING"}


# Fixed seeds make failures reproducible while varying which array member and
# scheduling boundary receives the cancellation.
def test_seeded_array_cancel_races_preserve_throttle_and_sibling_outcomes(tmp_path):
    for seed in (7, 29, 101):
        rng = random.Random(seed)
        gpus = [f"GPU-{i}" for i in range(5)]
        case_dir = tmp_path / str(seed)
        case_dir.mkdir()
        h = Harness(case_dir, [FakeNode("n1", gpus=gpus, run_ticks=3)])
        h.add_node("n1", gpus=gpus, capacity={"job_slots": 5})
        jobs = h.submit(
            key=f"generated-{seed}",
            control={"array": {"indices": [2, 5, 8, 13, 21], "throttle": 2}},
        )["jobs"]
        cancelled = jobs[rng.randrange(len(jobs))]
        cancel_after_ticks = rng.randrange(0, 5)
        peak_active = 0

        async def go():
            nonlocal peak_active
            ctl = h.start()
            await ctl.startup()
            for tick in range(60):
                if tick == cancel_after_ticks:
                    h.store.run_sync(lambda c: state.update_job(
                        c, cancelled, event="cancel_requested", actor="generated-fault",
                        desired_state="CANCEL", cancel_requested=1))
                await h.ticks(1)
                phases = [h.job(job)["phase"] for job in jobs]
                active = sum(phase in ACTIVE for phase in phases)
                peak_active = max(peak_active, active)
                # The throttle counts uncertain/cancelling members too, so the
                # bound must hold at every observed transition, not just RUNNING.
                assert active <= 2, (seed, tick, phases)
                assert h.invariants() == [], (seed, tick, h.invariants())
                if all(phase == "TERMINAL" for phase in phases):
                    break

        asyncio.run(go())
        assert all(h.job(job)["phase"] == "TERMINAL" for job in jobs), seed
        assert h.job(cancelled)["execution_outcome"] == "CANCELLED", seed
        assert all(h.job(job)["success"] == 1 for job in jobs if job != cancelled), seed
        assert len(h.attempts(cancelled)) <= 1, seed
        assert h.invariants() == [], seed
        h.close()


def test_seeded_corrupt_dependency_cycles_do_not_make_any_member_eligible(tmp_path):
    """Legacy/corrupt cycles remain blocked regardless of graph size/order."""
    for seed, size in ((3, 2), (17, 4), (41, 6)):
        case_dir = tmp_path / f"dag-{seed}"
        case_dir.mkdir()
        h = Harness(case_dir)
        jobs = [h.submit(key=f"node-{i}")["jobs"][0] for i in range(size)]
        rng = random.Random(seed)
        order = list(jobs)
        rng.shuffle(order)
        # A cycle through every generated member, inserted as legacy data.
        edges = [(order[i], order[(i + 1) % size]) for i in range(size)]
        h.store.run_sync(lambda c: c.executemany(
            "INSERT INTO deps (job_id, parent_id, type) VALUES (?, ?, 'afterok')", edges))
        ctl = h.start()
        eligible = h.store.run_sync(ctl._eligible_jobs)
        assert eligible == [], (seed, eligible)
        assert all(h.attempts(job) == [] for job in jobs), seed
        assert h.fake.entries() == {}, seed
        assert h.invariants() == [], seed
        h.close()


def test_seeded_mixed_dependencies_never_dispatch_before_their_gate(tmp_path):
    """Generated parent outcomes exercise all four dependency gates over ticks."""
    kinds = ("afterok", "afterany", "afternotok", "after")
    for seed, outcome in ((5, "COMPLETED"), (23, "FAILED"), (67, "CANCELLED")):
        rng = random.Random(seed)
        node = FakeNode("n1", gpus=[f"GPU-{i}" for i in range(5)], run_ticks=7,
                        outcome="FAILED" if outcome == "FAILED" else "COMPLETED",
                        exit_code=1 if outcome == "FAILED" else 0)
        case_dir = tmp_path / str(seed)
        case_dir.mkdir()
        h = Harness(case_dir, [node])
        parent = h.submit(key="parent")['jobs'][0]
        order = list(kinds)
        rng.shuffle(order)
        children = {
            kind: h.submit(key=f"child-{kind}", control={"after": [{"job": parent, "type": kind}]})["jobs"][0]
            for kind in order
        }
        cancel_at = rng.randint(1, 3)

        async def go():
            ctl = h.start()
            await ctl.startup()
            for tick in range(24):
                if outcome == "CANCELLED" and tick == cancel_at:
                    h.store.run_sync(lambda c: state.update_job(
                        c, parent, event="cancel_requested", actor="generated-dependency",
                        desired_state="CANCEL", cancel_requested=1))
                await h.ticks(1)
                p = h.job(parent)
                for kind, child in children.items():
                    started = bool(h.attempts(child))
                    if kind == "after":
                        gate = any(h.fake.entries().get(a["id"], 0) > 0 for a in h.attempts(parent))
                    elif kind == "afterany":
                        gate = p["phase"] == "TERMINAL"
                    elif kind == "afterok":
                        gate = p["phase"] == "TERMINAL" and p["success"] == 1
                    else:
                        gate = p["phase"] == "TERMINAL" and p["success"] == 0
                    assert not started or gate, (seed, tick, kind, dict(p), h.attempts(child))
                assert h.invariants() == [], (seed, tick, h.invariants())
                if p["phase"] == "TERMINAL" and all(
                    h.job(j)["phase"] in ("TERMINAL", "BLOCKED") for j in children.values()
                ):
                    break

        asyncio.run(go())
        assert h.job(parent)["phase"] == "TERMINAL", seed
        assert len(h.attempts(children["after"])) == 1, seed
        assert len(h.attempts(children["afterany"])) == 1, seed
        if outcome == "COMPLETED":
            assert len(h.attempts(children["afterok"])) == 1, seed
            assert h.job(children["afternotok"])["phase"] == "BLOCKED", seed
        else:
            assert h.job(children["afterok"])["phase"] == "BLOCKED", seed
            assert len(h.attempts(children["afternotok"])) == 1, seed
        assert h.invariants() == [], seed
        h.close()
