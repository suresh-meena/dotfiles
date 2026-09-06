"""Bounded hub orchestration for direct hosts and Slurm login targets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__, notify
from .config import HubConfig, ensure_backup_filesystem
from .database import Database, observation_slots
from .discovery import (
    Inventory,
    Target,
    admitted_targets,
    discover,
    discover_async,
)
from .poller import PollController, PollResult, backoff_seconds
from .protocol import SCHEMA_VERSION, ProtocolError, decode_snapshot
from .slurm import (
    SACCT_COMPAT_FIELDS,
    fleetctl_slurm_argv,
    parse_sacct,
    parse_squeue,
    parse_squeue_text,
    sacct_argv,
    sacct_local_time,
    squeue_argv,
    squeue_text_argv,
)
from .state import OperationalState, sanitize_error

LOG = logging.getLogger(__name__)
SUPPORTED_HELPER_VERSION = str(SCHEMA_VERSION)
SLURM_OUTPUT_LIMIT = 1024 * 1024
RETENTION_INTERVAL_SECONDS = 24 * 60 * 60
MAX_CAPTURE_SKEW_SECONDS = 24 * 60 * 60
STALE_AFTER_POLL_INTERVALS = 2
SCHEDULER_MIN_INTERVAL_SECONDS = 60.0
SCHEDULE_TICK_SECONDS = 0.2
SCHEDULE_MAX_TICK_SECONDS = 1.0
HUB_HEALTH_INTERVAL_SECONDS = 30.0
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
BACKUP_KEEP_COUNT = 7


class HubRuntime:
    def __init__(
        self,
        config: HubConfig,
        *,
        database: Database | None = None,
        discover_fn: Callable[..., Inventory] = discover,
        poll_controller: PollController | None = None,
    ):
        self.config = config
        self.db = database or Database(self.config.database_path)
        self.notify_poster = notify.default_poster
        ensure_backup_filesystem(self.config)
        self.state = OperationalState(self.config.state_dir / "runtime.json")
        self.discover_fn = discover_fn
        self.controller = poll_controller or PollController(
            self.config.ssh_concurrency,
            self.config.launch_interval_seconds,
        )
        self.controller.polling_enabled = self.config.polling_enabled
        self.controller.disabled_targets = set(self.config.disabled_targets)
        self.inventory: Inventory | None = None
        self.admitted_names: set[str] = set()
        self.last_inventory = 0.0
        self.last_retention = 0.0
        self.last_backup_attempt = 0.0
        self.last_backup_success = 0.0
        self.last_backup_path: str | None = None
        self.last_backup_error: str | None = None
        self.started = time.time()

    def _include_tags(self) -> set[str]:
        return {self.config.include_tag} if self.config.include_tag else set()

    def _exclude_tags(self) -> set[str]:
        return {self.config.exclude_tag} if self.config.exclude_tag else set()

    def refresh_inventory(self) -> Inventory:
        """Refresh inventory, retaining the last known good copy on failure."""

        try:
            inventory = self._discover()
        except Exception as exc:
            return self._inventory_failed(exc)
        return self._apply_inventory(inventory)

    async def refresh_inventory_async(self) -> Inventory:
        """Refresh without blocking the hub event loop on fleetctl pipes."""

        try:
            if self.discover_fn is discover:
                inventory = await discover_async(str(self.config.fleetctl_path))
            else:
                inventory = self._discover()
        except Exception as exc:
            return self._inventory_failed(exc)
        return self._apply_inventory(inventory)

    def _discover(self) -> Inventory:
        try:
            return self.discover_fn(str(self.config.fleetctl_path))
        except TypeError:
            return self.discover_fn()

    def _inventory_failed(self, exc: Exception) -> Inventory:
        self.state.data["inventory_error"] = sanitize_error(exc)
        self.state.save()
        if self.inventory is not None:
            return self.inventory
        raise exc

    def _apply_inventory(self, inventory: Inventory) -> Inventory:
        self.inventory = inventory
        self.admitted_names = {
            target.name
            for target in admitted_targets(
                inventory,
                self._include_tags(),
                self._exclude_tags(),
            )
        }
        self.last_inventory = time.time()
        scheduler_names = {target.name for target in inventory.scheduler_targets}
        current_names = {target.name for target in inventory.targets}
        existing = {row["target"]: row for row in self.db.hosts()}

        for target in inventory.targets:
            old = existing.get(target.name)
            helper = old["helper_path"] if old is not None else None
            managed = (
                target.name in self.admitted_names or target.name in scheduler_names
            )
            if managed and (
                not self.config.polling_enabled
                or target.name in self.config.disabled_targets
            ):
                state = "polling_disabled"
            elif target.name in self.admitted_names:
                state = (
                    old["state"]
                    if old is not None and old["state"] in {"live", "partial"}
                    else "helper_missing"
                )
            elif target.name in scheduler_names:
                state = "scheduler"
            else:
                state = "retired"
            self.db.upsert_host(
                target.name,
                target.role,
                target.protocol,
                state,
                helper,
            )

        for target, row in existing.items():
            if target not in current_names:
                self.db.upsert_host(
                    target,
                    row["role"],
                    row["protocol"],
                    "retired",
                )

        self.state.data["inventory_error"] = None
        self.state.save()
        return inventory

    def admitted(self, target: Target) -> bool:
        return target.name in self.admitted_names

    def _has_disk_reserve(self) -> bool:
        try:
            usage = shutil.disk_usage(self.config.database_path.parent)
        except OSError:
            return False
        okay = usage.free >= self.config.disk_reserve_bytes
        self.state.data["disk_low"] = not okay
        if not okay:
            self.db.checkpoint()
        return okay

    async def _notify_gpu_free(self, target: str) -> None:
        if not self.config.notify_url:
            return
        try:
            await self._notify_gpu_free_inner(target)
        except Exception:
            LOG.exception("gpu-free notification evaluation failed")

    def _gpu_freshness_seconds(self) -> float:
        return max(10.0, self.config.poll_interval_seconds * STALE_AFTER_POLL_INTERVALS)

    async def _notify_gpu_free_inner(self, target: str) -> None:
        if not self.config.notify_url:
            return
        now = time.time()
        target_state = self.state.target(target)
        last_success = target_state.get("last_success")
        if (
            last_success is None
            or now - float(last_success) > self._gpu_freshness_seconds()
        ):
            self.state.update_target(target, gpu_free_counts={}, gpu_free_notified={})
            return
        events, new_counts, notified = notify.evaluate_gpu_free(
            self.db.gpu_recent(target),
            target_state.get("gpu_free_counts") or {},
            target_state.get("gpu_free_notified") or {},
            now=now,
            freshness_seconds=self._gpu_freshness_seconds(),
        )
        new_counts, notified = notify.prune_tracking(new_counts, notified)
        old_counts = target_state.get("gpu_free_counts") or {}
        old_notified = target_state.get("gpu_free_notified") or {}
        if not events and new_counts == old_counts and notified == old_notified:
            return
        if events:
            notified, next_retry = await asyncio.to_thread(
                notify.notify_target,
                target,
                events,
                notified,
                float(target_state.get("gpu_free_next_retry", 0) or 0),
                now,
                self.config.notify_url,
                self.notify_poster,
            )
            self.state.update_target(
                target,
                gpu_free_counts=new_counts,
                gpu_free_notified=notified,
                gpu_free_next_retry=next_retry,
            )
        else:
            self.state.update_target(
                target, gpu_free_counts=new_counts, gpu_free_notified=notified
            )

    def _helper_path(self, target: str) -> str | None:
        helper = self.state.target(target).get("helper_path")
        if not helper:
            rows = self.db.query(
                "SELECT helper_path FROM hosts WHERE target=?", (target,)
            )
            helper = rows[0]["helper_path"] if rows else None
        if not isinstance(helper, str) or not Path(helper).is_absolute():
            return None
        return helper

    @staticmethod
    def _protocol_error_code(exc: ProtocolError) -> str:
        text = str(exc).lower()
        if "json" in text:
            return "invalid_json"
        if "version" in text:
            return "version_mismatch"
        return "invalid_schema"

    @staticmethod
    def _backoff(target: str, failures: int) -> float:
        base = backoff_seconds(failures)
        digest = hashlib.blake2s(
            f"{target}:{failures}".encode(), digest_size=2
        ).digest()
        fraction = int.from_bytes(digest, "big") / 65_535
        return base * (0.9 + 0.2 * fraction)

    async def poll_target(self, target: Target, helper_path: str | None = None) -> str:
        if (
            not self.config.polling_enabled
            or target.name in self.config.disabled_targets
        ):
            self.db.upsert_host(
                target.name, target.role, target.protocol, "polling_disabled"
            )
            return "polling_disabled"
        if not self.admitted(target):
            return "retired"
        prior_state = self.state.target(target.name)
        if float(prior_state.get("next_retry", 0) or 0) > time.time():
            return "backoff"
        if not self._has_disk_reserve():
            return "disk_low"

        helper = helper_path or self._helper_path(target.name)
        if helper is None:
            self.db.upsert_host(
                target.name, target.role, target.protocol, "helper_missing"
            )
            return "helper_missing"

        poll_id = uuid.uuid4().hex
        started = time.time()
        argv = [
            str(self.config.fleetctl_path),
            "exec",
            "--target",
            target.name,
            "--",
            helper,
        ]
        try:
            result = await self.controller.poll(
                target.name,
                argv,
                timeout=self.config.remote_timeout_seconds,
                stdout_limit=self.config.snapshot_stdout_bytes,
                stderr_limit=self.config.stderr_bytes,
            )
        except (OSError, ValueError) as exc:
            LOG.warning(
                "poll launch failed for %s: %s", target.name, type(exc).__name__
            )
            result = PollResult(None, b"", b"")
        if result is None:
            return "polling_disabled"

        if result.overflow:
            code = "output_overflow"
        elif result.timed_out:
            code = "timeout"
        elif result.returncode != 0:
            code = "transport"
        else:
            try:
                document = decode_snapshot(
                    result.stdout,
                    max_bytes=self.config.snapshot_stdout_bytes,
                )
                if document["helper_version"] != SUPPORTED_HELPER_VERSION:
                    raise ProtocolError("unsupported helper version")
                received = time.time()
                captured = datetime.fromisoformat(
                    document["captured_at"].replace("Z", "+00:00")
                ).timestamp()
                if abs(received - captured) > MAX_CAPTURE_SKEW_SECONDS:
                    raise ProtocolError("capture time outside accepted skew")
                self.db.snapshot(
                    poll_id,
                    target.name,
                    document,
                    received,
                    started_at=started,
                )
                self._gpu_window_cache = None
                # Classification keeps the latest two observations; thin the
                # now-older history to the configured cadence so fast polling
                # cannot multiply stored rows.
                self.db.downsample_history(
                    target.name, self.config.history_interval_seconds
                )
                self.state.update_target(
                    target.name,
                    failures=0,
                    last_success=received,
                    backoff=0,
                    next_retry=0,
                    last_error=None,
                )
                await self._notify_gpu_free(target.name)
                return "ok"
            except ProtocolError as exc:
                code = self._protocol_error_code(exc)

        ended = time.time()
        self.db.record_error(
            poll_id,
            target.name,
            code,
            ended,
            started_at=started,
        )
        previous_failures = int(prior_state.get("failures", 0))
        failures = min(previous_failures + 1, 4)
        delay = self._backoff(target.name, failures)
        self.state.update_target(
            target.name,
            failures=failures,
            backoff=delay,
            next_retry=ended + delay,
            last_error=code,
        )
        host_state = "unreachable" if code in {"transport", "timeout"} else code
        self.db.record_host_failure(
            target.name,
            target.role,
            target.protocol,
            host_state,
            code,
            delay,
        )
        return code

    async def _poll_slurm_command(
        self, target: Target, key: str, command: list[str]
    ) -> PollResult | None:
        argv = [
            str(self.config.fleetctl_path),
            *fleetctl_slurm_argv(target.name, command, self.config.scheduler_timezone),
        ]
        try:
            return await self.controller.poll(
                f"slurm:{target.name}:{key}",
                argv,
                timeout=self.config.remote_timeout_seconds,
                stdout_limit=SLURM_OUTPUT_LIMIT,
                stderr_limit=self.config.stderr_bytes,
            )
        except (OSError, ValueError):
            return PollResult(None, b"", b"")

    async def poll_slurm_target(self, target: Target) -> str:
        if (
            not self.config.polling_enabled
            or target.name in self.config.disabled_targets
        ):
            return "polling_disabled"
        scheduler_names = (
            {item.name for item in self.inventory.scheduler_targets}
            if self.inventory
            else set()
        )
        if target.name not in scheduler_names:
            return "retired"
        target_state = self.state.target(target.name)
        if float(target_state.get("slurm_next_retry", 0) or 0) > time.time():
            return "backoff"
        if not self._has_disk_reserve():
            return "disk_low"

        queue_result = await self._poll_slurm_command(target, "squeue", squeue_argv())
        if queue_result is None:
            return "polling_disabled"
        if queue_result.timed_out or queue_result.overflow:
            return self._slurm_failure(
                target, "timeout" if queue_result.timed_out else "output_overflow"
            )

        jobs: list[dict[str, Any]] | None = None
        if queue_result.returncode == 0:
            try:
                jobs = parse_squeue(queue_result.stdout)
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                # Older Slurm releases can accept --json yet emit unusable
                # output. Try the single fixed-column fallback once.
                jobs = None
        if jobs is None:
            queue_result = await self._poll_slurm_command(
                target, "squeue-text", squeue_text_argv()
            )
            if queue_result is None:
                return "polling_disabled"
            if queue_result.timed_out or queue_result.overflow:
                return self._slurm_failure(
                    target,
                    "timeout" if queue_result.timed_out else "output_overflow",
                )
            if queue_result.returncode != 0:
                return self._slurm_failure(target, "transport")
            try:
                jobs = parse_squeue_text(queue_result.stdout)
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                return self._slurm_failure(target, "invalid_json")
        self.db.upsert_slurm_jobs(target.name, jobs)
        # Queue success is the scheduler freshness signal; accounting is
        # intentionally throttled and must not make a healthy queue look stale.
        watermark = self.state.target(target.name).get("sacct_watermark")
        self.db.set_slurm_state(target.name, watermark, "live")

        now = datetime.now(timezone.utc)
        last_sacct_attempt = float(
            target_state.get("last_sacct_attempt", target_state.get("last_sacct", 0))
            or 0
        )
        if time.time() - last_sacct_attempt >= 300:
            watermark = target_state.get("sacct_watermark")
            minimum_start = now - timedelta(hours=24)
            if isinstance(watermark, str):
                try:
                    start = datetime.fromisoformat(watermark)
                    if start.tzinfo is None:
                        raise ValueError("watermark must include timezone")
                except ValueError:
                    start = minimum_start
                else:
                    start -= timedelta(minutes=5)
            else:
                start = minimum_start
            # A stale watermark must not turn recovery into an unbounded
            # accounting query. Future/invalid clocks also fall back safely.
            if start < minimum_start or start > now:
                start = minimum_start
            tz = self.config.scheduler_timezone
            accounting = await self._poll_slurm_command(
                target,
                "sacct",
                sacct_argv(
                    sacct_local_time(start, tz),
                    sacct_local_time(now, tz),
                    tz,
                ),
            )
            rows: list[dict[str, str | None]] | None = None
            if (
                accounting is not None
                and accounting.returncode == 0
                and not accounting.timed_out
                and not accounting.overflow
            ):
                try:
                    rows = parse_sacct(accounting.stdout)
                except (UnicodeDecodeError, ValueError):
                    return self._slurm_accounting_failure(target, "invalid_json")
            elif accounting is not None and (
                accounting.returncode != 0
                and not accounting.timed_out
                and not accounting.overflow
            ):
                # Older schedulers reject newer accounting fields; one bounded
                # retry with the compatibility field set.
                accounting = await self._poll_slurm_command(
                    target,
                    "sacct-compat",
                    sacct_argv(
                        sacct_local_time(start, tz),
                        sacct_local_time(now, tz),
                        tz,
                        compat=True,
                    ),
                )
                if (
                    accounting is not None
                    and accounting.returncode == 0
                    and not accounting.timed_out
                    and not accounting.overflow
                ):
                    try:
                        rows = parse_sacct(
                            accounting.stdout, fields=SACCT_COMPAT_FIELDS
                        )
                    except (UnicodeDecodeError, ValueError):
                        return self._slurm_accounting_failure(target, "invalid_json")
            if rows is not None:
                self.db.upsert_slurm_jobs(target.name, rows)
                new_watermark = now.isoformat()
                self.state.update_target(
                    target.name,
                    last_sacct=time.time(),
                    last_sacct_attempt=time.time(),
                    sacct_watermark=new_watermark,
                    sacct_last_error=None,
                )
                self.db.set_slurm_state(target.name, new_watermark, "live")
            else:
                code = "transport"
                if accounting is not None:
                    if accounting.timed_out:
                        code = "timeout"
                    elif accounting.overflow:
                        code = "output_overflow"
                return self._slurm_accounting_failure(target, code)
        self.state.update_target(
            target.name,
            slurm_failures=0,
            slurm_next_retry=0,
            slurm_last_error=None,
        )
        return "ok"

    def _slurm_failure(self, target: Target, code: str) -> str:
        target_state = self.state.target(target.name)
        failures = min(int(target_state.get("slurm_failures", 0)) + 1, 4)
        delay = self._backoff(f"slurm:{target.name}", failures)
        self.state.update_target(
            target.name,
            slurm_failures=failures,
            slurm_next_retry=time.time() + delay,
            slurm_last_error=code,
        )
        watermark = target_state.get("sacct_watermark")
        self.db.set_slurm_state(target.name, watermark, "error", code)
        return code

    def _slurm_accounting_failure(self, target: Target, code: str) -> str:
        """Record degraded accounting without hiding a fresh queue result."""

        target_state = self.state.target(target.name)
        watermark = target_state.get("sacct_watermark")
        self.state.update_target(
            target.name,
            last_sacct_attempt=time.time(),
            sacct_last_error=code,
            slurm_failures=0,
            slurm_next_retry=0,
            slurm_last_error=None,
        )
        self.db.set_slurm_state(target.name, watermark, "live", code)
        return "partial"

    def retain(self) -> int:
        deleted = self.db.retain(time.time() - self.config.retention_days * 86_400)
        self.db.checkpoint()
        self.last_retention = time.time()
        return deleted

    def maybe_backup(self) -> None:
        """Run at most one daily online backup; failures are a status field.

        There is deliberately no retry loop: the next attempt happens on a
        later daily cycle, and the bounded error never contains poll output.
        """

        if self.config.backup_dir is None:
            return
        now = time.time()
        if (
            self.last_backup_attempt
            and now - self.last_backup_attempt < BACKUP_INTERVAL_SECONDS
        ):
            return
        self.last_backup_attempt = now
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.config.backup_dir / f"fleet-{timestamp}.db"
        try:
            self.db.backup(destination)
        except Exception as exc:
            self.last_backup_error = sanitize_error(exc)
            return
        self.last_backup_error = None
        self.last_backup_success = now
        self.last_backup_path = str(destination)
        self._prune_backups(self.config.backup_dir, BACKUP_KEEP_COUNT)

    @staticmethod
    def _prune_backups(directory: Path, keep: int) -> None:
        """Keep only the newest ``keep`` timestamped fleet backup files."""

        try:
            backups = sorted(
                path
                for path in directory.iterdir()
                if path.name.startswith("fleet-") and path.suffix == ".db"
            )
        except OSError:
            return
        for path in backups[: max(0, len(backups) - keep)]:
            with suppress(OSError):
                path.unlink()

    def create_app(self):
        from .web.app import create_app

        return create_app(
            self,
            bind=self.config.bind_host,
            authenticated=bool(self.config.auth_token),
            auth_token=self.config.auth_token,
        )

    def hosts(self, **_: Any) -> list[dict[str, Any]]:
        return [self._freshen_host(dict(row)) for row in self.db.hosts()]

    def host(self, target: str, **kwargs: Any) -> dict[str, Any] | None:
        value = self.db.host(target, **kwargs)
        if value is None:
            return None
        value = self._freshen_host(value)
        self._annotate_gpu_availability(target, value)
        return value

    def host_charts(self, **kwargs: Any) -> dict[str, Any]:
        return self.db.host_charts(**kwargs)

    def sparklines(self, **_: Any) -> dict[str, Any]:
        return self.db.sparklines()

    def jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.jobs(**kwargs)]

    def overview(self, **_: Any) -> list[dict[str, Any]]:
        rows = [self._freshen_host(dict(row)) for row in self.db.overview()]
        self._annotate_gpu_summary(rows)
        return rows

    def _classify_slots(
        self, slots_by_uuid: dict[str, list[dict[str, Any] | None]]
    ) -> dict[str, tuple[str, str]]:
        now = time.time()
        freshness = self._gpu_freshness_seconds()
        return {
            uuid: notify.classify_gpu_slots(slots, now=now, freshness_seconds=freshness)
            for uuid, slots in slots_by_uuid.items()
        }

    def _annotate_gpu_availability(self, target: str, value: dict[str, Any]) -> None:
        """Attach the shared availability classification to host GPU rows."""

        gpus = value.get("gpus")
        if not isinstance(gpus, list) or not gpus:
            return
        classifications = self._classify_slots(self.db.gpu_recent(target))
        for gpu in gpus:
            if not isinstance(gpu, dict):
                continue
            gpu["availability"], gpu["reason"] = classifications.get(
                gpu.get("uuid") or "", (notify.GPU_UNKNOWN, "no_observation")
            )
            gpu["received_at"] = value.get("last_received")
            gpu["freshness_seconds"] = self._gpu_freshness_seconds()
            if value.get("state") not in {"live", "partial"}:
                gpu["availability"], gpu["reason"] = (
                    notify.GPU_UNKNOWN,
                    "host_unavailable",
                )
            gpu["flag"] = notify.gpu_flag(gpu["availability"])

    GPU_WINDOW_TTL_SECONDS = 2.0

    def _gpu_window_cached(self) -> dict[str, list[dict[str, Any]]]:
        """Serve the fleet GPU window from a short cache shared by readers.

        The dashboard polls several endpoints every couple of seconds; each
        of them needs the same two-observation window per target. One
        computation per TTL keeps real-time refreshes cheap without changing
        what any single request sees.
        """

        now = time.time()
        cached = getattr(self, "_gpu_window_cache", None)
        if cached is not None and now - cached[0] <= self.GPU_WINDOW_TTL_SECONDS:
            return cached[1]
        window = self.db.gpu_window()
        self._gpu_window_cache = (now, window)
        return window

    def _annotate_gpu_summary(self, rows: list[dict[str, Any]]) -> None:
        """Add per-host idle/busy/unknown GPU counts from the shared window."""

        if not rows:
            return
        summaries: dict[str, dict[str, int]] = {}
        for target, observations in self._gpu_window_cached().items():
            counts = {"idle": 0, "busy": 0, "unknown": 0}
            for _uuid, (availability, _reason) in self._classify_slots(
                observation_slots(observations)
            ).items():
                counts[availability] += 1
            summaries[target] = counts
        for row in rows:
            counts = summaries.get(row.get("target") or "", {})
            if row.get("state") not in {"live", "partial"}:
                counts = {"unknown": sum(counts.values())}
            row["gpu_idle"] = counts.get("idle", 0)
            row["gpu_busy"] = counts.get("busy", 0)
            row["gpu_unknown"] = counts.get("unknown", 0)
            # Hardened flag view: every GPU that is not provably idle or
            # busy from fresh data is stale.
            row["gpu_stale"] = counts.get("unknown", 0)

    MAX_IDLE_GPU_OWNERS = 4

    def idle_gpus(self, **_: Any) -> dict[str, Any]:
        """One bounded read-only listing of every known latest GPU.

        Pure stored data: no API-triggered remote calls. Each row carries the
        latest telemetry plus the shared availability classification and its
        reason, built from the two latest consecutive observations, the
        hardened flag, and the current owners of the GPU's allocations.
        """

        now = time.time()
        freshness = self._gpu_freshness_seconds()
        items: list[dict[str, Any]] = []
        summary = {"idle": 0, "busy": 0, "unknown": 0}
        states = {
            row["target"]: self._freshen_host(dict(row))["state"]
            for row in self.db.hosts()
        }
        for target, observations in sorted(self._gpu_window_cached().items()):
            owners_by_uuid = self._gpu_owners(target)
            rows: list[
                tuple[int, str, dict[str, Any], tuple[Any, list[dict[str, Any] | None]]]
            ] = []
            for gpu_uuid, slots in observation_slots(observations).items():
                gpu: dict[str, Any] = {}
                received: Any = None
                for slot in slots:
                    if isinstance(slot, dict) and isinstance(slot.get("gpu"), dict):
                        gpu, received = slot["gpu"], slot.get("received_at")
                        break
                idx = gpu.get("idx")
                rows.append(
                    (
                        idx if isinstance(idx, int) else 0,
                        gpu_uuid,
                        gpu,
                        (received, slots),
                    )
                )
            rows.sort(key=lambda row: (row[0], row[1]))
            for _idx, gpu_uuid, gpu, (received, slots) in rows:
                availability, reason = notify.classify_gpu_slots(
                    slots, now=now, freshness_seconds=freshness
                )
                if states.get(target) not in {"live", "partial"}:
                    availability, reason = notify.GPU_UNKNOWN, "host_unavailable"
                flag = notify.gpu_flag(availability)
                vram_total = gpu.get("vram_total")
                vram_used = gpu.get("vram_used")
                items.append(
                    {
                        "target": target,
                        "uuid": gpu_uuid,
                        "idx": gpu.get("idx"),
                        "model": gpu.get("model"),
                        "utilization": gpu.get("utilization"),
                        "vram_total": vram_total,
                        "vram_used": vram_used,
                        "vram_free": (
                            vram_total - vram_used
                            if isinstance(vram_total, int)
                            and isinstance(vram_used, int)
                            and vram_total >= vram_used
                            else None
                        ),
                        "compute_process_count": gpu.get("compute_process_count"),
                        "received_at": received,
                        "freshness_seconds": freshness,
                        "age_seconds": (
                            round(max(0.0, now - received), 3)
                            if isinstance(received, (int, float))
                            else None
                        ),
                        "availability": availability,
                        "reason": reason,
                        "flag": flag,
                        "owners": owners_by_uuid.get(gpu_uuid, []),
                    }
                )
                summary[availability] += 1
        return {"items": items, "summary": summary}

    def _gpu_owners(self, target: str) -> dict[str, list[str]]:
        """Map GPU uuid to the distinct owners of its current allocations.

        Reads the stored current-process tables only (no remote work). Owner
        names are bounded per GPU; a missing username falls back to the uid.
        """

        owners: dict[str, list[str]] = {}
        try:
            rows = self.db.query(
                """
                SELECT a.gpu_uuid, p.username, p.uid
                FROM current_process_allocations AS a
                JOIN current_processes AS p
                  ON p.target = a.target AND p.pid = a.pid
                WHERE a.target = ?
                """,
                (target,),
            )
        except Exception:
            return owners
        for row in rows:
            uuid = row["gpu_uuid"]
            if not isinstance(uuid, str) or not uuid:
                continue
            username = row["username"]
            uid = row["uid"]
            owner = (
                username
                if isinstance(username, str) and username
                else (f"uid {uid}" if isinstance(uid, int) else "unknown")
            )
            names = owners.setdefault(uuid, [])
            if owner not in names and len(names) < self.MAX_IDLE_GPU_OWNERS:
                names.append(owner)
        return owners

    def _freshen_host(self, row: dict[str, Any]) -> dict[str, Any]:
        """Derive freshness at read time without mutating persisted history."""

        state = row.get("state")
        now = time.time()
        threshold = self._gpu_freshness_seconds()
        if state in {"live", "partial"}:
            received = row.get("last_received")
            if not isinstance(received, (int, float)) or now - received > threshold:
                row["state"] = "stale"
        elif state == "scheduler":
            threshold = (
                self.config.scheduler_interval_seconds * STALE_AFTER_POLL_INTERVALS
            )
            states = self.db.query(
                "SELECT state, updated_at FROM slurm_poll_state WHERE target=?",
                (row.get("target"),),
            )
            if states:
                scheduler = states[0]
                updated = scheduler["updated_at"]
                if (
                    scheduler["state"] != "live"
                    or not isinstance(updated, (int, float))
                    or now - updated > threshold
                ):
                    row["state"] = "slurm_stale"
        return row

    @staticmethod
    def _hub_rss_bytes() -> int | None:
        """Read the hub's current resident size without extra dependencies."""
        try:
            with open("/proc/self/statm", encoding="ascii") as handle:
                resident = int(handle.read().split()[1])
            return resident * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None

    HUB_CPU_BUDGET_PERCENT = 5.0
    HUB_RSS_BUDGET_BYTES = 250 * 1024 * 1024

    def _hub_cpu_percent(self, now: float) -> float | None:
        """Return hub CPU as percent of one core since the previous call."""

        try:
            with open("/proc/self/stat", encoding="ascii") as handle:
                fields = handle.read().split()
            ticks = int(fields[13]) + int(fields[14])
        except (OSError, ValueError, IndexError):
            self._last_cpu_ticks = None
            return None
        previous = getattr(self, "_last_cpu_ticks", None)
        previous_time = getattr(self, "_last_cpu_time", None)
        self._last_cpu_ticks = ticks
        self._last_cpu_time = now
        if previous is None or previous_time is None or now <= previous_time:
            return None
        clock_ticks = os.sysconf("SC_CLK_TCK")
        elapsed = now - previous_time
        return max(0.0, (ticks - previous) / clock_ticks / elapsed * 100.0)

    def _hub_health(self, cpu_percent: float | None, rss_bytes: int | None) -> None:
        """Track resource-budget breaches and alert with backoff, at most."""

        over_cpu = cpu_percent is not None and cpu_percent > self.HUB_CPU_BUDGET_PERCENT
        over_rss = rss_bytes is not None and rss_bytes > self.HUB_RSS_BUDGET_BYTES
        streak = int(self.state.data.get("hub_breach_streak", 0) or 0)
        streak = streak + 1 if (over_cpu or over_rss) else 0
        self.state.data["hub_breach_streak"] = streak
        self.state.data["hub_cpu_percent"] = cpu_percent
        self.state.data["hub_overloaded"] = bool(streak >= 2)
        self.state.save()
        if streak < 2 or not self.config.notify_url:
            return
        now = time.time()
        if now < float(self.state.data.get("hub_health_next_retry", 0) or 0):
            return
        payload = {
            "event": "hub_overloaded",
            "cpu_percent": round(cpu_percent, 2) if cpu_percent is not None else None,
            "rss_mib": round(rss_bytes / (1024 * 1024), 1) if rss_bytes else None,
        }
        code = self.notify_poster(self.config.notify_url, payload)
        if code == "ok":
            self.state.data["hub_health_notified"] = now
        self.state.data["hub_health_next_retry"] = now + 300.0
        self.state.save()

    def _recent_poll_stats(self) -> dict[str, Any]:
        """Aggregate the last hour of polls with one bounded query."""
        rows = self.db.query(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) AS errors,
                   AVG(ended_at - started_at) AS latency
            FROM polls WHERE ended_at>=?
            """,
            (time.time() - 3600,),
        )
        row = rows[0] if rows else None
        if row is None:
            return {
                "recent_polls_total": 0,
                "recent_poll_errors": 0,
                "recent_error_rate": None,
                "recent_avg_latency_seconds": None,
            }
        total = int(row["total"])
        errors = int(row["errors"] or 0)
        latency = row["latency"]
        if not isinstance(latency, (int, float)):
            latency = None
        return {
            "recent_polls_total": total,
            "recent_poll_errors": errors,
            "recent_error_rate": round(errors / total, 4) if total else None,
            "recent_avg_latency_seconds": latency,
        }

    def hub_status(self) -> dict[str, Any]:
        try:
            free_disk = shutil.disk_usage(self.config.database_path.parent).free
        except OSError:
            free_disk = None
        try:
            db_size = self.config.database_path.stat().st_size
        except OSError:
            db_size = None
        try:
            wal_size = Path(f"{self.config.database_path}-wal").stat().st_size
        except OSError:
            wal_size = 0
        if self.config.backup_dir is None:
            backup_status = "backup_disabled"
        elif self.last_backup_error:
            backup_status = "backup_error"
        elif self.last_backup_success:
            backup_status = "backup_ok"
        else:
            backup_status = "backup_pending"
        return {
            "status": "critical" if self.state.data.get("disk_low") else "ready",
            "polling_enabled": self.config.polling_enabled,
            "in_flight": self.controller.active_count,
            "inventory_age_seconds": (
                max(0, time.time() - self.last_inventory)
                if self.last_inventory
                else None
            ),
            "inventory_error": self.state.data.get("inventory_error"),
            "free_disk_bytes": free_disk,
            "database_size_bytes": db_size,
            "wal_size_bytes": wal_size,
            "hub_rss_bytes": self._hub_rss_bytes(),
            "hub_cpu_percent": self.state.data.get("hub_cpu_percent"),
            "hub_overloaded": bool(self.state.data.get("hub_overloaded")),
            "backup_status": backup_status,
            "last_backup_at": self.last_backup_success or None,
            "last_backup_path": self.last_backup_path,
            "last_backup_error": self.last_backup_error,
            "retention_days": self.config.retention_days,
            "uptime_seconds": max(0, time.time() - self.started),
            "version": __version__,
            **self._recent_poll_stats(),
        }

    async def _inventory(self) -> Inventory:
        if (
            self.inventory is None
            or time.time() - self.last_inventory
            >= self.config.inventory_interval_seconds
        ):
            return await self.refresh_inventory_async()
        return self.inventory

    async def run_once(self) -> list[str]:
        inventory = await self._inventory()
        direct = [
            target
            for target in inventory.direct_targets
            if target.name in self.admitted_names
        ]
        tasks = [self.poll_target(target) for target in direct]
        tasks.extend(
            self.poll_slurm_target(target) for target in inventory.scheduler_targets
        )
        if not tasks:
            return []
        return list(await asyncio.gather(*tasks))

    @staticmethod
    def _stagger(target: str, interval: float) -> float:
        digest = hashlib.blake2s(target.encode("utf-8"), digest_size=4).digest()
        return int.from_bytes(digest, "big") / (2**32 - 1) * interval

    def _schedule_interval(self, key: str) -> float:
        """Direct targets honor the configured poll interval; scheduler
        targets keep a conservative cadence of at least 60 seconds."""

        if key.startswith("slurm:"):
            return max(
                SCHEDULER_MIN_INTERVAL_SECONDS,
                self.config.scheduler_interval_seconds,
            )
        return self.config.poll_interval_seconds

    def _desired_schedules(
        self, inventory: Inventory
    ) -> dict[str, tuple[Callable[[Target], Any], Target]]:
        desired: dict[str, tuple[Callable[[Target], Any], Target]] = {}
        if not self.config.polling_enabled:
            return desired
        for target in inventory.direct_targets:
            if target.name in self.admitted_names:
                desired[target.name] = (self.poll_target, target)
        for target in inventory.scheduler_targets:
            desired[f"slurm:{target.name}"] = (self.poll_slurm_target, target)
        return desired

    async def _run_schedule(
        self,
        key: str,
        poller: Callable[[Target], Any],
        target: Target,
    ) -> None:
        """Poll one target on its own cadence, independent of every other.

        A slow or backed-off target only delays its own next attempt; the
        bounded controller still caps concurrency, launch rate, and one poll
        in flight per target.
        """

        await asyncio.sleep(self._stagger(key, self._schedule_interval(key)))
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await poller(target)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("poll schedule failed for %s", key)
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.1, self._schedule_interval(key) - elapsed))

    async def run_forever(self) -> None:
        """Poll continuously with one independent schedule per target."""

        schedules: dict[str, asyncio.Task[None]] = {}
        last_health = 0.0
        try:
            while True:
                cycle_started = asyncio.get_running_loop().time()
                if time.time() - last_health >= HUB_HEALTH_INTERVAL_SECONDS:
                    last_health = time.time()
                    self._hub_health(
                        self._hub_cpu_percent(time.time()), self._hub_rss_bytes()
                    )
                try:
                    inventory = await self._inventory()
                    desired = self._desired_schedules(inventory)
                    retired: list[asyncio.Task[None]] = []
                    for key, task in list(schedules.items()):
                        if key not in desired:
                            retired.append(task)
                            schedules.pop(key)
                        elif task.done():
                            if not task.cancelled():
                                with suppress(Exception):
                                    task.result()
                            schedules.pop(key)
                    for task in retired:
                        task.cancel()
                    if retired:
                        await asyncio.gather(*retired, return_exceptions=True)
                    for key, (poller, target) in desired.items():
                        if key in schedules:
                            continue
                        schedules[key] = asyncio.create_task(
                            self._run_schedule(key, poller, target)
                        )
                    if time.time() - self.last_retention >= RETENTION_INTERVAL_SECONDS:
                        self.retain()
                    self.maybe_backup()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.error("hub cycle failed: %s", type(exc).__name__)

                elapsed = asyncio.get_running_loop().time() - cycle_started
                tick = min(
                    SCHEDULE_MAX_TICK_SECONDS,
                    max(SCHEDULE_TICK_SECONDS, self.config.poll_interval_seconds / 10),
                )
                await asyncio.sleep(max(0.05, tick - elapsed))
        except asyncio.CancelledError:
            for task in schedules.values():
                task.cancel()
            if schedules:
                await asyncio.gather(*schedules.values(), return_exceptions=True)
            raise

    def close(self) -> None:
        self.db.close()
