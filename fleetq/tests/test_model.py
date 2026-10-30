"""Randomized model test (§13.2): random event interleavings keep every invariant.

Uses seeded stdlib ``random`` (hypothesis isn't available offline), so every
failure is reproducible from its seed. After every step it asserts:

* no GPU UUID has two unreleased reservations (invariant 1);
* no job has two possibly-live attempts (invariant 2);
* no attempt's payload was ever entered twice (invariant 3);
* no TERMINAL job keeps a possibly-live attempt;
* no RELEASED attempt still holds reservations.
"""

from __future__ import annotations

import asyncio
import os
import random

import pytest

from fleetq.engine import state
from fake_executor import FakeNode

from harness import Harness

SEEDS = int(os.environ.get("FQ_MODEL_SEEDS", "40"))
STEPS = int(os.environ.get("FQ_MODEL_STEPS", "60"))
ACTIVE = {"DISPATCHING", "RUNNING", "SUBMITTED", "SUBMISSION_UNKNOWN",
          "RECONCILING", "CANCELLING", "FINALIZING"}


def _attempts_count(h: Harness) -> int:
    return h.store.run_sync(lambda c: c.execute("SELECT count(*) FROM attempts").fetchone()[0])


def _check(h: Harness, seed: int, step: int, action: str) -> None:
    problems = h.invariants()
    assert not problems, f"seed={seed} step={step} after {action}: {problems}"
    doubled = {a: n for a, n in h.fake.entries().items() if n > 1}
    assert not doubled, f"seed={seed} step={step} after {action}: payload entered twice {doubled}"


@pytest.mark.model
@pytest.mark.parametrize("seed", range(SEEDS))
def test_random_interleavings_preserve_invariants(tmp_path, seed):
    rng = random.Random(seed)
    nodes = [FakeNode("n1", gpus=["GPU-a", "GPU-b"], run_ticks=rng.randint(1, 4)),
             FakeNode("n2", gpus=["GPU-x"], run_ticks=rng.randint(1, 4))]
    h = Harness(tmp_path, nodes)
    jobs: list[int] = []
    counter = 0
    clock = [True]

    async def scenario():
        nonlocal counter
        ctl = h.start()
        ctl.clock_ok = lambda: clock[0]
        await ctl.startup()
        for step in range(STEPS):
            action = rng.choice([
                "submit", "submit", "submit", "tick", "tick", "tick", "tick", "cancel", "hold", "release",
                "partition", "heal", "reboot", "lose_responses", "refuse", "unrefuse", "restart",
                "clock_lost", "clock_recover",
            ])
            attempts_before = _attempts_count(h) if not clock[0] else None
            if action == "submit":
                counter += 1
                target = rng.choice([["n1"], ["n2"], ["n1", "n2"]])
                gpus = rng.choice([0, 1, 1, 2])
                try:
                    resp = h.submit(key=f"k{counter}", placement={"on": target},
                                    resources={"gpus": gpus}, control={"retry": rng.choice([0, 1, 2])})
                    jobs.extend(resp["jobs"])
                except Exception as exc:
                    assert getattr(exc, "code", "") in ("unsatisfiable",), exc
            elif action == "tick":
                await h.ticks(rng.randint(1, 3))
            elif action in ("cancel", "hold", "release") and jobs:
                jid = rng.choice(jobs)

                def mutate(conn, jid=jid, action=action):
                    job = state.get_job(conn, jid)
                    if job["phase"] == "TERMINAL":
                        return
                    if action == "cancel":
                        state.update_job(conn, jid, event="cancel", actor="model", desired_state="CANCEL",
                                         cancel_requested=1)
                    elif action == "hold" and job["phase"] == "PENDING":
                        state.update_job(conn, jid, event="hold", actor="model", desired_state="HOLD", phase="HELD")
                    elif action == "release" and job["phase"] == "HELD":
                        state.update_job(conn, jid, event="release", actor="model", desired_state="RUN", phase="PENDING")
                h.store.run_sync(mutate)
            elif action == "partition":
                h.fake.node(rng.choice(["n1", "n2"])).reachable = False
            elif action == "heal":
                for node in h.fake.nodes.values():
                    node.reachable = True
            elif action == "reboot":
                h.fake.reboot(rng.choice(["n1", "n2"]))
            elif action == "lose_responses":
                h.fake.node(rng.choice(["n1", "n2"])).lose_launch_response = rng.random() < 0.5
            elif action == "refuse":
                node = h.fake.node("n1")
                node.refuse_gpus = {rng.choice(node.gpus)}
            elif action == "unrefuse":
                h.fake.node("n1").refuse_gpus = set()
            elif action == "restart":
                # Crash: drop the controller mid-flight and start a new one on the same DB.
                await ctl.drain()
                ctl = h.start()
                ctl.clock_ok = lambda: clock[0]
                await ctl.startup()
            elif action == "clock_lost":
                clock[0] = False
                before = _attempts_count(h)
                await h.ticks()
                assert _attempts_count(h) == before, (
                    f"seed={seed} step={step}: unhealthy clock allowed a new attempt"
                )
            elif action == "clock_recover":
                clock[0] = True
                await h.ticks()
            _check(h, seed, step, action)
            if attempts_before is not None and not clock[0]:
                assert _attempts_count(h) == attempts_before, (
                    f"seed={seed} step={step}: unhealthy clock allowed a new attempt after {action}"
                )

        # Let everything settle with a healthy fleet, then check liveness too.
        for node in h.fake.nodes.values():
            node.reachable = True
            node.lose_launch_response = False
            node.refuse_gpus = set()
        clock[0] = True
        await h.ticks(40)
        _check(h, seed, STEPS, "settle")

    asyncio.run(scenario())
    # Liveness: with a healthy fleet, nothing should be stuck mid-dispatch.
    stuck = h.store.run_sync(lambda c: c.execute(
        "SELECT id, phase, reason FROM jobs WHERE phase IN ('DISPATCHING','CANCELLING')").fetchall())
    assert not stuck, f"seed={seed}: jobs stuck after settling: {[dict(r) for r in stuck]}"
    h.close()



