"""The workstation executor: fq-node over fleetctl (§2.3–2.8).

Every node operation is one ``fleetctl exec`` of the installed fq-node shim,
which prints one JSON object. Mapping rules:

* the transport says nothing ran (refusal, route down before send) →
  ``NEVER_STARTED`` / unreachable, so it is safe to try elsewhere;
* the transport says it *may have run* (timeout or ssh 255 after the send,
  malformed reply) → ``UNKNOWN``, reconciled from node evidence and never
  replayed blind;
* fq-node's own typed answers (``started``, ``placement_refused``,
  ``never_started``) pass through as themselves.
"""

from __future__ import annotations

import json
import shutil
import secrets
import tempfile
from pathlib import Path
from typing import Any

from ..transport.fleetctl import Fleetctl, FleetctlResult
from ..onboard import ROOT_CHECK_SCRIPT
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

NODE_TIMEOUT_S = 90

# Runs the installed shim as itself (argv[0] is its path, so units still pin the
# release it resolves to). Bytecode writes are disabled until fq-node has checked
# the configured root; the launcher must not create paths under an untrusted root.
NODE_LAUNCHER = (
    "import importlib.machinery, importlib.util, sys\n"
    "path = sys.argv[1]\n"
    "sys.dont_write_bytecode = True\n"
    "sys.argv = [path, *sys.argv[2:]]\n"
    "loader = importlib.machinery.SourceFileLoader('fq_node', path)\n"
    "mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('fq_node', loader))\n"
    "sys.modules['fq_node'] = mod\n"
    "loader.exec_module(mod)\n"
    "sys.exit(mod.main())\n"
)
STAGE_TIMEOUT_S = 900


