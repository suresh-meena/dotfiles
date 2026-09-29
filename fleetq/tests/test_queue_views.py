"""`fq q` / `fq history` (squeue / sacct): what they show must match what the scheduler does."""

from __future__ import annotations

import pytest

from fleetq import auth
from fleetq.executors.fake import FakeNode

from harness import Harness
from test_api import _app, _auth, _client, _spec, run


@pytest.fixture()
def h(tmp_path):
    gpus = ["GPU-a", "GPU-b"]
    harness = Harness(tmp_path, [FakeNode("n1", gpus=gpus, run_ticks=50)])
    harness.add_node("n1", gpus=gpus, capacity={"job_slots": 8})
    (tmp_path / "bundles").mkdir()
    yield harness
    harness.close()


def _submit(c, h, key, **over):
    return c.post("/api/v1/jobs", json=_spec(**over), headers={**_auth(h.token), "Idempotency-Key": key})


def test_waiting_jobs_are_listed_in_the_order_the_scheduler_will_try_them(h):
    async def go():
        ctl, app = _app(h)
        async with _client(app) as c:
            running = [(await _submit(c, h, f"r{i}", name=f"run-{i}")).json()["jobs"][0] for i in range(2)]
            await ctl.startup()
            for _ in range(4):
                await h.ticks(1)
            assert all(h.job(j)["phase"] == "RUNNING" for j in running)
            low = (await _submit(c, h, "low", name="low", control={"priority": 1})).json()["jobs"][0]
            high = (await _submit(c, h, "high", name="high", control={"priority": 50})).json()["jobs"][0]
            held = (await _submit(c, h, "held", name="held", control={"hold": True})).json()["jobs"][0]
            await h.ticks(1)
            body = (await c.get("/api/v1/queue", headers=_auth(h.token))).json()
            ids = [j["id"] for j in body["jobs"]]
            assert ids[:2] == sorted(running), "running first"
            assert ids[2:4] == [high, low], "then waiting jobs in dispatch order"
            assert ids[4] == held
            rows = {j["id"]: j for j in body["jobs"]}
            assert rows[high]["position"] == 1 and rows[low]["position"] == 2 and rows[held]["position"] is None
            order, _ = h.store.run_sync(ctl.dispatch_view)
            assert [j for j in ids if rows[j]["position"]] == order, "the view is the scheduler's own order"
            r = rows[running[0]]
            assert r["st"] == "R" and r["where"] == "n1" and len(r["gpu_ids"]) == 1 and r["elapsed_s"] is not None
            assert rows[held]["st"] == "HD" and rows[high]["st"] == "PD"
            only = (await c.get("/api/v1/queue?state=PD&name=h*", headers=_auth(h.token))).json()["jobs"]
            assert [j["id"] for j in only] == [high]
            placed = (await c.get("/api/v1/queue?where=n1", headers=_auth(h.token))).json()["jobs"]
            assert sorted(j["id"] for j in placed) == sorted(running)
    run(go())


def test_other_users_jobs_need_manage_all(h):
    _, other = h.store.run_sync(lambda c: auth.create_token(c, owner="labmate", kind="human", label="theirs"))
    _, admin = h.store.run_sync(lambda c: auth.create_token(c, owner="suresh", kind="human", label="admin",
                                                            scopes=("read", "manage_all")))

    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            await c.post("/api/v1/jobs", json=_spec(control={"hold": True}),
                         headers={**_auth(other), "Idempotency-Key": "theirs"})
            mine = (await c.get("/api/v1/queue", headers=_auth(h.token))).json()["jobs"]
            assert mine == []
            assert (await c.get("/api/v1/queue?all_users=true", headers=_auth(h.token))).status_code == 403
            everyone = (await c.get("/api/v1/queue?all_users=true", headers=_auth(admin))).json()["jobs"]
            assert [j["owner"] for j in everyone] == ["labmate"]
    run(go())


def test_history_reports_wait_run_time_and_gpu_hours(h):
    h.fake.nodes["n1"].run_ticks = 2

    async def go():
        ctl, app = _app(h)
        async with _client(app) as c:
            ok = (await _submit(c, h, "ok", resources={"gpus": 2})).json()["jobs"][0]
            bad = (await _submit(c, h, "bad", resources={"gpus": 0})).json()["jobs"][0]
            await ctl.startup()
            for _ in range(12):
                await h.ticks(1)
            assert h.job(ok)["phase"] == "TERMINAL" and h.job(bad)["phase"] == "TERMINAL"
            rows = (await c.get("/api/v1/history", headers=_auth(h.token))).json()["jobs"]
            by_id = {r["id"]: r for r in rows}
            assert by_id[ok]["run_s"] >= 0 and by_id[ok]["wait_s"] is not None
            assert by_id[ok]["gpu_hours"] == round(2 * by_id[ok]["run_s"] / 3600, 4)
            summary = (await c.get("/api/v1/history?summary=true", headers=_auth(h.token))).json()["summary"]
            assert summary["total"]["jobs"] == 2 and summary["by_where"][0]["where"] == "n1"
            old = (await c.get("/api/v1/history?since_s=0", headers=_auth(h.token))).json()["jobs"]
            assert old == [], "nothing ended in the last zero seconds"
    run(go())


def test_a_read_all_dashboard_token_sees_everything_and_changes_nothing(h):
    _, other = h.store.run_sync(lambda c: auth.create_token(c, owner="labmate", kind="human", label="theirs"))
    _, dash = h.store.run_sync(lambda c: auth.create_token(c, owner="fleetmon", kind="service", label="fleetmon",
                                                           scopes=("read", "read_all")))

    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            jid = (await c.post("/api/v1/jobs", json=_spec(control={"hold": True}),
                                headers={**_auth(other), "Idempotency-Key": "t"})).json()["jobs"][0]
            everyone = (await c.get("/api/v1/queue?all_users=true", headers=_auth(dash))).json()["jobs"]
            assert [j["id"] for j in everyone] == [jid]
            assert (await c.get(f"/api/v1/jobs/{jid}", headers=_auth(dash))).status_code == 200
            assert (await c.get(f"/api/v1/jobs/{jid}/explain", headers=_auth(dash))).status_code == 200
            for action in ("cancel", "release", "hold", "top"):
                r = await c.post(f"/api/v1/jobs/{jid}/{action}", headers=_auth(dash))
                assert r.status_code in (403, 404), (action, r.status_code)
            assert h.job(jid)["phase"] == "HELD"
    run(go())