def test_seeded_clock_cancel_and_dependency_interleavings(tmp_path):
    """Every action preserves scheduler invariants and all gates eventually settle."""
    for seed in (13, 37, 83):
        rng = random.Random(seed)
        case_dir = tmp_path / str(seed)
        case_dir.mkdir()
        node = FakeNode("n1", gpus=["GPU-a", "GPU-b"], run_ticks=4)
        h = Harness(case_dir, [node])

        parent = None
        dependent = {}
        array = []
        cancelled = None
        clock = [True]

        def check(step: int, action: str) -> None:
            problems = h.invariants()
            assert not problems, (seed, step, action, problems)
            phases = [h.job(j)["phase"] for j in array]
            active = sum(phase in ACTIVE for phase in phases)
            assert active <= 2, (seed, step, action, phases)
            parent_row = h.job(parent)
            for kind, jid in dependent.items():
                started = bool(h.attempts(jid))
                if kind == "after":
                    gate = any(h.fake.entries().get(a["id"], 0) for a in h.attempts(parent))
                elif kind == "afterany":
                    gate = parent_row["phase"] == "TERMINAL"
                elif kind == "afterok":
                    gate = parent_row["phase"] == "TERMINAL" and parent_row["success"] == 1
                else:
                    gate = parent_row["phase"] == "TERMINAL" and parent_row["success"] == 0
                assert not started or gate, (seed, step, action, kind, dict(parent_row))

        async def scenario():
            ctl = h.start()
            ctl.clock_ok = lambda: clock[0]
            await ctl.startup()
            nonlocal parent, dependent, array, cancelled
            parent = h.submit(key="parent", resources={"gpus": 0})["jobs"][0]
            dependent = {
                kind: h.submit(key=f"dependent-{kind}", resources={"gpus": 0},
                               control={"after": [{"job": parent, "type": kind}]})["jobs"][0]
            for kind in ("after", "afterok", "afterany", "afternotok")
            }
            array = h.submit(key="array", control={"array": {"indices": [1, 3, 8, 13], "throttle": 2}})["jobs"]
            cancelled = array[rng.randrange(2)]
            for step in range(24):
                action = ("tick", "cancel", "clock_lost", "tick", "clock_recover")[step] if step < 5 else rng.choice(
                    ("tick", "tick", "clock_lost", "clock_recover", "tick"))
                if action == "clock_lost":
                    clock[0] = False
                    before = _attempts_count(h)
                    await h.ticks()
                    assert _attempts_count(h) == before, (seed, step, "clock_lost")
                elif action == "clock_recover":
                    clock[0] = True
                    await h.ticks()
                elif action == "cancel":
                    if h.job(cancelled)["phase"] != "TERMINAL":
                        h.store.run_sync(lambda c: state.update_job(
                            c, cancelled, event="cancel_requested", actor="p5-model",
                            desired_state="CANCEL", cancel_requested=1))
                    await h.ticks()
                else:
                    before = _attempts_count(h) if not clock[0] else None
                    await h.ticks(rng.randint(1, 2))
                    if before is not None:
                        assert _attempts_count(h) == before, (seed, step, "clock_unhealthy_tick")
                check(step, action)

            clock[0] = True
            await h.ticks(40)
            check(24, "settle")

        asyncio.run(scenario())
        assert h.job(parent)["phase"] == "TERMINAL", seed
        assert h.job(dependent["after"])["phase"] == "TERMINAL", seed
        assert h.job(dependent["afterok"])["phase"] == "TERMINAL", seed
        assert h.job(dependent["afterany"])["phase"] == "TERMINAL", seed
        assert h.job(dependent["afternotok"])["phase"] == "BLOCKED", seed
        assert h.job(cancelled)["execution_outcome"] == "CANCELLED", seed
        assert all(h.job(j)["phase"] == "TERMINAL" for j in array), seed
        assert h.invariants() == [], seed
        h.close()



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
