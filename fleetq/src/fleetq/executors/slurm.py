"""The Slurm executor: bounded one-shot commands through a login node (§3).

Nothing is installed or left running on the cluster. Each operation is one
``fleetctl exec --admin`` session with a declared operation class, and each
first obtains a permit from the central budget authority (§3.5).

Submission uses the submit-once protocol in ``slurm.render``: a remote claim
then a single sbatch whose ``--parsable`` receipt is persisted on the
cluster. An uncertain submission becomes ``UNKNOWN``, and is resolved from
the receipt, then an exact job-name lookup, then accounting. Nothing here
ever sends a second sbatch for the same attempt, and three empty lookups
don't authorize one (§3.3).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..slurm.render import (CACHE_GC_SCRIPT, CACHE_PIN_RELEASE_SCRIPT, CANCEL_SCRIPT, COLLECT_CLEAN_SCRIPT,
                           COLLECT_STAGE_SCRIPT, CONTROL_ROOT_CHECK_SCRIPT, FENCE_SCRIPT, LOGS_SCRIPT,
                           OBSERVE_SCRIPT, STAGE_WRAPPER, SUBMIT_WRAPPER, bash_argv, render_batch, sh_argv)
from ..slurm.managed_snapshot import (
    MANAGED_SQUEUE_SCRIPT,
    MANAGED_SQUEUE_TEXT_SCRIPT,
    MAX_SQUEUE_BYTES,
    parse_squeue_json,
    parse_squeue_text,
)
from ..transport.fleetctl import Fleetctl
from .base import (
    AttemptContext,
    AttemptObservation,
    CancelResult,
    CollectManifest,
    FenceResult,
    LaunchKind,
    LogChunk,
    LaunchResult,
    ObserveResult,
    ReleaseResult,
    StageResult,
)

TIMEOUT_S = 120
STAGE_TIMEOUT_S = 900
PENDING_STATES = {"PENDING", "CONFIGURING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "SUSPENDED", "RESIZING",
                  "SIGNALING", "STAGE_OUT"}
RUNNING_STATES = {"RUNNING", "COMPLETING"}
TERMINAL_MAP = {"COMPLETED": "COMPLETED", "FAILED": "FAILED", "CANCELLED": "CANCELLED", "TIMEOUT": "TIMEOUT",
                "OUT_OF_MEMORY": "OUT_OF_MEMORY", "NODE_FAIL": "NODE_FAIL", "PREEMPTED": "PREEMPTED",
                "BOOT_FAIL": "NODE_FAIL", "DEADLINE": "TIMEOUT", "SPECIAL_EXIT": "FAILED"}
# Reasons that describe a request that can never run as submitted. Aggregate
# association/QOS limits (AssocGrpGRES, QOSGrp*) are NOT here: they can clear (§3.7).
BLOCKING_REASONS = {"AccountNotAllowed", "QOSNotAllowed", "PartitionTimeLimit", "PartitionNodeLimit",
                    "InvalidAccount", "InvalidQOS", "BadConstraints", "ReqNodeNotAvail, UnavailableNodes",
                    "PartitionConfig", "AssocMaxWallDurationPerJobLimit", "QOSMaxWallDurationPerJobLimit"}

PermitFn = Callable[[str, str, int, dict[str, int] | None], Awaitable[tuple[bool, str | None, float | None]]]


class SlurmExecutor:
    backend = "slurm"

    def __init__(self, transport: Fleetctl, cfg: Any, *, permit: PermitFn | None = None) -> None:
        self.transport = transport
        self.sites = {n.id: n for n in cfg.nodes if n.backend == "slurm"}
        self.permit = permit
        controller_cfg = getattr(cfg, "controller", {}) or {}
        collect_max_bytes = int(controller_cfg.get("collect_max_bytes", 2 * 1024 ** 3))
        collect_max_files = int(controller_cfg.get("collect_max_files", 10_000))
        # Budget the payload cap plus per-file manifest/transport overhead.
        # Bound the overhead explicitly so unusual site settings cannot make
        # the transfer permit effectively unlimited.
        self.collect_pull_budget_bytes = collect_max_bytes + min(64 * 1024 ** 2,
                                                                 max(1, collect_max_files) * 4096)
        self.fleet_id = ""
        self._receipts: dict[str, str] = {}

    def _site(self, target: str):
        site = self.sites[target]
        if not site.control_root or not site.control_root.startswith("/"):
            raise KeyError(f"site {target} needs an absolute control_root on its shared filesystem")
        return site

    async def _permit(self, target: str, op_class: str, rpc: int,
                      cost: dict[str, int] | None = None) -> tuple[bool, str | None, float | None]:
        if self.permit is None:
            return False, None, 300.0
        try:
            granted, permit_id, retry = await self.permit(target, op_class, rpc, cost)
        except Exception:
            return False, None, 300.0
        # A nominal grant without an opaque permit cannot be redeemed and must
        # never permit an operation to fall back to an unmetered path.
        if not granted or not permit_id:
            return False, None, retry or 300.0
        return True, permit_id, retry

    async def _exec(self, target: str, script: str, *args: str, op_class: str, mutation: bool, rpc: int):
        site = self._site(target)
        # Match fleetctl's permit redemption envelope for `exec`; rpc remains
        # the executor's accounting estimate, while fleetctl conservatively
        # charges each CLI operation for its full nested-call envelope.
        granted, permit_id, retry = await self._permit(
            target, op_class, rpc, {"rpc": 8, "sessions": 2, "bytes": 0}
        )
        if not granted:
            return None, retry, None
        res = await self.transport.exec(site.fleetctl_target or target, sh_argv(script, *args), timeout=TIMEOUT_S,
                                        mutation=mutation, admin=True, op_class=op_class, permit=permit_id,
                                        expected_role="login")
        return res, None, (res.payload() if res.outcome in ("ok", "remote_failed") else None)

    async def managed_queue_snapshot(self, target: str) -> dict[str, Any]:
        """Fetch one bounded all-user squeue snapshot under the monitor budget."""
        res, retry, _ = await self._exec(
            target, MANAGED_SQUEUE_SCRIPT, op_class="monitor", mutation=False, rpc=1
        )
        if res is None:
            return {"complete": False, "error": "budget_deferred", "retry_after": retry}
        truncated = bool((res.envelope or {}).get("stdout_truncated"))
        if truncated:
            return {"complete": False, "error": "output_truncated"}
        if res.outcome not in ("ok", "remote_failed"):
            return {"complete": False, "error": res.outcome}
        jobs = None
        output_bytes = len(res.stdout.encode("utf-8"))
        if res.outcome == "ok" and output_bytes <= MAX_SQUEUE_BYTES:
            try:
                jobs = parse_squeue_json(res.stdout)
            except (UnicodeError, ValueError, json.JSONDecodeError):
                jobs = None
        if jobs is not None:
            return {"complete": True, "jobs": jobs, "output_bytes": output_bytes}
        if output_bytes > MAX_SQUEUE_BYTES:
            return {"complete": False, "error": "output_truncated"}
        # Match Fleetmon's one fixed parsable2 fallback for older Slurm, with
        # a separate permit because it is a second scheduler RPC/session.
        fallback, retry, _ = await self._exec(
            target, MANAGED_SQUEUE_TEXT_SCRIPT, op_class="monitor", mutation=False, rpc=1
        )
        if fallback is None:
            return {"complete": False, "error": "budget_deferred", "retry_after": retry}
        if bool((fallback.envelope or {}).get("stdout_truncated")):
            return {"complete": False, "error": "output_truncated"}
        if fallback.outcome != "ok":
            return {"complete": False, "error": "remote_failed" if fallback.outcome == "remote_failed" else fallback.outcome}
        output_bytes = len(fallback.stdout.encode("utf-8"))
        if output_bytes > MAX_SQUEUE_BYTES:
            return {"complete": False, "error": "output_truncated"}
        try:
            jobs = parse_squeue_text(fallback.stdout)
        except (UnicodeError, ValueError):
            return {"complete": False, "error": "invalid_output"}
        return {"complete": True, "jobs": jobs, "output_bytes": output_bytes}

    # ---- fence ------------------------------------------------------------------

    async def fence(self, target: str, *, epoch: int, fleet_id: str) -> FenceResult:
        self.fleet_id = fleet_id
        site = self._site(target)
        res, retry, body = await self._exec(target, FENCE_SCRIPT, site.control_root, fleet_id, str(epoch),
                                            op_class="action", mutation=True, rpc=0)
        if res is None:
            return FenceResult(reachable=False, reason="budget_deferred")
        if body is None:
            return FenceResult(reachable=False, reason=res.outcome)
        return FenceResult(reachable=True, accepted=bool(body.get("accepted")),
                           highest_epoch_seen=int(body.get("highest_epoch_seen", 0) or 0),
                           remote_attempts=list(body.get("attempts") or []), reason=body.get("reason"))

    # ---- staging -------------------------------------------------------------------

    async def stage(self, ctx: AttemptContext) -> StageResult:
        site = self._site(ctx.target)
        root = site.control_root
        adir = f"{root}/attempts/{ctx.attempt_id}"
        root_check, _retry, root_body = await self._exec(
            ctx.target, CONTROL_ROOT_CHECK_SCRIPT, root, op_class="action", mutation=False, rpc=1
        )
        if root_check is None:
            return StageResult(ok=False, retryable=True, reason="control_root_check_unavailable")
        if not root_body or not root_body.get("ok"):
            return StageResult(ok=False, retryable=False, reason="unsafe_control_root")
        bundle_sha = None
        if ctx.bundle_path is not None:
            with ctx.bundle_path.open("rb") as source:
                bundle_sha = hashlib.file_digest(source, "sha256").hexdigest()
        batch = render_batch(attempt_id=ctx.attempt_id, adir=adir, root=root, request=ctx.resources, spec=ctx.spec,
                             site=site.site or {}, bundle_digest=ctx.bundle_digest, bundle_sha256=bundle_sha,
                             array_index=ctx.array_index, job_id=ctx.job_id, attempt_n=ctx.n)
        tmp = Path(tempfile.mkdtemp(prefix="fq-slurm-"))
        stage_id = uuid.uuid4().hex
        try:
            att_dir = tmp / "attempt"
            att_dir.mkdir()
            (att_dir / "batch.sh").write_text(batch)
            (att_dir / "manifest.json").write_text(json.dumps({
                "attempt_id": ctx.attempt_id, "fleet_id": ctx.fleet_id, "job_id": ctx.job_id, "epoch": ctx.epoch,
                "spec_digest": ctx.spec_digest, "request": ctx.resources,
                "bundle_digest": ctx.bundle_digest}, sort_keys=True) + "\n")
            if (site.site or {}).get("preflight", True):
                lint = await self.transport.preflight(att_dir / "batch.sh", site.fleetctl_target or ctx.target)
                if not lint.ok:
                    return StageResult(ok=False, retryable=False,
                                       reason="preflight_failed: " + (lint.stderr or lint.stdout)[-300:])
                if lint.details["errors"]:
                    return StageResult(ok=False, retryable=False,
                                       reason="preflight_refused: " + ",".join(lint.details["errors"]))
            payload_dir = tmp / "payload"
            payload_dir.mkdir()
            shutil.move(str(att_dir), payload_dir / "attempt")
            cache_key = ""
            if ctx.bundle_path is not None:
                cache = payload_dir / "cache"
                cache.mkdir()
                cache_key = ctx.bundle_digest.split(":", 1)[1]
                shutil.copyfile(ctx.bundle_path, cache / f"{cache_key}.tar.gz")
            # Count the whole payload; it contains the attempt files and optional bundle.
            staged_bytes = sum(p.stat().st_size for p in payload_dir.rglob("*") if p.is_file())
            granted, permit_id, _retry = await self._permit(ctx.target, "transfer", 0,
                                                              {"rpc": 6, "sessions": 2, "bytes": staged_bytes})
            if not granted:
                return StageResult(ok=False, retryable=True, reason="budget_deferred")
            staged_root = f"{root}/cache/.stage-{stage_id}/"
            pushed = await self.transport.sync_push(site.fleetctl_target or ctx.target, Path(f"{payload_dir}/"),
                                                    staged_root, timeout=STAGE_TIMEOUT_S, permit=permit_id,
                                                    budget_bytes=staged_bytes, expected_role="login")
            if not pushed.ok:
                return StageResult(ok=False, retryable=True, reason=f"stage push {pushed.outcome}")
            res, retry, body = await self._exec(ctx.target, STAGE_WRAPPER, root, ctx.fleet_id, str(ctx.epoch),
                                               ctx.attempt_id, stage_id, cache_key, op_class="action",
                                               mutation=True, rpc=0)
            if res is None:
                return StageResult(ok=False, retryable=True, reason="budget_deferred")
            if body is None:
                return StageResult(ok=False, retryable=True, reason=f"stage publish {res.outcome}")
            if not body.get("ok"):
                reason = str(body.get("reason") or "stage_publish_refused")
                return StageResult(ok=False, retryable=reason not in {"stale_or_unfenced_epoch", "fleet_mismatch",
                                                                      "already_submitted"}, reason=reason)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return StageResult(ok=True)

    # ---- submission ------------------------------------------------------------------

    async def launch(self, ctx: AttemptContext) -> LaunchResult:
        site = self._site(ctx.target)
        res, retry, body = await self._exec(ctx.target, SUBMIT_WRAPPER, site.control_root, ctx.fleet_id,
                                            str(ctx.epoch), ctx.attempt_id, op_class="action", mutation=True, rpc=2)
        if res is None:
            return LaunchResult(LaunchKind.NEVER_STARTED, reason="budget_deferred")
        if body is None:
            # The command ran (ok/remote_failed) or may have, yet said nothing we can read:
            # the start may have happened. fleetctl's may_have_executed is false on a clean
            # exit 0, which proves the wrapper ran, not that it did nothing.
            if res.may_have_executed or res.outcome in ("ok", "remote_failed"):
                return LaunchResult(LaunchKind.UNKNOWN, reason=f"unreadable launch reply ({res.outcome})")
            return LaunchResult(LaunchKind.NEVER_STARTED, reason=f"transport {res.outcome}")
        result = body.get("result")
        if result == "started":
            job_id = str(body.get("receipt", "")).split(";", 1)[0]
            self._receipts[ctx.attempt_id] = job_id
            return LaunchResult(LaunchKind.STARTED, remote_id=job_id)
        if result == "never_started":
            return LaunchResult(LaunchKind.NEVER_STARTED, reason=body.get("reason"),
                                permanent=bool(body.get("permanent")), detail={"stderr": body.get("stderr")})
        return LaunchResult(LaunchKind.UNKNOWN, reason=body.get("reason"), detail={"stderr": body.get("stderr")})

    # ---- observation -----------------------------------------------------------------

    async def observe(self, target: str, attempt_ids: list[str]) -> ObserveResult:
        site = self._site(target)
        accounting = "1" if (site.site or {}).get("accounting", True) else "0"
        rpc = 1 + (1 if accounting == "1" else 0) + len(attempt_ids)  # squeue, sacct, name lookups (upper bound)
        res, retry, _ = await self._exec(target, OBSERVE_SCRIPT, site.control_root, accounting, *attempt_ids,
                                         op_class="monitor", mutation=False, rpc=rpc)
        if res is None:
            return ObserveResult(reachable=True, deferred=True, retry_after=retry)
        if bool((res.envelope or {}).get("stdout_truncated")):
            return ObserveResult(reachable=False, reason="output_truncated")
        if res.outcome not in ("ok", "remote_failed") or "__END" not in res.stdout:
            return ObserveResult(reachable=False, reason=res.outcome)
        attempts = parse_observation(res.stdout, attempt_ids, accounting=accounting == "1")
        for aid, obs in attempts.items():
            if obs.remote_id:
                self._receipts[aid] = obs.remote_id     # cancel must target the exact id (§3.3)
        return ObserveResult(reachable=True, attempts=attempts)

    # ---- cancellation ----------------------------------------------------------------

    async def cancel(self, target: str, attempt_id: str, *, epoch: int) -> CancelResult:
        site = self._site(target)
        job_id = self._receipts.get(attempt_id, "")
        res, retry, body = await self._exec(target, CANCEL_SCRIPT, site.control_root, self.fleet_id,
                                            str(epoch), attempt_id, job_id,
                                            op_class="action", mutation=True, rpc=1 if job_id else 0)
        if res is None or body is None:
            return CancelResult(reachable=False, reason="budget_deferred" if res is None else res.outcome)
        if not body.get("tombstone"):
            return CancelResult(reachable=True, stopped=False, reason=str(body.get("reason", "cancel_rejected")))
        # scancel only acknowledges a request; stopped is confirmed by a later observation (§3.6).
        return CancelResult(reachable=True, stopped=False, reason="tombstone written; awaiting scheduler evidence")

    async def release(self, target: str, attempt_id: str) -> ReleaseResult:
        # Nothing remote is held once the scheduler says the allocation is over.
        self._receipts.pop(attempt_id, None)
        return ReleaseResult(released=True)

    async def release_cache_pin(self, target: str, attempt_id: str, *, epoch: int) -> bool:
        """Write the fenced cache unpin fact after the controller approves finalization."""
        site = self._site(target)
        res, _retry, body = await self._exec(target, CACHE_PIN_RELEASE_SCRIPT, site.control_root,
                                             self.fleet_id, str(epoch), attempt_id,
                                             op_class="action", mutation=True, rpc=1)
        return bool(res is not None and body and body.get("ok")
                    and (body.get("released") or body.get("reason") == "no_bundle"))

    async def cache_gc(self, target: str, *, epoch: int, purge: str | None = None) -> dict[str, Any] | None:
        """Inspect or purge one reviewed Slurm cache digest through fleetctl admin."""
        site = self._site(target)
        mode = "purge" if purge is not None else "inspect"
        res, _retry, body = await self._exec(target, CACHE_GC_SCRIPT, site.control_root, self.fleet_id,
                                             str(epoch), mode, purge or "",
                                             op_class="action", mutation=purge is not None, rpc=0)
        return body if res is not None and body else None


    async def read_logs(self, target: str, attempt_id: str, requests: dict[str, int], *, remote_id: str | None,
                        max_bytes: int) -> dict[str, LogChunk]:
        import base64
        if not remote_id:
            return {st: LogChunk(st, off) for st, off in requests.items()}   # nothing to read yet
        site = self._site(target)
        offsets = {st: int(requests.get(st, -1)) for st in ("stdout", "stderr")}
        res, retry, body = await self._exec(target, LOGS_SCRIPT, site.control_root, attempt_id, remote_id,
                                            str(offsets["stdout"]), str(offsets["stderr"]), str(max_bytes),
                                            op_class="monitor", mutation=False, rpc=1)
        if res is None:
            return {st: LogChunk(st, off, deferred=True) for st, off in requests.items()}
        if body is None:
            return {st: LogChunk(st, off, reachable=False) for st, off in requests.items()}
        out = {}
        for st, off in requests.items():
            part = body.get(st) or {}
            size = part.get("size")
            out[st] = LogChunk(st, off, base64.b64decode(part.get("data") or ""),
                               remote_size=int(size) if isinstance(size, int) or str(size).isdigit() else None)
        return out


    # ---- artifacts (§6.4) ------------------------------------------------------------------

    async def collect_stage(self, target: str, attempt_id: str, paths: list[str], *, workdir_hint: str | None,
                            max_files: int, max_bytes: int) -> CollectManifest:
        site = self._site(target)
        granted, permit_id, _retry = await self._permit(target, "action", 1)
        if not granted:
            return CollectManifest(ok=False, deferred=True)
        workdir = f"{site.control_root}/attempts/{attempt_id}/code/{workdir_hint or '.'}"
        res = await self.transport.exec(site.fleetctl_target or target,
                                        bash_argv(COLLECT_STAGE_SCRIPT, site.control_root, attempt_id, workdir,
                                                  str(max_files), str(max_bytes), *paths),
                                        timeout=STAGE_TIMEOUT_S, mutation=True, admin=True, op_class="action",
                                        permit=permit_id, expected_role="login")
        if res.outcome not in ("ok", "remote_failed"):
            return CollectManifest(ok=False, reachable=False)
        return parse_collect_manifest(res.stdout)

    async def collect_pull(self, target: str, attempt_id: str, local: Path) -> tuple[bool, str | None]:
        site = self._site(target)
        granted, permit_id, _retry = await self._permit(target, "transfer", 6,
                                                        {"rpc": 6, "sessions": 2,
                                                         "bytes": self.collect_pull_budget_bytes})
        if not granted:
            return False, "budget_deferred"
        res = await self.transport.sync_pull(site.fleetctl_target or target,
                                             f"{site.control_root}/attempts/{attempt_id}/outbox", local,
                                             timeout=STAGE_TIMEOUT_S, admin=True, permit=permit_id,
                                             budget_bytes=self.collect_pull_budget_bytes, expected_role="login")
        return res.ok, None if res.ok else res.outcome

    async def collect_clean(self, target: str, attempt_id: str) -> bool:
        site = self._site(target)
        res, _retry, body = await self._exec(target, COLLECT_CLEAN_SCRIPT, site.control_root, attempt_id,
                                             op_class="action", mutation=True, rpc=1)
        return bool(body and body.get("cleaned"))


def parse_collect_manifest(text: str) -> CollectManifest:
    """The staging script's framed records; anything unframed is not a manifest."""
    import base64
    lines = text.splitlines()
    if lines and lines[0].startswith("E "):
        return CollectManifest(ok=False, error=lines[0].split()[1])
    if len(lines) < 2 or lines[0] not in ("S", "A") or lines[-1] != "D":
        return CollectManifest(ok=False, reachable=False)
    files, missing, refused = [], [], []
    for line in lines[1:-1]:
        parts = line.split(" ")
        try:
            if parts[0] == "F":
                files.append({"slot": parts[1], "size": int(parts[2]),
                              "relpath": base64.b64decode(parts[3]).decode("utf-8", "surrogateescape")})
            elif parts[0] == "M":
                missing.append(base64.b64decode(parts[1]).decode("utf-8", "surrogateescape"))
            elif parts[0] == "R":
                refused.append({"path": base64.b64decode(parts[1]).decode("utf-8", "surrogateescape"),
                                "reason": parts[2]})
        except (IndexError, ValueError):
            return CollectManifest(ok=False, reachable=False)
    return CollectManifest(ok=True, files=files, missing=missing, refused=refused)


