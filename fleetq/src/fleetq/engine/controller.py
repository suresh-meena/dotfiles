"""The controller: reconciliation, cancellation, observation, placement, dispatch.

One ``Controller`` runs per fleetqd (§1.1). Its loop is event-driven with a
10-second tick. The ordering of work within a tick encodes priorities: fencing
and reconciliation first, then cancellation (which must never wait behind new
work), then observation, then new placement last (§4.4, §3.5).

The dispatch protocol, per attempt:

1. commit ``PLANNED`` plus all reservations (placement);
2. commit ``STAGING``; stage idempotently (executes nothing);
3. re-check dispatchability, then commit ``LAUNCHING`` with
   ``remote_may_be_live = 1`` *before* sending (invariant 4);
4. send the launch; apply the typed result.

A crash between any two steps is recovered at startup:

* ``PLANNED``/``STAGING`` never sent anything, so they become ``NEVER_STARTED``;
* ``LAUNCHING``/``SUBMITTING`` may have been sent, so they become unknown and
  are reconciled from remote evidence. They are never re-sent blindly.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from ..db.store import Store
from ..executors.base import AttemptContext, Executor, LaunchKind, ObserveResult
from ..util import parse_utc, utcnow
from . import artifacts, fence, state
from .admission import effective_quota
from .placement import Placement, SharedCapacity, place_job
from .fence import ControllerIdentity

log = logging.getLogger("fleetq.controller")

RETRY_CLASS_OF = {
    "NODE_FAIL": "node_fail",
    "FAILED": "exit",
    "TIMEOUT": "timeout",
    "OUT_OF_MEMORY": "oom",
    "PREEMPTED": "preempted",
}


@dataclass
class ControllerConfig:
    tick_s: float = 10.0
    global_dispatch: int = 4
    observe_interval_s: float = 15.0          # workstations
    # One observation session per cluster per cycle (§3.5): quick while things are
    # changing, slow while jobs merely sit, none at all with nothing live there.
    slurm_observe_fast_s: float = 30.0
    slurm_observe_slow_s: float = 120.0
    slurm_fast_window_s: float = 300.0
    slurm_unreachable_max_s: float = 1800.0  # a down login node backs off to this
    # Log collection (§6.4): bounded bytes per stream per read, bounded attempts per tick.
    log_chunk_bytes: int = 256 * 1024
    log_attempts_per_tick: int = 8
    slurm_log_interval_s: float = 300.0       # a running cluster job's log is not an interactive feed
    log_final_window_s: float = 86400.0       # after this, a finished attempt's tail is not chased
    # Artifact collection (§6.4).
    artifact_jobs_per_tick: int = 2
    cache_pin_releases_per_tick: int = 2
    artifact_backoff_s: float = 60.0
    artifact_max_tries: int = 5
    artifact_deadline_s: float = 86400.0
    collect_max_bytes: int = 2 * 1024 ** 3
    collect_max_files: int = 10000
    artifact_owner_max_bytes: int = 16 * 1024 ** 3
    artifact_global_max_bytes: int = 64 * 1024 ** 3
    artifact_free_reserve_bytes: int = 2 * 1024 ** 3
    lost_contact_s: float = 86400.0
    gpu_cooldown_s: int = 300
    post_job_cooldown_s: int = 60
    refusal_backoff_s: int = 30
    stage_backoff_s: int = 60
    retry_backoff_s: int = 60
    max_refusals_per_hour: int = 20
    starve_after_s: float = 6 * 3600.0       # a job pending this long gets a node drained for it (0: off)
    age_boost_per_hour: int = 1
    age_boost_cap: int = 50

    def __post_init__(self) -> None:
        values = (self.collect_max_bytes, self.collect_max_files, self.artifact_owner_max_bytes,
                  self.artifact_global_max_bytes, self.artifact_free_reserve_bytes)
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("artifact collection byte/file limits must be positive integers")
        if self.artifact_owner_max_bytes < self.collect_max_bytes:
            raise ValueError("artifact_owner_max_bytes must cover collect_max_bytes")
        if self.artifact_global_max_bytes < self.artifact_owner_max_bytes:
            raise ValueError("artifact_global_max_bytes must cover artifact_owner_max_bytes")


class Hub:
    """Sequence-numbered change notification for long-polls.

    Waiters read ``seq`` *before* re-checking state, then wait for ``seq`` to
    advance, so a publish between the check and the wait is never lost (§6.2).
    """

    def __init__(self) -> None:
        self.seq = 0
        self._event = asyncio.Event()

    def publish(self) -> None:
        self.seq += 1
        event, self._event = self._event, asyncio.Event()
        event.set()

    async def wait_beyond(self, seq: int, timeout: float) -> bool:
        if self.seq > seq:
            return True
        event = self._event
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.seq > seq


def _later(seconds: float) -> str:
    return (parse_utc(utcnow()) + _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM controller_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


class Controller:
    def __init__(
        self,
        store: Store,
        executors: dict[str, Executor],
        ident: ControllerIdentity,
        *,
        config: ControllerConfig | None = None,
        bundle_dir: Path | None = None,
        shared_capacity: SharedCapacity | None = None,
        clock_ok: Callable[[], bool] = lambda: False,
        notifier: Callable[[sqlite3.Connection, str, dict[str, Any]], None] | None = None,
        log_cache: Any = None,
        artifact_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.log_cache = log_cache
        self.artifact_dir = artifact_dir
        self._artifact_retry_at: dict[int, float] = {}
        self._artifact_tries: dict[int, int] = {}
        self._last_log_read: dict[str, float] = {}
        self.executors = executors
        self.ident = ident
        self.config = config or ControllerConfig()
        self.bundle_dir = bundle_dir
        self.shared_capacity = shared_capacity
        self.clock_ok = clock_ok
        self.notifier = notifier
        self.hub = Hub()
        self.outbox = None
        self._wake = asyncio.Event()
        self._inflight: dict[str, asyncio.Task] = {}      # attempt_id -> dispatch task
        self._inflight_targets: dict[str, str] = {}        # attempt_id -> target
        self._last_observe: dict[str, float] = {}
        self._unreachable_since: dict[str, float] = {}
        self._last_transition: dict[str, float] = {}      # target -> loop time of the last change seen
        self._observe_failures: dict[str, int] = {}        # target -> consecutive unreachable observations
        self._last_managed_slurm_snapshot: dict[str, float] = {}
        self._fence_retry_at: dict[str, float] = {}
        self._last_cache_pin_release_id: str | None = None
        self._stopping = False
        self.startup_complete = False
        self.last_tick_at: float | None = None

    # ---- plumbing -----------------------------------------------------------------

    def wake(self) -> None:
        self._wake.set()

    async def _tx(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        result = await self.store.run(fn)
        self.hub.publish()
        return result

    def _executor(self, backend: str) -> Executor | None:
        return self.executors.get(backend)

    def _alert(self, conn: sqlite3.Connection, kind: str, detail: dict[str, Any]) -> None:
        if self.notifier is not None:
            self.notifier(conn, kind, detail)

    # ---- lifecycle ----------------------------------------------------------------

    async def run_forever(self) -> None:
        await self.startup()
        self.startup_complete = True
        while not self._stopping:
            try:
                await self.tick()
                self.last_tick_at = asyncio.get_running_loop().time()
            except Exception:  # never let one bad tick kill the controller
                log.exception("controller tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.config.tick_s)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping = True
        self.wake()
        for task in list(self._inflight.values()):
            try:
                await asyncio.wait_for(asyncio.shield(task), 30)
            except Exception:
                pass

    async def drain(self) -> None:
        """Wait for in-flight dispatch tasks (tests)."""
        while self._inflight:
            await asyncio.gather(*list(self._inflight.values()), return_exceptions=True)

    async def startup(self) -> None:
        """Recover interrupted dispatches, then fence every enabled target."""
        await self._tx(self._recover_interrupted)
        restoring = await self.store.run(fence.restore_discovery_pending)
        targets = await self.store.run(lambda c: [r["id"] for r in c.execute(
            "SELECT id FROM nodes WHERE enabled = 1 OR "
            "(? AND (fence_epoch IS NOT NULL OR state='QUARANTINED'))",
            (int(restoring),))])
        # A restored DB may carry an epoch older than a target's accepted
        # fence. Raising our epoch invalidates every earlier fence, so repeat
        # the complete discovery pass at the new epoch before opening dispatch.
        while True:
            pass_epoch = self.ident.epoch
            for target in targets:
                await self._fence_target(target)
            restore_pending = await self.store.run(fence.restore_discovery_pending)
            if self.ident.epoch == pass_epoch and (not restore_pending or restoring):
                break
            restoring = restore_pending
            targets = await self.store.run(lambda c: [r["id"] for r in c.execute(
                "SELECT id FROM nodes WHERE enabled = 1 OR "
                "(? AND (fence_epoch IS NOT NULL OR state='QUARANTINED'))",
                (int(restoring),))])
        await self._complete_restore_discovery_if_ready()
        # The flag means startup reconciliation was attempted; readyz separately
        # checks that every enabled target is actually reconciled at this epoch.

    def _recover_interrupted(self, conn: sqlite3.Connection) -> None:
        """Attempts left mid-protocol by a crash (§9)."""
        for att in conn.execute("SELECT * FROM attempts WHERE state IN ('PLANNED','STAGING')").fetchall():
            # Nothing past staging was sent; staging executes nothing.
            state.update_attempt(conn, att["id"], state="NEVER_STARTED", event="recovered_unsent",
                                 evidence={"why": "controller restarted before launch was sent"})
            state.release_reservations(conn, att["id"], reason="recovered_unsent")
            self._job_after_nonstart(conn, att["job_id"], reason="recovered after restart", backoff=0)
        for att in conn.execute("SELECT * FROM attempts WHERE state = 'LAUNCHING'").fetchall():
            state.update_attempt(conn, att["id"], state="START_UNKNOWN", event="recovered_launch_unknown",
                                 evidence={"why": "controller restarted after the launch may have been sent"})
            job = state.get_job(conn, att["job_id"])
            if job["phase"] == "DISPATCHING":
                state.update_job(conn, job["id"], event="reconciling", actor="controller",
                                 phase="RECONCILING", reason="start_unknown")
        for att in conn.execute("SELECT * FROM attempts WHERE state = 'SUBMITTING'").fetchall():
            state.update_attempt(conn, att["id"], state="SUBMISSION_UNKNOWN", event="recovered_submit_unknown",
                                 evidence={"why": "controller restarted after sbatch may have been sent"})
            job = state.get_job(conn, att["job_id"])
            if job["phase"] == "DISPATCHING":
                state.update_job(conn, job["id"], event="submission_unknown", actor="controller",
                                 phase="SUBMISSION_UNKNOWN", reason="submission_unknown")

    async def _fence_target(self, target: str) -> bool:
        row = await self.store.run(lambda c: c.execute("SELECT * FROM nodes WHERE id = ?", (target,)).fetchone())
        executor = self._executor(row["backend"]) if row else None
        if executor is None:
            return False
        epoch = self.ident.epoch
        res = await executor.fence(target, epoch=epoch, fleet_id=self.ident.fleet_id)
        if not res.reachable:
            self._unreachable_since.setdefault(target, asyncio.get_running_loop().time())
            self._fence_retry_at[target] = asyncio.get_running_loop().time() + 30
            await self._tx(lambda c: c.execute("UPDATE nodes SET state='UNREACHABLE', updated_at=? WHERE id=?",
                                               (utcnow(), target)))
            return False
        if not res.accepted:
            def refused(conn):
                state.add_event(conn, "fence_refused", target=target, actor="controller",
                                detail={"reason": res.reason, "highest_seen": res.highest_epoch_seen})
                if res.highest_epoch_seen >= epoch:
                    # Another or newer controller fenced this target: we may be a
                    # restored instance. Stop all dispatch until discovery (§1.3).
                    conn.execute("INSERT INTO controller_meta (key, value) VALUES ('restore_discovery_pending','1')"
                                 " ON CONFLICT(key) DO UPDATE SET value='1'")
                    self._alert(conn, "fence_conflict", {"target": target, "seen": res.highest_epoch_seen})
                    if self.ident.restore_epoch_raise_allowed:
                        raised = fence.raise_epoch_above(conn, res.highest_epoch_seen)
                        if raised > epoch:
                            # Old per-target readiness must never survive a fleet-wide epoch bump.
                            conn.execute("UPDATE nodes SET reconciled_epoch = NULL")
                            return raised
            raised_epoch = await self._tx(refused)
            if raised_epoch is not None:
                self.ident = replace(self.ident, epoch=raised_epoch, restored=True)
            return False

        known = await self.store.run(lambda c: {
            r["id"] for r in c.execute("SELECT id FROM attempts WHERE target = ?", (target,))
        })
        orphans = [a for a in res.remote_attempts if a not in known]
        live = await self.store.run(lambda c: [
            r["id"] for r in c.execute(
                "SELECT id FROM attempts WHERE target = ? AND remote_may_be_live = 1", (target,))
        ])
        if live:
            obs = await executor.observe(target, live)
            if not obs.reachable:
                return False
            await self._apply_observations(target, obs)

        def finish(conn):
            if orphans:
                # Remote work this database doesn't know: a restore gap. Keep the
                # target quarantined; a human imports or resolves it (§1.3, §14).
                state.add_event(conn, "orphans_discovered", target=target, actor="controller",
                                detail={"attempts": orphans})
                # Persist that this target has accepted fleetq fencing even
                # though its namespace is quarantined for operator resolution.
                conn.execute("UPDATE nodes SET state='QUARANTINED', fence_epoch=?, updated_at=? WHERE id=?",
                             (epoch, utcnow(), target))
                self._alert(conn, "orphans_discovered", {"target": target, "attempts": orphans})
                return
            prior = conn.execute("SELECT state FROM nodes WHERE id = ?", (target,)).fetchone()
            fence.mark_target_reconciled(conn, target, epoch)
            if prior is None or prior["state"] != "QUARANTINED":
                conn.execute("UPDATE nodes SET state='UP', updated_at=? WHERE id=?", (utcnow(), target))
        await self._tx(finish)
        await self._complete_restore_discovery_if_ready()
        self._unreachable_since.pop(target, None)
        return not orphans

    async def _complete_restore_discovery_if_ready(self) -> None:
        """Resume restored dispatch only after every enabled target is resolved."""
        def complete(conn):
            if not fence.restore_discovery_pending(conn):
                return
            targets = conn.execute("SELECT state, reconciled_epoch FROM nodes WHERE enabled = 1 "
                                   "OR fence_epoch IS NOT NULL OR state='QUARANTINED'").fetchall()
            # An unresolved orphan on a quarantined target may represent a
            # request key lost with the backup. Keep admission and dispatch
            # gated globally until that history is resolved, not merely fenced.
            if all(row["state"] != "QUARANTINED" and row["reconciled_epoch"] == self.ident.epoch
                   for row in targets):
                fence.complete_restore_discovery(conn)
        await self._tx(complete)

    # ---- the tick ---------------------------------------------------------------

    async def tick(self) -> None:
        await self._retry_unfenced()
        await self._process_cancels()
        await self._observe_live()
        await self._observe_managed_slurm()
        await self._collect_logs()
        await self._collect_artifacts()
        await self._release_remote_cache_pins()
        await self._evaluate_dependencies()
        await self._place_and_dispatch()

    async def _observe_managed_slurm(self) -> None:
        """Refresh explicitly enabled all-user snapshots; API reads use only the cache."""
        from ..slurm.managed_snapshot import (
            record_snapshot_failure,
            record_snapshot_success,
            site_snapshot_settings,
        )

        loop = asyncio.get_running_loop()
        now_mono = loop.time()
        candidates = await self.store.run(lambda c: c.execute(
            "SELECT id, config_json FROM nodes WHERE backend='slurm' AND enabled=1 ORDER BY id"
        ).fetchall())
        executor = self._executor("slurm")
        for row in candidates:
            try:
                config = json.loads(row["config_json"] or "{}")
                enabled, interval, _stale = site_snapshot_settings(config)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            target = row["id"]
            if not enabled or now_mono - self._last_managed_slurm_snapshot.get(target, -1e9) < interval:
                continue
            self._last_managed_slurm_snapshot[target] = now_mono
            poll = getattr(executor, "managed_queue_snapshot", None) if executor else None
            if not callable(poll):
                result = {"complete": False, "error": "snapshot_unavailable"}
            else:
                try:
                    result = await poll(target)
                except Exception:
                    log.exception("managed Slurm snapshot failed for %s", target)
                    result = {"complete": False, "error": "snapshot_unavailable"}
            if not isinstance(result, dict):
                result = {"complete": False, "error": "invalid_output"}
            attempted_at = utcnow()
            if result.get("complete") is True:
                jobs = result.get("jobs")
                if not isinstance(jobs, list):
                    result = {"complete": False, "error": "invalid_output"}
                else:
                    try:
                        await self._tx(lambda c, target=target, jobs=jobs, result=result, at=attempted_at:
                                       record_snapshot_success(c, target, jobs=jobs, observed_at=at,
                                                               output_bytes=int(result.get("output_bytes", 0))))
                        continue
                    except (TypeError, ValueError, OverflowError):
                        result = {"complete": False, "error": "invalid_output"}
            error = str(result.get("error") or "snapshot_unavailable")
            await self._tx(lambda c, target=target, at=attempted_at, error=error:
                           record_snapshot_failure(c, target, attempted_at=at, error=error))
            retry_after = result.get("retry_after")
            if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and retry_after > 0:
                # This value is compared against the normal interval below, so
                # offset the stored timestamp to make retry_after the total wait.
                self._last_managed_slurm_snapshot[target] = now_mono + max(0.0, float(retry_after) - interval)

    async def _retry_unfenced(self) -> None:
        now = asyncio.get_running_loop().time()
        epoch = self.ident.epoch
        pending = await self.store.run(lambda c: [
            r["id"] for r in c.execute(
                "SELECT id FROM nodes WHERE (enabled = 1 OR (? AND fence_epoch IS NOT NULL))"
                " AND state <> 'QUARANTINED'"
                " AND (reconciled_epoch IS NULL OR reconciled_epoch <> ?)",
                (int(fence.restore_discovery_pending(c)), epoch))
        ])
        for target in pending:
            if self._fence_retry_at.get(target, 0) <= now:
                await self._fence_target(target)

    # ---- cancellation (priority lane) ---------------------------------------------

    async def _process_cancels(self) -> None:
        rows = await self.store.run(lambda c: c.execute(
            "SELECT id FROM jobs WHERE desired_state = 'CANCEL' AND phase <> 'TERMINAL'").fetchall())
        for row in rows:
            await self._cancel_job(row["id"])

    async def _cancel_job(self, job_id: int) -> None:
        att = await self.store.run(lambda c: state.active_attempt(c, job_id))
        if att is not None and att["id"] in self._inflight:
            return  # the dispatch task re-checks desired state at the launch boundary
        if att is None or att["state"] in ("PLANNED", "STAGING"):
            def local(conn):
                if att is not None:
                    state.update_attempt(conn, att["id"], state="NEVER_STARTED", event="cancelled_before_launch")
                    state.release_reservations(conn, att["id"], reason="cancelled")
                job = state.get_job(conn, job_id)
                if job["phase"] != "TERMINAL" and state.possibly_live_attempt(conn, job_id) is None:
                    state.update_job(conn, job_id, event="cancelled", actor="controller",
                                     phase="TERMINAL", execution_outcome="CANCELLED", success=0, ended_at=utcnow())
            await self._tx(local)
            return
        if att["state"] == "STOPPED":
            await self._release(att)
            return
        executor = self._executor(att["backend"])
        if executor is None:
            return
        await self._tx(lambda c: (state.get_job(c, job_id)["phase"] != "CANCELLING") and state.update_job(
            c, job_id, event="cancelling", actor="controller", phase="CANCELLING", reason="cancel_requested"))
        res = await executor.cancel(att["target"], att["id"], epoch=self.ident.epoch)
        if not res.reachable:
            # Intent stays durable; resources stay held; retried next tick (§2.8).
            return

        def apply(conn):
            current = state.get_attempt(conn, att["id"])
            if res.never_started and current["state"] in ("LAUNCHING", "START_UNKNOWN", "SUBMITTING", "SUBMISSION_UNKNOWN"):
                state.update_attempt(conn, att["id"], state="NEVER_STARTED", event="cancel_tombstone_won",
                                     evidence={"cancel": "tombstone before payload entry"})
                state.release_reservations(conn, att["id"], reason="cancelled")
                state.update_job(conn, job_id, event="cancelled", actor="controller", phase="TERMINAL",
                                 execution_outcome="CANCELLED", success=0, ended_at=utcnow())
                return
            if res.stopped:
                if current["state"] not in ("STOPPED",):
                    target_state = "STOPPED"
                    if current["state"] in ("RUNNING", "SUBMITTED", "START_UNKNOWN", "SUBMISSION_UNKNOWN", "STOPPING"):
                        state.update_attempt(conn, att["id"], state=target_state, event="cancel_confirmed",
                                             outcome=current["outcome"] or "CANCELLED", ended_at=utcnow(),
                                             evidence={"cancel": "termination confirmed"})
            elif current["state"] in ("RUNNING", "SUBMITTED", "SUBMISSION_UNKNOWN", "START_UNKNOWN"):
                state.update_attempt(conn, att["id"], state="STOPPING", event="cancel_sent",
                                     evidence={"cancel": res.reason or "acknowledged, not yet confirmed"})
        await self._tx(apply)
        self._transitioned(att["target"])
        refreshed = await self.store.run(lambda c: state.get_attempt(c, att["id"]))
        if refreshed["state"] == "STOPPED":
            await self._release(refreshed)

    # ---- observation ----------------------------------------------------------------

    async def _observe_live(self) -> None:
        rows = await self.store.run(lambda c: c.execute(
            "SELECT id, target, backend FROM attempts WHERE remote_may_be_live = 1").fetchall())
        by_target: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            if row["id"] in self._inflight:
                continue
            by_target.setdefault((row["target"], row["backend"]), []).append(row["id"])
        now = asyncio.get_running_loop().time()
        intervals = await self.store.run(lambda c: {
            r["id"]: json.loads(r["config_json"] or "{}").get("observe_interval_s")
            for r in c.execute("SELECT id, config_json FROM nodes")})
        # Shared bare GPUs need post-launch occupancy observations even when
        # no attempt remains live; otherwise a process escaping its allocation
        # would be invisible as soon as the last tracked job ended.
        shared_targets = await self.store.run(lambda c: [r["id"] for r in c.execute(
            "SELECT id FROM nodes WHERE backend='bare' AND mode='shared' AND enabled=1")])
        for shared_target in shared_targets:
            by_target.setdefault((shared_target, "bare"), [])
        for (target, backend), ids in by_target.items():
            interval = self._observe_interval(target, backend, intervals.get(target), now)
            if now - self._last_observe.get(target, -1e9) < interval:
                continue
            executor = self._executor(backend)
            if executor is None:
                continue
            self._last_observe[target] = now
            if getattr(executor, "logs_in_observe", False) and self.log_cache is not None:
                from ..logs import STREAMS
                requests = {aid: {st: m["end"] for st in STREAMS
                                  if not (m := self.log_cache.meta(aid, st))["complete"]} for aid in ids}
                obs = await executor.observe(target, ids, log_requests={a: r for a, r in requests.items() if r})
            else:
                obs = await executor.observe(target, ids)
            await self._apply_observations(target, obs)
            for aid, streams in obs.logs.items():
                for stream, chunk in streams.items():
                    if chunk.remote_size is not None:
                        self.log_cache.append(aid, stream, offset=chunk.offset, data=chunk.data,
                                              remote_size=chunk.remote_size, final=False)

    async def _collect_logs(self) -> None:
        """Append bounded log deltas to the numpi cache; requests only ever read the cache (§6.4)."""
        if self.log_cache is None:
            return
        from ..logs import STREAMS
        since = _later(-self.config.log_final_window_s)
        rows = await self.store.run(lambda c: c.execute(
            "SELECT id, target, backend, state, remote_id, remote_may_be_live FROM attempts WHERE started_at IS NOT NULL"
            " AND state IN ('RUNNING','STOPPING','STOPPED','RELEASED') AND (ended_at IS NULL OR ended_at >= ?)"
            " ORDER BY started_at DESC LIMIT 64", (since,)).fetchall())
        now = asyncio.get_running_loop().time()
        done = 0
        for row in rows:
            if done >= self.config.log_attempts_per_tick:
                break
            executor = self._executor(row["backend"])
            if executor is None or not hasattr(executor, "read_logs"):
                continue
            final = row["state"] in ("STOPPED", "RELEASED")
            if not final and row["remote_may_be_live"] and getattr(executor, "logs_in_observe", False):
                continue                          # its observation carries the deltas
            pending = {st: m["end"] for st in STREAMS if not (m := self.log_cache.meta(row["id"], st))["complete"]}
            if not pending:
                continue
            if final:
                interval = 0.0                    # chase the final tail each tick, one chunk at a time
            elif row["backend"] == "slurm":
                interval = self.config.slurm_log_interval_s
            else:
                interval = self.config.observe_interval_s
            if now - self._last_log_read.get(row["id"], -1e9) < interval:
                continue
            self._last_log_read[row["id"]] = now
            done += 1
            chunks = await executor.read_logs(row["target"], row["id"], pending, remote_id=row["remote_id"],
                                              max_bytes=self.config.log_chunk_bytes)
            for stream, chunk in chunks.items():
                if chunk.deferred or not chunk.reachable:
                    continue
                if chunk.remote_size is None:
                    if final:
                        self.log_cache.mark_source_gone(row["id"], stream)
                    continue
                self.log_cache.append(row["id"], stream, offset=chunk.offset, data=chunk.data,
                                      remote_size=chunk.remote_size, final=final)

    async def _collect_artifacts(self) -> None:
        if self.artifact_dir is None:
            return
        rows = await self.store.run(lambda c: artifacts.pending_jobs(c, self.config.artifact_jobs_per_tick * 4))
        now = asyncio.get_running_loop().time()
        done = 0
        for row in rows:
            if done >= self.config.artifact_jobs_per_tick:
                break
            if self._artifact_retry_at.get(row["job_id"], 0) > now:
                continue
            done += 1
            await self._collect_job_artifacts(row)

    async def _release_remote_cache_pins(self) -> None:
        """Release terminal remote cache pins and durably remember acknowledgments.

        Each executor budgets its call. Keep the work per tick small so cache
        cleanup cannot starve reconciliation or placement.
        A false result or exception leaves the row eligible for a later retry.
        """
        limit = max(0, self.config.cache_pin_releases_per_tick)
        if not limit:
            return
        backends = [backend for backend in ("bare", "slurm")
                    if callable(getattr(self._executor(backend), "release_cache_pin", None))]
        if not backends:
            return
        backend_slots = ",".join("?" for _ in backends)
        cursor = self._last_cache_pin_release_id or ""
        epoch = self.ident.epoch
        rows = await self.store.run(lambda c: c.execute(
            "SELECT a.id, a.target, a.backend FROM attempts a JOIN jobs j ON j.id=a.job_id "
            "JOIN nodes n ON n.id=a.target "
            "LEFT JOIN remote_cache_pin_release_acks ack ON ack.attempt_id=a.id "
            f"WHERE a.backend IN ({backend_slots}) AND a.state='RELEASED' AND j.phase='TERMINAL' "
            "AND j.artifacts_state IN ('COMPLETE','NOT_REQUESTED') AND ack.attempt_id IS NULL "
            "AND n.backend=a.backend AND n.fence_epoch=? AND n.reconciled_epoch=? "
            "AND n.state <> 'QUARANTINED' "
            "ORDER BY CASE WHEN a.id > ? THEN 0 ELSE 1 END, a.id LIMIT ?",
            (*backends, epoch, epoch, cursor, limit)).fetchall())
        for row in rows:
            # Advance before attempting the call so a false result or exception
            # doesn't pin the bounded window on this same attempt next tick.
            self._last_cache_pin_release_id = row["id"]
            executor = self._executor(row["backend"])
            release = getattr(executor, "release_cache_pin", None) if executor else None
            if not callable(release):
                continue
            try:
                acknowledged = await release(row["target"], row["id"], epoch=epoch)
            except Exception:
                log.exception("remote cache pin release failed for attempt %s", row["id"])
                continue
            if acknowledged is not True:
                continue

            def record(conn: sqlite3.Connection, attempt_id: str = row["id"]) -> None:
                # Recheck eligibility at commit time. If state was changed by
                # another controller action, leave it pending for a safe retry.
                conn.execute(
                    "INSERT OR IGNORE INTO remote_cache_pin_release_acks "
                    "(attempt_id, acknowledged_at, controller_epoch) "
                    "SELECT a.id, ?, ? FROM attempts a JOIN jobs j ON j.id=a.job_id "
                    "JOIN nodes n ON n.id=a.target "
                    "WHERE a.id=? AND a.backend IN ('bare','slurm') AND a.state='RELEASED' "
                    "AND j.phase='TERMINAL' AND j.artifacts_state IN ('COMPLETE','NOT_REQUESTED') "
                    "AND n.backend=a.backend AND n.fence_epoch=? AND n.reconciled_epoch=? "
                    "AND n.state <> 'QUARANTINED'",
                    (utcnow(), epoch, attempt_id, epoch, epoch))
            await self._tx(record)

    async def _collect_job_artifacts(self, row: sqlite3.Row) -> None:
        job_id, attempt_id = row["job_id"], row["attempt_id"]
        spec = json.loads(row["spec_json"])
        collect = spec.get("collect") or []

        async def end(state_: str, reason: str | None, detail: dict | None = None) -> None:
            ok = await self.store.run(lambda c: artifacts.finish(c, job_id, artifacts_state=state_, reason=reason,
                                                                 detail=detail))
            await self._tx(lambda c: self._alert(c, "job_finished", {"job": job_id, "success": ok,
                                                                     "artifacts": state_, "reason": reason}))
            self._artifact_retry_at.pop(job_id, None)
            self._artifact_tries.pop(job_id, None)

        async def later(reason: str) -> None:
            self._artifact_retry_at[job_id] = asyncio.get_running_loop().time() + self.config.artifact_backoff_s
            def mark_deferred(conn):
                job = state.get_job(conn, job_id)
                if job["artifacts_state"] != "RETRY_WAIT" or job["reason"] != reason:
                    state.update_job(conn, job_id, event="artifacts_deferred", actor="controller",
                                     artifacts_state="RETRY_WAIT", reason=reason)
            await self._tx(mark_deferred)
            log.info("artifacts for job %s deferred: %s", job_id, reason)

        if row["ended_at"] and row["ended_at"] < _later(-self.config.artifact_deadline_s):
            await end("EXPIRED", "artifact collection deadline passed")
            return
        executor = self._executor(row["backend"])
        if executor is None or not hasattr(executor, "collect_stage"):
            await end("FAILED", f"backend {row['backend']} cannot collect outputs")
            return
        if row["artifacts_state"] != "COLLECTING":
            await self._tx(lambda c: state.update_job(c, job_id, event="artifacts_collecting", actor="controller",
                                                      artifacts_state="COLLECTING"))
        staged = await executor.collect_stage(row["target"], attempt_id, [c["path"] for c in collect],
                                              workdir_hint=spec["workdir"].get("subdir"),
                                              max_files=self.config.collect_max_files,
                                              max_bytes=self.config.collect_max_bytes)
        if staged.deferred or not staged.reachable:
            await later("node unreachable or budget deferred")
            return
        if not staged.ok:
            await end("FAILED", f"output staging refused: {staged.error}")
            return
        _, manifest_error = artifacts.validate_manifest(staged.files, max_files=self.config.collect_max_files,
                                                        max_bytes=self.config.collect_max_bytes)
        if manifest_error:
            await end("FAILED", f"output staging refused: {manifest_error}")
            return
        bad = [c["path"] for c in collect if c["required"] and not artifacts.covered(c["path"], staged.files)]
        unsafe = [f["relpath"] for f in staged.files if artifacts.safe_relpath(f["relpath"]) != f["relpath"]]
        if bad or unsafe:
            await end("FAILED", "required output missing" if bad else "unsafe output path",
                      {"missing": bad, "unsafe": unsafe, "refused": staged.refused})
            return
        partial = self.artifact_dir / ".partial" / attempt_id
        try:
            artifacts.remove_staging(partial)
            artifacts.remove_staging(partial.with_name(partial.name + ".pub"))
        except (OSError, ValueError) as exc:
            await later(f"unsafe artifact staging path: {exc}")
            return
        staged_bytes, _ = artifacts.validate_manifest(staged.files, max_files=self.config.collect_max_files,
                                                       max_bytes=self.config.collect_max_bytes)
        try:
            self.artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            # f_bavail excludes blocks reserved for the filesystem owner/root.
            free_bytes = artifacts.available_bytes(self.artifact_dir)
        except OSError as exc:
            await later(f"artifact filesystem unavailable: {exc.__class__.__name__}")
            return
        if free_bytes < staged_bytes + self.config.artifact_free_reserve_bytes:
            await later("artifact filesystem reserve would be crossed")
            return
        reserved, reason = await self.store.run(lambda c: artifacts.reserve_files(
            c, job_id, attempt_id, collect, staged.files,
            owner_max_bytes=self.config.artifact_owner_max_bytes,
            global_max_bytes=self.config.artifact_global_max_bytes))
        if not reserved:
            await later(f"artifact storage quota deferred: {reason}")
            return
        try:
            artifacts.ensure_real_directory(partial, create=True)
        except (OSError, ValueError) as exc:
            await later(f"unsafe artifact staging path: {exc}")
            return
        pulled, why = await executor.collect_pull(row["target"], attempt_id, partial)
        if not pulled:
            await later(f"pull {why}")
            return
        problems = await asyncio.to_thread(artifacts.verify, partial, staged.files)
        if problems:
            tries = self._artifact_tries[job_id] = self._artifact_tries.get(job_id, 0) + 1
            if tries >= self.config.artifact_max_tries:
                await end("FAILED", "outputs did not verify", {"problems": problems[:20]})
            else:
                await later(f"verification: {problems[0]}")
            return
        final = self.artifact_dir / str(job_id) / str(row["n"])
        try:
            await asyncio.to_thread(artifacts.publish, partial, final, staged.files)
        except (OSError, ValueError) as exc:
            await later(f"artifact publish deferred: {exc}")
            return
        await self._tx(lambda c: artifacts.mark_complete(c, attempt_id, final, staged.files))
        # Finish the remote staging cleanup before publishing terminal state:
        # a waiter observing COMPLETE must not race a still-running cleanup.
        # Published local artifacts remain valid if cleanup itself is refused.
        if not await executor.collect_clean(row["target"], attempt_id):
            log.warning("remote artifact staging cleanup deferred for attempt %s", attempt_id)
        await end("COMPLETE", None, {"files": len(staged.files), "refused": staged.refused,
                                     "missing_optional": staged.missing})

    def _observe_interval(self, target: str, backend: str, configured: float | None, now: float) -> float:
        if configured:
            base = float(configured)
        elif backend == "slurm":
            recent = now - self._last_transition.get(target, -1e9) < self.config.slurm_fast_window_s
            base = self.config.slurm_observe_fast_s if recent else self.config.slurm_observe_slow_s
        else:
            base = self.config.observe_interval_s
        failures = self._observe_failures.get(target, 0)
        if backend == "slurm" and failures:
            base = min(base * 2 ** failures, self.config.slurm_unreachable_max_s)
        return base

    def _transitioned(self, target: str) -> None:
        self._last_transition[target] = asyncio.get_running_loop().time()

    async def _apply_observations(self, target: str, obs: ObserveResult) -> None:
        loop_now = asyncio.get_running_loop().time()
        if obs.deferred:
            # The budget authority said "not now"; try again later, never spin.
            self._last_observe[target] = loop_now + (obs.retry_after or 0)
            return
        if not obs.reachable:
            self._observe_failures[target] = self._observe_failures.get(target, 0) + 1
            since = self._unreachable_since.setdefault(target, loop_now)

            def unreachable(conn):
                conn.execute("UPDATE nodes SET state='UNREACHABLE', updated_at=? WHERE id=?", (utcnow(), target))
                if loop_now - since >= self.config.lost_contact_s:
                    for att in conn.execute(
                        "SELECT * FROM attempts WHERE target = ? AND remote_may_be_live = 1", (target,)).fetchall():
                        job = state.get_job(conn, att["job_id"])
                        if job["phase"] != "RECONCILING" or job["reason"] != "LOST_CONTACT":
                            # Time is not proof of death (§4.1): escalate, keep holds.
                            state.update_job(conn, job["id"], event="lost_contact", actor="controller",
                                             phase="RECONCILING", reason="LOST_CONTACT")
                            self._alert(conn, "lost_contact", {"job": job["id"], "target": target})
            await self._tx(unreachable)
            return
        self._unreachable_since.pop(target, None)
        self._observe_failures.pop(target, None)
        to_release: list[str] = []
        to_relaunch: list[str] = []
        changed: list[bool] = []

        def apply(conn):
            conn.execute("UPDATE nodes SET state='UP', boot_id=COALESCE(?, boot_id), updated_at=? WHERE id=?",
                         (obs.boot_id, utcnow(), target))
            self._apply_gpu_process_facts(conn, target, obs)
            before = {aid: state.get_attempt(conn, aid)["state"] for aid in obs.attempts}
            apply_each(conn)
            if any(state.get_attempt(conn, aid)["state"] != st for aid, st in before.items()):
                changed.append(True)

        def apply_each(conn):
            for aid, o in obs.attempts.items():
                att = state.get_attempt(conn, aid)
                if att["remote_may_be_live"] != 1:
                    continue
                job = state.get_job(conn, att["job_id"])
                if o.state == "running":
                    if att["state"] in ("LAUNCHING", "START_UNKNOWN", "SUBMITTED", "SUBMISSION_UNKNOWN"):
                        state.update_attempt(conn, aid, state="RUNNING", event="observed_running",
                                             boot_id=o.boot_id or att["boot_id"],
                                             started_at=att["started_at"] or utcnow(),
                                             remote_id=o.remote_id or att["remote_id"])
                    self._job_running(conn, job, first_entry=att["started_at"] is None and o.payload_entered)
                elif o.state == "pending":
                    # Accepted by Slurm and waiting: resolves a submission uncertainty.
                    if att["state"] == "SUBMISSION_UNKNOWN":
                        state.update_attempt(conn, aid, state="SUBMITTED", event="observed_submitted",
                                             remote_id=o.remote_id or att["remote_id"],
                                             evidence={"observation": o.evidence})
                    if job["phase"] in ("SUBMISSION_UNKNOWN", "DISPATCHING", "RECONCILING"):
                        state.update_job(conn, job["id"], event="submitted", actor="controller",
                                         phase="SUBMITTED", reason=None)
                    why = (o.evidence or {}).get("reason")
                    job = state.get_job(conn, job["id"])
                    if why and job["reason"] != f"slurm: {why}" and job["phase"] == "SUBMITTED":
                        state.update_job(conn, job["id"], event="slurm_pending", actor="controller",
                                         reason=f"slurm: {why}")
                        if (o.evidence or {}).get("blocking"):
                            self._alert(conn, "blocked", {"job": job["id"], "reason": why})
                elif o.state == "stopped":
                    if o.cgroup_empty is False and not o.boot_changed:
                        state.add_event(conn, "descendants_remain", job_id=job["id"], attempt_id=aid,
                                        target=target, actor="controller")
                        continue
                    if att["state"] != "STOPPED":
                        outcome = o.outcome or ("UNKNOWN_EXIT" if o.exit_code is None else
                                                ("COMPLETED" if o.exit_code == 0 else "FAILED"))
                        if job["desired_state"] == "CANCEL" and outcome == "CANCELLED":
                            outcome = "CANCELLED"
                        if att["state"] not in ("RUNNING", "STOPPING", "START_UNKNOWN", "SUBMITTED",
                                                "SUBMISSION_UNKNOWN", "LAUNCHING", "SUBMITTING"):
                            continue
                        if att["started_at"] is None and o.payload_entered:
                            state.update_job(conn, job["id"], event="payload_started", actor="controller",
                                             started_at=utcnow(), executions_used=job["executions_used"] + 1)
                        state.update_attempt(conn, aid, state="STOPPED", event="observed_stopped",
                                             outcome=outcome, exit_code=o.exit_code, exit_signal=o.exit_signal,
                                             ended_at=utcnow(), started_at=att["started_at"] or (utcnow() if o.payload_entered else None),
                                             evidence={"observation": o.evidence, "boot_changed": o.boot_changed})
                    to_release.append(aid)
                elif o.state == "refused":
                    if att["state"] in ("LAUNCHING", "START_UNKNOWN"):
                        state.update_attempt(conn, aid, state="REFUSED", event="observed_refused")
                        state.release_reservations(conn, aid, reason="refused")
                        self._job_after_nonstart(conn, job["id"], reason="placement_refused",
                                                 backoff=self.config.refusal_backoff_s)
                elif o.state in ("staged", "absent"):
                    # Conclusive only for attempts whose start was never
                    # proven: the node holds no start request (or no record).
                    if att["state"] in ("START_UNKNOWN", "LAUNCHING", "SUBMISSION_UNKNOWN", "SUBMITTING"):
                        if o.state == "staged":
                            # The start/submit was never requested; re-issuing it is
                            # safe because the remote claim admits it at most once.
                            to_relaunch.append(aid)
                        else:
                            if att["backend"] == "slurm":
                                continue   # absence on a cluster is never proof (§3.3)
                            state.update_attempt(conn, aid, state="NEVER_STARTED", event="observed_never_started",
                                                 evidence={"node": "no record of this attempt"})
                            state.release_reservations(conn, aid, reason="never_started")
                            self._job_after_nonstart(conn, job["id"], reason="never_started",
                                                     backoff=self.config.stage_backoff_s)
                elif o.state == "start_requested":
                    if att["state"] == "START_UNKNOWN":
                        to_relaunch.append(aid)
        await self._tx(apply)
        if changed or to_release or to_relaunch:
            self._transitioned(target)
        for aid in to_release:
            att = await self.store.run(lambda c, aid=aid: state.get_attempt(c, aid))
            await self._release(att)
        for aid in to_relaunch:
            await self._relaunch_unknown(aid)

    def _apply_gpu_process_facts(self, conn: sqlite3.Connection, target: str, obs: ObserveResult) -> None:
        """Fail closed on uncertain shared-node GPU attribution; never signal processes."""
        node = conn.execute("SELECT backend, mode FROM nodes WHERE id=?", (target,)).fetchone()
        if not node or node["backend"] != "bare" or node["mode"] != "shared":
            return
        gpu_rows = conn.execute("SELECT uuid, drained, drain_reason FROM node_gpus WHERE node_id=?", (target,)).fetchall()
        known = {r["uuid"] for r in gpu_rows}
        facts = (obs.node_facts or {}).get("gpu_processes")
        expected_boot = obs.boot_id
        clean_reasons = {"gpu_attribution_unknown", "gpu_process_unattributed", "gpu_foreign_process"}

        def drain(uuid: str, reason: str) -> None:
            conn.execute("UPDATE node_gpus SET drained=1, drain_reason=? WHERE node_id=? AND uuid=?"
                         " AND (drained=0 OR drain_reason IN ('gpu_attribution_unknown','gpu_process_unattributed','gpu_foreign_process'))",
                         (reason, target, uuid))

        def alert(kind: str, uuid: str, process: dict[str, Any] | None, reason: str) -> None:
            identity = "unknown" if process is None else (
                f"{process.get('pid')}:{process.get('boot_id')}:{process.get('process_start_ticks')}")
            self._alert(conn, kind, {"target": target, "gpu": uuid, "reason": reason,
                                     "pid": process.get("pid") if process else None,
                                     "incident": f"{target}:{uuid}:{identity}:{reason}"})

        # Verify the complete, current inventory before interpreting any row.
        doc_ok = (isinstance(facts, dict) and facts.get("complete") is True
                  and isinstance(expected_boot, str) and facts.get("boot_id") == expected_boot
                  and isinstance(facts.get("gpus"), list))
        by_gpu: dict[str, dict[str, Any]] = {}
        if doc_ok:
            for item in facts["gpus"]:
                if (not isinstance(item, dict) or item.get("complete") is not True
                        or not isinstance(item.get("uuid"), str) or item["uuid"] in by_gpu
                        or not isinstance(item.get("processes"), list)):
                    doc_ok = False
                    break
                by_gpu[item["uuid"]] = item
            if set(by_gpu) != known:
                doc_ok = False
        if not doc_ok:
            for uuid in known:
                drain(uuid, "gpu_attribution_unknown")
                alert("gpu_attribution_unknown", uuid, None, "incomplete_gpu_process_inventory")
            return

        reservations = {
            r["gpu_uuid"]: r["attempt_id"]
            for r in conn.execute(
                "SELECT gpu_uuid, attempt_id FROM resource_reservations WHERE node_id=? AND kind='gpu' AND released_at IS NULL",
                (target,))
        }
        for uuid, item in by_gpu.items():
            bad = False
            for process in item["processes"]:
                if (not isinstance(process, dict) or isinstance(process.get("pid"), bool)
                        or not isinstance(process.get("pid"), int) or process["pid"] <= 0
                        or isinstance(process.get("process_start_ticks"), bool)
                        or not isinstance(process.get("process_start_ticks"), int)
                        or process["process_start_ticks"] <= 0
                        or process.get("boot_id") != expected_boot):
                    bad = True
                    break
                attribution = process.get("attribution")
                attempt_id = process.get("fleetq_attempt_id")
                if attribution == "ownership_unknown":
                    bad = True
                    break
                if attribution == "foreign":
                    if uuid in reservations:
                        drain(uuid, "gpu_foreign_process")
                        alert("foreign_process", uuid, process, "foreign_process_on_allocated_gpu")
                    else:
                        drain(uuid, "gpu_process_unattributed")
                        alert("gpu_escape", uuid, process, "foreign_process_on_unallocated_gpu")
                    continue
                if attribution != "fleetq_attempt" or not isinstance(attempt_id, str):
                    bad = True
                    break
                att = conn.execute("SELECT target, remote_may_be_live FROM attempts WHERE id=?", (attempt_id,)).fetchone()
                if (not att or att["target"] != target or att["remote_may_be_live"] != 1
                        or reservations.get(uuid) != attempt_id):
                    drain(uuid, "gpu_process_unattributed")
                    alert("gpu_escape", uuid, process, "fleetq_process_on_unallocated_gpu")
            if bad:
                drain(uuid, "gpu_attribution_unknown")
                alert("gpu_attribution_unknown", uuid, None, "incomplete_process_identity")
            elif not item["processes"]:
                # Clear only controller-owned transient drains, never operator/probe drains.
                conn.execute("UPDATE node_gpus SET drained=0, drain_reason=NULL WHERE node_id=? AND uuid=?"
                             " AND drain_reason IN ('gpu_attribution_unknown','gpu_process_unattributed','gpu_foreign_process')",
                             (target, uuid))

    def _job_running(self, conn: sqlite3.Connection, job: sqlite3.Row, *, first_entry: bool) -> None:
        fields: dict[str, Any] = {}
        if job["phase"] in ("DISPATCHING", "RECONCILING", "SUBMITTED", "SUBMISSION_UNKNOWN"):
            fields.update(phase="RUNNING", reason=None)
        if first_entry:
            fields.update(started_at=job["started_at"] or utcnow(), executions_used=job["executions_used"] + 1)
        if fields:
            state.update_job(conn, job["id"], event="running", actor="controller", **fields)

    async def _relaunch_unknown(self, attempt_id: str) -> None:
        """Re-issue the *same* attempt's idempotent launch (§2.3).

        Safe only because the node's durable runner-entry claim guarantees the
        payload is entered at most once per attempt, however many launch RPCs
        arrive.
        """
        att = await self.store.run(lambda c: state.get_attempt(c, attempt_id))
        executor = self._executor(att["backend"])
        if executor is None:
            return
        ctx = await self.store.run(lambda c: self._context(c, att))
        res = await executor.launch(ctx)
        await self._apply_launch(att["id"], res)

    async def _release(self, att: sqlite3.Row) -> None:
        executor = self._executor(att["backend"])
        if executor is None:
            return
        res = await executor.release(att["target"], att["id"])
        if not res.released:
            await self._tx(lambda c: state.add_event(c, "release_deferred", attempt_id=att["id"], job_id=att["job_id"],
                                                     target=att["target"], actor="controller",
                                                     detail={"reason": res.reason}))
            return

        def finish(conn):
            current = state.get_attempt(conn, att["id"])
            if current["state"] != "STOPPED":
                return
            cooldown = _later(self.config.post_job_cooldown_s)
            for r in conn.execute("SELECT gpu_uuid FROM resource_reservations WHERE attempt_id = ? AND kind='gpu'"
                                  " AND released_at IS NULL", (att["id"],)):
                conn.execute("UPDATE node_gpus SET cooldown_until = ? WHERE node_id = ? AND uuid = ?",
                             (cooldown, att["target"], r["gpu_uuid"]))
            state.release_reservations(conn, att["id"], reason="stopped_and_released")
            state.update_attempt(conn, att["id"], state="RELEASED", event="released")
            self._finish_job(conn, current)
        await self._tx(finish)

    def _finish_job(self, conn: sqlite3.Connection, att: sqlite3.Row) -> None:
        job = state.get_job(conn, att["job_id"])
        spec = json.loads(job["spec_json"])
        outcome = att["outcome"] or "UNKNOWN_EXIT"
        if job["desired_state"] == "CANCEL":
            # A natural completion that raced a cancel keeps its real outcome (§2.8).
            state.update_job(conn, job["id"], event="finished", actor="controller", phase="TERMINAL",
                             execution_outcome=outcome, exit_code=att["exit_code"], exit_signal=att["exit_signal"],
                             success=int(outcome == "COMPLETED" and att["exit_code"] == 0 and not spec["collect"]),
                             ended_at=utcnow())
            return
        retry_class = RETRY_CLASS_OF.get(outcome)
        ctl = spec["control"]
        if (retry_class and retry_class in ctl["retry_on"] and job["executions_used"] <= ctl["retry"]
                and job["desired_state"] == "RUN"):
            state.update_job(conn, job["id"], event="retry_scheduled", actor="controller", phase="PENDING",
                             reason=f"retry after {outcome}", not_before=_later(self.config.retry_backoff_s))
            return
        exec_success = outcome == "COMPLETED" and (att["exit_code"] in (0, None))
        if spec["collect"]:
            state.update_job(conn, job["id"], event="finalizing", actor="controller", phase="FINALIZING",
                             execution_outcome=outcome, exit_code=att["exit_code"], exit_signal=att["exit_signal"],
                             artifacts_state="PENDING", ended_at=utcnow())
            return
        state.update_job(conn, job["id"], event="finished", actor="controller", phase="TERMINAL",
                         execution_outcome=outcome, exit_code=att["exit_code"], exit_signal=att["exit_signal"],
                         success=int(exec_success), ended_at=utcnow())
        self._alert(conn, "job_finished", {"job": job["id"], "outcome": outcome, "success": exec_success})

    def _job_after_nonstart(self, conn: sqlite3.Connection, job_id: int, *, reason: str, backoff: int,
                            permanent: bool = False) -> None:
        job = state.get_job(conn, job_id)
        if job["phase"] == "TERMINAL":
            return
        if job["desired_state"] == "CANCEL":
            state.update_job(conn, job_id, event="cancelled", actor="controller", phase="TERMINAL",
                             execution_outcome="CANCELLED", success=0, ended_at=utcnow())
        elif job["desired_state"] == "HOLD":
            state.update_job(conn, job_id, event="held", actor="controller", phase="HELD", reason=reason)
        elif permanent:
            state.update_job(conn, job_id, event="blocked", actor="controller", phase="BLOCKED", reason=reason)
        else:
            state.update_job(conn, job_id, event="requeued_local", actor="controller", phase="PENDING",
                             reason=reason, not_before=_later(backoff) if backoff else None)

    # ---- dependencies ----------------------------------------------------------------

    async def _evaluate_dependencies(self) -> None:
        def run(conn):
            for job in conn.execute(
                "SELECT DISTINCT j.* FROM jobs j JOIN deps d ON d.job_id = j.id WHERE j.phase = 'PENDING'"
            ).fetchall():
                verdict = dependency_verdict(conn, job["id"])
                if verdict == "never":
                    state.update_job(conn, job["id"], event="blocked", actor="controller", phase="BLOCKED",
                                     reason="DependencyNeverSatisfied")
                elif verdict == "wait" and job["reason"] != "Dependency":
                    state.update_job(conn, job["id"], event="waiting_dependency", actor="controller",
                                     reason="Dependency")
        await self._tx(run)

    # ---- placement and dispatch ------------------------------------------------------

    async def _place_and_dispatch(self) -> None:
        paused = await self.store.run(lambda c: _meta(c, "dispatch", "on") != "on" or fence.restore_discovery_pending(c))
        if paused or not self.clock_ok():
            return
        if len(self._inflight) >= self.config.global_dispatch:
            return
        candidates = await self.store.run(self._eligible_jobs)
        held_for = await self.store.run(lambda c: self._starvation_holds(c, candidates))
        for job_id in candidates:
            if len(self._inflight) >= self.config.global_dispatch:
                break
            placed = await self._tx(lambda c, job_id=job_id: self._plan(c, job_id, held_for))
            if placed is None:
                continue
            attempt_id, target = placed
            self._inflight_targets[attempt_id] = target
            task = asyncio.create_task(self._dispatch(attempt_id))
            self._inflight[attempt_id] = task
            task.add_done_callback(lambda _t, a=attempt_id: (self._inflight.pop(a, None),
                                                              self._inflight_targets.pop(a, None), self.wake()))

    def _eligible_jobs(self, conn: sqlite3.Connection) -> list[int]:
        now = utcnow()
        rows = conn.execute(
            "SELECT id, priority, submitted_at FROM jobs WHERE phase = 'PENDING' AND desired_state = 'RUN'"
            " AND (not_before IS NULL OR not_before <= ?)", (now,)).fetchall()
        scored = []
        active_in_group: dict[str, int] = {}
        for row in rows:
            if dependency_verdict(conn, row["id"]) != "ok":
                continue
            if not self._under_array_throttle(conn, row["id"], active_in_group):
                continue
            scored.append((-self.effective_priority(row["priority"], row["submitted_at"], now), row["id"]))
        return [job_id for _, job_id in sorted(scored)]

    def effective_priority(self, priority: int, submitted_at: str, now: str) -> int:
        """Base priority plus the age boost (+age_boost_per_hour per hour waited, capped)."""
        age_h = (parse_utc(now) - parse_utc(submitted_at)).total_seconds() / 3600
        return priority + min(int(age_h * self.config.age_boost_per_hour), self.config.age_boost_cap)

    def dispatch_view(self, conn: sqlite3.Connection) -> tuple[list[int], dict[int, int]]:
        """What the scheduler would try next, in order, and every waiting job's effective priority.

        The same ordering code the dispatcher runs, so `fq q` can't disagree with it.
        """
        now = utcnow()
        effective = {r["id"]: self.effective_priority(r["priority"], r["submitted_at"], now) for r in conn.execute(
            "SELECT id, priority, submitted_at FROM jobs WHERE phase IN ('PENDING','HELD','BLOCKED')")}
        return self._eligible_jobs(conn), effective

    def _under_array_throttle(self, conn: sqlite3.Connection, job_id: int, active: dict[str, int]) -> bool:
        """``--array 0-99%4``: at most 4 members of the array past PENDING at once (§4.5)."""
        job = conn.execute("SELECT group_id, spec_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job["group_id"] is None:
            return True
        throttle = ((json.loads(job["spec_json"]).get("control") or {}).get("array") or {}).get("throttle")
        if not throttle:
            return True
        if job["group_id"] not in active:
            active[job["group_id"]] = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE group_id = ? AND phase NOT IN"
                " ('PENDING','HELD','BLOCKED','TERMINAL')", (job["group_id"],)).fetchone()[0]
        if active[job["group_id"]] >= throttle:
            return False
        active[job["group_id"]] += 1        # the ones admitted in this pass count too
        return True

    def _starvation_holds(self, conn: sqlite3.Connection, candidates: list[int]) -> dict[str, int]:
        """Target -> starving job it is held for (§4.4).

        Greedy placement lets a stream of small jobs take every GPU as it frees, so a
        big job could wait forever. Once an eligible GPU job has pended longer than
        ``starve_after_s`` and still can't be placed, the node that could hold it
        and is closest to free stops taking other work until that job starts.
        Oldest-by-priority first; one hold per node, one node per job.
        """
        if not self.config.starve_after_s:
            return {}
        from .placement import candidate_targets, could_ever_fit, gpus_in_use
        cutoff = _later(-self.config.starve_after_s)
        holds: dict[str, int] = {}
        for job_id in candidates:
            job = state.get_job(conn, job_id)
            spec = json.loads(job["spec_json"])
            if job["submitted_at"] > cutoff or not spec["resources"]["gpus"]:
                continue
            fits = [t for t, _ in candidate_targets(conn, job, spec)
                    if t not in holds and could_ever_fit(conn, spec, t)]
            if fits:
                holds[min(fits, key=lambda t: (gpus_in_use(conn, t), t))] = job_id
        return holds

    def _plan(self, conn: sqlite3.Connection, job_id: int,
              held_for: dict[str, int] | None = None) -> tuple[str, str] | None:
        job = state.get_job(conn, job_id)
        if job["phase"] != "PENDING" or job["desired_state"] != "RUN" or state.active_attempt(conn, job_id):
            return None
        token = conn.execute("SELECT * FROM tokens WHERE id = ?", (job["token_id"],)).fetchone()
        if token is None or token["revoked_at"]:
            state.update_job(conn, job_id, event="blocked", actor="controller", phase="BLOCKED",
                             reason="submitting token revoked")
            return None
        from ..auth import Principal
        principal = Principal(token["id"], token["owner"], token["kind"], token["label"],
                              frozenset(token["scopes"].split()), bool(token["allow_clusters"]),
                              json.loads(token["quota_json"]))
        busy = set(self._inflight_targets.values())
        placement, rejections = place_job(conn, job, busy_targets=busy, quota=effective_quota(principal),
                                          shared=self.shared_capacity, held_for=held_for)
        if placement is None:
            reason = _summarize(rejections)
            if job["reason"] != reason:
                state.update_job(conn, job_id, event="waiting", actor="controller", reason=reason)
                conn.execute("INSERT INTO placement_decisions (ts, job_id, decision, detail_json) VALUES (?,?,?,?)",
                             (utcnow(), job_id, "no_placement",
                              json.dumps([{"target": r.target, "reason": r.reason, **r.detail} for r in rejections])))
            return None
        att = state.create_attempt(conn, job_id, backend=placement.backend, target=placement.target,
                                   epoch=self.ident.epoch, reservations=placement.reservations,
                                   profile_digest=None)
        conn.execute("INSERT INTO placement_decisions (ts, job_id, attempt_id, decision, detail_json) VALUES (?,?,?,?,?)",
                     (utcnow(), job_id, att["id"], "placed",
                      json.dumps({"target": placement.target, "gpus": placement.gpus,
                                  "resources": placement.resources, "queue": placement.queue})))
        return att["id"], placement.target

    def _context(self, conn: sqlite3.Connection, att: sqlite3.Row) -> AttemptContext:
        job = state.get_job(conn, att["job_id"])
        spec = json.loads(job["spec_json"])
        gpus = [r["gpu_uuid"] for r in conn.execute(
            "SELECT gpu_uuid FROM resource_reservations WHERE attempt_id = ? AND kind = 'gpu' ORDER BY gpu_uuid",
            (att["id"],))]
        amounts = {r["kind"]: r["amount"] for r in conn.execute(
            "SELECT kind, amount FROM resource_reservations WHERE attempt_id = ? AND kind <> 'gpu'", (att["id"],))}
        decision = conn.execute(
            "SELECT detail_json FROM placement_decisions WHERE attempt_id = ? AND decision = 'placed'",
            (att["id"],)).fetchone()
        placed = json.loads(decision["detail_json"]) if decision else {}
        node = conn.execute("SELECT config_json FROM nodes WHERE id = ?", (att["target"],)).fetchone()
        bundle_path = None
        if job["bundle_digest"] and self.bundle_dir is not None:
            bundle_path = self.bundle_dir / (job["bundle_digest"].split(":", 1)[1] + ".tar.gz")
        return AttemptContext(
            attempt_id=att["id"], job_id=job["id"], n=att["n"], target=att["target"], epoch=att["epoch"],
            fleet_id=self.ident.fleet_id, spec=spec, spec_digest=att["spec_digest"],
            launch_op_id=att["launch_op_id"], gpus=gpus,
            resources=placed.get("resources") or {
                "mem_mb": amounts.get("ram"), "cpus": amounts.get("cpu"), "scratch_mb": amounts.get("scratch"),
                "gpus": len(gpus)},
            bundle_path=bundle_path, bundle_digest=job["bundle_digest"], queue=placed.get("queue"),
            profile=json.loads(node["config_json"] or "{}") if node else {},
            array_index=job["array_index"],
        )

    async def _dispatch(self, attempt_id: str) -> None:
        att = await self.store.run(lambda c: state.get_attempt(c, attempt_id))
        executor = self._executor(att["backend"])

        def to_staging(conn):
            state.update_attempt(conn, attempt_id, state="STAGING", event="staging")
            return self._context(conn, state.get_attempt(conn, attempt_id))
        ctx = await self._tx(to_staging)
        try:
            staged = await executor.stage(ctx)
        except Exception as exc:  # staging executes nothing; failure is safe
            log.exception("stage failed")
            from ..executors.base import StageResult
            staged = StageResult(ok=False, retryable=True, reason=f"stage error: {exc}")
        if not staged.ok:
            def stage_failed(conn):
                state.update_attempt(conn, attempt_id, state="NEVER_STARTED", event="stage_failed",
                                     evidence={"reason": staged.reason})
                state.release_reservations(conn, attempt_id, reason="stage_failed")
                self._job_after_nonstart(conn, att["job_id"], reason=f"staging failed: {staged.reason}",
                                         backoff=self.config.stage_backoff_s, permanent=not staged.retryable)
            await self._tx(stage_failed)
            return

        launch_state = "SUBMITTING" if att["backend"] == "slurm" else "LAUNCHING"

        def launch_boundary(conn):
            # Recheck everything right before the remote launch boundary (§4.4).
            job = state.get_job(conn, att["job_id"])
            token = conn.execute("SELECT revoked_at FROM tokens WHERE id = ?", (job["token_id"],)).fetchone()
            node = conn.execute("SELECT enabled, drain_kind FROM nodes WHERE id = ?", (att["target"],)).fetchone()
            paused = _meta(conn, "dispatch", "on") != "on"
            ok = (job["desired_state"] == "RUN" and job["phase"] == "DISPATCHING" and not paused
                  and token is not None and not token["revoked_at"] and node is not None and node["enabled"]
                  and not node["drain_kind"] and fence.target_dispatch_ready(conn, att["target"]))
            if not ok:
                state.update_attempt(conn, attempt_id, state="NEVER_STARTED", event="launch_aborted",
                                     evidence={"why": "no longer dispatchable at the launch boundary"})
                state.release_reservations(conn, attempt_id, reason="launch_aborted")
                self._job_after_nonstart(conn, job["id"], reason="launch aborted", backoff=0)
                return False
            state.update_attempt(conn, attempt_id, state=launch_state, event="launch_intended")
            conn.execute(
                "INSERT INTO operations (id, attempt_id, target, kind, state, epoch, created_at, updated_at)"
                " VALUES (?,?,?,?, 'INTENDED', ?, ?, ?)",
                (att["launch_op_id"], attempt_id, att["target"], "launch", att["epoch"], utcnow(), utcnow()))
            return True
        if not await self._tx(launch_boundary):
            return
        try:
            result = await executor.launch(ctx)
        except Exception as exc:
            log.exception("launch raised after the send boundary")
            from ..executors.base import LaunchResult
            result = LaunchResult(LaunchKind.UNKNOWN, reason=f"launch error: {exc}")
        await self._apply_launch(attempt_id, result)

    async def _apply_launch(self, attempt_id: str, res) -> None:
        def apply(conn):
            att = state.get_attempt(conn, attempt_id)
            job = state.get_job(conn, att["job_id"])
            slurm = att["backend"] == "slurm"
            # Only a response arriving while its attempt still awaits the
            # launch RPC may reduce attempt/job state. A concurrent
            # observation or cancellation can prove a later state first; a
            # delayed refusal/timeout must not roll it back or release holds.
            if att["state"] not in ("LAUNCHING", "SUBMITTING", "START_UNKNOWN", "SUBMISSION_UNKNOWN"):
                state.add_event(conn, "stale_launch_reply", job_id=job["id"], attempt_id=attempt_id,
                                target=att["target"], actor="controller",
                                detail={"reply_kind": res.kind.value, "attempt_state": att["state"],
                                        "reason": res.reason})
                return
            op_state = {"started": "DONE", "unknown": "UNCERTAIN"}.get(res.kind.value, "FAILED")
            conn.execute("UPDATE operations SET state = ?, detail_json = ?, updated_at = ? WHERE id = ?",
                         (op_state, json.dumps({"kind": res.kind.value, "reason": res.reason}), utcnow(),
                          att["launch_op_id"]))
            if res.kind is LaunchKind.STARTED:
                next_state = "SUBMITTED" if slurm else "RUNNING"
                state.update_attempt(conn, attempt_id, state=next_state, event="launched",
                                     remote_id=res.remote_id, boot_id=res.boot_id,
                                     started_at=None if slurm else utcnow())
                if slurm:
                    if job["phase"] in ("DISPATCHING", "SUBMISSION_UNKNOWN", "RECONCILING"):
                        state.update_job(conn, job["id"], event="submitted", actor="controller",
                                         phase="SUBMITTED", reason=None)
                else:
                    self._job_running(conn, job, first_entry=True)
            elif res.kind is LaunchKind.PLACEMENT_REFUSED:
                state.update_attempt(conn, attempt_id, state="REFUSED", event="placement_refused",
                                     evidence={"reason": res.reason, "gpus": res.cooldown_gpus})
                state.release_reservations(conn, attempt_id, reason="placement_refused")
                until = _later(self.config.gpu_cooldown_s)
                for uuid in res.cooldown_gpus:
                    conn.execute("UPDATE node_gpus SET cooldown_until = ? WHERE node_id = ? AND uuid = ?",
                                 (until, att["target"], uuid))
                conn.execute("INSERT INTO placement_decisions (ts, job_id, attempt_id, decision, detail_json)"
                             " VALUES (?,?,?,?,?)", (utcnow(), job["id"], attempt_id, "placement_refused",
                                                     json.dumps({"reason": res.reason, "gpus": res.cooldown_gpus})))
                refusals = conn.execute(
                    "SELECT COUNT(*) FROM placement_decisions WHERE job_id = ? AND decision = 'placement_refused'"
                    " AND ts >= ?", (job["id"], _later(-3600))).fetchone()[0]
                backoff = self.config.refusal_backoff_s * (4 if refusals >= self.config.max_refusals_per_hour else 1)
                self._job_after_nonstart(conn, job["id"], reason=f"placement refused: {res.reason}", backoff=backoff)
            elif res.kind is LaunchKind.NEVER_STARTED:
                state.update_attempt(conn, attempt_id, state="NEVER_STARTED", event="launch_never_started",
                                     evidence={"reason": res.reason})
                state.release_reservations(conn, attempt_id, reason="never_started")
                self._job_after_nonstart(conn, job["id"], reason=f"not started: {res.reason}",
                                         backoff=self.config.stage_backoff_s, permanent=res.permanent)
            else:
                unknown = "SUBMISSION_UNKNOWN" if slurm else "START_UNKNOWN"
                if att["state"] in ("LAUNCHING", "SUBMITTING"):
                    state.update_attempt(conn, attempt_id, state=unknown, event="launch_unknown",
                                         evidence={"reason": res.reason})
                phase = "SUBMISSION_UNKNOWN" if slurm else "RECONCILING"
                if job["phase"] in ("DISPATCHING",):
                    state.update_job(conn, job["id"], event="launch_unknown", actor="controller",
                                     phase=phase, reason=unknown.lower())
                self._alert(conn, "launch_unknown", {"job": job["id"], "attempt": attempt_id, "reason": res.reason})
            launched_on.append(att["target"])
        launched_on: list[str] = []
        await self._tx(apply)
        for target in launched_on:
            self._transitioned(target)


def dependency_verdict(conn: sqlite3.Connection, job_id: int) -> str:
    """``ok`` (all satisfied), ``wait``, or ``never`` (§4.5).

    Unknown or possibly-live parents never satisfy a terminal dependency.
    """
    verdict = "ok"
    for dep in conn.execute("SELECT * FROM deps WHERE job_id = ?", (job_id,)).fetchall():
        parent = state.get_job(conn, dep["parent_id"])
        terminal = parent["phase"] == "TERMINAL"
        success = parent["success"] == 1
        if dep["type"] == "after":
            started = conn.execute(
                "SELECT 1 FROM attempts WHERE job_id = ? AND started_at IS NOT NULL", (parent["id"],)).fetchone()
            if not started:
                if terminal:
                    return "never"
                verdict = "wait"
        elif not terminal:
            verdict = "wait"
        elif dep["type"] == "afterok" and not success:
            return "never"
        elif dep["type"] == "afternotok" and success:
            return "never"
    return verdict


def _summarize(rejections: list) -> str:
    if not rejections:
        return "no candidate destinations"
    reasons = sorted({r.reason for r in rejections})
    return "waiting: " + ", ".join(reasons[:4])
