"""fleetq integration: the capacity feed fleetmon publishes, and the queue it shows.

Both directions are read-only. The feed is built only from stored samples; the
queue is read from fleetqd through a bounded, cached client that never raises.
"""

from __future__ import annotations

import asyncio
import copy
import http.server
import json
import threading
import time
from pathlib import Path

import pytest
from test_service import Controller, config, gpu_document, inventory
from test_web import request

from fleetmon.config import ConfigError, load_config
from fleetmon.poller import PollResult
from fleetmon.protocol import encode_snapshot
from fleetmon.scheduler import SchedulerClient
from fleetmon.service import HubRuntime

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "fleetmon-capacity-v1.schema.json"
FLEETQ_COPY = Path(__file__).resolve().parents[2] / "fleetq" / "contracts" / "fleetmon-capacity-v1.schema.json"


# ---- the capacity feed ---------------------------------------------------------------


class Sequence(Controller):
    """Answers each poll with the next document (the last one repeats)."""

    def __init__(self, documents):
        super().__init__(None)
        self.documents = list(documents)

    async def poll(self, target, argv, **kwargs):
        self.calls.append((target, argv, kwargs))
        doc = self.documents.pop(0) if len(self.documents) > 1 else self.documents[0]
        return PollResult(0, encode_snapshot(doc), b"")


def _poll(runtime, times):
    target = runtime.inventory.direct_targets[0]
    for _ in range(times):
        assert asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"


def _runtime(tmp_path, documents, **changes):
    runtime = HubRuntime(config(tmp_path, **changes), discover_fn=inventory, poll_controller=Sequence(documents))
    runtime.refresh_inventory()
    return runtime


def test_the_feed_carries_each_poll_as_one_sample_in_the_contract_units(tmp_path):
    busy = gpu_document(idle=False)
    for gpu in busy["gpus"]:
        gpu["vram_used_bytes"] = 512 * 2**20
        gpu["vram_total_bytes"] = 1024 * 2**20
    runtime = _runtime(tmp_path, [gpu_document(idle=True), busy])
    try:
        _poll(runtime, 2)
        feed = runtime.capacity_feed()
    finally:
        runtime.close()
    assert feed["schema"] == "fleetmon.capacity/v1" and feed["generated_at"] > 0
    (host,) = feed["hosts"]
    assert host["target"] == "gpu1"
    gpus = {g["uuid"]: g["samples"] for g in host["gpus"]}
    idle = gpus["GPU-IDLE-0"]
    assert len(idle) == 2 and idle[0]["sample_id"] != idle[1]["sample_id"], "one sample per poll"
    assert idle[0]["sample_time"] <= idle[1]["sample_time"], "oldest first"
    assert idle[0]["utilization"] == 0.0 and idle[0]["process_count"] == 0
    assert idle[1]["utilization"] == 60.0, "percent, not a fraction"
    assert idle[1]["mem_used_mib"] == 512.0, "MiB, not bytes"
    assert all(s["supported"] and s["complete"] for s in idle)
    for sample in idle:
        assert set(sample) >= {"sample_id", "sample_time", "boot_id", "supported", "complete",
                               "process_count", "mem_used_mib", "utilization"}


def test_mig_or_nvml_trouble_is_never_reported_as_complete(tmp_path):
    mig = gpu_document(idle=True)
    mig["gpus"][0]["mig_detected"] = True
    broken = copy.deepcopy(gpu_document(idle=True))
    broken["gpus"][0]["supported"] = False
    broken["gpus"][0]["error"] = "NVML_ERROR_GPU_IS_LOST"
    broken["gpus"][0]["utilization_fraction"] = None
    broken["status"] = "partial"
    broken["capabilities"]["nvml_error"] = "NVML_ERROR_GPU_IS_LOST"
    runtime = _runtime(tmp_path, [mig, broken])
    try:
        _poll(runtime, 2)
        feed = runtime.capacity_feed()
    finally:
        runtime.close()
    samples = {g["uuid"]: g["samples"] for g in feed["hosts"][0]["gpus"]}["GPU-IDLE-0"]
    assert samples[0]["supported"] and not samples[0]["complete"], "MIG is out of scope for fleetq v1"
    assert not samples[1]["supported"] and not samples[1]["complete"]
    assert samples[1]["process_count"] is None and samples[1]["utilization"] is None, "unknown is never zero"


