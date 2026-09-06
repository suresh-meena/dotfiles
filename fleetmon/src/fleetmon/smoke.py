"""Bounded multi-phase one-host acceptance smoke run."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

from .config import HubConfig
from .discovery import Inventory, Target, admitted_targets, discover
from .state import MAX_STATE_BYTES, HubLock

SMOKE_BUDGET_SECONDS = 180.0
MIN_POLL_GAP_SECONDS = 30.0
MAX_POLL_GAP_SECONDS = 60.0
HTTP_TIMEOUT_SECONDS = 5.0
HTTP_READ_LIMIT_BYTES = 1024 * 1024
BOUNDED_POLL_ERRORS = {
    "timeout",
    "output_overflow",
    "transport",
    "invalid_json",
    "invalid_schema",
    "version_mismatch",
}
PHASES = (
    "admission",
    "helper",
    "poll_first",
    "poll_second",
    "storage",
    "budget",
    "failure_injection",
    "integrity",
    "dashboard",
    "kill_switch",
)


def poll_gap_seconds(config: HubConfig) -> float:
    """Two valid polls about 60 s apart per the plan.

    The gap is clamped to 30..60 s (the hard poll-interval bounds) so the
    default smoke run always stays inside the 180 s budget even when the
    configured interval is very long or at the minimum.
    """

    return max(
        MIN_POLL_GAP_SECONDS, min(config.poll_interval_seconds, MAX_POLL_GAP_SECONDS)
    )


class _BrokenHelperController:
    """Local failure injection; never launches a subprocess or remote command."""

    polling_enabled = True
    disabled_targets: set[str] = set()

    async def poll(self, target: str, argv: list[str], **_: Any) -> Any:
        from .poller import PollResult

        return PollResult(1, b"", b"")


class _CountingController:
    """Records would-be launches; used to prove the kill switch blocks them."""

    def __init__(self) -> None:
        self.polling_enabled = True
        self.disabled_targets: set[str] = set()
        self.calls: list[list[str]] = []

    async def poll(self, target: str, argv: list[str], **_: Any) -> Any:
        from .poller import PollResult

        self.calls.append(argv)
        return PollResult(None, b"", b"")


def _runtime(
    config: HubConfig,
    discover_fn: Callable[..., Inventory],
    poll_controller: Any,
) -> Any:
    from .service import HubRuntime

    return HubRuntime(config, discover_fn=discover_fn, poll_controller=poll_controller)


def _temp_config(config: HubConfig, state_dir: Path) -> HubConfig:
    return replace(
        config,
        state_dir=state_dir,
        database_path=state_dir / "fleet.db",
        backup_dir=None,
    )


def _match_target(inventory: Inventory, target_name: str) -> Target | None:
    matches = [item for item in inventory.direct_targets if item.name == target_name]
    return matches[0] if matches else None


def _phase_admission(
    runtime: Any, config: HubConfig, target_name: str, holder: dict[str, Any]
) -> dict[str, Any]:
    inventory = runtime.refresh_inventory()
    holder["inventory"] = inventory
    include = {config.include_tag} if config.include_tag else set()
    exclude = {config.exclude_tag} if config.exclude_tag else set()
    matches = [
        item
        for item in admitted_targets(inventory, include, exclude)
        if item.name == target_name
    ]
    if not matches:
        return {"ok": False, "error": "target_not_admitted"}
    protocol = inventory.protocols.get(matches[0].protocol)
    return {
        "ok": True,
        "role": matches[0].role,
        "protocol": matches[0].protocol,
        "protocol_kind": protocol.kind if protocol is not None else None,
        "transport_probe": "fleetctl_inventory",
    }


def _phase_helper(runtime: Any, target_name: str) -> dict[str, Any]:
    helper = runtime._helper_path(target_name)
    if helper is None:
        return {"ok": True, "skipped": "helper_missing"}
    return {"ok": True, "helper_path": helper}


def _read_helper_path(config: HubConfig, target_name: str) -> str | None:
    """Read-only helper lookup for use while the hub holds the lock."""

    path = config.state_dir / "runtime.json"
    try:
        if not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    targets = data.get("targets") if isinstance(data, dict) else None
    entry = targets.get(target_name) if isinstance(targets, dict) else None
    helper = entry.get("helper_path") if isinstance(entry, dict) else None
    if isinstance(helper, str) and helper and Path(helper).is_absolute():
        return helper
    return None


def _phase_poll(runtime: Any, target: Target) -> tuple[dict[str, Any], str | None]:
    outcome = asyncio.run(runtime.poll_target(target))
    rows = runtime.db.query(
        """
        SELECT poll_id, started_at, ended_at, outcome FROM polls
        WHERE target=? ORDER BY started_at DESC, rowid DESC LIMIT 1
        """,
        (target.name,),
    )
    row = dict(rows[0]) if rows else None
    poll_id = row["poll_id"] if row else None
    stored = bool(
        poll_id
        and runtime.db.query(
            "SELECT poll_id FROM host_samples WHERE poll_id=? AND target=?",
            (poll_id, target.name),
        )
    )
    detail = {
        "ok": bool(
            outcome == "ok" and row is not None and row["outcome"] == "ok" and stored
        ),
        "outcome": outcome,
        "poll_id": poll_id,
        "host_sample_stored": stored,
        "wall_seconds": (
            round(row["ended_at"] - row["started_at"], 3) if row is not None else None
        ),
    }
    return detail, poll_id if detail["ok"] else None


def _count(runtime: Any, sql: str, args: tuple[Any, ...]) -> int:
    rows = runtime.db.query(sql, args)
    return int(rows[0][0]) if rows else 0


def _phase_storage(
    runtime: Any, target_name: str, poll_ids: list[str]
) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {}
    ok = True
    for index, poll_id in enumerate(poll_ids):
        row_counts = {
            "host_samples": _count(
                runtime,
                "SELECT COUNT(*) FROM host_samples WHERE poll_id=? AND target=?",
                (poll_id, target_name),
            ),
            "gpu_samples": _count(
                runtime,
                "SELECT COUNT(*) FROM gpu_samples WHERE poll_id=?",
                (poll_id,),
            ),
            "user_samples": _count(
                runtime,
                "SELECT COUNT(*) FROM user_samples WHERE poll_id=?",
                (poll_id,),
            ),
        }
        # current_processes is a per-target replace-on-each-poll view; only
        # the newest poll's rows are expected to remain.
        if index == len(poll_ids) - 1:
            row_counts["current_processes"] = _count(
                runtime,
                "SELECT COUNT(*) FROM current_processes WHERE target=? AND poll_id=?",
                (target_name, poll_id),
            )
        counts[poll_id] = row_counts
        ok = ok and all(row_counts.values())
    return {"ok": ok, "counts": counts}


def _phase_budget(runtime: Any, poll_ids: list[str]) -> dict[str, Any]:
    report: dict[str, dict[str, Any]] = {}
    for poll_id in poll_ids:
        poll_rows = runtime.db.query(
            "SELECT started_at, ended_at FROM polls WHERE poll_id=?", (poll_id,)
        )
        sample_rows = runtime.db.query(
            """
            SELECT collection_duration_seconds, observation_duration_seconds
            FROM host_samples WHERE poll_id=?
            """,
            (poll_id,),
        )
        wall = (
            poll_rows[0]["ended_at"] - poll_rows[0]["started_at"] if poll_rows else None
        )
        report[poll_id] = {
            "poll_wall_seconds": (
                round(wall, 3) if isinstance(wall, (int, float)) else None
            ),
            "helper_collection_seconds": (
                sample_rows[0]["collection_duration_seconds"] if sample_rows else None
            ),
            "helper_observation_seconds": (
                sample_rows[0]["observation_duration_seconds"] if sample_rows else None
            ),
        }
    return {"ok": True, "budget": report}


def _phase_failure_injection(
    config: HubConfig,
    target_name: str,
    inventory: Inventory | None,
    discover_fn: Callable[..., Inventory],
) -> dict[str, Any]:
    lookup = (
        (lambda *args, **kwargs: inventory) if inventory is not None else discover_fn
    )
    with tempfile.TemporaryDirectory(prefix="fleetmon-smoke-") as temp:
        runtime = _runtime(
            _temp_config(config, Path(temp) / "state"),
            lookup,
            _BrokenHelperController(),
        )
        try:
            used = runtime.refresh_inventory()
            target = _match_target(used, target_name)
            if target is None:
                return {"ok": False, "error": "target_not_admitted"}
            runtime.state.update_target(
                target_name, helper_path="/nonexistent/fleetmon-snapshot"
            )
            outcome = asyncio.run(runtime.poll_target(target))
            polls = runtime.db.query(
                "SELECT outcome, error FROM polls WHERE target=?", (target_name,)
            )
            samples = runtime.db.query("SELECT poll_id FROM host_samples")
            ok = bool(
                outcome in BOUNDED_POLL_ERRORS
                and len(polls) == 1
                and polls[0]["outcome"] == "error"
                and polls[0]["error"] in BOUNDED_POLL_ERRORS
                and not samples
            )
            return {
                "ok": ok,
                "outcome": outcome,
                "error_rows": [dict(row) for row in polls],
                "host_samples": len(samples),
            }
        finally:
            runtime.close()


def _phase_integrity(config: HubConfig) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(
            f"file:{quote(str(config.database_path), safe='/')}?mode=ro",
            uri=True,
            timeout=2,
        )
    except sqlite3.Error:
        return {"ok": False, "error": "integrity_unavailable"}
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    except sqlite3.Error:
        return {"ok": False, "error": "integrity_unavailable"}
    finally:
        conn.close()
    return {
        "ok": integrity == ["ok"] and violations == 0,
        "integrity_check": integrity[0] if integrity else "unknown",
        "foreign_key_violations": violations,
    }


def _bind_url(config: HubConfig, path: str) -> str:
    host = config.bind_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{config.bind_port}{path}"


def _http_get(url: str) -> tuple[int | None, bytes]:
    try:
        with urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status, response.read(HTTP_READ_LIMIT_BYTES)
    except HTTPError as exc:
        return exc.code, b""
    except (OSError, ValueError):
        return None, b""


def _phase_dashboard(
    runtime: Any, config: HubConfig, target_name: str, expected_poll_id: str | None
) -> dict[str, Any]:
    if runtime is not None:
        rows = [dict(row) for row in runtime.overview()]
        match = next((row for row in rows if row.get("target") == target_name), None)
        sample_poll_id = match.get("sample_poll_id") if match is not None else None
        ok = match is not None and (
            expected_poll_id is None or sample_poll_id == expected_poll_id
        )
        return {
            "ok": ok,
            "mode": "direct",
            "target_visible": match is not None,
            "sample_poll_id": sample_poll_id,
            "gpu_count": match.get("gpu_count") if match is not None else None,
            "visible_users": match.get("visible_users") if match is not None else None,
        }
    ready_status, _ = _http_get(_bind_url(config, "/readyz"))
    overview_status, payload = _http_get(_bind_url(config, "/api/overview"))
    visible = False
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        document = {}
    items = document.get("items") if isinstance(document, dict) else None
    if isinstance(items, list):
        visible = any(
            isinstance(item, dict) and item.get("target") == target_name
            for item in items
        )
    return {
        "ok": bool(ready_status == 200 and overview_status == 200 and visible),
        "mode": "http",
        "readyz_status": ready_status,
        "overview_status": overview_status,
        "target_visible": visible,
    }


def _phase_kill_switch(
    config: HubConfig,
    target_name: str,
    inventory: Inventory | None,
    discover_fn: Callable[..., Inventory],
) -> dict[str, Any]:
    lookup = (
        (lambda *args, **kwargs: inventory) if inventory is not None else discover_fn
    )
    with tempfile.TemporaryDirectory(prefix="fleetmon-smoke-") as temp:
        controller = _CountingController()
        runtime = _runtime(
            _temp_config(replace(config, polling_enabled=False), Path(temp) / "state"),
            lookup,
            controller,
        )
        try:
            used = runtime.refresh_inventory()
            target = _match_target(used, target_name)
            if target is None:
                return {"ok": False, "error": "target_not_admitted"}
            outcome = asyncio.run(runtime.poll_target(target))
            return {
                "ok": bool(outcome == "polling_disabled" and not controller.calls),
                "outcome": outcome,
                "remote_calls": len(controller.calls),
            }
        finally:
            runtime.close()


def run_smoke(
    config: HubConfig,
    target_name: str,
    *,
    discover_fn: Callable[..., Inventory] = discover,
    poll_controller: Any = None,
    poll_gap: float | None = None,
    budget_seconds: float = SMOKE_BUDGET_SECONDS,
    lock_factory: Callable[[Path], HubLock] = HubLock,
) -> dict[str, Any]:
    """Run the ten-point one-host acceptance procedure and return its report."""

    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": True,
        "target": target_name,
        "phases": [],
        "skipped": [],
    }
    phases: list[dict[str, Any]] = result["phases"]
    skipped: list[dict[str, Any]] = result["skipped"]
    holder: dict[str, Any] = {}
    poll_ids: list[str] = []

    def skip(name: str, reason: str) -> None:
        skipped.append({"name": name, "reason": reason})

    def expired() -> bool:
        return time.monotonic() - started >= budget_seconds

    def run(name: str, factory: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        if expired():
            skip(name, "budget_exhausted")
            return None
        try:
            phase = factory()
        except Exception as exc:
            phase = {
                "ok": False,
                "error": "phase_error",
                "exception": type(exc).__name__,
            }
        phase["name"] = name
        phases.append(phase)
        if not phase["ok"]:
            result["ok"] = False
        return phase

    lock = None
    lock_held = False
    try:
        lock = lock_factory(config.state_dir / "hub.lock")
        lock.acquire()
    except RuntimeError:
        lock = None
        lock_held = True

    runtime = None
    try:
        if lock_held:
            for name in ("admission", "poll_first", "poll_second", "storage", "budget"):
                skip(name, "hub_running")
            run(
                "helper",
                lambda: {
                    "ok": True,
                    **(
                        {"skipped": "helper_missing"}
                        if _read_helper_path(config, target_name) is None
                        else {"helper_path": _read_helper_path(config, target_name)}
                    ),
                },
            )
            run(
                "failure_injection",
                lambda: _phase_failure_injection(
                    config, target_name, None, discover_fn
                ),
            )
            run("integrity", lambda: _phase_integrity(config))
            run(
                "dashboard",
                lambda: _phase_dashboard(None, config, target_name, None),
            )
            run(
                "kill_switch",
                lambda: _phase_kill_switch(config, target_name, None, discover_fn),
            )
            return result

        runtime = _runtime(config, discover_fn, poll_controller)
        admission = run(
            "admission",
            lambda: _phase_admission(runtime, config, target_name, holder),
        )
        if admission is None or not admission["ok"]:
            reason = "budget_exhausted" if admission is None else "admission_failed"
            for name in PHASES[1:]:
                skip(name, reason)
            return result

        helper_phase = run("helper", lambda: _phase_helper(runtime, target_name))
        helper_missing = (
            helper_phase is not None and helper_phase.get("skipped") == "helper_missing"
        )
        helper_ok = helper_phase is not None and bool(helper_phase.get("ok"))
        polls_allowed = helper_ok and not helper_missing
        if not polls_allowed:
            reason = "helper_missing" if helper_missing else "helper_lookup_failed"
            if helper_phase is None:
                reason = "budget_exhausted"
            for name in ("poll_first", "poll_second", "storage", "budget"):
                skip(name, reason)

        polls_ready = False
        if polls_allowed:
            target = _match_target(holder["inventory"], target_name)
            if target is None:
                for name in ("poll_first", "poll_second", "storage", "budget"):
                    skip(name, "target_not_admitted")
            else:
                first = run("poll_first", lambda: _phase_poll(runtime, target)[0])
                if first is None:
                    for name in ("poll_second", "storage", "budget"):
                        skip(name, "budget_exhausted")
                elif not first["ok"]:
                    skip("poll_second", "poll_first_failed")
                    skip("storage", "polls_missing")
                    skip("budget", "polls_missing")
                else:
                    if first["poll_id"] is not None:
                        poll_ids.append(first["poll_id"])
                    gap = poll_gap if poll_gap is not None else poll_gap_seconds(config)
                    remaining = budget_seconds - (time.monotonic() - started)
                    wait = max(0.0, min(gap, remaining))
                    first["wait_seconds"] = round(wait, 3)
                    if wait > 0:
                        time.sleep(wait)
                    second = run("poll_second", lambda: _phase_poll(runtime, target)[0])
                    if second is None:
                        skip("storage", "budget_exhausted")
                        skip("budget", "budget_exhausted")
                    elif not second["ok"] or second["poll_id"] in poll_ids:
                        skip("storage", "polls_missing")
                        skip("budget", "polls_missing")
                    else:
                        poll_ids.append(second["poll_id"])
                        polls_ready = True
                if polls_ready:
                    run(
                        "storage",
                        lambda: _phase_storage(runtime, target_name, list(poll_ids)),
                    )
                    run("budget", lambda: _phase_budget(runtime, list(poll_ids)))

        inventory = holder.get("inventory")
        run(
            "failure_injection",
            lambda: _phase_failure_injection(
                config, target_name, inventory, discover_fn
            ),
        )
        run("integrity", lambda: _phase_integrity(config))
        run(
            "dashboard",
            lambda: _phase_dashboard(
                runtime, config, target_name, poll_ids[-1] if poll_ids else None
            ),
        )
        run(
            "kill_switch",
            lambda: _phase_kill_switch(config, target_name, inventory, discover_fn),
        )
        return result
    finally:
        if runtime is not None:
            runtime.close()
        if lock is not None:
            lock.release()
