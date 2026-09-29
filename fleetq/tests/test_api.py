"""HTTP API behaviour (§6), driven in-loop against a live controller."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fleetq import auth, bundles
from fleetq.api.app import Runtime, create_app
from fleetq.executors.fake import FakeNode
from fleetq.util import utcnow

from harness import Harness


@pytest.fixture()
def h(tmp_path):
    harness = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a", "GPU-b"], run_ticks=2)])
    (tmp_path / "bundles").mkdir()
    yield harness
    harness.close()


def _app(h: Harness):
    ctl = h.start()
    rt = Runtime(store=h.store, controller=ctl, bundle_dir=h.tmp / "bundles", bundle_limits=bundles.BundleLimits(), clock_ok=lambda: True)
    return ctl, create_app(rt)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fq")


def _spec(**over):
    spec = {"name": "t", "command": {"argv": ["python", "t.py"]}, "workdir": {"in_place": "/data/p"},
            "placement": {"on": ["n1"]}, "resources": {"gpus": 1}}
    spec.update(over)
    return spec


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def run(coro):
    return asyncio.run(coro)


def test_requires_a_valid_token(h):
    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            assert (await c.get("/api/v1/whoami")).status_code == 401
            assert (await c.get("/api/v1/whoami", headers=_auth("fq_nope"))).status_code == 401
            bad = "fq_" + "0" * 16 + "_" + "A" * 43
            r = await c.get("/api/v1/whoami", headers=_auth(bad))
            assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
            ok = await c.get("/api/v1/whoami", headers=_auth(h.token))
            assert ok.status_code == 200 and ok.json()["owner"] == "suresh"
    run(go())


def test_readyz_reports_unhealthy_clock_without_overriding_controller_readiness(h):
    ctl = h.start()
    rt = Runtime(store=h.store, controller=ctl, bundle_dir=h.tmp / "bundles",
                 bundle_limits=bundles.BundleLimits(), clock_ok=lambda: False)
    app = create_app(rt)
    healthy_app = create_app(Runtime(store=h.store, controller=ctl, bundle_dir=h.tmp / "bundles",
                                     bundle_limits=bundles.BundleLimits(), clock_ok=lambda: True))

    async def go():
        await ctl.startup()
        await ctl.tick()
        async with _client(app) as c:
            response = await c.get("/readyz")
        async with _client(healthy_app) as c:
            healthy_response = await c.get("/readyz")
        body = response.json()
        assert body["ok"] == healthy_response.json()["ok"]
        assert body["clock_healthy"] is False
        assert body["dispatch_ready"] is False

    run(go())


def test_submit_needs_idempotency_key_and_replays(h):
    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            r = await c.post("/api/v1/jobs", json=_spec(), headers=_auth(h.token))
            assert r.status_code == 400
            hdr = {**_auth(h.token), "Idempotency-Key": "abc"}
            first = await c.post("/api/v1/jobs", json=_spec(), headers=hdr)
            assert first.status_code == 201
            again = await c.post("/api/v1/jobs", json=_spec(), headers=hdr)
            assert again.status_code == 200 and again.json()["jobs"] == first.json()["jobs"]
            clash = await c.post("/api/v1/jobs", json=_spec(resources={"gpus": 2}), headers=hdr)
            assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_conflict"
    run(go())


def test_owner_isolation_and_agent_management_boundary(h):
    other_id, other = h.store.run_sync(lambda c: auth.create_token(c, owner="labmate", kind="human", label="lm"))

    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            r = await c.post("/api/v1/jobs", json=_spec(), headers={**_auth(h.token), "Idempotency-Key": "x"})
            jid = r.json()["jobs"][0]
            assert (await c.get(f"/api/v1/jobs/{jid}", headers=_auth(other))).status_code == 404
            # Same owner's agent can read but not manage a job the human submitted.
            assert (await c.get(f"/api/v1/jobs/{jid}", headers=_auth(h.agent_token))).status_code == 200
            r = await c.post(f"/api/v1/jobs/{jid}/cancel", headers=_auth(h.agent_token))
            assert r.status_code == 403
            r = await c.post(f"/api/v1/jobs/{jid}/cancel", headers=_auth(h.token))
            assert r.status_code == 200 and r.json()["job"]["desired_state"] == "CANCEL"
    run(go())


def test_artifact_download_rejects_internal_symlink_from_database_path(h):
    artifact_root = h.tmp / "state" / "artifacts"
    jid = h.submit(key="artifact-symlink", name="artifact-symlink")["jobs"][0]
    attempt_dir = artifact_root / str(jid) / "1"
    attempt_dir.mkdir(parents=True)
    target = artifact_root / "other-job.txt"
    target.write_text("other job data")
    real_path = attempt_dir / "real.txt"
    real_path.write_text("expected artifact")
    link_path = attempt_dir / "linked.txt"
    link_path.symlink_to(target)

    def record(conn):
        conn.execute("INSERT INTO attempts(id,job_id,n,backend,target,epoch,state,remote_may_be_live,"
                     "launch_op_id,spec_digest,created_at,updated_at) "
                     "VALUES(?, ?,1,'slurm','n1',1,'RELEASED',0,?,'spec','now','now')",
                     (f"artifact-{jid}", jid, f"artifact-op-{jid}"))
        for name, path, content in (("real.txt", real_path, b"expected artifact"),
                                    ("linked.txt", link_path, b"other job data")):
            conn.execute("INSERT INTO artifacts(job_id,attempt_id,relpath,state,size,sha256,local_path,updated_at) "
                         "VALUES(?,?,?,'COMPLETE',?,?,?,'now')",
                         (jid, f"artifact-{jid}", name, len(content), "sha", str(path)))
    h.store.run_sync(record)

    async def go():
        ctl = h.start()
        rt = Runtime(store=h.store, controller=ctl, bundle_dir=h.tmp / "bundles",
                     bundle_limits=bundles.BundleLimits(), artifact_dir=artifact_root, clock_ok=lambda: True)
        app = create_app(rt)
        async with _client(app) as c:
            real = await c.get(f"/api/v1/jobs/{jid}/artifacts/real.txt", headers=_auth(h.token))
            assert real.status_code == 200 and real.content == b"expected artifact"
            linked = await c.get(f"/api/v1/jobs/{jid}/artifacts/linked.txt", headers=_auth(h.token))
            assert linked.status_code == 404
    run(go())


def test_wait_times_out_without_cancelling_and_wakes_on_completion(h):
    async def go():
        ctl, app = _app(h)
        await ctl.startup()
        async with _client(app) as c:
            r = await c.post("/api/v1/jobs", json=_spec(), headers={**_auth(h.token), "Idempotency-Key": "w"})
            jid = r.json()["jobs"][0]
            short = await c.get(f"/api/v1/jobs/{jid}/wait?timeout=0.05", headers=_auth(h.token))
            assert short.json()["waited"]["met"] is False
            assert short.json()["job"]["desired_state"] == "RUN"      # a wait timeout never cancels

            async def driver():
                for _ in range(10):
                    await asyncio.sleep(0.01)
                    await ctl.tick()
                    await ctl.drain()
            waiter = asyncio.create_task(c.get(f"/api/v1/jobs/{jid}/wait?timeout=10", headers=_auth(h.token)))
            await driver()
            done = await asyncio.wait_for(waiter, 5)
            body = done.json()
            assert body["waited"]["met"] is True and body["job"]["terminal"] is True
            assert body["job"]["success"] is True
    run(go())


def test_waiters_do_not_cause_remote_calls(h):
    async def go():
        ctl, app = _app(h)
        await ctl.startup()
        async with _client(app) as c:
            r = await c.post("/api/v1/jobs", json=_spec(), headers={**_auth(h.token), "Idempotency-Key": "q"})
            jid = r.json()["jobs"][0]
            before = len(h.fake.calls)
            waits = [c.get(f"/api/v1/jobs/{jid}/wait?timeout=0.2", headers=_auth(h.token)) for _ in range(15)]
            reads = [c.get(f"/api/v1/jobs/{jid}", headers=_auth(h.token)) for _ in range(35)]
            await asyncio.gather(*waits, *reads)
            assert len(h.fake.calls) == before                        # invariant 8
    run(go())


def test_fifty_waiters_share_one_job_without_remote_calls(h):
    extra_tokens = [h.store.run_sync(lambda conn, i=i: auth.create_token(
        conn, owner="suresh", kind="human", label=f"waiter-{i}"))[1] for i in range(3)]

    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            r = await c.post("/api/v1/jobs", json=_spec(control={"hold": True}),
                             headers={**_auth(h.token), "Idempotency-Key": "50-waiters"})
            assert r.status_code == 201
            jid = r.json()["jobs"][0]
            before = len(h.fake.calls)
            tokens = [h.token, *extra_tokens]
            waits = [c.get(f"/api/v1/jobs/{jid}/wait?timeout=0.1", headers=_auth(tokens[i % 4]))
                     for i in range(50)]
            responses = await asyncio.gather(*waits)
            assert all(response.status_code == 200 for response in responses)
            assert all(response.json()["waited"]["met"] is False for response in responses)
            assert len(h.fake.calls) == before
    run(go())


def test_per_token_waiter_cap(h):
    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            r = await c.post("/api/v1/jobs", json=_spec(), headers={**_auth(h.token), "Idempotency-Key": "cap"})
            jid = r.json()["jobs"][0]
            results = await asyncio.gather(*[c.get(f"/api/v1/jobs/{jid}/wait?timeout=0.3", headers=_auth(h.token))
                                             for _ in range(20)])
            codes = sorted(r.status_code for r in results)
            assert codes.count(429) >= 4 and codes.count(200) <= 16
    run(go())


def test_agent_submission_rate_limit(h):
    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            codes = []
            for i in range(12):
                r = await c.post("/api/v1/jobs", json=_spec(resources={"gpus": 0}),
                                 headers={**_auth(h.agent_token), "Idempotency-Key": f"r{i}"})
                codes.append(r.status_code)
            assert codes[:10] == [201] * 10 and codes[10] == 429
            assert "retry-after" in {k.lower() for k in r.headers}
    run(go())


def test_version_checked_mutation(h):
    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            r = await c.post("/api/v1/jobs", json=_spec(control={"hold": True}),
                             headers={**_auth(h.token), "Idempotency-Key": "v"})
            jid = r.json()["jobs"][0]
            stale = await c.post(f"/api/v1/jobs/{jid}/release", headers={**_auth(h.token), "If-Match": "99"})
            assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
            doc = (await c.get(f"/api/v1/jobs/{jid}", headers=_auth(h.token))).json()["job"]
            fresh = await c.post(f"/api/v1/jobs/{jid}/release",
                                 headers={**_auth(h.token), "If-Match": str(doc["version"])})
            assert fresh.status_code == 200 and fresh.json()["job"]["phase"] == "PENDING"
    run(go())


def test_bundle_upload_validates_and_scopes_to_owner(h, tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "train.py").write_text("print('hi')\n")
    out = tmp_path / "b.tar.gz"
    info = bundles.bundle_build(src, out)
    data = out.read_bytes()
    other_id, other = h.store.run_sync(lambda c: auth.create_token(c, owner="labmate", kind="human", label="lm"))
    _, reader = h.store.run_sync(lambda c: auth.create_token(
        c, owner="suresh", kind="service", label="ro", scopes=("read",)))

    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            url = f"/api/v1/bundles/{info.digest}"
            assert (await c.head(url, headers=_auth(h.token))).status_code == 404
            r = await c.put(url, content=data, headers=_auth(h.token))
            assert r.status_code == 200, r.text
            assert (await c.head(url, headers=_auth(h.token))).status_code == 200
            # A stale DB reference must not make the client skip a missing bundle.
            (h.tmp / "bundles" / f"{info.digest.split(':', 1)[1]}.tar.gz").unlink()
            assert (await c.head(url, headers=_auth(h.token))).status_code == 404
            assert (await c.put(url, content=data, headers=_auth(h.token))).status_code == 200
            # Knowing the digest grants a different owner nothing (§6.3).
            assert (await c.head(url, headers=_auth(other))).status_code == 404
            assert (await c.head(url, headers=_auth(reader))).status_code == 403
            spec = _spec(workdir={"bundle": info.digest})
            ok = await c.post("/api/v1/jobs", json=spec, headers={**_auth(h.token), "Idempotency-Key": "b1"})
            assert ok.status_code == 201
            denied = await c.post("/api/v1/jobs", json=_spec(workdir={"bundle": info.digest}, placement={}),
                                  headers={**_auth(other), "Idempotency-Key": "b2"})
            assert denied.status_code == 404 and denied.json()["error"]["code"] == "bundle_missing"
            tampered = bytearray(data)
            tampered[-20] ^= 0xFF
            bad = await c.put(url, content=bytes(tampered), headers=_auth(h.token))
            assert bad.status_code == 400 and bad.json()["error"]["code"] == "bundle_invalid"
            wrong = await c.put("/api/v1/bundles/sha256:" + "0" * 64, content=data, headers=_auth(h.token))
            assert wrong.status_code == 400
    run(go())


def test_budget_authority_grants_denies_and_redeems_once(h):
    def seed(conn):
        conn.execute("INSERT INTO nodes (id, backend, enabled, config_json, updated_at) VALUES (?,?,?,?,?)",
                     ("kiac", "slurm", 1, json.dumps({"fleetctl_target": "kiac-ayand",
                         "budget": {"monitor_per_minute": 2, "burst": 2,
                                    "max_sessions_per_operation": 2, "max_bytes_per_transfer": 1024,
                                    "sessions_per_minute": 20, "sessions_burst": 20,
                                    "bytes_per_minute": 1024, "bytes_burst": 1024,
                                    "action_session_reserve": 2}}), utcnow()))
    h.store.run_sync(seed)
    svc_id, svc = h.store.run_sync(lambda c: auth.create_token(c, owner="suresh", kind="service", label="laptop-fleetctl",
                                                                scopes=("permits",)))

    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            ask = {"cluster": "kiac-ayand", "op_class": "monitor", "cost": {"rpc": 1}}
            grants = [(await c.post("/api/v1/permits", json=ask, headers=_auth(svc))).json() for _ in range(3)]
            assert [g["granted"] for g in grants] == [True, True, False]
            assert grants[2]["retry_after"] > 0 and grants[0]["cluster"] == "kiac"
            # No approved budget for this class: deny by default.
            action = await c.post("/api/v1/permits", json={**ask, "op_class": "action"}, headers=_auth(svc))
            assert action.json()["granted"] is False
            pid = grants[0]["permit_id"]
            redeem = f"/api/v1/permits/{pid}/redeem"
            first = (await c.post(redeem, json={"cluster": "kiac-ayand", "op_class": "monitor"},
                                  headers=_auth(svc))).json()
            assert first["valid"] is True and first["cluster"] == "kiac-ayand" and first["canonical"] == "kiac"
            again = await c.post(redeem, json={"cluster": "kiac", "op_class": "monitor"}, headers=_auth(svc))
            assert again.json()["valid"] is False
            # A permit only pays for the bucket it was drawn from, and a mismatched redeem burns it.
            other = grants[1]["permit_id"]
            wrong = await c.post(f"/api/v1/permits/{other}/redeem", json={"cluster": "kiac", "op_class": "action"},
                                 headers=_auth(svc))
            assert wrong.json() == {"schema": "fq.permit/v1", "ok": True, "valid": False, "reason": "bucket_mismatch"}
            burnt = await c.post(f"/api/v1/permits/{other}/redeem", json={"cluster": "kiac", "op_class": "monitor"},
                                 headers=_auth(svc))
            assert burnt.json()["valid"] is False
            bare = await c.post(f"/api/v1/permits/{pid}/redeem", headers=_auth(svc))
            assert bare.json()["valid"] is False
            unknown = await c.post("/api/v1/permits", json={**ask, "cluster": "nowhere"}, headers=_auth(svc))
            assert unknown.status_code == 403
            no_scope = await c.post("/api/v1/permits", json=ask, headers=_auth(h.token))
            assert no_scope.status_code == 403
    run(go())


def test_remote_cost_ledger_shows_spent_tokens_and_denials_only_to_operators(h):
    policy = {"fleetctl_target": "kiac-ayand", "budget": {
        "monitor_per_minute": 2, "action_per_minute": 3, "transfer_per_minute": 4, "burst": 2,
        "max_sessions_per_operation": 1, "max_bytes_per_transfer": 100,
        "sessions_per_minute": 10, "sessions_burst": 10,
        "bytes_per_minute": 100, "bytes_burst": 100,
        "action_session_reserve": 1}}
    h.store.run_sync(lambda c: c.execute(
        "INSERT INTO nodes (id, backend, enabled, config_json, updated_at) VALUES (?,?,?,?,?)",
        ("kiac", "slurm", 1, json.dumps(policy), utcnow())))
    _, permit_token = h.store.run_sync(lambda c: auth.create_token(
        c, owner="suresh", kind="service", label="permits", scopes=("permits",)))
    _, operator_token = h.store.run_sync(lambda c: auth.create_token(
        c, owner="suresh", kind="service", label="dashboard", scopes=("read", "read_all")))

    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            ask = {"cluster": "kiac-ayand", "op_class": "monitor", "cost": {"rpc": 1}}
            grants = [(await c.post("/api/v1/permits", json=ask, headers=_auth(permit_token))).json()
                      for _ in range(3)]
            assert [row["granted"] for row in grants] == [True, True, False]
            ordinary = (await c.get("/api/v1/status", headers=_auth(h.token))).json()
            assert "remote_cost_ledger" not in ordinary
            operator = (await c.get("/api/v1/status", headers=_auth(operator_token))).json()
            ledger = operator["remote_cost_ledger"]
            assert len(ledger) == 1 and ledger[0]["site_id"] == "kiac"
            classes = {row["op_class"]: row for row in ledger[0]["classes"]}
            monitor = classes["monitor"]
            assert monitor["calls_today"] == 2 and monitor["rpc_today"] == 2
            assert monitor["denied_today"] == 1 and monitor["per_minute"] == 2
            assert 0 <= monitor["available"] < monitor["burst"]
            assert ledger[0]["sessions"]["used_today"] == 2
            assert ledger[0]["bytes"]["used_today"] == 0
            assert classes["action"]["available"] == classes["action"]["burst"]
    run(go())


def test_admin_pause_and_accept_switches(h):
    admin_id, admin = h.store.run_sync(lambda c: auth.create_token(c, owner="suresh", kind="human", label="admin",
                                                                    scopes=("admin",)))

    async def go():
        _, app = _app(h)
        async with _client(app) as c:
            r = await c.post("/api/v1/admin/accept", json={"value": "off"}, headers=_auth(admin))
            assert r.status_code == 200
            refused = await c.post("/api/v1/jobs", json=_spec(), headers={**_auth(h.token), "Idempotency-Key": "z"})
            assert refused.status_code == 503 and refused.json()["error"]["code"] == "draining"
            assert (await c.post("/api/v1/admin/accept", json={"value": "on"}, headers=_auth(h.token))).status_code == 403
    run(go())
