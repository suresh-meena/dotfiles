import asyncio
import dataclasses
import logging
import os
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

import pytest

import fleetmon.config as config_module
import fleetmon.service as service_module
from fleetmon.config import ConfigError, HubConfig
from fleetmon.discovery import Inventory, Protocol, Target
from fleetmon.poller import PollResult
from fleetmon.protocol import encode_snapshot
from fleetmon.service import HubRuntime


def document(helper_version="1"):
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "observation_duration_seconds": 0.25,
        "collection_duration_seconds": 0.3,
        "boot_id": "boot",
        "helper_version": helper_version,
        "status": "ok",
        "cpu": {
            "logical_count": 4,
            "busy_fraction": 0.5,
            "load_1m": 1.0,
            "load_5m": 1.0,
            "load_15m": 1.0,
        },
        "memory": {
            "total_bytes": 100,
            "available_bytes": 50,
            "used_bytes": 50,
            "swap_total_bytes": 0,
            "swap_used_bytes": 0,
        },
        "disk": {"total_bytes": 100, "free_bytes": 50},
        "gpus": [],
        "users": [],
        "processes": [],
        "visibility": {
            "partial": False,
            "permission_denied": 0,
            "processes_visible": 0,
            "processes_emitted": 0,
            "counters_truncated": False,
        },
        "limits": {"processes": 80, "users": 128, "truncated": False},
        "capabilities": {
            "nvml_supported": True,
            "nvml_error": None,
            "psutil_error": None,
        },
    }


def config(tmp_path, **changes):
    fleetctl = tmp_path / "fleetctl"
    fleetctl.write_text("#!/bin/sh\nexit 0\n")
    fleetctl.chmod(0o700)
    defaults = HubConfig.defaults()
    values = {
        **defaults.__dict__,
        "fleetctl_path": fleetctl,
        "state_dir": tmp_path / "state",
        "database_path": tmp_path / "state" / "fleet.db",
        "disk_reserve_bytes": 64 * 1024 * 1024,
    }
    values.update(changes)
    return HubConfig(**values)


def inventory():
    target = Target("gpu1", True, "compute", "direct")
    return Inventory([target], {"direct": Protocol("direct", "direct")})


def two_host_inventory():
    fast = Target("gpu-fast", True, "compute", "direct")
    slow = Target("gpu-slow", True, "compute", "direct")
    return Inventory([fast, slow], {"direct": Protocol("direct", "direct")})


def gpu_document(idle=True, idle_gpus=1, busy_gpus=1, with_rows=True):
    document_value = document()
    util = 0.0 if idle else 0.6
    processes = 0 if idle else 1
    gpus = []
    for index in range(idle_gpus):
        gpus.append(
            {
                "uuid": f"GPU-IDLE-{index}",
                "index": index,
                "model": "A100",
                "utilization_fraction": util,
                "vram_total_bytes": 1000,
                "vram_used_bytes": 10,
                "compute_process_count": processes,
                "supported": True,
                "error": None,
                "mig_detected": False,
                "instance_supported": False,
            }
        )
    for index in range(busy_gpus):
        gpus.append(
            {
                "uuid": f"GPU-BUSY-{index}",
                "index": idle_gpus + index,
                "model": "A100",
                "utilization_fraction": 0.75,
                "vram_total_bytes": 1000,
                "vram_used_bytes": 500,
                "compute_process_count": 2,
                "supported": True,
                "error": None,
                "mig_detected": False,
                "instance_supported": False,
            }
        )
    if not with_rows:
        gpus = []
        document_value["capabilities"]["nvml_error"] = "driver_not_loaded"
        document_value["status"] = "partial"
    document_value["gpus"] = gpus
    return document_value


class Controller:
    def __init__(self, result):
        self.result = result
        self.polling_enabled = True
        self.disabled_targets = set()
        self.calls = []
        self.active_count = 0

    async def poll(self, target, argv, **kwargs):
        self.calls.append((target, argv, kwargs))
        return self.result


class SlurmController(Controller):
    async def poll(self, target, argv, **kwargs):
        self.calls.append((target, argv, kwargs))
        if target.endswith(":squeue"):
            return PollResult(0, b'{"jobs":[{"job_id":1,"state":"RUNNING"}]}', b"")
        row = "cluster|2|||user|account|COMPLETED|gpu|node|||||gpu:1|gpu:1"
        return PollResult(0, row.encode(), b"")