def test_an_error_poll_is_a_gap_and_old_samples_age_out(tmp_path):
    runtime = _runtime(tmp_path, [gpu_document(idle=True)])
    target = runtime.inventory.direct_targets[0]
    try:
        _poll(runtime, 1)
        runtime.controller = Controller(PollResult(255, b"", b"ssh: connection refused"))
        asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot"))
        feed = runtime.capacity_feed()
        assert len(feed["hosts"][0]["gpus"][0]["samples"]) == 1, "the failed poll adds nothing"
        import sqlite3
        with sqlite3.connect(runtime.config.database_path) as conn:
            conn.execute("UPDATE host_samples SET received_at = received_at - 3600")
        for ring in runtime._capacity_ring.values():
            for obs in ring:
                obs["received_at"] -= 3600
        assert runtime.capacity_feed()["hosts"] == [], "nothing older than the window"
    finally:
        runtime.close()


def test_the_feed_contract_is_the_one_fleetq_reads():
    if not FLEETQ_COPY.exists():
        pytest.skip("fleetq checkout not beside fleetmon")
    assert json.loads(CONTRACT.read_text()) == json.loads(FLEETQ_COPY.read_text())


def test_the_feed_route_serves_the_document():
    from fleetmon.web.app import create_app

    class Queries:
        def capacity_feed(self):
            return {"schema": "fleetmon.capacity/v1", "generated_at": 1.0, "hosts": []}

    assert request(create_app(Queries()), "GET", "/api/feed/v1/capacity").json()["hosts"] == []

    class Broken:
        def capacity_feed(self):
            return {"schema": "something/v9"}

    assert request(create_app(Broken()), "GET", "/api/feed/v1/capacity").status_code == 503


# ---- reading the queue from fleetqd ---------------------------------------------------


