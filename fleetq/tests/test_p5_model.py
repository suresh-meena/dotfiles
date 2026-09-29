"""Small seeded P5 state-machine checks for dependency, array and clock gates."""

from __future__ import annotations

import asyncio
import random

from fleetq.engine import state
from fleetq.executors.fake import FakeNode

from harness import Harness

ACTIVE = {"DISPATCHING", "RUNNING", "SUBMITTED", "SUBMISSION_UNKNOWN",
          "RECONCILING", "CANCELLING", "FINALIZING"}


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

        def attempts_count():
            return h.store.run_sync(lambda c: c.execute("SELECT count(*) FROM attempts").fetchone()[0])

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
                    before = attempts_count()
                    await h.ticks()
                    assert attempts_count() == before, (seed, step, "clock_lost")
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
                    before = attempts_count() if not clock[0] else None
                    await h.ticks(rng.randint(1, 2))
                    if before is not None:
                        assert attempts_count() == before, (seed, step, "clock_unhealthy_tick")
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
