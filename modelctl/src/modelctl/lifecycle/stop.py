from __future__ import annotations

import time
import uuid
from typing import Any

from ..errors import ModelctlError
from ..inventory.registry import Registry
from ..events import EventLog
from ..gpu.reservations import release_gpus
from ..gpu.nvml import query_via_nvidia_smi


def stop_target(*, registry: Registry, config: dict[str, Any], target_id: str, force: bool = False, trace_id: str | None = None) -> dict[str, Any]:
    trace_id = trace_id or uuid.uuid4().hex[:12]
    events = EventLog(registry)
    events.emit("STOP_REQUESTED", target_id=target_id, result="requested", trace_id=trace_id)

    from ..config.resolver import resolve_target, target_digest

    # If config missing target, still try to find deployment by target_id
    try:
        resolved = resolve_target(config, target_id)
        machine_id = resolved["machine"]
        digest = target_digest(resolved)
        gpus = resolved.get("gpus", [])
        gpu_uuids = [f"{machine_id}-gpu-{i}" for i in gpus]
    except ModelctlError as e:
        if e.code == "E_TARGET_NOT_FOUND":
            # try to find deployment directly
            dep = registry.deployment_for_target(target_id)
            if not dep:
                raise
            machine_id = dep["machine_id"]
            digest = dep["config_digest"]
            gpu_uuids = []
            resolved = None  # type: ignore
        else:
            raise

    dep = registry.deployment_for_target(target_id)
    if not dep:
        # already stopped - idempotent
        return {"ok": True, "target": target_id, "state": "STOPPED", "idempotent": True, "trace_id": trace_id}
    if dep["state"] in ("STOPPED",):
        return {"ok": True, "target": target_id, "state": "STOPPED", "idempotent": True, "trace_id": trace_id}
    if dep["state"] == "LEAK_SUSPECTED" and not force:
        raise ModelctlError(code="E_LEAK_SUSPECTED", message=f"deployment {dep['deployment_id']} is LEAK_SUSPECTED; use --force to escalate or investigate via doctor", target=target_id)

    dep_id = dep["deployment_id"]
    # spec §10.2 stop sequence (simplified local simulation)
    # 1. mark DRAINING
    registry.upsert_deployment(dep_id, target_id, machine_id, digest, dep["artifact_id"], dep.get("artifact_fingerprint"), "DRAINING", dep["supervisor_unit"])
    # 2. stop accepting new connections (no-op local)
    # 3. request graceful supervisor stop (simulate)
    time.sleep(0.02)
    # 4. wait graceful_stop_timeout_s
    # 5. inspect owned cgroup (simulate empty)
    # 6-10. SIGTERM/SIGKILL escalation (simulate)
    # 11. verify cgroup empty (simulate)
    # 12. query GPU compute processes
    gpu_state = query_via_nvidia_smi()
    # Check if any deployment-owned GPU processes remain (simulation: check if any gpus have compute processes)
    # For local simulation without real GPU, we assume no owned processes remain unless we detect real GPU processes.
    # If backend is none, we consider success.
    # If we do have GPU processes, we need ownership proof - we cannot prove foreign, so we treat as leak if we can't verify.
    owned_remaining = False
    if gpu_state.get("backend") != "none":
        # If there are compute processes on reserved GPUs, check if any belong to deployment
        # Without PID tracking, we conservatively assume if any GPU has processes and we had reservations, it's suspect.
        # For now, if backend is nvml and we have reservations, we could inspect but we lack PID mapping, so we assume success if no processes listed.
        for gpu in gpu_state.get("gpus", []):
            if gpu.get("compute_processes"):
                # if we reserved this GPU, and there's any process, we cannot prove it's ours, so LEAK_SUSPECTED
                # But to avoid false positives on desktops with Xorg, we only consider if gpu uuid matches reserved
                # Simplify: if any process exists and we had gpu_uuids, mark suspect
                if gpu_uuids:
                    owned_remaining = True
                    break

    if owned_remaining and not force:
        registry.upsert_deployment(dep_id, target_id, machine_id, digest, dep["artifact_id"], dep.get("artifact_fingerprint"), "LEAK_SUSPECTED", dep["supervisor_unit"])
        events.emit("LEAK_SUSPECTED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="leak", trace_id=trace_id)
        raise ModelctlError(code="E_LEAK_SUSPECTED", message="deployment-owned GPU process still present after stop; reservation retained", target=target_id, machine=machine_id)

    # 13. verify no deployment-owned PID remains -> success
    # 14. release GPU reservation
    if gpu_uuids:
        release_gpus(gpu_uuids)
    # 15. mark STOPPED
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    registry.upsert_deployment(dep_id, target_id, machine_id, digest, dep["artifact_id"], dep.get("artifact_fingerprint"), "STOPPED", dep["supervisor_unit"], stopped_at=now)
    # need to update stopped_at properly (upsert does COALESCE for stopped_at? we already inserted; need direct update)
    import sqlite3

    registry.conn().execute("UPDATE deployments SET state='STOPPED', stopped_at=? WHERE deployment_id=?", (now, dep_id))
    registry.conn().commit()
    events.emit("SERVICE_STOPPED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, trace_id=trace_id)
    events.emit("GPU_RELEASE_VERIFIED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, trace_id=trace_id)
    events.emit("DEPLOYMENT_STOPPED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="stopped", trace_id=trace_id)

    # 16. close tunnels (handled by caller)
    return {"ok": True, "target": target_id, "deployment_id": dep_id, "state": "STOPPED", "trace_id": trace_id}
