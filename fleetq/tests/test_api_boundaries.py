"""API limits and authorization across a sleeping wait."""

from __future__ import annotations

import asyncio
import json

import httpx

from fleetq import auth, bundles
from fleetq.api import app as api_app
from fleetq.api.app import Runtime, create_app
from fleetq.executors.fake import FakeNode

from harness import Harness


def _spec():
    return {"name": "held", "command": {"argv": ["python", "x.py"]},
            "workdir": {"in_place": "/data/p"}, "placement": {"on": ["n1"]},
            "resources": {"gpus": 0}, "control": {"hold": True}}


def test_revoked_token_cannot_receive_wait_result(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=[])])
    try:
        ctl = h.start()
        rt = Runtime(store=h.store, controller=ctl, bundle_dir=tmp_path / "bundles",
                     bundle_limits=bundles.BundleLimits(), clock_ok=lambda: True)

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)),
                                         base_url="http://fq") as client:
                headers = {"Authorization": f"Bearer {h.token}", "Idempotency-Key": "wait-revoke"}
                submitted = await client.post("/api/v1/jobs", json=_spec(), headers=headers)
                assert submitted.status_code == 201
                job_id = submitted.json()["jobs"][0]
                waiter = asyncio.create_task(client.get(
                    f"/api/v1/jobs/{job_id}/wait?timeout=2", headers=headers))
                for _ in range(100):
                    if rt.waiters:
                        break
                    await asyncio.sleep(0.001)
                assert rt.waiters == 1
                h.store.run_sync(lambda c: auth.revoke_token(c, h.token_id))
                ctl.hub.publish()
                response = await asyncio.wait_for(waiter, 2)
                assert response.status_code == 401
                assert response.json()["error"]["code"] == "unauthorized"
                assert rt.waiters == 0

        asyncio.run(go())
    finally:
        h.close()


def test_json_body_is_bounded_before_parsing_and_rejects_deep_or_nonobject_input(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=[])])
    try:
        rt = Runtime(store=h.store, controller=h.start(), bundle_dir=tmp_path / "bundles",
                     bundle_limits=bundles.BundleLimits(), clock_ok=lambda: True)

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)),
                                         base_url="http://fq") as client:
                headers = {"Authorization": f"Bearer {h.token}", "Idempotency-Key": "body-cap"}
                nested = {}
                for _ in range(api_app.MAX_JSON_DEPTH + 1):
                    nested = {"x": nested}
                bodies = [json.dumps({"padding": "x" * api_app.MAX_JSON_BODY}).encode(),
                          json.dumps(nested).encode(), b"[]"]
                for body in bodies:
                    response = await client.post("/api/v1/jobs", content=body, headers=headers)
                    assert response.status_code == 400
                    assert response.json()["error"]["code"] == "invalid_argument"

        asyncio.run(go())
    finally:
        h.close()


def test_jobs_page_limit_is_clamped(tmp_path, monkeypatch):
    h = Harness(tmp_path, [FakeNode("n1", gpus=[])])
    try:
        rt = Runtime(store=h.store, controller=h.start(), bundle_dir=tmp_path / "bundles",
                     bundle_limits=bundles.BundleLimits(), clock_ok=lambda: True)
        monkeypatch.setattr(api_app, "PAGE_MAX", 2)

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)),
                                         base_url="http://fq") as client:
                for i in range(3):
                    response = await client.post("/api/v1/jobs", json=_spec(), headers={
                        "Authorization": f"Bearer {h.token}", "Idempotency-Key": f"page-{i}"})
                    assert response.status_code == 201
                response = await client.get("/api/v1/jobs?limit=10000", headers={
                    "Authorization": f"Bearer {h.token}"})
                assert response.status_code == 200
                assert len(response.json()["jobs"]) == 2
                assert response.json()["next_before"] is not None

        asyncio.run(go())
    finally:
        h.close()
