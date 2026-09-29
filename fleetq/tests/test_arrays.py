"""Arrays (§4.5): every member knows its index, and a throttle caps how many run at once."""

from __future__ import annotations

import asyncio

from fleetq.executors.fake import FakeNode

from harness import Harness

ACTIVE = ("DISPATCHING", "RUNNING", "SUBMITTED", "SUBMISSION_UNKNOWN", "RECONCILING", "CANCELLING", "FINALIZING")


def test_the_throttle_caps_simultaneous_members_until_all_finish(tmp_path):
    gpus = [f"GPU-{i}" for i in range(8)]
    h = Harness(tmp_path, [FakeNode("n1", gpus=gpus, run_ticks=3)])
    h.add_node("n1", gpus=gpus, capacity={"job_slots": 8})
    resp = h.submit(key="arr", control={"array": {"indices": list(range(7)), "throttle": 2}})
    jobs = resp["jobs"]
    assert len(jobs) == 7
    peak = 0

    async def go():
        nonlocal peak
        ctl = h.start()
        await ctl.startup()
        for _ in range(80):
            await h.ticks(1)
            phases = [h.job(j)["phase"] for j in jobs]
            peak = max(peak, sum(p in ACTIVE for p in phases))
            if all(p == "TERMINAL" for p in phases):
                break
    asyncio.run(go())
    assert all(h.job(j)["phase"] == "TERMINAL" for j in jobs)
    assert peak == 2, "capacity allowed 7 at once; the throttle held it to 2"
    assert h.invariants() == []
    h.close()


def test_an_unthrottled_array_and_its_indices(tmp_path):
    gpus = [f"GPU-{i}" for i in range(4)]
    h = Harness(tmp_path, [FakeNode("n1", gpus=gpus, run_ticks=2)])
    h.add_node("n1", gpus=gpus, capacity={"job_slots": 4})
    resp = h.submit(key="arr2", control={"array": {"indices": [3, 5, 9]}})
    assert sorted(h.job(j)["array_index"] for j in resp["jobs"]) == [3, 5, 9]
    contexts = []

    async def go():
        ctl = h.start()
        real = ctl._context
        ctl._context = lambda conn, att: (contexts.append(c := real(conn, att)) or c)
        await ctl.startup()
        await h.ticks(10)
    asyncio.run(go())
    assert sorted({c.attempt_id: c.array_index for c in contexts}.values()) == [3, 5, 9], "every attempt carries its own index"
    h.close()


def test_a_job_can_wait_for_a_whole_array(tmp_path):
    gpus = [f"GPU-{i}" for i in range(4)]
    h = Harness(tmp_path, [FakeNode("n1", gpus=gpus, run_ticks=2)])
    h.add_node("n1", gpus=gpus, capacity={"job_slots": 4})
    arr = h.submit(key="arr", control={"array": {"indices": [0, 1, 2], "throttle": 1}})
    group = arr["group"]["id"]
    after = h.submit(key="reduce", control={"after": [{"group": group, "type": "afterok"}]})["jobs"][0]
    edges = h.store.run_sync(lambda c: c.execute("SELECT COUNT(*) FROM deps WHERE job_id = ?", (after,)).fetchone()[0])
    assert edges == 3

    async def go():
        ctl = h.start()
        await ctl.startup()
        for _ in range(60):
            await h.ticks(1)
            members = [h.job(j) for j in arr["jobs"]]
            if not all(m["phase"] == "TERMINAL" for m in members):
                assert not h.attempts(after), "the reducer never starts before every member has finished"
            if h.job(after)["phase"] == "TERMINAL":
                break
    asyncio.run(go())
    assert h.job(after)["phase"] == "TERMINAL" and h.job(after)["success"] == 1
    h.close()


def test_group_dependencies_are_checked_at_submission(tmp_path):
    import pytest
    from fleetq import auth
    from fleetq.errors import FqError
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"])])
    with pytest.raises(FqError) as exc:
        h.submit(key="x", control={"after": [{"group": "grp_0000000000000000"}]})
    assert "does not exist" in exc.value.message
    with pytest.raises(FqError) as exc:
        h.submit(key="y", control={"after": [{"group": "not-a-group"}]})
    assert "grp_" in exc.value.message
    with pytest.raises(FqError, match="does not exist or is not yours"):
        h.submit(key="missing-parent", control={"after": [{"job": 999999}]})
    arr = h.submit(key="arr", control={"array": {"indices": [0, 1]}})
    _, other = h.store.run_sync(lambda c: auth.create_token(c, owner="labmate", kind="human", label="theirs"))
    foreign_parent = h.submit(key="foreign-parent", token=other)["jobs"][0]
    with pytest.raises(FqError, match="does not exist or is not yours"):
        h.submit(key="foreign-job", control={"after": [{"job": foreign_parent}]})
    with pytest.raises(FqError) as exc:
        h.submit(key="z", token=other, control={"after": [{"group": arr["group"]["id"]}]})
    assert "not yours" in exc.value.message, "another owner's group is not a dependency target"
    h.close()


