"""Resource and data-integrity regressions from the dashboard review."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")

from test_database import sample
from test_service import config, inventory
from test_web import request

from fleetmon.database import Database
from fleetmon.service import HubRuntime
from fleetmon.web.app import _invoke_with_executor, create_app


def test_cancelled_request_keeps_query_capacity_until_worker_finishes():
    started = threading.Event()
    release = threading.Event()
    capacity = threading.BoundedSemaphore(1)

    class Queries:
        def overview(self):
            started.set()
            assert release.wait(5)
            return []

    async def run(executor):
        task = asyncio.create_task(
            _invoke_with_executor(executor, Queries(), "overview", capacity)
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            assert not capacity.acquire(blocking=False)
            with pytest.raises(Exception) as error:
                await _invoke_with_executor(executor, Queries(), "overview", capacity)
            assert error.value.status_code == 503
        finally:
            release.set()
            await asyncio.to_thread(executor.shutdown, wait=True)
        await asyncio.sleep(0)
        assert capacity.acquire(blocking=False)

    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


def test_database_failure_returns_503_without_exposing_error_details():
    class Queries:
        def overview(self, **kwargs):
            raise RuntimeError("private database path")

    app = create_app(Queries())
    try:
        response = request(app, "GET", "/api/overview")
        assert response.status_code == 503
        assert "private database path" not in response.text
    finally:
        app.state.query_executor.shutdown(wait=True)


def test_non_ascii_basic_credentials_do_not_crash_authentication():
    from base64 import b64encode

    app = create_app(authenticated=True, auth_token="s" * 32)
    try:
        basic = b64encode("usér:pässword".encode()).decode()
        response = request(
            app, "GET", "/overview", headers={"Authorization": f"Basic {basic}"}
        )
        assert response.status_code == 401
    finally:
        app.state.query_executor.shutdown(wait=True)


def test_maintenance_leaves_event_loop_responsive_and_drains_before_close(tmp_path):
    runtime = HubRuntime(config(tmp_path, polling_enabled=False), discover_fn=inventory)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    retain = runtime.db.retain

    def delayed_retain(*args, **kwargs):
        started.set()
        assert release.wait(5)
        result = retain(*args, **kwargs)
        finished.set()
        return result

    runtime.db.retain = delayed_retain

    async def run():
        task = asyncio.create_task(runtime.run_forever())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            # If maintenance blocked the loop, it would have timed out before
            # this task could resume. It must still be waiting for our release.
            assert not finished.is_set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            release.set()
            await asyncio.to_thread(runtime.close)
            assert finished.is_set()
        finally:
            release.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_every_gpu_gets_the_requested_history_points(tmp_path):
    db = Database(tmp_path / "charts.db")
    try:
        doc = sample()
        doc["gpus"] = [
            {"uuid": f"gpu-{i}", "index": i, "utilization_fraction": i / 10}
            for i in range(8)
        ]
        for i in range(12):
            db.snapshot(f"p-{i}", "host", doc, 1000 + i)
        charts = db.host_charts("host", points=4)
        gpu_series = [s for s in charts["series"] if s["chart"] == "gpu_util"]
        assert len(gpu_series) == 8
        assert all(
            [p[0] for p in s["points"]] == [1008, 1009, 1010, 1011] for s in gpu_series
        )
    finally:
        db.close()


def test_long_chart_range_covers_the_full_window_with_bounded_points(tmp_path):
    db = Database(tmp_path / "range.db")
    try:
        start = 1_700_000_000.0
        for i in range(169):
            db.snapshot(f"p-{i}", "host", sample(), start + i * 3600)
        end = start + 168 * 3600
        charts = db.host_charts(
            "host",
            points=8,
            start=datetime.fromtimestamp(start, timezone.utc).isoformat(),
            end=datetime.fromtimestamp(end, timezone.utc).isoformat(),
        )
        cpu = next(s for s in charts["series"] if s["chart"] == "cpu")["points"]
        assert 1 < len(cpu) <= 8
        assert cpu[0][0] < start + 86400
        assert cpu[-1][0] == end
        assert all(start <= p[0] <= end for p in cpu)
    finally:
        db.close()


def test_retention_expires_unconfirmed_jobs_in_batches_and_preserves_fresh_jobs(
    tmp_path,
):
    db = Database(tmp_path / "retention.db")
    try:
        db.upsert_slurm_jobs(
            "cluster",
            [{"job_id": i, "state": "RUNNING"} for i in range(1, 22)],
            updated_at=1,
        )
        db.upsert_slurm_jobs(
            "cluster", [{"job_id": 22, "state": "RUNNING"}], updated_at=3
        )
        db.retain(2, batch=3)
        assert [row["job_id"] for row in db.jobs()] == ["22"]
        assert db.query("PRAGMA foreign_key_check") == []
        assert db.query("PRAGMA integrity_check")[0][0] == "ok"
    finally:
        db.close()


def test_failed_backup_does_not_leave_a_partial_file(tmp_path, monkeypatch):
    import sqlite3

    db = Database(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    try:

        def fail_connect(*args, **kwargs):
            assert destination.stat().st_mode & 0o777 == 0o600
            raise sqlite3.OperationalError("disk full")

        monkeypatch.setattr(sqlite3, "connect", fail_connect)
        with pytest.raises(sqlite3.OperationalError):
            db.backup(destination)
        assert not destination.exists()
    finally:
        db.close()


def test_inventory_failure_retries_on_inventory_cadence(tmp_path):
    runtime = HubRuntime(config(tmp_path), discover_fn=inventory)
    runtime.refresh_inventory()
    attempts = []

    def failed_discovery(*args):
        attempts.append(1)
        raise RuntimeError("inventory offline")

    runtime.discover_fn = failed_discovery
    runtime._last_inventory_attempt = None

    async def run():
        previous = runtime.inventory
        assert await runtime._inventory() is previous
        assert await runtime._inventory() is previous
        assert len(attempts) == 1

    try:
        asyncio.run(run())
    finally:
        runtime.close()
