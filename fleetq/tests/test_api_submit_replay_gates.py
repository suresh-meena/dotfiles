"""A committed submit can be recovered while new submits are gated."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from fleetq.api.app import Runtime, create_app
from fleetq.bundles import BundleLimits
from fleetq.executors.fake import FakeNode
from fleetq.errors import FqError
from fleetq.engine import admission, fence

from harness import Harness


@pytest.fixture()
def h(tmp_path):
    harness = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"])])
    (tmp_path / "bundles").mkdir()
    yield harness
    harness.close()


def _spec(gpus: int = 0):
    return {"name": "replay", "command": {"argv": ["true"]},
            "workdir": {"in_place": "/data/p"}, "placement": {"on": ["n1"]},
            "resources": {"gpus": gpus}}


def _auth(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_replay_works_while_accept_is_off_and_conflicts_still_fail(h):
    rt = Runtime(store=h.store, controller=h.start(), bundle_dir=h.tmp / "bundles",
                 bundle_limits=BundleLimits(), clock_ok=lambda: True)
    app = create_app(rt)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://fq") as c:
            headers = {**_auth(h.token), "Idempotency-Key": "committed"}
            first = await c.post("/api/v1/jobs", json=_spec(), headers=headers)
            assert first.status_code == 201
            h.store.run_sync(lambda db: db.execute(
                "INSERT INTO controller_meta (key, value) VALUES ('accept', 'off')"
                " ON CONFLICT(key) DO UPDATE SET value='off'"))

            replay = await c.post("/api/v1/jobs", json=_spec(), headers=headers)
            assert replay.status_code == 200
            assert replay.json()["jobs"] == first.json()["jobs"]
            changed = await c.post("/api/v1/jobs", json=_spec(gpus=1), headers=headers)
            assert changed.status_code == 409
            assert changed.json()["error"]["code"] == "idempotency_conflict"
            fresh = await c.post("/api/v1/jobs", json=_spec(),
                                 headers={**_auth(h.token), "Idempotency-Key": "new"})
            assert fresh.status_code == 503 and fresh.json()["error"]["code"] == "draining"

    asyncio.run(go())


def test_replay_does_not_consume_or_need_submit_rate_limit(h):
    rt = Runtime(store=h.store, controller=h.start(), bundle_dir=h.tmp / "bundles",
                 bundle_limits=BundleLimits(), clock_ok=lambda: True)
    app = create_app(rt)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://fq") as c:
            headers = {**_auth(h.agent_token), "Idempotency-Key": "committed"}
            first = await c.post("/api/v1/jobs", json=_spec(), headers=headers)
            assert first.status_code == 201
            rt.submit_times[h.agent_id] = [time.monotonic()] * 10

            replay = await c.post("/api/v1/jobs", json=_spec(), headers=headers)
            assert replay.status_code == 200
            assert replay.json()["jobs"] == first.json()["jobs"]
            fresh = await c.post("/api/v1/jobs", json=_spec(),
                                 headers={**_auth(h.agent_token), "Idempotency-Key": "new"})
            assert fresh.status_code == 429 and fresh.json()["error"]["code"] == "rate_limited"

    asyncio.run(go())


def test_admission_checks_accept_in_its_own_transaction(h):
    first = h.submit(key="committed", resources={"gpus": 0})["jobs"]
    h.store.run_sync(lambda db: db.execute(
        "INSERT INTO controller_meta (key, value) VALUES ('accept', 'off')"
        " ON CONFLICT(key) DO UPDATE SET value='off'"))
    assert h.submit(key="committed", resources={"gpus": 0})["jobs"] == first
    with pytest.raises(FqError) as exc:
        h.submit(key="new", resources={"gpus": 0})
    assert exc.value.code == "draining"


def test_restore_discovery_gates_new_submit_but_allows_existing_replay(h):
    first = h.submit(key="committed", resources={"gpus": 0})
    h.store.run_sync(lambda db: fence.start_controller(db, restored_from_backup=True))
    with pytest.raises(FqError) as exc:
        h.submit(key="new", resources={"gpus": 0})
    assert exc.value.code == "not_ready"
    assert h.submit(key="committed", resources={"gpus": 0})["jobs"] == first["jobs"]
    assert h.submit(key="committed", resources={"gpus": 0})["idempotent_replay"] is True


def test_requeue_is_gated_during_restore_discovery(h):
    job_id = h.submit(key="finished", resources={"gpus": 0})["jobs"][0]
    h.store.run_sync(lambda db: db.execute(
        "UPDATE jobs SET phase='TERMINAL', execution_outcome='COMPLETED', success=1 WHERE id=?", (job_id,)))
    h.store.run_sync(lambda db: fence.start_controller(db, restored_from_backup=True))
    principal = h.principal()
    with pytest.raises(FqError) as exc:
        h.store.run_sync(lambda db: admission.requeue_job(
            db, principal, job_id, actor="test"))
    assert exc.value.code == "not_ready"