def test_failed_fanout_quota_admission_leaves_no_partial_group(tmp_path):
    """A fan-out is one admission transaction, including its quota check."""
    from fleetq import auth
    from fleetq.errors import FqError
    import pytest

    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"]), FakeNode("n2", gpus=["GPU-b"])])
    _, limited = h.store.run_sync(lambda c: auth.create_token(
        c, owner="suresh", kind="human", label="limited", quota={"active_jobs": 2, "group_size": 5}))
    existing = h.submit(key="existing", token=limited)["jobs"][0]

    with pytest.raises(FqError) as exc:
        h.submit(key="fanout", token=limited, placement={"each": ["n1", "n2"]})
    assert exc.value.code == "quota_exceeded"
    assert h.store.run_sync(lambda c: c.execute(
        "SELECT COUNT(*) FROM jobs WHERE token_id = (SELECT id FROM tokens WHERE label='limited')").fetchone()[0]) == 1
    assert h.store.run_sync(lambda c: c.execute(
        "SELECT COUNT(*) FROM groups WHERE owner='suresh'").fetchone()[0]) == 0
    assert h.store.run_sync(lambda c: c.execute(
        "SELECT COUNT(*) FROM idempotency WHERE key='fanout'").fetchone()[0]) == 0
    assert h.job(existing)["phase"] == "PENDING"
    h.close()


def test_cycle_in_durable_dependency_graph_never_dispatches(tmp_path):
    """Defend against a corrupt/legacy cycle even though new submits are forward-only."""
    h = Harness(tmp_path)
    a = h.submit(key="a")["jobs"][0]
    b = h.submit(key="b")["jobs"][0]
    h.store.run_sync(lambda c: c.executemany(
        "INSERT INTO deps (job_id, parent_id, type) VALUES (?,?, 'afterok')", [(a, b), (b, a)]))

    ctl = h.start()
    assert h.store.run_sync(ctl._eligible_jobs) == []
    assert h.job(a)["phase"] == h.job(b)["phase"] == "PENDING"
    assert h.attempts(a) == h.attempts(b) == []
    assert h.fake.entries() == {}
    h.close()


def test_afterany_does_not_cross_an_unresolved_cancel_parent(tmp_path):
    """A requested cancel is not terminal evidence for dependent work."""
    from fleetq.engine.controller import dependency_verdict
    from fleetq.engine import state

    h = Harness(tmp_path)
    parent = h.submit(key="parent")["jobs"][0]
    child = h.submit(key="child",
                     control={"after": [{"job": parent, "type": "afterany"}]})["jobs"][0]

    def check(conn):
        state.update_job(conn, parent, event="dispatch", actor="test", phase="DISPATCHING")
        state.update_job(conn, parent, event="cancel", actor="test", phase="CANCELLING",
                         desired_state="CANCEL", cancel_requested=1)
        assert dependency_verdict(conn, child) == "wait"
        state.update_job(conn, parent, event="cancel_confirmed", actor="test", phase="TERMINAL",
                         execution_outcome="CANCELLED", success=0)
        assert dependency_verdict(conn, child) == "ok"
    h.store.run_sync(check)
    h.close()


def test_held_array_member_does_not_use_a_throttle_slot(tmp_path):
    """A held member stays out of the throttle count; releasing it joins the queue."""
    from fleetq.engine import state

    gpus = [f"GPU-{i}" for i in range(2)]
    h = Harness(tmp_path, [FakeNode("n1", gpus=gpus, run_ticks=2)])
    h.add_node("n1", gpus=gpus, capacity={"job_slots": 2})
    jobs = h.submit(key="held-array", control={"array": {"indices": [0, 1, 2], "throttle": 1}})["jobs"]
    held = jobs[0]
    h.store.run_sync(lambda c: state.update_job(c, held, event="hold", actor="test",
                                               phase="HELD", desired_state="HOLD"))

    async def go():
        ctl = h.start()
        await ctl.startup()
        for _ in range(20):
            await h.ticks(1)
        assert h.job(held)["phase"] == "HELD"
        assert all(h.job(j)["phase"] == "TERMINAL" for j in jobs[1:])
        assert h.attempts(held) == []
        h.store.run_sync(lambda c: state.update_job(c, held, event="release", actor="test",
                                                   phase="PENDING", desired_state="RUN"))
        for _ in range(20):
            await h.ticks(1)
            if h.job(held)["phase"] == "TERMINAL":
                break

    asyncio.run(go())
    assert h.job(held)["phase"] == "TERMINAL"
    assert h.job(held)["success"] == 1
    assert h.invariants() == []
    h.close()