@pytest.fixture()
def fleetqd():
    """A tiny stand-in for fleetqd's read API on loopback."""
    state = {"calls": [], "delay": 0.0, "body": None, "status": 200, "auth": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["calls"].append(self.path)
            state["auth"].append(self.headers.get("Authorization"))
            time.sleep(state["delay"])
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "http://192.0.2.1/elsewhere")
                self.end_headers()
                return
            body = state["body"] if state["body"] is not None else json.dumps(
                {"schema": "fq.queue/v1", "ok": True, "jobs": [{"id": 1}], "path": self.path}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()


def test_reads_are_authenticated_cached_and_never_raise(fleetqd):
    url, state = fleetqd
    client = SchedulerClient(url, "fq_secret", ttl=0.5)
    first = client.get("/api/v1/queue")
    assert first["available"] and first["jobs"] == [{"id": 1}]
    assert state["auth"] == ["Bearer fq_secret"]
    client.get("/api/v1/queue")
    assert len(state["calls"]) == 1, "a dashboard left open costs one request per cache period"
    time.sleep(0.6)
    client.get("/api/v1/queue")
    assert len(state["calls"]) == 2


@pytest.mark.parametrize("setup,expected", [
    ({"delay": 1.5}, "unreachable"),
    ({"body": b"<html>not json"}, "invalid JSON"),
    ({"body": b"x" * (2 * 1024 * 1024 + 10)}, "too large"),
    ({"status": 503, "body": b'{"error": {"code": "not_ready"}}'}, "not_ready"),
])
def test_a_sick_scheduler_greys_the_page_instead_of_breaking_it(fleetqd, setup, expected):
    url, state = fleetqd
    state.update(setup)
    value = SchedulerClient(url, None, timeout=1.0, ttl=0).get("/api/v1/queue")
    assert value["available"] is False and expected in value["error"]


def test_redirects_are_not_followed(fleetqd):
    url, state = fleetqd
    value = SchedulerClient(url, "fq_secret", ttl=0).get("/redirect")
    assert value == {"available": False, "error": "HTTP 302", "status": 302}
    assert state["calls"] == ["/redirect"], "the token never follows a redirect elsewhere"


def test_a_refused_token_is_reported_not_shown_as_an_empty_queue(fleetqd):
    url, state = fleetqd
    state.update(status=401, body=b'{"ok": false, "error": {"code": "unauthorized"}}')
    value = SchedulerClient(url, "fq_revoked", ttl=0).get("/api/v1/queue")
    assert value == {"available": False, "error": "unauthorized", "status": 401}


def test_the_runtime_asks_for_everyone_and_marks_held_gpus(tmp_path, fleetqd):
    url, state = fleetqd
    runtime = _runtime(tmp_path, [gpu_document(idle=True), gpu_document(idle=True)],
                       scheduler_url=url, scheduler_token="fq_dash")
    try:
        runtime.scheduler_queue(finished=True)
        assert state["calls"] == ["/api/v1/queue?all_users=true&limit=300&finished=true", "/api/v1/status"]
        _poll(runtime, 2)
        state["body"] = json.dumps({"schema": "fq.nodes/v1", "ok": True, "nodes": [
            {"id": "gpu1", "gpus": [{"uuid": "GPU-IDLE-0", "fleetq_job": 42}, {"uuid": "GPU-BUSY-0",
                                                                           "fleetq_job": None}]}]}).encode()
        listing = runtime.idle_gpus()
        held = {item["uuid"]: item["fleetq_job"] for item in listing["items"]}
        assert held == {"GPU-IDLE-0": 42, "GPU-BUSY-0": None}
    finally:
        runtime.close()


def test_without_a_scheduler_everything_still_works(tmp_path):
    runtime = _runtime(tmp_path, [gpu_document(idle=True)])
    try:
        queue = runtime.scheduler_queue()
        assert queue == {"available": False, "configured": False,
                         "error": "no [scheduler] url in the fleetmon config",
                         "managed_slurm_snapshot": {"available": False,
                                                     "reason": "not configured", "sites": []},
                         "managed_slurm_jobs": []}
        _poll(runtime, 1)
        assert all(item["fleetq_job"] is None for item in runtime.idle_gpus()["items"])
    finally:
        runtime.close()


def test_the_queue_page_and_routes(fleetqd):
    from fleetmon.web.app import create_app

    class Queries:
        def scheduler_queue(self, finished=False):
            return {"available": True, "jobs": [], "finished": finished}

        def scheduler_job(self, job_id):
            return {"available": True, "job": {"id": job_id}}

        def scheduler_status(self):
            return {"available": False, "error": "scheduler unreachable"}

    app = create_app(Queries())
    assert "Queue" in request(app, "GET", "/queue").text
    assert request(app, "GET", "/api/scheduler/queue?finished=true").json()["finished"] is True
    assert request(app, "GET", "/api/scheduler/jobs/7").json()["job"] == {"id": 7}
    assert request(app, "GET", "/api/scheduler/jobs/0").status_code == 404
    assert request(app, "GET", "/api/scheduler/status").json()["available"] is False


# ---- configuration --------------------------------------------------------------------


def _write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_scheduler_config_and_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEETMON_SCHEDULER_TOKEN", "fq_abc_def")
    cfg = load_config(_write(tmp_path, '[scheduler]\nurl = "http://127.0.0.1:8089/"\n'))
    assert cfg.scheduler_url == "http://127.0.0.1:8089" and cfg.scheduler_token == "fq_abc_def"
    monkeypatch.delenv("FLEETMON_SCHEDULER_TOKEN")
    assert load_config(_write(tmp_path, "")).scheduler_url is None
    for bad in ('url = "ftp://x"', 'url = "http://user:pw@127.0.0.1:8089"', 'url = "http://h/api"',
                'token = "fq_in_toml"'):
        with pytest.raises(ConfigError):
            load_config(_write(tmp_path, f"[scheduler]\n{bad}\n"))


def test_the_scheduler_token_file_must_be_private(tmp_path, monkeypatch):
    token = tmp_path / "sched.token"
    token.write_text("fq_file_token\n")
    token.chmod(0o644)
    monkeypatch.setenv("FLEETMON_SCHEDULER_TOKEN_FILE", str(token))
    path = _write(tmp_path, '[scheduler]\nurl = "http://127.0.0.1:8089"\n')
    with pytest.raises(ConfigError, match="FLEETMON_SCHEDULER_TOKEN_FILE"):
        load_config(path)
    token.chmod(0o600)
    assert load_config(path).scheduler_token == "fq_file_token"


# ---- end to end with fleetq's own idle rule ------------------------------------------

FLEETQ_SRC = Path(__file__).resolve().parents[2] / "fleetq" / "src"


def _spread(runtime, ages):
    """Place the recent observations at these ages (oldest first), like polls every ~50 s."""
    now = time.time()
    for ring in runtime._capacity_ring.values():
        for obs, age in zip(ring, ages, strict=False):
            obs["received_at"] = now - age
    return now


def _fleetq_verdicts(feed, now):
    import sys
    if not FLEETQ_SRC.exists():
        pytest.skip("fleetq checkout not beside fleetmon")
    sys.path.insert(0, str(FLEETQ_SRC))
    from fleetq.engine.idle import IdleHistory, ingest_capacity_feed
    history = IdleHistory(clock=lambda: now)
    ingest_capacity_feed(history, feed)
    return {g["uuid"]: history.verdict(h["target"], g["uuid"]) for h in feed["hosts"] for g in h["gpus"]}


def test_fleetq_reads_the_real_feed_and_units_matter(tmp_path):
    warm = gpu_document(idle=True)
    warm["gpus"][0]["utilization_fraction"] = 0.5          # 50 %, no processes, no memory
    runtime = _runtime(tmp_path, [gpu_document(idle=True)] * 3)
    try:
        _poll(runtime, 3)
        now = _spread(runtime, [130, 65, 1])
        verdicts = _fleetq_verdicts(runtime.capacity_feed(), now)
    finally:
        runtime.close()
    assert verdicts["GPU-IDLE-0"] == (True, "idle_history_clean"), verdicts
    assert verdicts["GPU-BUSY-0"][0] is False

    (tmp_path / "warm").mkdir()
    runtime = _runtime(tmp_path / "warm", [warm] * 3)
    try:
        _poll(runtime, 3)
        now = _spread(runtime, [130, 65, 1])
        verdicts = _fleetq_verdicts(runtime.capacity_feed(), now)
    finally:
        runtime.close()
    assert verdicts["GPU-IDLE-0"] == (False, "utilization_above_profile"), \
        "a 0-1 fraction read as percent would have called this idle"


def test_samples_too_close_together_are_not_sustained_idleness(tmp_path):
    runtime = _runtime(tmp_path, [gpu_document(idle=True)] * 3)
    try:
        _poll(runtime, 3)
        now = _spread(runtime, [3, 2, 1])
        verdicts = _fleetq_verdicts(runtime.capacity_feed(), now)
    finally:
        runtime.close()
    assert verdicts["GPU-IDLE-0"] == (False, "history_too_short")


def test_the_feed_keeps_samples_that_history_thinning_drops(tmp_path):
    """Storage keeps the newest two polls plus one per minute; the feed must not."""
    runtime = _runtime(tmp_path, [gpu_document(idle=True)] * 3)
    try:
        _poll(runtime, 3)
        import sqlite3
        with sqlite3.connect(runtime.config.database_path) as conn:
            stored = conn.execute("SELECT COUNT(*) FROM host_samples").fetchone()[0]
        samples = runtime.capacity_feed()["hosts"][0]["gpus"][0]["samples"]
    finally:
        runtime.close()
    assert stored == 2, "history thinning kept two"
    assert len(samples) == 3, "the feed still has every recent poll"


def test_after_a_restart_the_feed_falls_back_to_stored_samples(tmp_path):
    runtime = _runtime(tmp_path, [gpu_document(idle=True)] * 2)
    try:
        _poll(runtime, 2)
        runtime._capacity_ring.clear()                    # what a restart leaves
        samples = runtime.capacity_feed()["hosts"][0]["gpus"][0]["samples"]
    finally:
        runtime.close()
    assert len(samples) == 2