def parse_observation(text: str, attempt_ids: list[str], *, accounting: bool) -> dict[str, AttemptObservation]:
    """Turn one observation session's output into per-attempt evidence.

    A failed or partial query is never read as "empty" (§3.6): if squeue's own
    exit status isn't 0, nothing it did or didn't list counts as absence.
    """
    per: dict[str, dict[str, str]] = {}
    current = None
    section = "attempts"
    squeue_rows: dict[str, list[str]] = {}
    name_rows: list[list[str]] = []
    sacct_rows: dict[str, list[str]] = {}
    squeue_ok = False
    squeue_invalid_id = False
    for line in text.splitlines():
        if line.startswith("__ATT "):
            current = line[6:].strip()
            per.setdefault(current, {})
            continue
        if line == "__SQUEUE_BEGIN":
            section = "squeue"
            continue
        if line == "__NAMES_BEGIN":
            section = "names"
            continue
        if line == "__SACCT_BEGIN":
            section = "sacct"
            continue
        if line == "__ATTEMPTS_BEGIN":
            section = "attempts"
            continue
        if line.startswith("__SQUEUE_RC "):
            squeue_ok = line.split()[1] == "0"
            continue
        if line.startswith("__SACCT_RC") or line == "__END":
            continue
        if section == "attempts" and current and line.startswith("__"):
            key, _, value = line[2:].partition(" ")
            per[current][key] = value.strip()
        elif section == "squeue" and "|" in line:
            cols = line.split("|")
            squeue_rows[cols[0].split("_")[0]] = cols
        elif section == "squeue" and "Invalid job id specified" in line:
            squeue_invalid_id = True
        elif section == "names" and "|" in line:
            name_rows.append(line.split("|"))
        elif section == "sacct" and "|" in line:
            cols = line.split("|")
            sacct_rows[cols[0].split(".")[0]] = cols
    out: dict[str, AttemptObservation] = {}
    for aid in attempt_ids:
        info = per.get(aid, {})
        receipt = (info.get("RECEIPT") or "").split(";", 1)[0]
        entered = info.get("ENTERED") == "1"
        result = None
        if info.get("RESULT"):
            try:
                result = json.loads(info["RESULT"])
            except json.JSONDecodeError:
                result = None
        if not receipt:
            if info.get("CLAIM") != "1":
                out[aid] = AttemptObservation(aid, "staged" if info.get("STAGED") == "1" else "absent")
                continue
            # Claimed, no receipt: resolve by exact job name, never by absence.
            name = f"fq-{aid}"
            # squeue rows: %i|%T|%r|%N|%j (name last); sacct rows: JobID|JobName|State|ExitCode.
            ids = sorted({r[0].split("_")[0].split(".")[0] for r in name_rows
                          if (len(r) == 5 and r[4] == name) or (len(r) == 4 and r[1] == name)})
            if len(ids) == 1:
                receipt = ids[0]
                # There is no receipt file, so `squeue -j` never listed it: the name
                # rows (same formats) are this session's scheduler evidence.
                for r in name_rows:
                    rid = r[0].split("_")[0].split(".")[0]
                    if rid == receipt and len(r) == 5:
                        squeue_rows.setdefault(receipt, r)
                    elif rid == receipt and len(r) == 4:
                        sacct_rows.setdefault(receipt, r)
            elif len(ids) > 1:
                out[aid] = AttemptObservation(aid, "unknown", evidence={"conflict": "multiple jobs carry this name",
                                                                        "ids": ids})
                continue
            else:
                out[aid] = AttemptObservation(aid, "unknown", evidence={"reason": "claim without receipt; no job found yet"})
                continue
        row = squeue_rows.get(receipt)
        evidence: dict[str, Any] = {"slurm_job_id": receipt, "result": result, "squeue": row}
        if row is not None:
            slurm_state = row[1]
            if slurm_state in PENDING_STATES:
                reason = row[2] if len(row) > 2 else None
                evidence.update(reason=reason, blocking=reason in BLOCKING_REASONS)
                out[aid] = AttemptObservation(aid, "pending", remote_id=receipt, evidence=evidence)
                continue
            if slurm_state in RUNNING_STATES:
                out[aid] = AttemptObservation(aid, "running", payload_entered=entered or bool(info.get("STARTED")),
                                              remote_id=receipt, evidence=evidence)
                continue
            terminal = slurm_state.split()[0]
            if terminal not in TERMINAL_MAP:
                out[aid] = AttemptObservation(aid, "unknown", remote_id=receipt,
                                              evidence={**evidence, "unrecognized_slurm_state": slurm_state})
                continue
        else:
            terminal = None
            acct = sacct_rows.get(receipt)
            if acct is not None:
                terminal = acct[2].split()[0]
                evidence["sacct"] = acct
                if terminal not in TERMINAL_MAP:
                    out[aid] = AttemptObservation(aid, "unknown", remote_id=receipt,
                                                  evidence={**evidence, "unrecognized_slurm_state": acct[2]})
                    continue
            elif result is not None and (squeue_ok or squeue_invalid_id):
                # Gone from squeue (MinJobAge) with no accounting to ask, but the batch
                # script's last act was publishing the payload's result.
                evidence["evidence"] = "result_file"
            elif accounting or not squeue_ok:
                # Accounting may lag, or the query failed: wait, don't guess.
                out[aid] = AttemptObservation(aid, "unknown", remote_id=receipt, evidence=evidence)
                continue
        outcome = TERMINAL_MAP.get(terminal or "", None)
        code = result.get("exit_code") if isinstance(result, dict) else None
        if outcome in (None, "COMPLETED", "FAILED"):
            # The scheduler says the allocation ended normally; the payload's own
            # rc decides success. Scheduler OOM/TIMEOUT/CANCEL/NODE_FAIL outrank it.
            if code is not None:
                outcome = "COMPLETED" if code == 0 else "FAILED"
            elif not entered and info.get("CANCEL") == "1":
                # The tombstone beat the start: the batch script ran nothing and exited 0.
                outcome = "CANCELLED"
            else:
                # No result means the payload never reported: a clean scheduler exit is
                # the batch script's (e.g. a requeued replay that skipped the payload),
                # not the payload's. Never call that a success.
                outcome = "UNKNOWN_EXIT"
        out[aid] = AttemptObservation(aid, "stopped", payload_entered=entered, outcome=outcome,
                                      exit_code=code, cgroup_empty=True, remote_id=receipt, evidence=evidence)
    return out
