"""Cross-module regression tests for the smallest safety boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fleetmon.config import HubConfig
from fleetmon.database import Database
from fleetmon.discovery import Inventory, Protocol, Target
from fleetmon.service import HubRuntime


def _config(tmp_path, **changes):
    fleetctl = tmp_path / "fleetctl"
    fleetctl.write_text("#!/bin/sh\nexit 0\n")
    fleetctl.chmod(0o700)
    values = {
        **HubConfig.defaults().__dict__,
        "fleetctl_path": fleetctl,
        "state_dir": tmp_path / "state",
        "database_path": tmp_path / "state" / "fleet.db",
        "disk_reserve_bytes": 64 * 1024 * 1024,
    }
    values.update(changes)
    return HubConfig(**values)


def _scheduler_inventory() -> tuple[Inventory, Target]:
    target = Target("login1", True, "login", "slurm")
    return Inventory([target], {"slurm": Protocol("slurm", "slurm")}), target


class _NoLaunchController:
    def __init__(self):
        self.polling_enabled = True
        self.disabled_targets: set[str] = set()
        self.active_count = 0
        self.calls: list[tuple[object, object]] = []

    async def poll(self, target, argv, **kwargs):
        self.calls.append((target, argv))
        raise AssertionError("disabled target must not launch a command")


def test_per_target_disable_stops_scheduler_poll_before_launch(tmp_path):
    inventory, target = _scheduler_inventory()
    controller = _NoLaunchController()
    runtime = HubRuntime(
        _config(tmp_path, disabled_targets=(target.name,)),
        discover_fn=lambda: inventory,
        poll_controller=controller,
    )
    try:
        runtime.refresh_inventory()
        result = asyncio.run(runtime.poll_slurm_target(target))
        assert result == "polling_disabled"
        assert controller.calls == []
    finally:
        runtime.close()


def test_retention_does_not_leave_current_process_from_deleted_poll(tmp_path):
    db = Database(tmp_path / "fleet.db")
    sample = {
        "status": "ok",
        "helper_version": "1",
        "captured_at": datetime.fromtimestamp(1, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "cpu": {},
        "memory": {},
        "disk": {},
        "gpus": [],
        "users": [],
        "processes": [{"pid": 7, "name": "worker"}],
    }
    try:
        db.upsert_host("gpu1", "compute", "direct")
        db.snapshot("poll-1", "gpu1", sample, received_at=1)
        assert len(db.current_processes("gpu1")) == 1
        db.retain(before=2)
        assert db.query("SELECT * FROM polls") == []
        assert db.current_processes("gpu1") == []
    finally:
        db.close()


def test_oversized_slurm_payload_remains_bounded_and_valid_json(tmp_path):
    db = Database(tmp_path / "fleet.db")
    try:
        db.upsert_slurm_jobs(
            "login1",
            [{"job_id": 42, "state": "RUNNING", "detail": "x" * 70_000}],
        )
        row = db.jobs()[0]
        payload = row["payload"]
        assert len(payload.encode("utf-8")) <= 65_536
        assert json.loads(payload)["job_id"] == 42
    finally:
        db.close()


class _ReadOnlyQuery:
    def __init__(self):
        self.remote_calls = 0

    def overview(self, **kwargs):
        return [{"target": "gpu1", "state": "live"}]

    def idle_gpus(self, **kwargs):
        return {"items": [], "summary": {"idle": 0, "busy": 0, "unknown": 0}}

    def poll_target(self, *args, **kwargs):
        self.remote_calls += 1
        raise AssertionError("HTTP queries must never trigger remote polling")


def test_dashboard_has_no_remote_polling_surface():
    httpx = pytest.importorskip("httpx")
    create_app = pytest.importorskip("fleetmon.web.app").create_app
    query = _ReadOnlyQuery()
    app = create_app(query)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return (
                await client.get("/api/overview"),
                await client.get("/api/idle-gpus"),
            )

    overview_response, idle_response = asyncio.run(request())
    assert overview_response.status_code == 200
    assert query.remote_calls == 0
    assert idle_response.status_code == 200
    assert idle_response.json()["summary"] == {"idle": 0, "busy": 0, "unknown": 0}
    assert query.remote_calls == 0


def test_documented_hub_installer_entrypoint_exists_and_is_executable():
    installer = Path(__file__).parents[1] / "scripts" / "install-hub"
    assert installer.is_file(), "README and plan advertise scripts/install-hub"
    assert installer.stat().st_mode & 0o111, "hub installer must be runnable"


@pytest.mark.parametrize(
    "argv",
    [["install-hub", "--dry-run"], ["install-helper", "--dry-run", "gpu1"]],
)
def test_plan_installer_commands_are_registered(argv):
    from fleetmon.cli import build_parser

    build_parser().parse_args(argv)