class SlurmFallbackController(Controller):
    async def poll(self, target, argv, **kwargs):
        self.calls.append((target, argv, kwargs))
        if target.endswith(":squeue"):
            return PollResult(0, b"not-json", b"")
        row = "cluster|2|||user|account|RUNNING|gpu|node|||||gpu:1|gpu:1"
        return PollResult(0, row.encode(), b"")


class SlurmAccountingFailureController(Controller):
    async def poll(self, target, argv, **kwargs):
        self.calls.append((target, argv, kwargs))
        if target.endswith(":squeue"):
            return PollResult(0, b'{"jobs":[{"job_id":1,"state":"RUNNING"}]}', b"")
        return PollResult(None, b"", b"", timed_out=True)


def test_valid_poll_uses_explicit_target_and_commits_atomically(tmp_path):
    controller = Controller(PollResult(0, encode_snapshot(document()), b""))
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    target = runtime.inventory.direct_targets[0]

    result = asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot"))

    assert result == "ok"
    argv = controller.calls[0][1]
    assert argv[1:6] == [
        "exec",
        "--target",
        "gpu1",
        "--",
        "/opt/fleetmon/snapshot",
    ]
    assert len(runtime.db.query("SELECT * FROM host_samples")) == 1
    assert runtime.db.hosts()[0]["state"] == "live"
    assert runtime.db.hosts()[0]["helper_version"] == "1"
    assert runtime.db.hosts()[0]["last_success"] is not None
    runtime.close()


def test_overview_includes_latest_telemetry_metrics(tmp_path):
    controller = Controller(PollResult(0, encode_snapshot(document()), b""))
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    try:
        assert (
            asyncio.run(
                runtime.poll_target(
                    runtime.inventory.direct_targets[0], "/opt/fleetmon/snapshot"
                )
            )
            == "ok"
        )
        row = runtime.overview()[0]
        assert row["load1"] == 1.0
        assert row["ram_total"] == 100
        assert row["gpu_count"] == 0
        assert row["visible_users"] == 0
    finally:
        runtime.close()


def test_sparklines_pass_the_database_provider_through(tmp_path):
    controller = Controller(PollResult(0, encode_snapshot(document()), b""))
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    try:
        assert (
            asyncio.run(
                runtime.poll_target(
                    runtime.inventory.direct_targets[0], "/opt/fleetmon/snapshot"
                )
            )
            == "ok"
        )
        spark = runtime.sparklines()
        assert spark["bounded"] is True and spark["points"] == 60
        series = spark["series"]
        assert len(series) == 1 and series[0]["target"] == "gpu1"
        assert series[0]["points"] == [
            [
                runtime.db.query("SELECT MAX(received_at) AS t FROM host_samples")[0][
                    "t"
                ],
                0.5,
            ]
        ]
    finally:
        runtime.close()


def test_missing_helper_never_launches_a_remote_command(tmp_path):
    controller = Controller(PollResult(0, b"", b""))
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()

    result = asyncio.run(runtime.poll_target(runtime.inventory.direct_targets[0]))

    assert result == "helper_missing"
    assert controller.calls == []
    runtime.close()


def test_invalid_wire_data_stores_only_a_sanitized_error(tmp_path):
    controller = Controller(PollResult(0, b'{"token":"secret"}', b"remote secret"))
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()

    result = asyncio.run(
        runtime.poll_target(
            runtime.inventory.direct_targets[0], "/opt/fleetmon/snapshot"
        )
    )

    assert result == "invalid_schema"
    row = runtime.db.query("SELECT * FROM polls")[0]
    assert row["error"] == "invalid_schema"
    assert not runtime.db.query("SELECT * FROM host_samples")
    runtime.close()


