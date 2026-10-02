"""Cached telemetry: read-only API, aging, outages and terminal rendering."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest

from fleetq import auth, client
from fleetq.api.app import Runtime, create_app
from fleetq.errors import FqError
from fleetq.feeds import fleetmon
from fleetq.feeds.usage import UsageCache

from test_api import _auth, _client, h, run  # noqa: F401


def document():
    now = time.time()
    return {"schema": "fleetmon.usage/v1", "generated_at": now, "hosts": [{
        "target": "n1", "state": "live", "sample_time": now - 12,
        "freshness_seconds": 90.0, "cpu_busy_fraction": 0.25,
        "ram_used_bytes": 16 * 2**30, "ram_total_bytes": 64 * 2**30,
        "gpus": [{"uuid": "GPU-a", "index": 0, "model": "A100",
                  "utilization_fraction": 0.8, "vram_used_bytes": 20 * 2**30,
                  "vram_total_bytes": 80 * 2**30, "supported": True},
                 {"uuid": "GPU-b", "index": 1, "model": "A100",
                  "utilization_fraction": None, "vram_used_bytes": None,
                  "vram_total_bytes": None, "supported": False}],
    }, {"target": "cpu-only", "state": "helper_missing", "sample_time": None,
        "freshness_seconds": 90.0, "cpu_busy_fraction": None,
        "ram_used_bytes": None, "ram_total_bytes": None, "gpus": []}]}


def cache():
    value = UsageCache(configured=True)
    value.update(document())
    return value


def test_reads_are_authenticated_filtered_and_never_fetch(h, monkeypatch):
    def no_fetch(*a, **kw):
        pytest.fail("a usage read triggered a feed fetch")
    monkeypatch.setattr(fleetmon, "fetch", no_fetch)
    usage = cache()
    app = create_app(Runtime(store=h.store, controller=None, bundle_dir=h.tmp,
                             clock_ok=lambda: True, usage_cache=usage))
    _, no_read = h.store.run_sync(lambda c: auth.create_token(
        c, owner="other", kind="human", label="submit-only", scopes=("submit",)))

    async def go():
        async with _client(app) as c:
            assert (await c.get("/api/v1/usage")).status_code == 401
            assert (await c.get("/api/v1/usage", headers=_auth(no_read))).status_code == 403
            for _ in range(5):
                body = (await c.get("/api/v1/usage?machine=n1", headers=_auth(h.token))).json()
                assert body["schema"] == "fq.usage/v1" and body["source"]["state"] == "live"
                assert [host["target"] for host in body["hosts"]] == ["n1"]
                assert body["hosts"][0]["gpus"][0]["utilization_fraction"] == 0.8
            body = (await c.get("/api/v1/usage", headers=_auth(h.token))).json()
            assert len(body["hosts"]) == 2
            missing = await c.get("/api/v1/usage?machine=absent", headers=_auth(h.token))
            assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    run(go())
    assert h.fake.calls == [], "usage must never touch remote executors"


def test_configuration_and_warmup_are_explicit():
    with pytest.raises(FqError) as exc:
        UsageCache().view()
    assert exc.value.code == "feature_unavailable"
    with pytest.raises(FqError) as exc:
        UsageCache(configured=True).view()
    assert exc.value.code == "not_ready"


def test_sample_and_transport_age_are_independent(monkeypatch):
    value = cache()
    first = value.view("n1")
    # A newly generated feed does not make a measurement newer.
    old = document()
    old["hosts"][0]["sample_time"] -= 600
    value.update(old)
    body = value.view("n1")
    assert body["source"]["state"] == "live"
    assert body["hosts"][0]["state"] == "stale" and body["hosts"][0]["sample_age_s"] >= 600
    value.update(document())
    value.fetched_monotonic -= 60
    body = value.view("n1")
    assert body["source"]["state"] == "stale" and body["hosts"][0]["stale"]
    value.update(document())
    value.failed = True
    body = value.view("n1")
    assert body["source"]["state"] == "unavailable" and body["hosts"][0]["state"] == "stale"
    assert body["hosts"][0]["gpus"] == first["hosts"][0]["gpus"]
    value.update(document())
    assert value.view("n1")["hosts"][0]["state"] == "live"
    body["hosts"][0]["gpus"][0]["model"] = "mutated"
    assert value.view("n1")["hosts"][0]["gpus"][0]["model"] == "A100"
    # A hub returning a previously generated document also expires.
    value.document["generated_at"] -= 60
    assert value.view()["source"]["state"] == "stale"


@pytest.mark.parametrize("change", [
    lambda d: d.update(schema="fleetmon.usage/v2"),
    lambda d: d.update(generated_at=float("nan")),
    lambda d: d.update(generated_at=time.time() + 600),
    lambda d: d["hosts"][0].update(sample_time=time.time() + 600),
    lambda d: d["hosts"][0].update(cpu_busy_fraction=2),
    lambda d: d["hosts"][0].update(ram_used_bytes=-1),
    lambda d: d["hosts"][0].update(ram_used_bytes=True),
    lambda d: d["hosts"].append(copy.deepcopy(d["hosts"][0])),
    lambda d: d["hosts"][0]["gpus"].append(copy.deepcopy(d["hosts"][0]["gpus"][0])),
    lambda d: d.update(hosts=d["hosts"] * 257),
])
def test_bad_feed_does_not_replace_good_cache(change):
    value = cache()
    before = value.view()
    bad = document()
    change(bad)
    with pytest.raises(ValueError):
        value.update(bad)
    assert value.view()["hosts"] == before["hosts"]


def test_background_poll_is_bounded_and_retains_cache_on_failure(monkeypatch):
    value = cache()
    calls = []
    def fetch(url, token):
        calls.append((url, token))
        raise OSError("hub down")
    async def stop(_seconds):
        raise asyncio.CancelledError
    monkeypatch.setattr(fleetmon, "fetch", fetch)
    monkeypatch.setattr(fleetmon.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        run(fleetmon.poll_usage_forever(value, "http://127.0.0.1:8088/prefix/api/feed/v1/capacity", "secret"))
    assert calls == [("http://127.0.0.1:8088/prefix/api/feed/v1/usage", "secret")]
    assert value.view()["source"]["state"] == "unavailable"
    with pytest.raises(ValueError):
        fleetmon.usage_url("http://127.0.0.1:8088/other")


def test_cli_table_and_json_read_only_one_daemon_endpoint(capsys):
    envelope = cache().view()
    calls = []
    class Api:
        def ok(self, method, path):
            calls.append((method, path))
            return envelope
    args = client.build_parser().parse_args(["usage"])
    assert client.cmd_usage(Api(), args) == 0
    table = capsys.readouterr().out
    assert "25%" in table and "80%" in table and "16.0/64.0" in table and "20.0/80.0" in table
    assert "cpu-only" in table and "helper_missing (stale)" in table and "AGE" in table
    assert calls == [("GET", "/api/v1/usage")]
    args = SimpleNamespace(machine="gpu &lab", json=True)
    assert client.cmd_usage(Api(), args) == 0
    assert json.loads(capsys.readouterr().out) == envelope
    assert calls[-1] == ("GET", "/api/v1/usage?machine=gpu%20%26lab")


def test_terminal_output_cleans_remote_text_and_warns_about_stale_values():
    value = cache()
    value.failed = True
    envelope = value.view("n1")
    envelope["hosts"][0]["gpus"][0]["model"] = "A100\x1b\nmalicious"
    output = client.fmt_usage(envelope)
    assert "\x1b" not in output and "A100malicious" in output
    assert "Fleetmon feed unavailable" in output and "stale" in output
