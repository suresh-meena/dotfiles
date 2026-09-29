"""Browser mutations require exact origin, host, and synchronizer-token checks."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlencode

import httpx

from fleetq import auth
from fleetq.api.app import Runtime, create_app
from fleetq.executors.fake import FakeNode
from fleetq import bundles
from fleetq.util import utcnow
from harness import Harness


ORIGIN = "https://fq.example"


async def _login(client, token, headers):
    response = await client.post("/ui/login", data={"token": token}, headers=headers,
                                 follow_redirects=False)
    assert response.status_code == 303
    home = await client.get("/ui/", headers={"Host": "fq.example"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)
    return csrf


def test_ui_rejects_cross_origin_missing_csrf_and_get_mutation(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"])])
    try:
        rt = Runtime(store=h.store, controller=None, bundle_dir=tmp_path / "bundles",
                     bundle_limits=bundles.BundleLimits(), ui_origin="https://fq.example", clock_ok=lambda: True)
        app = create_app(rt)

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://fq.example") as client:
                headers = {"Host": "fq.example", "Origin": "https://fq.example"}
                login = await client.post("/ui/login", data={"token": h.token}, headers=headers,
                                          follow_redirects=False)
                assert login.status_code == 303
                assert "httponly" in login.headers["set-cookie"].lower()
                assert "secure" in login.headers["set-cookie"].lower()
                assert "strict" in login.headers["set-cookie"].lower()
                home = await client.get("/ui/", headers={"Host": "fq.example"})
                assert home.status_code == 200
                assert "Content-Security-Policy" in home.headers
                csrf = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)

                # Origin comparison is exact and applies before route execution.
                forged_origin = await client.post("/ui/logout", data={"csrf": csrf},
                                                   headers={**headers, "Origin": "https://evil.example"})
                assert forged_origin.status_code == 403
                wrong_host = await client.get("/ui/", headers={"Host": "evil.example"})
                assert wrong_host.status_code == 421

                admitted = await client.post("/api/v1/jobs", json={
                    "name": "ui", "command": {"argv": ["true"]},
                    "workdir": {"in_place": "/data/p"}, "placement": {"on": ["n1"]},
                    "resources": {"gpus": 0}},
                    headers={"Authorization": f"Bearer {h.token}", "Idempotency-Key": "ui-test"})
                jid = admitted.json()["jobs"][0]
                page = await client.get(f"/ui/jobs/{jid}/confirm/cancel", headers={"Host": "fq.example"})
                assert page.status_code == 200
                version = re.search(r'name="version" value="(\d+)"', page.text).group(1)
                base = {"confirm": "yes", "version": version}
                no_csrf = await client.post(f"/ui/jobs/{jid}/actions/cancel", data=base, headers=headers)
                assert no_csrf.status_code == 403
                csrf_form = {**base, "csrf": csrf}
                get_action = await client.get(f"/ui/jobs/{jid}/actions/cancel?{urlencode(csrf_form)}",
                                              headers={"Host": "fq.example"})
                assert get_action.status_code == 405
                state = (await client.get(f"/api/v1/jobs/{jid}",
                                          headers={"Authorization": f"Bearer {h.token}"})).json()["job"]
                assert state["desired_state"] == "RUN"

        asyncio.run(go())
    finally:
        h.close()


def test_status_is_read_only_owner_scoped_and_never_discloses_existing_secrets(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"])])
    try:
        _other_id, other_secret = h.store.run_sync(lambda c: auth.create_token(
            c, owner="other-owner", kind="human", label="other"))
        app = create_app(Runtime(store=h.store, controller=None, bundle_dir=tmp_path / "bundles",
                                 ui_origin=ORIGIN, clock_ok=lambda: True))

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=ORIGIN) as client:
                headers = {"Host": "fq.example", "Origin": ORIGIN}
                csrf = await _login(client, h.token, headers)
                status = await client.get("/ui/status", headers={"Host": "fq.example"})
                assert status.status_code == 200
                assert "active_jobs" in status.text and "Tokens for suresh" in status.text
                assert h.token not in status.text and h.agent_token not in status.text
                assert other_secret not in status.text and "other-owner" not in status.text
                before = h.store.run_sync(lambda c: c.execute("SELECT COUNT(*) FROM tokens").fetchone()[0])
                denied = await client.post("/ui/tokens/create", data={
                    "csrf": csrf, "owner": "attacker", "kind": "agent", "label": "x"}, headers=headers)
                assert denied.status_code == 403
                assert h.store.run_sync(lambda c: c.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]) == before

        asyncio.run(go())
    finally:
        h.close()


def test_admin_token_create_is_csrf_protected_one_time_and_revokeable(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"])])
    try:
        h.store.run_sync(lambda c: c.execute(
            "INSERT INTO nodes (id, backend, enabled, config_json, updated_at) VALUES (?,?,?,?,?)",
            ("site<&", "slurm", 1, json.dumps({"budget": {"monitor_per_minute": 2, "burst": 2}}), utcnow())))
        _admin_id, admin_secret = h.store.run_sync(lambda c: auth.create_token(
            c, owner="suresh", kind="human", label="administrator",
            scopes=("read", "logs", "submit", "manage_own", "admin")))
        app = create_app(Runtime(store=h.store, controller=None, bundle_dir=tmp_path / "bundles",
                                 ui_origin=ORIGIN, clock_ok=lambda: True))

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=ORIGIN) as client:
                headers = {"Host": "fq.example", "Origin": ORIGIN}
                csrf = await _login(client, admin_secret, headers)
                status = await client.get("/ui/status", headers={"Host": "fq.example"})
                assert status.status_code == 200 and "Create token" in status.text
                assert "Managed site query budgets" in status.text
                assert "site&lt;&amp;" in status.text and "site<&" not in status.text
                form = {"owner": "new-owner", "kind": "agent", "label": "one-shot",
                        "scopes": "read logs", "quota": "{}"}
                no_csrf = await client.post("/ui/tokens/create", data=form, headers=headers)
                assert no_csrf.status_code == 403
                created = await client.post("/ui/tokens/create", data={**form, "csrf": csrf},
                                            headers=headers)
                assert created.status_code == 200 and "Copy this value now" in created.text
                secret = re.search(r"<pre>([^<]+)</pre>", created.text).group(1)
                assert secret.startswith("fq_") and secret not in status.text

                refreshed = await client.get("/ui/status", headers={"Host": "fq.example"})
                assert secret not in refreshed.text
                token_id = re.search(r"Token ID: ([^<]+)", created.text).group(1)
                revoked = await client.post("/ui/tokens/revoke", data={
                    "csrf": csrf, "token_id": token_id}, headers=headers, follow_redirects=False)
                assert revoked.status_code == 303
                final = await client.get("/ui/status", headers={"Host": "fq.example"})
                assert "revoked" in final.text and secret not in final.text

        asyncio.run(go())
    finally:
        h.close()