def test_disk_guard_prevents_launch(tmp_path):
    controller = Controller(PollResult(0, encode_snapshot(document()), b""))
    runtime = HubRuntime(
        config(tmp_path, disk_reserve_bytes=10**18),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()

    result = asyncio.run(
        runtime.poll_target(
            runtime.inventory.direct_targets[0], "/opt/fleetmon/snapshot"
        )
    )

    assert result == "disk_low"
    assert controller.calls == []
    runtime.close()


def test_helper_version_mismatch_is_rejected(tmp_path):
    controller = Controller(
        PollResult(0, encode_snapshot(document(helper_version="2")), b"")
    )
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()

    result = asyncio.run(
        runtime.poll_target(
            runtime.inventory.direct_targets[0], "/opt/fleetmon/snapshot"
        )
    )

    assert result == "version_mismatch"
    assert runtime.db.hosts()[0]["state"] == "version_mismatch"
    runtime.close()


def test_stale_capture_is_not_accepted_as_current(tmp_path):
    stale = document()
    stale["captured_at"] = "2020-01-01T00:00:00Z"
    controller = Controller(PollResult(0, encode_snapshot(stale), b""))
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()

    result = asyncio.run(
        runtime.poll_target(
            runtime.inventory.direct_targets[0], "/opt/fleetmon/snapshot"
        )
    )

    assert result == "invalid_schema"
    assert not runtime.db.query("SELECT * FROM host_samples")
    runtime.close()


def test_slurm_poll_uses_admin_allowlist_and_updates_watermark(tmp_path):
    scheduler = Target("login1", True, "login", "slurm")
    scheduler_inventory = Inventory([scheduler], {"slurm": Protocol("slurm", "slurm")})
    controller = SlurmController(None)
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=lambda: scheduler_inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()

    result = asyncio.run(runtime.poll_slurm_target(scheduler))

    assert result == "ok"
    assert controller.calls[0][1][1:6] == [
        "exec",
        "--admin",
        "--target",
        "login1",
        "--",
    ]
    assert len(runtime.db.jobs()) == 2
    assert runtime.state.target("login1")["sacct_watermark"]
    runtime.close()


def test_slurm_json_failure_uses_fixed_text_fallback(tmp_path):
    scheduler = Target("login1", True, "login", "slurm")
    scheduler_inventory = Inventory([scheduler], {"slurm": Protocol("slurm", "slurm")})
    controller = SlurmFallbackController(None)
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=lambda: scheduler_inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    try:
        assert asyncio.run(runtime.poll_slurm_target(scheduler)) == "ok"
        assert any(target.endswith(":squeue-text") for target, _, _ in controller.calls)
    finally:
        runtime.close()


def test_slurm_accounting_failure_does_not_hide_fresh_queue(tmp_path):
    scheduler = Target("login1", True, "login", "slurm")
    scheduler_inventory = Inventory([scheduler], {"slurm": Protocol("slurm", "slurm")})
    controller = SlurmAccountingFailureController(None)
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=lambda: scheduler_inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    try:
        assert asyncio.run(runtime.poll_slurm_target(scheduler)) == "partial"
        state = runtime.db.query(
            "SELECT state, error FROM slurm_poll_state WHERE target=?", ("login1",)
        )[0]
        assert state["state"] == "live"
        assert state["error"] == "timeout"
        assert runtime.hosts()[0]["state"] == "scheduler"
        assert runtime.state.target("login1")["last_sacct_attempt"] > 0
    finally:
        runtime.close()


def test_direct_host_becomes_stale_at_query_time(tmp_path):
    runtime = HubRuntime(
        config(tmp_path, poll_interval_seconds=30),
        discover_fn=inventory,
        poll_controller=Controller(PollResult(1, b"", b"")),
    )
    runtime.refresh_inventory()
    document_value = document()
    runtime.db.snapshot(
        "old-poll",
        "gpu1",
        document_value,
        received_at=time.time() - 61,
    )
    try:
        assert runtime.hosts()[0]["state"] == "stale"
    finally:
        runtime.close()


def test_scheduler_becomes_stale_when_last_query_is_old(tmp_path):
    scheduler = Target("login1", True, "login", "slurm")
    scheduler_inventory = Inventory([scheduler], {"slurm": Protocol("slurm", "slurm")})
    runtime = HubRuntime(
        config(tmp_path, poll_interval_seconds=30),
        discover_fn=lambda: scheduler_inventory,
        poll_controller=Controller(None),
    )
    runtime.refresh_inventory()
    runtime.db.set_slurm_state("login1", None, "live")
    runtime.db.conn.execute(
        "UPDATE slurm_poll_state SET updated_at=? WHERE target=?",
        (time.time() - 121, "login1"),
    )
    try:
        assert runtime.hosts()[0]["state"] == "slurm_stale"
    finally:
        runtime.close()


def test_slurm_recovery_clamps_old_watermark_to_24_hours(tmp_path):
    scheduler = Target("login1", True, "login", "slurm")
    scheduler_inventory = Inventory([scheduler], {"slurm": Protocol("slurm", "slurm")})
    controller = SlurmController(None)
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=lambda: scheduler_inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    runtime.state.update_target(
        "login1",
        sacct_watermark=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    )
    try:
        assert asyncio.run(runtime.poll_slurm_target(scheduler)) == "ok"
        sacct_call = next(
            argv
            for target_name, argv, _ in controller.calls
            if target_name.endswith(":sacct")
        )
        start = datetime.fromisoformat(sacct_call[sacct_call.index("-S") + 1])
        end = datetime.fromisoformat(sacct_call[sacct_call.index("-E") + 1])
        assert (end - start).total_seconds() <= 24 * 60 * 60 + 2
    finally:
        runtime.close()


def test_hub_status_reports_storage_size_and_disabled_backup(tmp_path):
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=Controller(None),
    )
    try:
        status = runtime.hub_status()
        assert status["database_size_bytes"] > 0
        assert status["wal_size_bytes"] >= 0
        assert status["backup_status"] == "backup_disabled"
    finally:
        runtime.close()


