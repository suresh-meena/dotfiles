"""The contract between the engine and an execution backend.

Executors return *typed evidence*, never booleans the engine must interpret.
The distinction that matters most: ``LaunchKind.UNKNOWN`` means the operation
crossed the send boundary and may have executed. The engine then keeps the
attempt possibly-live and reconciles; it never retries the attempt (§2.3,
§3.3). ``PLACEMENT_REFUSED`` and ``NEVER_STARTED`` are positive proof the
payload did not start, and only they let the engine release resources and
move on.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class LaunchKind(enum.Enum):
    STARTED = "started"                      # remote confirmed the start/submission
    PLACEMENT_REFUSED = "placement_refused"  # typed gate refusal; payload never started
    NEVER_STARTED = "never_started"          # conclusive pre-execution refusal
    UNKNOWN = "unknown"                      # may have executed; reconcile, never replay


@dataclass
class AttemptContext:
    attempt_id: str
    job_id: int
    n: int
    target: str
    epoch: int
    fleet_id: str
    spec: dict[str, Any]
    spec_digest: str
    launch_op_id: str
    gpus: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    bundle_path: Path | None = None
    bundle_digest: str | None = None
    queue: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    array_index: int | None = None       # exported to the payload as FQ_ARRAY_TASK_ID


@dataclass
class StageResult:
    ok: bool
    retryable: bool = True
    reason: str | None = None


@dataclass
class LaunchResult:
    kind: LaunchKind
    reason: str | None = None
    remote_id: str | None = None
    boot_id: str | None = None
    permanent: bool = False                  # for NEVER_STARTED: blocks rather than retries
    cooldown_gpus: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttemptObservation:
    """What a backend knows about one attempt right now.

    ``state`` vocabulary:
      * ``absent``         — the target has no record of this attempt at all
      * ``staged``         — inputs present, start never requested (never ran)
      * ``start_requested``— a start was requested; payload entry not proven
      * ``running``        — the payload is (or may be) executing
      * ``pending``        — accepted by a scheduler, waiting to start (Slurm)
      * ``stopped``        — execution ended; ``cgroup_empty`` says whether
                              everything it spawned is gone
      * ``refused``        — the gate refused it; payload never started
      * ``unknown``        — the target answered but evidence is inconclusive
    """

    attempt_id: str
    state: str
    payload_entered: bool = False
    outcome: str | None = None               # COMPLETED/FAILED/TIMEOUT/OUT_OF_MEMORY/CANCELLED/NODE_FAIL/...
    exit_code: int | None = None
    exit_signal: int | None = None
    cgroup_empty: bool | None = None
    boot_id: str | None = None
    boot_changed: bool = False
    remote_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ObserveResult:
    reachable: bool
    attempts: dict[str, AttemptObservation] = field(default_factory=dict)
    # The remote-call budget said "not now": not a reachability signal (§3.5).
    deferred: bool = False
    retry_after: float | None = None
    boot_id: str | None = None
    reason: str | None = None
    node_facts: dict[str, Any] = field(default_factory=dict)
    logs: dict[str, dict[str, "LogChunk"]] = field(default_factory=dict)   # attempt -> stream -> delta


@dataclass
class CancelResult:
    reachable: bool
    stopped: bool = False                    # termination confirmed (cgroup empty / scheduler says gone)
    never_started: bool = False              # tombstone landed before the payload entered
    reason: str | None = None


@dataclass
class ReleaseResult:
    released: bool
    reason: str | None = None


@dataclass
class FenceResult:
    reachable: bool
    accepted: bool = False
    highest_epoch_seen: int = 0
    remote_attempts: list[str] = field(default_factory=list)
    reason: str | None = None


@dataclass
class LogChunk:
    """Bytes read from one remote stream. ``remote_size`` None means the source is unreadable."""
    stream: str
    offset: int
    data: bytes = b""
    remote_size: int | None = None
    reachable: bool = True
    deferred: bool = False            # the budget said not now


@dataclass
class CollectManifest:
    """What a node staged for one pull: numbered slots mapped to relative paths."""
    ok: bool
    files: list[dict[str, Any]] = field(default_factory=list)   # slot, relpath, size, sha256?
    missing: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None          # a permanent refusal (too_large, in_place_collect, ...)
    reachable: bool = True
    deferred: bool = False


class Executor(Protocol):
    backend: str

    async def fence(self, target: str, *, epoch: int, fleet_id: str) -> FenceResult: ...

    async def stage(self, ctx: AttemptContext) -> StageResult: ...

    async def launch(self, ctx: AttemptContext) -> LaunchResult: ...

    async def observe(self, target: str, attempt_ids: list[str]) -> ObserveResult: ...

    async def cancel(self, target: str, attempt_id: str, *, epoch: int) -> CancelResult: ...

    async def release(self, target: str, attempt_id: str) -> ReleaseResult: ...
