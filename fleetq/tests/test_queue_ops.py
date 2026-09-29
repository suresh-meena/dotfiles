"""Manipulating the queue (§4.2): modify, top, requeue, begin, and what stays refused."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from fleetq import auth
from fleetq.engine import state
from fleetq.executors.fake import FakeNode
from fleetq.util import utcnow

from harness import Harness
from test_api import _app, _auth, _client, _spec, run


@pytest.fixture()
def h(tmp_path):
    harness = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a", "GPU-b"], run_ticks=2)])
    (tmp_path / "bundles").mkdir()
    yield harness
    harness.close()


def _submit(c, h, key, **over):
    return c.post("/api/v1/jobs", json=_spec(**over), headers={**_auth(h.token), "Idempotency-Key": key})


def test_modify_changes_a_waiting_job_and_revalidates_it(h):
    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            jid = (await _submit(c, h, "m1", control={"hold": True})).json()["jobs"][0]
            v = (await c.get(f"/api/v1/jobs/{jid}", headers=_auth(h.token))).json()["job"]["version"]
            r = await c.patch(f"/api/v1/jobs/{jid}", json={"resources": {"gpus": 2, "mem_mb": 8000},
                                                           "control": {"priority": 7}, "name": "bigger"},
                              headers={**_auth(h.token), "If-Match": str(v)})
            assert r.status_code == 200, r.text
            spec = h.store.run_sync(lambda conn: state.get_job(conn, jid))
            assert spec["name"] == "bigger" and spec["priority"] == 7
            assert '"gpus":2' in spec["spec_json"] and '"mem_mb":8000' in spec["spec_json"]
            stale = await c.patch(f"/api/v1/jobs/{jid}", json={"control": {"priority": 1}},
                                  headers={**_auth(h.token), "If-Match": str(v)})
            assert stale.status_code == 409
            # What the job *is* can't change, and nothing that could never run is accepted.
            for bad in ({"command": {"argv": ["rm", "-rf", "/"]}}, {"workdir": {"in_place": "/tmp"}},
                        {"control": {"after": []}}, {"resources": {"gpus": 3, "made_up": 1}}):
                r = await c.patch(f"/api/v1/jobs/{jid}", json=bad, headers=_auth(h.token))
                assert r.status_code in (400, 409, 422), (bad, r.text)
            never = await c.patch(f"/api/v1/jobs/{jid}", json={"resources": {"gpus": 9}}, headers=_auth(h.token))
            assert never.status_code == 422 and never.json()["error"]["code"] == "unsatisfiable"
            assert '"gpus":2' in h.store.run_sync(lambda conn: state.get_job(conn, jid))["spec_json"]
    run(go())


def test_modify_is_refused_once_the_job_has_started(h):
    async def go():
        ctl, app = _app(h)
        async with _client(app) as c:
            jid = (await _submit(c, h, "m2")).json()["jobs"][0]
            await ctl.startup()
            await h.ticks(2)
            assert h.attempts(jid)
            r = await c.patch(f"/api/v1/jobs/{jid}", json={"resources": {"gpus": 2}}, headers=_auth(h.token))
            assert r.status_code == 409 and r.json()["error"]["code"] == "not_modifiable"
    run(go())


def test_a_blocked_job_that_is_fixed_goes_back_to_pending(h):
    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            jid = (await _submit(c, h, "m3")).json()["jobs"][0]
            h.store.run_sync(lambda conn: state.update_job(conn, jid, event="blocked", actor="test",
                                                           phase="BLOCKED", reason="sbatch_rejected"))
            r = await c.patch(f"/api/v1/jobs/{jid}", json={"resources": {"mem_mb": 2000}}, headers=_auth(h.token))
            assert r.status_code == 200 and r.json()["job"]["phase"] == "PENDING"
    run(go())


def test_top_moves_a_job_ahead_of_the_owners_other_waiting_jobs(h):
    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            ids = [(await _submit(c, h, f"t{i}", control={"hold": True, "priority": p})).json()["jobs"][0]
                   for i, p in enumerate((5, 40, 0))]
            r = await c.post(f"/api/v1/jobs/{ids[2]}/top", headers=_auth(h.token))
            assert r.status_code == 200
            prio = {j: h.store.run_sync(lambda conn, j=j: state.get_job(conn, j))["priority"] for j in ids}
            assert prio[ids[2]] == 41 and prio[ids[2]] > max(prio[ids[0]], prio[ids[1]])
    run(go())


def test_requeue_runs_a_finished_job_again_as_a_new_one(h):
    async def go():
        ctl, app = _app(h)
        async with _client(app) as c:
            jid = (await _submit(c, h, "r1")).json()["jobs"][0]
            early = await c.post(f"/api/v1/jobs/{jid}/requeue", headers=_auth(h.token))
            assert early.status_code == 409, "only a finished job can be requeued"
            await ctl.startup()
            for _ in range(10):
                await h.ticks(1)
            assert h.job(jid)["phase"] == "TERMINAL"
            r = await c.post(f"/api/v1/jobs/{jid}/requeue", headers=_auth(h.token))
            assert r.status_code == 200, r.text
            new = r.json()["job"]["id"]
            assert new != jid and r.json()["requeue_of"] == jid
            assert h.job(jid)["phase"] == "TERMINAL", "the original's history is untouched"
            assert h.job(new)["spec_json"] == h.job(jid)["spec_json"] and h.job(new)["phase"] == "PENDING"
            for _ in range(10):
                await h.ticks(1)
            assert h.job(new)["phase"] == "TERMINAL" and h.job(new)["success"] == 1
            _, reader = h.store.run_sync(lambda conn: auth.create_token(conn, owner="suresh", kind="service",
                                                                         label="ro", scopes=("read", "manage_own")))
            ro = await c.post(f"/api/v1/jobs/{jid}/requeue", headers=_auth(reader))
            assert ro.status_code == 403, "requeue is a submission"
    run(go())


def test_begin_holds_a_job_until_its_time(h):
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1.0)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    async def go():
        ctl, app = _app(h)
        async with _client(app) as c:
            jid = (await _submit(c, h, "b1", control={"begin": soon})).json()["jobs"][0]
            assert h.job(jid)["not_before"] == soon
            await ctl.startup()
            await h.ticks(3)
            assert not h.attempts(jid) and utcnow() < soon
            await asyncio.sleep(1.1)
            await h.ticks(3)
            assert h.attempts(jid), "dispatched once its time came"
            bad = await _submit(c, h, "b2", control={"begin": "tomorrow"})
            assert bad.status_code == 400
    run(go())


def test_unknown_control_fields_are_refused_not_ignored(h):
    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            r = await _submit(c, h, "u1", control={"retyr": 3})
            assert r.status_code == 400 and "retyr" in r.text
    run(go())