def test_hub_status_includes_recent_poll_stats_and_hub_rss(tmp_path):
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=Controller(None),
    )
    try:
        started = time.time() - 2
        runtime.db.record_error("err-1", "gpu1", "timeout", started + 1)
        runtime.db.snapshot("ok-1", "gpu1", document(), started + 2, started_at=started)
        status = runtime.hub_status()
        assert status["recent_polls_total"] == 2
        assert status["recent_poll_errors"] == 1
        assert status["recent_error_rate"] == 0.5
        assert status["recent_avg_latency_seconds"] > 0.9
        assert status["hub_rss_bytes"] is None or status["hub_rss_bytes"] > 0
    finally:
        runtime.close()


def test_hub_status_reports_recent_error_rate_when_only_errors_exist(tmp_path):
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=Controller(None),
    )
    try:
        runtime.db.record_error("err-1", "gpu1", "timeout", time.time())
        status = runtime.hub_status()
        assert status["recent_polls_total"] == 1
        assert status["recent_error_rate"] == 1.0
    finally:
        runtime.close()


def test_prune_backups_keeps_only_the_newest_seven(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    for index in range(9):
        (backup_dir / f"fleet-2026090{index}T000000Z.db").touch()
    (backup_dir / "unrelated.txt").touch()
    HubRuntime._prune_backups(backup_dir, service_module.BACKUP_KEEP_COUNT)
    remaining = sorted(path.name for path in backup_dir.iterdir())
    assert remaining == [
        "fleet-20260902T000000Z.db",
        "fleet-20260903T000000Z.db",
        "fleet-20260904T000000Z.db",
        "fleet-20260905T000000Z.db",
        "fleet-20260906T000000Z.db",
        "fleet-20260907T000000Z.db",
        "fleet-20260908T000000Z.db",
        "unrelated.txt",
    ]


def test_configured_backup_runs_once_and_reports_ok(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setattr(
        config_module,
        "_stat_dev",
        lambda target: 2 if target == backup_dir else 1,
    )
    runtime = HubRuntime(
        config(tmp_path, backup_dir=backup_dir),
        discover_fn=inventory,
        poll_controller=Controller(None),
    )
    try:
        runtime.maybe_backup()
        backups = list(backup_dir.glob("fleet-*.db"))
        assert len(backups) == 1
        assert backups[0].stat().st_mode & 0o777 == 0o600
        status = runtime.hub_status()
        assert status["backup_status"] == "backup_ok"
        assert status["last_backup_path"] == str(backups[0])
        assert status["last_backup_error"] is None
        runtime.maybe_backup()
        assert len(list(backup_dir.glob("fleet-*.db"))) == 1
    finally:
        runtime.close()


def test_configured_backup_failure_is_a_bounded_status_field(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setattr(
        config_module,
        "_stat_dev",
        lambda target: 2 if target == backup_dir else 1,
    )
    runtime = HubRuntime(
        config(tmp_path, backup_dir=backup_dir),
        discover_fn=inventory,
        poll_controller=Controller(None),
    )
    try:
        original_backup = runtime.db.backup
        runtime.db.backup = lambda destination: (_ for _ in ()).throw(
            RuntimeError("boom /etc/passwd")
        )
        runtime.maybe_backup()
        runtime.db.backup = original_backup
        status = runtime.hub_status()
        assert status["backup_status"] == "backup_error"
        assert status["last_backup_path"] is None
        assert "boom" not in status["last_backup_error"]
        runtime.maybe_backup()
        assert not list(backup_dir.glob("fleet-*.db"))
    finally:
        runtime.close()


def test_unconfigured_backup_keeps_reporting_disabled(tmp_path):
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=Controller(None),
    )
    try:
        runtime.maybe_backup()
        assert runtime.hub_status()["backup_status"] == "backup_disabled"
    finally:
        runtime.close()


def test_auth_token_never_reaches_log_output(tmp_path, caplog):
    token = "x" * 40
    runtime = HubRuntime(
        config(tmp_path, auth_token=token),
        discover_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(f"inventory blew up: {token}")
        ),
        poll_controller=Controller(None),
    )
    try:
        with caplog.at_level(logging.DEBUG), suppress(Exception):
            runtime.refresh_inventory()
        status = runtime.hub_status()
        assert token not in caplog.text
        assert token not in str(status)
        assert token not in (runtime.config.state_dir / "runtime.json").read_text()
    finally:
        runtime.close()


def test_same_device_backup_dir_fails_closed_at_startup(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    with pytest.raises(ConfigError, match="separate filesystem"):
        HubRuntime(
            config(tmp_path, backup_dir=backup_dir),
            discover_fn=inventory,
            poll_controller=Controller(None),
        )


def test_hub_self_monitor_tracks_cpu_and_alerts_on_sustained_breach(tmp_path):
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=Controller(None),
    )
    try:
        calls = []

        def poster(url, payload):
            calls.append(payload)
            return "ok"

        runtime.notify_poster = poster
        runtime.config = dataclasses.replace(
            runtime.config, notify_url="https://ntfy/topic"
        )
        assert runtime._hub_cpu_percent(100.0) is None
        ticks = int(os.sysconf("SC_CLK_TCK"))
        with open("/proc/self/stat", encoding="ascii") as handle:
            fields = handle.read().split()
        current = int(fields[13]) + int(fields[14])
        runtime._last_cpu_ticks = current
        runtime._last_cpu_time = 100.0
        percent = runtime._hub_cpu_percent(100.0 + ticks * 2)
        assert percent is not None and 0 <= percent <= 100
        runtime._hub_health(6.0, 300 * 1024 * 1024)
        assert runtime.state.data["hub_breach_streak"] == 1
        assert not calls
        runtime._hub_health(6.0, 300 * 1024 * 1024)
        assert runtime.state.data["hub_overloaded"] is True
        assert len(calls) == 1
        assert calls[0]["event"] == "hub_overloaded"
        first_retry = runtime.state.data["hub_health_next_retry"]
        runtime._hub_health(6.0, 300 * 1024 * 1024)
        assert len(calls) == 1
        assert runtime.state.data["hub_health_next_retry"] == first_retry
        runtime._hub_health(1.0, 10 * 1024 * 1024)
        assert runtime.state.data["hub_breach_streak"] == 0
        assert runtime.state.data["hub_overloaded"] is False
        status = runtime.hub_status()
        assert "hub_cpu_percent" in status and "hub_overloaded" in status
    finally:
        runtime.close()


class DelayedController(Controller):
    """Fake controller that records per-target poll counts and delays."""

    def __init__(self, result, delays=None):
        super().__init__(result)
        self.delays = delays or {}
        self.counts: dict[str, int] = {}

    async def poll(self, target, argv, **kwargs):
        self.calls.append((target, argv, kwargs))
        name = target.split(":")[-1]
        self.counts[name] = self.counts.get(name, 0) + 1
        await asyncio.sleep(self.delays.get(target, 0.0))
        return self.result


def _run_hub_for(runtime, seconds):
    async def main():
        task = asyncio.create_task(runtime.run_forever())
        await asyncio.sleep(seconds)
        started = time.monotonic()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    return asyncio.run(main())


def _seed_helpers(runtime, *targets):
    for name in targets:
        runtime.state.update_target(name, helper_path="/opt/fleetmon/snapshot")


def test_notification_transitions_fire_rearm_and_refire(tmp_path):
    controller = Controller(
        PollResult(0, encode_snapshot(gpu_document(idle=True)), b"")
    )
    runtime = HubRuntime(
        config(tmp_path, notify_url="https://ntfy/topic"),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    calls = []

    def poster(url, payload):
        calls.append(payload)
        return "ok"

    runtime.notify_poster = poster
    target = runtime.inventory.direct_targets[0]
    try:
        # First idle observation: chain not complete yet, nothing sent.
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        assert calls == []
        # Second distinct consecutive idle observation fires immediately.
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        assert len(calls) == 1 and calls[0]["event"] == "gpu_free"
        assert calls[0]["gpu_uuid"] == "GPU-IDLE-0"
        state = runtime.state.target("gpu1")
        assert state["gpu_free_notified"] == {"GPU-IDLE-0": "sent"}
        assert state["gpu_free_counts"]["GPU-IDLE-0"] == 2
        assert state["gpu_free_counts"]["GPU-BUSY-0"] == 0
        # Remaining idle never repeats the notification.
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        assert len(calls) == 1
        # Busy: chain resets and the sent flag rearms without a new event.
        controller.result = PollResult(
            0, encode_snapshot(gpu_document(idle=False)), b""
        )
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        state = runtime.state.target("gpu1")
        assert state["gpu_free_counts"]["GPU-IDLE-0"] == 0
        assert "GPU-IDLE-0" not in state["gpu_free_notified"]
        assert len(calls) == 1
        # One idle observation after busy is not enough (previous was busy)...
        controller.result = PollResult(0, encode_snapshot(gpu_document(idle=True)), b"")
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        assert len(calls) == 1
        # ...but the second consecutive idle observation notifies again.
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        assert len(calls) == 2 and calls[1]["gpu_uuid"] == "GPU-IDLE-0"
    finally:
        runtime.close()


def test_stored_missing_gpu_rows_break_the_chain_without_false_alerts(tmp_path):
    controller = Controller(
        PollResult(0, encode_snapshot(gpu_document(idle=True)), b"")
    )
    runtime = HubRuntime(
        config(tmp_path, notify_url="https://ntfy/topic"),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    calls = []

    def poster(url, payload):
        calls.append(payload)
        return "ok"

    runtime.notify_poster = poster
    target = runtime.inventory.direct_targets[0]
    try:
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        assert len(calls) == 1
        # A committed observation without GPU rows must break the chain
        # instead of letting historic rows masquerade as consecutive.
        controller.result = PollResult(
            0, encode_snapshot(gpu_document(with_rows=False)), b""
        )
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        state = runtime.state.target("gpu1")
        assert state["gpu_free_counts"]["GPU-IDLE-0"] == 0
        assert "GPU-IDLE-0" not in state["gpu_free_notified"]
        assert len(calls) == 1
        # First idle observation after the gap does not alert...
        controller.result = PollResult(0, encode_snapshot(gpu_document(idle=True)), b"")
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        assert len(calls) == 1
        # ...the chain restarts only after two consecutive idle observations.
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        assert len(calls) == 2
        # The idle listing reports the GPU as unknown while NVML data is
        # missing; the host-level NVML error is the root-cause reason.
        controller.result = PollResult(
            0, encode_snapshot(gpu_document(with_rows=False)), b""
        )
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        listing = runtime.idle_gpus()
        by_uuid = {item["uuid"]: item for item in listing["items"]}
        assert by_uuid["GPU-IDLE-0"]["availability"] == "unknown"
        assert by_uuid["GPU-IDLE-0"]["reason"] == "nvml_error"
    finally:
        runtime.close()


def test_idle_gpus_listing_shares_classifier_with_host_and_overview(tmp_path):
    controller = Controller(
        PollResult(0, encode_snapshot(gpu_document(idle=True)), b"")
    )
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    target = runtime.inventory.direct_targets[0]
    try:
        # One observation classifies activity immediately but never idle.
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        listing = runtime.idle_gpus()
        by_uuid = {item["uuid"]: item for item in listing["items"]}
        assert (
            by_uuid["GPU-IDLE-0"]["availability"],
            by_uuid["GPU-IDLE-0"]["reason"],
        ) == (
            "unknown",
            "awaiting_second_observation",
        )
        assert by_uuid["GPU-BUSY-0"]["availability"] == "busy"
        assert listing["summary"] == {"idle": 0, "busy": 1, "unknown": 1}
        # The second consecutive observation completes the classification.
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        listing = runtime.idle_gpus()
        assert listing["summary"] == {"idle": 1, "busy": 1, "unknown": 0}
        by_uuid = {item["uuid"]: item for item in listing["items"]}
        idle = by_uuid["GPU-IDLE-0"]
        assert idle["target"] == "gpu1" and idle["idx"] == 0
        assert idle["model"] == "A100"
        assert idle["utilization"] == 0.0
        assert idle["vram_total"] == 1000 and idle["vram_used"] == 10
        assert idle["vram_free"] == 990
        assert idle["compute_process_count"] == 0
        assert idle["received_at"] is not None and idle["age_seconds"] >= 0
        assert (idle["availability"], idle["reason"]) == (
            "idle",
            "consecutive_idle_observations",
        )
        busy = by_uuid["GPU-BUSY-0"]
        assert busy["availability"] == "busy"
        assert busy["reason"] == "compute_processes_and_utilization"
        # Host GPU rows and the overview summary share the classification.
        host = runtime.host("gpu1")
        annotated = {g["uuid"]: g["availability"] for g in host["gpus"]}
        assert annotated == {"GPU-IDLE-0": "idle", "GPU-BUSY-0": "busy"}
        overview_row = runtime.overview()[0]
        assert overview_row["gpu_idle"] == 1
        assert overview_row["gpu_busy"] == 1
        assert overview_row["gpu_unknown"] == 0
    finally:
        runtime.close()


def gpu_document_with_owner(username="alice", uid=1000):
    doc = gpu_document(idle=True)
    doc["processes"] = [
        {
            "pid": 4321,
            "create_time": 1000.0,
            "uid": uid,
            "username": username,
            "name": "train",
            "executable": "python",
            "cpu_cores": 1.0,
            "rss_bytes": 1000,
            "gpu_process": True,
            "gpu_uuid": "GPU-BUSY-0",
            "gpu_index": 1,
            "vram_bytes": 500,
            "gpu_allocations": [
                {"gpu_uuid": "GPU-BUSY-0", "gpu_index": 1, "vram_bytes": 500}
            ],
        }
    ]
    doc["users"] = [
        {
            "uid": uid,
            "username": username,
            "cpu_cores": 1.0,
            "rss_bytes": 1000,
            "process_count": 1,
            "gpu_process_count": 1,
            "vram_bytes": 500,
        }
    ]
    doc["visibility"]["processes_visible"] = 1
    doc["visibility"]["processes_emitted"] = 1
    return doc


def test_idle_gpus_carry_hardened_flag_and_owners(tmp_path):
    controller = Controller(
        PollResult(0, encode_snapshot(gpu_document_with_owner()), b"")
    )
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    target = runtime.inventory.direct_targets[0]
    try:
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        listing = runtime.idle_gpus()
        by_uuid = {item["uuid"]: item for item in listing["items"]}
        # Hardened flag: busy stays busy, anything unconfirmed is stale.
        assert by_uuid["GPU-BUSY-0"]["flag"] == "busy"
        assert by_uuid["GPU-IDLE-0"]["flag"] == "stale"
        # Owners come from the stored current allocations, not remote work.
        assert by_uuid["GPU-BUSY-0"]["owners"] == ["alice"]
        assert by_uuid["GPU-IDLE-0"]["owners"] == []
        host = runtime.host("gpu1")
        host_flags = {g["uuid"]: g["flag"] for g in host["gpus"]}
        assert host_flags["GPU-BUSY-0"] == "busy"
        assert host_flags["GPU-IDLE-0"] == "stale"
        overview_row = runtime.overview()[0]
        assert overview_row["gpu_busy"] == 1
        assert overview_row["gpu_stale"] == 1
        assert overview_row["gpu_idle"] == 0
    finally:
        runtime.close()


def test_idle_gpus_owner_falls_back_to_uid_when_username_missing(tmp_path):
    controller = Controller(
        PollResult(
            0, encode_snapshot(gpu_document_with_owner(username=None, uid=4321)), b""
        )
    )
    runtime = HubRuntime(
        config(tmp_path),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    target = runtime.inventory.direct_targets[0]
    try:
        assert (
            asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot")) == "ok"
        )
        listing = runtime.idle_gpus()
        by_uuid = {item["uuid"]: item for item in listing["items"]}
        assert by_uuid["GPU-BUSY-0"]["owners"] == ["uid 4321"]
    finally:
        runtime.close()


def test_scheduler_cadence_is_separated_from_direct_poll_interval(tmp_path):
    runtime = HubRuntime(
        config(tmp_path, poll_interval_seconds=2, scheduler_interval_seconds=2),
        discover_fn=inventory,
        poll_controller=Controller(None),
    )
    try:
        assert runtime._schedule_interval("gpu1") == 2
        # The scheduler cadence keeps a conservative 60-second floor even
        # when direct polling is configured down to two seconds.
        assert runtime._schedule_interval("slurm:login1") == 60.0
        runtime.config = dataclasses.replace(
            runtime.config, scheduler_interval_seconds=300
        )
        assert runtime._schedule_interval("slurm:login1") == 300
    finally:
        runtime.close()


def test_fast_polling_downsamples_history_but_keeps_latest_two(tmp_path):
    controller = Controller(
        PollResult(0, encode_snapshot(gpu_document(idle=True)), b"")
    )
    runtime = HubRuntime(
        config(tmp_path, poll_interval_seconds=2, history_interval_seconds=60),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    target = runtime.inventory.direct_targets[0]
    try:
        for _ in range(4):
            assert (
                asyncio.run(runtime.poll_target(target, "/opt/fleetmon/snapshot"))
                == "ok"
            )
        assert len(runtime.db.query("SELECT * FROM polls")) == 2
        assert len(runtime.db.query("SELECT * FROM host_samples")) == 2
        # The surviving window still classifies both GPUs.
        listing = runtime.idle_gpus()
        assert listing["summary"] == {"idle": 1, "busy": 1, "unknown": 0}
        assert runtime.db.query("PRAGMA integrity_check")[0]["integrity_check"] == "ok"
    finally:
        runtime.close()


def test_slow_target_schedule_never_delays_healthy_targets(tmp_path):
    controller = DelayedController(
        PollResult(0, encode_snapshot(document()), b""), {"gpu-slow": 1.5}
    )
    runtime = HubRuntime(
        config(tmp_path, poll_interval_seconds=2),
        discover_fn=two_host_inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    _seed_helpers(runtime, "gpu-fast", "gpu-slow")
    runtime._stagger = lambda _key, _interval: 0.0
    try:
        _run_hub_for(runtime, 3.3)
        assert controller.counts.get("gpu-fast", 0) >= 2
        assert controller.counts.get("gpu-slow", 0) == 2
    finally:
        runtime.close()


def test_run_forever_cancels_promptly_even_mid_poll(tmp_path):
    controller = DelayedController(
        PollResult(0, encode_snapshot(document()), b""), {"gpu1": 5.0}
    )
    runtime = HubRuntime(
        config(tmp_path, poll_interval_seconds=2),
        discover_fn=inventory,
        poll_controller=controller,
    )
    runtime.refresh_inventory()
    _seed_helpers(runtime, "gpu1")
    runtime._stagger = lambda _key, _interval: 0.0
    try:
        elapsed = _run_hub_for(runtime, 0.6)
        assert elapsed < 2.0
        assert controller.counts.get("gpu1") == 1
    finally:
        runtime.close()
