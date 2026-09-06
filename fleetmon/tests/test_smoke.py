import json
import time
from datetime import datetime, timezone

from fleetmon import cli
from fleetmon.config import HubConfig
from fleetmon.discovery import Inventory, Protocol, Target
from fleetmon.poller import PollResult
from fleetmon.protocol import encode_snapshot
from fleetmon.smoke import PHASES, poll_gap_seconds, run_smoke
from fleetmon.state import OperationalState


def document():
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "observation_duration_seconds": 0.25,
        "collection_duration_seconds": 0.3,
        "boot_id": "boot",
        "helper_version": "1",
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
        "gpus": [
            {
                "uuid": "GPU-1",
                "index": 0,
                "model": "Test GPU",
                "utilization_fraction": 0.25,
                "vram_total_bytes": 100,
                "vram_used_bytes": 50,
                "temperature_c": 50.0,
                "power_watts": 100.0,
                "compute_process_count": 1,
                "supported": True,
                "error": None,
                "mig_detected": False,
                "instance_supported": False,
            }
        ],
        "users": [
            {
                "uid": 1000,
                "username": "user",
                "cpu_cores": 0.5,
                "rss_bytes": 10,
                "process_count": 1,
                "gpu_process_count": 1,
                "vram_bytes": 50,
            }
        ],
        "processes": [
            {
                "pid": 4242,
                "create_time": 1.0,
                "uid": 1000,
                "username": "user",
                "name": "task",
                "executable": "task",
                "cpu_cores": 0.5,
                "rss_bytes": 10,
                "gpu_process": True,
                "gpu_uuid": "GPU-1",
                "gpu_index": 0,
                "vram_bytes": 50,
                "gpu_allocations": [
                    {"gpu_uuid": "GPU-1", "gpu_index": 0, "vram_bytes": 50}
                ],
            }
        ],
        "visibility": {
            "partial": False,
            "permission_denied": 0,
            "processes_visible": 1,
            "processes_emitted": 1,
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
    values = {
        **HubConfig.defaults().__dict__,
        "fleetctl_path": fleetctl,
        "state_dir": tmp_path / "state",
        "database_path": tmp_path / "state" / "fleet.db",
        "disk_reserve_bytes": 64 * 1024 * 1024,
    }
    values.update(changes)
    return HubConfig(**values)


def direct_inventory():
    target = Target("gpu1", True, "compute", "direct")
    return Inventory([target], {"direct": Protocol("direct", "direct")})


def login_inventory():
    target = Target("login1", True, "login", "slurm")
    return Inventory([target], {"slurm": Protocol("slurm", "slurm")})


class Controller:
    def __init__(self, result):
        self.result = result
        self.polling_enabled = True
        self.disabled_targets = set()
        self.calls = []

    async def poll(self, target, argv, **kwargs):
        self.calls.append((target, argv, kwargs))
        return self.result


class BusyLock:
    def __init__(self, path):
        self.path = path

    def acquire(self):
        raise RuntimeError("hub is already running")

    def release(self):
        pass


def seed_helper(config, target="gpu1", path="/opt/fleetmon/snapshot"):
    OperationalState(config.state_dir / "runtime.json").update_target(
        target, helper_path=path
    )


def by_name(result):
    return {phase["name"]: phase for phase in result["phases"]}


def test_full_pass_reports_every_phase_green(tmp_path):
    cfg = config(tmp_path)
    seed_helper(cfg)
    controller = Controller(PollResult(0, encode_snapshot(document()), b""))
    started = time.monotonic()
    result = run_smoke(
        cfg,
        "gpu1",
        discover_fn=direct_inventory,
        poll_controller=controller,
        poll_gap=0.0,
    )
    elapsed = time.monotonic() - started

    assert result["ok"] is True
    assert result["target"] == "gpu1"
    assert result["skipped"] == []
    assert [phase["name"] for phase in result["phases"]] == list(PHASES)
    phases = by_name(result)
    assert phases["admission"]["ok"] is True
    assert phases["admission"]["protocol_kind"] == "direct"
    assert phases["helper"]["helper_path"] == "/opt/fleetmon/snapshot"
    first = phases["poll_first"]
    second = phases["poll_second"]
    assert first["ok"] and second["ok"]
    assert first["host_sample_stored"] and second["host_sample_stored"]
    assert first["poll_id"] != second["poll_id"]
    assert second["poll_id"] in phases["storage"]["counts"]
    latest = phases["storage"]["counts"][second["poll_id"]]
    assert latest["current_processes"] == 1
    assert latest["gpu_samples"] == 1
    assert latest["user_samples"] == 1
    assert phases["budget"]["budget"][second["poll_id"]]["poll_wall_seconds"] >= 0
    assert phases["failure_injection"]["outcome"] == "transport"
    assert phases["failure_injection"]["host_samples"] == 0
    assert phases["integrity"]["integrity_check"] == "ok"
    assert phases["integrity"]["foreign_key_violations"] == 0
    assert phases["dashboard"]["mode"] == "direct"
    assert phases["dashboard"]["sample_poll_id"] == second["poll_id"]
    assert phases["kill_switch"]["outcome"] == "polling_disabled"
    assert phases["kill_switch"]["remote_calls"] == 0
    assert elapsed < 30
    # exactly two committed polls; the failure injection stayed in its temp DB
    assert len(controller.calls) == 2


def test_admission_refusal_skips_remaining_phases(tmp_path):
    cfg = config(tmp_path)
    seed_helper(cfg)
    controller = Controller(PollResult(0, encode_snapshot(document()), b""))

    result = run_smoke(
        cfg,
        "gpu1",
        discover_fn=login_inventory,
        poll_controller=controller,
        poll_gap=0.0,
    )

    assert result["ok"] is False
    assert [phase["name"] for phase in result["phases"]] == ["admission"]
    assert result["phases"][0]["error"] == "target_not_admitted"
    assert [item["reason"] for item in result["skipped"]] == ["admission_failed"] * 9
    assert controller.calls == []


def test_kill_switch_phase_never_launches_a_remote_command(tmp_path):
    from fleetmon.smoke import _phase_kill_switch

    phase = _phase_kill_switch(config(tmp_path), "gpu1", direct_inventory(), None)

    assert phase["ok"] is True
    assert phase["outcome"] == "polling_disabled"
    assert phase["remote_calls"] == 0


def test_helper_less_mode_skips_poll_phases_and_still_verifies(tmp_path):
    cfg = config(tmp_path)
    controller = Controller(PollResult(0, encode_snapshot(document()), b""))

    result = run_smoke(
        cfg,
        "gpu1",
        discover_fn=direct_inventory,
        poll_controller=controller,
        poll_gap=0.0,
    )

    assert result["ok"] is True
    phases = by_name(result)
    assert phases["helper"]["skipped"] == "helper_missing"
    skipped = {item["name"]: item["reason"] for item in result["skipped"]}
    assert skipped == {
        "poll_first": "helper_missing",
        "poll_second": "helper_missing",
        "storage": "helper_missing",
        "budget": "helper_missing",
    }
    assert phases["failure_injection"]["ok"] is True
    assert phases["integrity"]["ok"] is True
    assert phases["dashboard"]["ok"] is True
    assert phases["kill_switch"]["ok"] is True
    assert controller.calls == []


def test_lock_busy_mode_skips_writer_phases_and_probes_http(tmp_path):
    cfg = config(tmp_path)
    seed_helper(cfg)

    result = run_smoke(
        cfg,
        "gpu1",
        discover_fn=direct_inventory,
        poll_gap=0.0,
        lock_factory=BusyLock,
    )

    skipped = {item["name"]: item["reason"] for item in result["skipped"]}
    assert skipped == {
        "admission": "hub_running",
        "poll_first": "hub_running",
        "poll_second": "hub_running",
        "storage": "hub_running",
        "budget": "hub_running",
    }
    phases = by_name(result)
    assert phases["helper"]["helper_path"] == "/opt/fleetmon/snapshot"
    assert phases["failure_injection"]["ok"] is True
    assert phases["integrity"]["error"] == "integrity_unavailable"
    assert phases["kill_switch"]["ok"] is True
    assert phases["dashboard"]["mode"] == "http"
    assert phases["dashboard"]["readyz_status"] is None
    assert result["ok"] is False
    assert not (cfg.state_dir / "fleet.db").exists()


def test_budget_exhaustion_bounds_total_runtime(tmp_path):
    cfg = config(tmp_path)
    seed_helper(cfg)
    controller = Controller(PollResult(0, encode_snapshot(document()), b""))
    started = time.monotonic()

    result = run_smoke(
        cfg,
        "gpu1",
        discover_fn=direct_inventory,
        poll_controller=controller,
        poll_gap=0.0,
        budget_seconds=0.0,
    )
    elapsed = time.monotonic() - started

    assert result["phases"] == []
    assert [item["name"] for item in result["skipped"]] == list(PHASES)
    assert all(item["reason"] == "budget_exhausted" for item in result["skipped"])
    assert elapsed < 5
    assert controller.calls == []


def test_poll_gap_default_is_capped_to_thirty_to_sixty_seconds(tmp_path):
    assert poll_gap_seconds(config(tmp_path, poll_interval_seconds=600)) == 60.0
    assert poll_gap_seconds(config(tmp_path, poll_interval_seconds=30)) == 30.0
    assert poll_gap_seconds(config(tmp_path, poll_interval_seconds=45)) == 45.0


def test_cli_smoke_refuses_invalid_target_shape(capsys):
    args = cli.build_parser().parse_args(["smoke", "bad target"])

    assert args.func(args) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "invalid_target",
    }
