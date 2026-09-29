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
from fleetq.executors.fake import FakeNode

from harness import Harness

SEEDS = int(os.environ.get("FQ_MODEL_SEEDS", "40"))
STEPS = int(os.environ.get("FQ_MODEL_STEPS", "60"))


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

    def attempts_count() -> int:
        return h.store.run_sync(lambda c: c.execute("SELECT count(*) FROM attempts").fetchone()[0])

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
            attempts_before = attempts_count() if not clock[0] else None
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
                before = attempts_count()
                await h.ticks()
                assert attempts_count() == before, (
                    f"seed={seed} step={step}: unhealthy clock allowed a new attempt"
                )
            elif action == "clock_recover":
                clock[0] = True
                await h.ticks()
            _check(h, seed, step, action)
            if attempts_before is not None and not clock[0]:
                assert attempts_count() == attempts_before, (
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
