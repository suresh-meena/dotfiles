"""Regression tests for fleetqd-owned Slurm observations in fleetmon."""

from __future__ import annotations

import asyncio
import json
import time

from fleetmon.discovery import Inventory, Protocol, Target
from fleetmon.poller import PollResult
import fleetmon.service as service_module
from fleetmon.service import HubRuntime

from test_service import config


class NoRemoteCalls:
    def __init__(self):
        self.calls = []

    async def poll(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return PollResult(0, b"", b"")


class ManagedFeed:
    def __init__(self, document):
        self.document = document
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return self.document


class HealthFeed:
    def __init__(self, document):
        self.document = document
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return self.document


def _inventory():
    target = Target("campus", True, "login", "slurm")
    return Inventory([target], {"slurm": Protocol("slurm", "slurm")})


def _site(*, stale=False, age=1.0):
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    return {
        "site_id": "campus", "complete": True, "stale": stale, "error": None,
        "observed_at": stamp, "last_success_at": stamp, "age_s": age,
        "jobs": [{"job_id": "123", "name": "cached", "state": "RUNNING"}],
    }


def _generated():
    return _site()["observed_at"]


def _managed_runtime(tmp_path, feed):
    cfg = config(tmp_path, managed_scheduler_targets=("campus",),
                 scheduler_interval_seconds=60.0)
    runtime = HubRuntime(cfg, discover_fn=lambda *_: _inventory(), poll_controller=NoRemoteCalls())
    runtime.refresh_inventory()
    runtime.scheduler = feed
    return runtime


def test_managed_cluster_never_uses_fleetmons_direct_slurm_poller(tmp_path):
    controller = NoRemoteCalls()
    runtime = HubRuntime(
        config(tmp_path, managed_scheduler_targets=("campus",)),
        discover_fn=lambda *_: _inventory(), poll_controller=controller,
    )
    try:
        runtime.refresh_inventory()
        result = asyncio.run(runtime.poll_slurm_target(_inventory().scheduler_targets[0]))
        assert result == "managed_by_fleetqd"
        assert controller.calls == []
    finally:
        runtime.close()


def test_unmanaged_fleetqd_health_outage_is_deduplicated_and_resolved(tmp_path, monkeypatch):
    controller = NoRemoteCalls()
    runtime = HubRuntime(
        config(tmp_path, scheduler_url="http://fleetqd.invalid", notify_url="https://notify.invalid"),
        discover_fn=lambda *_: _inventory(), poll_controller=controller,
    )
    feed = HealthFeed({"available": False, "error": "scheduler unreachable"})
    runtime.scheduler = feed
    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(service_module.asyncio, "to_thread", inline_to_thread)
    async def no_dispatch():
        return None
    runtime._dispatch_notifications = no_dispatch
    try:
        async def observe_transitions():
            assert await runtime.poll_fleetqd_health() == "unavailable"
            assert await runtime.poll_fleetqd_health() == "unavailable"
            rows = runtime.db.query("SELECT dedupe_key,payload FROM notification_outbox")
            assert [row["dedupe_key"] for row in rows] == ["fleetqd:unavailable"]
            assert json.loads(rows[0]["payload"]) == {
                "event": "fleetqd_unavailable", "target": "fleetqd"
            }

            feed.document = {"available": True, "ok": True}
            assert await runtime.poll_fleetqd_health() == "live"
            assert runtime.db.query(
                "SELECT * FROM notification_outbox WHERE dedupe_key='fleetqd:unavailable'"
            ) == []

            feed.document = {"available": False, "error": "offline again"}
            assert await runtime.poll_fleetqd_health() == "unavailable"

        asyncio.run(observe_transitions())
        assert feed.calls == ["/healthz"] * 4
        assert controller.calls == []
    finally:
        runtime.close()


def test_managed_targets_use_snapshot_outage_path_not_health_probe(tmp_path):
    runtime = _managed_runtime(tmp_path, ManagedFeed({"available": False}))
    try:
        assert asyncio.run(runtime.poll_fleetqd_health()) == "not_configured"
        assert runtime.scheduler.calls == []
    finally:
        runtime.close()


def test_managed_snapshot_is_cached_and_stale_or_failed_feeds_are_visible(tmp_path):
    feed = ManagedFeed({"available": True, "schema": "fleetq.managed-slurm/v1",
                        "generated_at": _generated(), "sites": [_site()]})
    runtime = _managed_runtime(tmp_path, feed)
    try:
        assert asyncio.run(runtime.poll_managed_slurm()) == "live"
        assert feed.calls == ["/api/v1/managed-slurm"]
        assert runtime.managed_slurm_status()["sites"][0]["state"] == "live"
        # Querying fleetmon's status is local and cannot trigger a fresh Slurm or fleetqd poll.
        runtime.managed_slurm_status()
        assert feed.calls == ["/api/v1/managed-slurm"]

        feed.document = {"available": True, "schema": "fleetq.managed-slurm/v1",
                         "generated_at": _generated(), "sites": [_site(stale=True)]}
        assert asyncio.run(runtime.poll_managed_slurm()) == "partial"
        status = runtime.managed_slurm_status()["sites"][0]
        assert status["state"] == "stale" and status["error"]

        feed.document = {"available": False, "error": "scheduler offline"}
        assert asyncio.run(runtime.poll_managed_slurm()) == "unavailable"
        failed = runtime.managed_slurm_status()["sites"][0]
        assert failed["state"] == "stale" and failed["error"] == "scheduler offline"
    finally:
        runtime.close()


def test_managed_slurm_feed_rejects_old_generation_and_old_site_data(tmp_path):
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    feed = ManagedFeed({"available": True, "schema": "fleetq.managed-slurm/v1",
                        "generated_at": old, "sites": [_site()]})
    runtime = _managed_runtime(tmp_path, feed)
    try:
        assert asyncio.run(runtime.poll_managed_slurm()) == "invalid_schema"
        assert runtime.managed_slurm_status()["sites"][0]["state"] == "unavailable"

        feed.document = {"available": True, "schema": "fleetq.managed-slurm/v1",
                         "generated_at": _generated(), "sites": [_site(age=10000)]}
        assert asyncio.run(runtime.poll_managed_slurm()) == "partial"
        assert runtime.managed_slurm_status()["sites"][0]["state"] == "stale"
    finally:
        runtime.close()


def test_managed_snapshot_status_reaches_queue_and_never_polls_slurm_directly(tmp_path):
    controller = NoRemoteCalls()
    feed = ManagedFeed({"available": False, "error": "fleetqd unavailable"})
    runtime = HubRuntime(
        config(tmp_path, managed_scheduler_targets=("campus",),
               scheduler_interval_seconds=60.0),
        discover_fn=lambda *_: _inventory(), poll_controller=controller,
    )
    try:
        runtime.refresh_inventory()
        runtime.scheduler = feed

        # Before the first good observation, fleetmon exposes unknown freshness.
        assert asyncio.run(runtime.poll_slurm_target(_inventory().scheduler_targets[0])) == "managed_by_fleetqd"
        assert asyncio.run(runtime.poll_managed_slurm()) == "unavailable"
        unknown = runtime.scheduler_queue()
        assert unknown["managed_slurm_snapshot"]["sites"][0]["state"] == "unavailable"

        # A live row comes from the fleetqd snapshot and is retained when its
        # next snapshot becomes unavailable; the queue row then advertises stale.
        feed.document = {"available": True, "schema": "fleetq.managed-slurm/v1",
                         "generated_at": _generated(), "sites": [_site()]}
        assert asyncio.run(runtime.poll_managed_slurm()) == "live"
        feed.document = {"available": False, "error": "scheduler offline"}
        assert asyncio.run(runtime.poll_managed_slurm()) == "unavailable"
        queue = runtime.scheduler_queue()
        assert queue["managed_slurm_snapshot"]["sites"][0]["state"] == "stale"
        assert queue["managed_slurm_jobs"][0]["snapshot_status"] == "stale"
        assert queue["managed_slurm_jobs"][0]["job_id"] == "123"
        assert controller.calls == []
    finally:
        runtime.close()
