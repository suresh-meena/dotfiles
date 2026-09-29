"""An in-memory executor for control-plane tests (P1a).

It models the parts of a real node that the safety argument depends on:

* launches are idempotent by attempt id, and a payload is *entered* at most
  once per attempt (the durable runner-entry claim, §2.3);
* a launch whose response is lost still executes (``lose_launch_response``);
* nodes can be unreachable, reboot (boot id changes), or refuse at the gate;
* cancellation writes a tombstone that wins if it lands before entry.

``payload_entries`` counts actual workload entries, separately from transport
calls: several idempotent launch RPCs are fine, a second entry is a bug (§13.2).
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field

from .base import (
    AttemptContext,
    AttemptObservation,
    CancelResult,
    FenceResult,
    LaunchKind,
    LaunchResult,
    ObserveResult,
    ReleaseResult,
    StageResult,
)


@dataclass
class FakeRemoteAttempt:
    attempt_id: str
    epoch: int
    staged: bool = False
    start_requested: bool = False
    entered: bool = False
    cancelled_before_entry: bool = False
    stopped: bool = False
    exit_code: int | None = None
    outcome: str | None = None
    ticks_left: int = 2
    boot_id: str = ""
    cgroup_empty: bool = True


@dataclass
class FakeNode:
    name: str
    gpus: list[str] = field(default_factory=list)
    reachable: bool = True
    boot_id: str = field(default_factory=lambda: secrets.token_hex(4))
    highest_epoch: int = 0
    fleet_id: str | None = None
    refuse_gpus: set[str] = field(default_factory=set)       # gate says busy
    lose_launch_response: bool = False                         # launch runs, reply lost
    fail_launch_before_send: bool = False
    stage_fails: bool = False
    run_ticks: int = 2                                         # observations until exit
    exit_code: int = 0
    outcome: str = "COMPLETED"
    leave_descendants: bool = False                            # cgroup not empty after stop
    attempts: dict[str, FakeRemoteAttempt] = field(default_factory=dict)


class FakeExecutor:
    backend = "fake"

    def __init__(self, nodes: list[FakeNode] | None = None) -> None:
        self.nodes: dict[str, FakeNode] = {n.name: n for n in (nodes or [])}
        self.payload_entries: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.delay = 0.0

    def node(self, name: str) -> FakeNode:
        return self.nodes[name]

    async def _hop(self, verb: str, target: str) -> FakeNode | None:
        self.calls.append((verb, target))
        if self.delay:
            await asyncio.sleep(self.delay)
        node = self.nodes.get(target)
        return node if node is not None and node.reachable else None

    # ---- protocol -----------------------------------------------------------

    async def fence(self, target: str, *, epoch: int, fleet_id: str) -> FenceResult:
        node = await self._hop("fence", target)
        if node is None:
            return FenceResult(reachable=False, reason="unreachable")
        if node.fleet_id not in (None, fleet_id):
            return FenceResult(reachable=True, accepted=False, reason="enrolled to another fleet")
        seen = node.highest_epoch
        if epoch <= seen:
            return FenceResult(reachable=True, accepted=False, highest_epoch_seen=seen,
                               remote_attempts=list(node.attempts), reason="stale epoch")
        node.fleet_id = fleet_id
        node.highest_epoch = epoch
        return FenceResult(reachable=True, accepted=True, highest_epoch_seen=seen, remote_attempts=list(node.attempts))

    async def stage(self, ctx: AttemptContext) -> StageResult:
        node = await self._hop("stage", ctx.target)
        if node is None:
            return StageResult(ok=False, retryable=True, reason="unreachable")
        if node.stage_fails:
            return StageResult(ok=False, retryable=True, reason="stage failed")
        rec = node.attempts.setdefault(ctx.attempt_id, FakeRemoteAttempt(ctx.attempt_id, ctx.epoch))
        rec.staged = True
        return StageResult(ok=True)

    async def launch(self, ctx: AttemptContext) -> LaunchResult:
        node = await self._hop("launch", ctx.target)
        if node is None or node.fail_launch_before_send:
            return LaunchResult(LaunchKind.NEVER_STARTED, reason="transport_failed_before_send")
        if ctx.epoch < node.highest_epoch:
            return LaunchResult(LaunchKind.NEVER_STARTED, reason="stale_epoch", permanent=False)
        rec = node.attempts.setdefault(ctx.attempt_id, FakeRemoteAttempt(ctx.attempt_id, ctx.epoch))
        busy = [g for g in ctx.gpus if g in node.refuse_gpus]
        if busy and not rec.entered:
            return LaunchResult(LaunchKind.PLACEMENT_REFUSED, reason="gpu_busy", cooldown_gpus=busy)
        if rec.cancelled_before_entry:
            return LaunchResult(LaunchKind.NEVER_STARTED, reason="cancelled")
        rec.start_requested = True
        if not rec.entered:
            # The durable runner-entry claim: exactly one entry per attempt.
            rec.entered = True
            rec.boot_id = node.boot_id
            rec.ticks_left = node.run_ticks
            self.payload_entries[ctx.attempt_id] = self.payload_entries.get(ctx.attempt_id, 0) + 1
        if node.lose_launch_response:
            return LaunchResult(LaunchKind.UNKNOWN, reason="response_lost")
        return LaunchResult(LaunchKind.STARTED, remote_id=f"fq-{ctx.attempt_id}.service", boot_id=node.boot_id)

    async def observe(self, target: str, attempt_ids: list[str]) -> ObserveResult:
        node = await self._hop("observe", target)
        if node is None:
            return ObserveResult(reachable=False, reason="unreachable")
        out: dict[str, AttemptObservation] = {}
        for aid in attempt_ids:
            rec = node.attempts.get(aid)
            if rec is None:
                out[aid] = AttemptObservation(aid, "absent", boot_id=node.boot_id)
                continue
            if rec.entered and not rec.stopped and rec.boot_id != node.boot_id:
                # Rebooted under it: the old process is gone, its exit unknown.
                out[aid] = AttemptObservation(aid, "stopped", payload_entered=True, outcome="NODE_FAIL",
                                              cgroup_empty=True, boot_id=node.boot_id, boot_changed=True)
                continue
            if rec.cancelled_before_entry and not rec.entered:
                out[aid] = AttemptObservation(aid, "refused", boot_id=node.boot_id)
                continue
            if rec.entered and not rec.stopped:
                rec.ticks_left -= 1
                if rec.ticks_left <= 0:
                    rec.stopped = True
                    rec.exit_code = node.exit_code
                    rec.outcome = node.outcome
                    rec.cgroup_empty = not node.leave_descendants
            if rec.stopped:
                out[aid] = AttemptObservation(aid, "stopped", payload_entered=True, outcome=rec.outcome,
                                              exit_code=rec.exit_code, cgroup_empty=rec.cgroup_empty,
                                              boot_id=node.boot_id)
            elif rec.entered:
                out[aid] = AttemptObservation(aid, "running", payload_entered=True, boot_id=node.boot_id)
            elif rec.start_requested:
                out[aid] = AttemptObservation(aid, "start_requested", boot_id=node.boot_id)
            else:
                out[aid] = AttemptObservation(aid, "staged", boot_id=node.boot_id)
        return ObserveResult(reachable=True, attempts=out, boot_id=node.boot_id)

    async def cancel(self, target: str, attempt_id: str, *, epoch: int) -> CancelResult:
        node = await self._hop("cancel", target)
        if node is None:
            return CancelResult(reachable=False, reason="unreachable")
        rec = node.attempts.get(attempt_id)
        if rec is None or not rec.entered:
            if rec is None:
                rec = node.attempts.setdefault(attempt_id, FakeRemoteAttempt(attempt_id, epoch))
            rec.cancelled_before_entry = True
            return CancelResult(reachable=True, stopped=True, never_started=True)
        if not rec.stopped:
            rec.stopped = True
            rec.outcome = "CANCELLED"
            rec.exit_code = None
            rec.cgroup_empty = not node.leave_descendants
        return CancelResult(reachable=True, stopped=rec.cgroup_empty)

    async def release(self, target: str, attempt_id: str) -> ReleaseResult:
        node = await self._hop("release", target)
        if node is None:
            return ReleaseResult(released=False, reason="unreachable")
        rec = node.attempts.get(attempt_id)
        if rec is not None and rec.entered and not rec.cgroup_empty:
            return ReleaseResult(released=False, reason="descendants_remain")
        return ReleaseResult(released=True)

    # ---- test helpers -------------------------------------------------------

    def reboot(self, target: str) -> None:
        self.nodes[target].boot_id = secrets.token_hex(4)

    def entries(self) -> dict[str, int]:
        return dict(self.payload_entries)