class BareExecutor:
    backend = "bare"
    # Log deltas ride on the status call (one fleetctl process per node per cycle, not three).
    logs_in_observe = True

    def __init__(self, transport: Fleetctl, cfg: Any) -> None:
        self.transport = transport
        self.nodes = {n.id: n for n in cfg.nodes if n.backend == "bare"}
        self._fenced_epochs: dict[str, int] = {}

    # ---- helpers -----------------------------------------------------------------

    def _node(self, target: str):
        node = self.nodes.get(target)
        if node is None or not node.control_root:
            raise KeyError(f"node {target} has no control_root configured")
        return node

    def _shim(self, target: str, *args: str) -> list[str]:
        node = self._node(target)
        root = node.control_root
        return [getattr(node, "node_python", None) or "python3", "-c", NODE_LAUNCHER,
                f"{root}/bin/fq-node", "--root", root, *args]

    async def _call(self, target: str, *args: str, mutation: bool, timeout: float = NODE_TIMEOUT_S
                    ) -> tuple[FleetctlResult, dict[str, Any] | None]:
        node = self._node(target)
        res = await self.transport.exec(node.fleetctl_target or target, self._shim(target, *args),
                                        timeout=timeout, mutation=mutation, expected_role="workstation")
        body = res.payload() if res.outcome in ("ok", "remote_failed") else None
        if body is not None and body.get("schema") != "fq-node/v1":
            body = None
        return res, body

    def _manifest(self, ctx: AttemptContext) -> dict[str, Any]:
        node = self._node(ctx.target)
        spec = ctx.spec
        manifest: dict[str, Any] = {
            "attempt_id": ctx.attempt_id, "fleet_id": ctx.fleet_id, "job_id": ctx.job_id, "epoch": ctx.epoch,
            "spec_digest": ctx.spec_digest, "command": spec["command"], "env": spec["env"],
            "setup": spec.get("setup"), "gpus": ctx.gpus,
            "resources": {"mem_mb": ctx.resources["mem_mb"], "cpus": ctx.resources["cpus"],
                          "time_s": ctx.resources["time_s"]},
            "kill_grace_s": spec["control"]["kill_grace_s"],
            "attempt_n": ctx.n,
            "warn": ({"signal": spec["control"]["warn_signal"], "before_s": spec["control"]["warn_before_s"]}
                     if spec["control"].get("warn_signal") else None),
            "gpu_policy": node.gpu_profile or {},
            "mem_headroom_mb": int((node.capacity or {}).get("mem_headroom_mb", 2048)),
            "needs": spec.get("needs", []), "needs_rw": spec.get("needs_rw", []),
            "array_index": ctx.array_index,
        }
        if "in_place" in spec["workdir"]:
            manifest["in_place"] = spec["workdir"]["in_place"]
        else:
            manifest["bundle_digest"] = ctx.bundle_digest
            manifest["subdir"] = spec["workdir"].get("subdir", ".")
        return manifest

    # ---- protocol --------------------------------------------------------------------

    async def fence(self, target: str, *, epoch: int, fleet_id: str) -> FenceResult:
        self.fleet_id = fleet_id
        res, body = await self._call(target, "fence", "--fleet-id", fleet_id, "--epoch", str(epoch), mutation=True)
        if body is None:
            return FenceResult(reachable=False, reason=res.outcome)
        if not body.get("ok"):
            return FenceResult(reachable=True, accepted=False, reason=body.get("error", {}).get("code"))
        seen = int(body.get("highest_epoch_seen", 0))
        if body.get("accepted"):
            # The reply reports the previous highest epoch on acceptance.
            self._fenced_epochs[target] = epoch
        else:
            self._fenced_epochs.pop(target, None)
        return FenceResult(reachable=True, accepted=bool(body.get("accepted")),
                           highest_epoch_seen=seen,
                           remote_attempts=list(body.get("attempts") or []))

    async def stage(self, ctx: AttemptContext) -> StageResult:
        node = self._node(ctx.target)
        target, root = node.fleetctl_target or ctx.target, node.control_root
        checked = await self.transport.exec(target, ["sh", "-c", ROOT_CHECK_SCRIPT, "fleetq", root,
                                                      getattr(node, "node_python", None) or "python3"],
                                             timeout=NODE_TIMEOUT_S, mutation=False, expected_role="workstation")
        check_body = checked.payload() if checked.outcome in ("ok", "remote_failed") else None
        if not isinstance(check_body, dict) or not check_body.get("ok"):
            if check_body is not None and check_body.get("error") == "unsafe_root":
                return StageResult(ok=False, retryable=False, reason="unsafe_control_root")
            return StageResult(ok=False, retryable=True, reason="control_root_check_unavailable")
        tmp = Path(tempfile.mkdtemp(prefix="fq-stage-"))
        try:
            (tmp / "manifest.json").write_text(json.dumps(self._manifest(ctx), sort_keys=True))
            if ctx.bundle_path is not None:
                shutil.copyfile(ctx.bundle_path, tmp / "bundle.tar.gz")
            stage_id = f"{ctx.attempt_id}-{ctx.epoch}-{secrets.token_hex(8)}"
            pushed = await self.transport.sync_push(target, Path(f"{tmp}/"),
                                                    f"{root}/inbox/.stage-{stage_id}/",
                                                    timeout=STAGE_TIMEOUT_S, expected_role="workstation")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not pushed.ok:
            return StageResult(ok=False, retryable=True, reason=f"push {pushed.outcome}")
        # Publication is a short node-locked operation. The potentially slow
        # rsync above targets a unique private path and cannot overwrite inbox/<attempt>.
        res, body = await self._call(ctx.target, "publish-stage", "--fleet-id", ctx.fleet_id,
                                     "--epoch", str(ctx.epoch), "--stage-id", stage_id, mutation=True)
        if body is None or not body.get("ok"):
            code = body.get("error", {}).get("code") if body else res.outcome
            return StageResult(ok=False, retryable=code not in ("stale_epoch", "future_epoch"), reason=code)
        res, body = await self._call(ctx.target, "prepare", "--fleet-id", ctx.fleet_id,
                                     "--epoch", str(ctx.epoch), ctx.attempt_id, mutation=False)
        if body is None:
            return StageResult(ok=False, retryable=True, reason=f"prepare {res.outcome}")
        if not body.get("ok"):
            code = body.get("error", {}).get("code", "prepare_failed")
            permanent = code in ("not_enrolled", "fleet_mismatch", "root_mismatch", "bundle_invalid")
            return StageResult(ok=False, retryable=not permanent, reason=code)
        return StageResult(ok=True)

    async def launch(self, ctx: AttemptContext) -> LaunchResult:
        res, body = await self._call(ctx.target, "launch", "--fleet-id", ctx.fleet_id, "--epoch", str(ctx.epoch),
                                     ctx.attempt_id, mutation=True)
        if body is None:
            # The command ran (ok/remote_failed) or may have, yet said nothing we can read:
            # the start may have happened. fleetctl's may_have_executed is false on a clean
            # exit 0, which proves the wrapper ran, not that it did nothing.
            if res.may_have_executed or res.outcome in ("ok", "remote_failed"):
                return LaunchResult(LaunchKind.UNKNOWN, reason=f"unreadable launch reply ({res.outcome})")
            return LaunchResult(LaunchKind.NEVER_STARTED, reason=f"transport {res.outcome}")
        if not body.get("ok"):
            code = body.get("error", {}).get("code", "launch_error")
            # Shim errors are raised before the start request is written.
            return LaunchResult(LaunchKind.NEVER_STARTED, reason=code,
                                permanent=code in ("not_enrolled", "fleet_mismatch", "root_mismatch"))
        result = body.get("result")
        if result == "started":
            return LaunchResult(LaunchKind.STARTED, remote_id=body.get("unit"), boot_id=body.get("boot_id"))
        if result == "placement_refused":
            return LaunchResult(LaunchKind.PLACEMENT_REFUSED, reason=body.get("reason"),
                                cooldown_gpus=list(body.get("gpus") or []))
        if result == "never_started":
            return LaunchResult(LaunchKind.NEVER_STARTED, reason=body.get("reason"))
        return LaunchResult(LaunchKind.UNKNOWN, reason=body.get("reason") or "unknown")

    @staticmethod
    def _log_args(log_requests: dict[str, dict[str, int]] | None) -> list[str]:
        return [a for aid, streams in (log_requests or {}).items() for st, off in streams.items()
                for a in ("--log", f"{aid}:{st}:{int(off)}")]

    @staticmethod
    def _log_chunks(body: dict[str, Any]) -> dict[str, dict[str, LogChunk]]:
        import base64
        out: dict[str, dict[str, LogChunk]] = {}
        for aid, streams in (body.get("logs") or {}).items():
            for st, c in streams.items():
                size = c.get("size")
                out.setdefault(aid, {})[st] = LogChunk(st, int(c.get("offset") or 0),
                                                       base64.b64decode(c.get("data_b64") or ""),
                                                       remote_size=int(size) if size is not None else None)
        return out

    async def observe(self, target: str, attempt_ids: list[str],
                      log_requests: dict[str, dict[str, int]] | None = None) -> ObserveResult:
        node_cfg = self.nodes.get(target)
        if node_cfg is None:
            return ObserveResult(reachable=False, reason="not configured")
        res, body = await self._call(target, "status", "--fleet-id", self._fleet_id_hint(target), *attempt_ids,
                                     *self._log_args(log_requests), mutation=False)
        if body is None or not body.get("ok"):
            return ObserveResult(reachable=False, reason=res.outcome if body is None else body.get("error", {}).get("code"))
        out = {}
        for aid, st in (body.get("attempts") or {}).items():
            out[aid] = AttemptObservation(
                attempt_id=aid, state=st.get("state", "unknown"), payload_entered=bool(st.get("payload_entered")),
                outcome=st.get("outcome"), exit_code=st.get("exit_code"), exit_signal=st.get("exit_signal"),
                cgroup_empty=st.get("cgroup_empty"), boot_id=st.get("boot_id"),
                boot_changed=bool(st.get("boot_changed")), evidence=st.get("evidence") or {})
        return ObserveResult(reachable=True, attempts=out, boot_id=body.get("boot_id"),
                             node_facts={"mem_available_mib": body.get("mem_available_mib"),
                                         "gpu_processes": body.get("gpu_processes")},
                             logs=self._log_chunks(body))

    async def cancel(self, target: str, attempt_id: str, *, epoch: int) -> CancelResult:
        res, body = await self._call(target, "cancel", "--fleet-id", self._fleet_id_hint(target), "--epoch",
                                     str(epoch), attempt_id, mutation=True)
        if body is None or not body.get("ok"):
            return CancelResult(reachable=False, reason=res.outcome)
        return CancelResult(reachable=True, stopped=bool(body.get("stopped")),
                            never_started=bool(body.get("never_started")), reason=body.get("reason"))

    async def release(self, target: str, attempt_id: str) -> ReleaseResult:
        epoch = self._fenced_epochs.get(target)
        if epoch is None:
            return ReleaseResult(released=False, reason="controller_epoch_unknown")
        res, body = await self._call(target, "release", "--fleet-id", self._fleet_id_hint(target),
                                     "--epoch", str(epoch), attempt_id, mutation=True)
        if body is None or not body.get("ok"):
            return ReleaseResult(released=False, reason=res.outcome)
        return ReleaseResult(released=bool(body.get("released")), reason=body.get("reason"))

    async def release_cache_pin(self, target: str, attempt_id: str, *, epoch: int) -> bool:
        """Release a bundle cache pin after the controller has finalized artifacts."""
        res, body = await self._call(target, "cache-release", "--fleet-id", self._fleet_id_hint(target),
                                     "--epoch", str(epoch), attempt_id, mutation=True)
        return bool(body and body.get("ok") and body.get("released"))

    # The fleet id is carried in manifests/fences; commands that address existing
    # attempts pass it so the shim can verify enrollment.
    fleet_id: str = ""

    def _fleet_id_hint(self, target: str) -> str:
        return self.fleet_id

    async def read_logs(self, target: str, attempt_id: str, requests: dict[str, int], *, remote_id: str | None,
                        max_bytes: int) -> dict[str, LogChunk]:
        """Both streams in one status call (a finished attempt's final tail)."""
        res, body = await self._call(target, "status", "--fleet-id", self._fleet_id_hint(target),
                                     *self._log_args({attempt_id: requests}),
                                     "--log-budget", str(max_bytes * max(1, len(requests))), mutation=False)
        if body is None:
            return {st: LogChunk(st, off, reachable=False) for st, off in requests.items()}
        if not body.get("ok"):
            return {st: LogChunk(st, off) for st, off in requests.items()}   # reachable, source unreadable
        chunks = self._log_chunks(body).get(attempt_id, {})
        return {st: chunks.get(st, LogChunk(st, off)) for st, off in requests.items()}

    # ---- artifacts (§6.4) -------------------------------------------------------------

    async def collect_stage(self, target: str, attempt_id: str, paths: list[str], *, workdir_hint: str | None,
                            max_files: int, max_bytes: int) -> CollectManifest:
        epoch = self._fenced_epochs.get(target)
        if epoch is None:
            return CollectManifest(ok=False, error="controller_epoch_unknown")
        args = ["collect-stage", "--fleet-id", self._fleet_id_hint(target), "--epoch", str(epoch), attempt_id,
                "--max-files", str(max_files), "--max-bytes", str(max_bytes)]
        for path in paths:
            args += ["--path", path]
        res, body = await self._call(target, *args, mutation=True, timeout=STAGE_TIMEOUT_S)
        if body is None:
            return CollectManifest(ok=False, reachable=False, error=None)
        if not body.get("ok"):
            return CollectManifest(ok=False, error=body.get("error", {}).get("code", "collect_failed"))
        return CollectManifest(ok=True, files=list(body.get("files") or []), missing=list(body.get("missing") or []),
                               refused=list(body.get("refused") or []))

    async def collect_pull(self, target: str, attempt_id: str, local: Path) -> tuple[bool, str | None]:
        node = self._node(target)
        res = await self.transport.sync_pull(node.fleetctl_target or target,
                                             f"{node.control_root}/attempts/{attempt_id}/outbox", local,
                                             timeout=STAGE_TIMEOUT_S, expected_role="workstation")
        return res.ok, None if res.ok else res.outcome

    async def collect_clean(self, target: str, attempt_id: str) -> bool:
        epoch = self._fenced_epochs.get(target)
        if epoch is None:
            return False
        res, body = await self._call(target, "collect-clean", "--fleet-id", self._fleet_id_hint(target),
                                     "--epoch", str(epoch), attempt_id,
                                     mutation=True)
        return bool(body and body.get("ok"))
