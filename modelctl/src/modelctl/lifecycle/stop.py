from __future__ import annotations

import uuid
from typing import Any

from ..errors import ModelctlError
from ..inventory.registry import Registry, utc_now
from ..events import EventLog
from ..gpu.reservations import release_for_owner
from ..runtime import pid_alive
from .procs import terminate_verified


def _gpu_uuids(machine_id: str, gpus: list[int]) -> list[str]:
    return [f"{machine_id}-gpu-{i}" for i in gpus]


def stop_target(*, registry: Registry, config: dict[str, Any], target_id: str, force: bool = False, trace_id: str | None = None) -> dict[str, Any]:
    trace_id = trace_id or uuid.uuid4().hex[:12]
    events = EventLog(registry)
    events.emit("STOP_REQUESTED", target_id=target_id, result="requested", trace_id=trace_id)

    from ..config.resolver import resolve_target, target_digest

    resolved = None
    try:
        resolved = resolve_target(config, target_id)
        machine_id = resolved["machine"]
        digest = target_digest(resolved)
        gpu_uuids = _gpu_uuids(machine_id, resolved.get("gpus", []))
        graceful = float(resolved.get("graceful_stop_timeout_s", 30))
        kill_t = float(resolved.get("kill_timeout_s", 15))
    except ModelctlError as e:
        if e.code != "E_TARGET_NOT_FOUND":
            raise
        dep = registry.deployment_for_target(target_id)
        if not dep:
            raise
        machine_id = dep["machine_id"]
        digest = dep["config_digest"]
        gpu_uuids = []
        graceful = 30.0
        kill_t = 15.0

    dep = registry.deployment_for_target(target_id)
    if not dep or dep["state"] == "STOPPED":
        return {"ok": True, "target": target_id, "state": "STOPPED", "idempotent": True, "trace_id": trace_id, "simulation": True, "backend": "local-simulation"}
    if dep["state"] == "LEAK_SUSPECTED" and not force:
        raise ModelctlError(code="E_LEAK_SUSPECTED", message=f"deployment {dep['deployment_id']} is LEAK_SUSPECTED; use --force to re-attempt termination or investigate via doctor", target=target_id)

    dep_id = dep["deployment_id"]
    # 1. mark DRAINING (stop accepting new connections is a no-op for the fake runtime)
    registry.upsert_deployment(dep_id, target_id, machine_id, digest, dep["artifact_id"], dep.get("artifact_fingerprint"), "DRAINING", dep["supervisor_unit"], server_pid=dep.get("server_pid"), port=dep.get("port"), lease_expires_at=dep.get("lease_expires_at"))

    # 2. terminate with mandatory verification (SIGTERM -> SIGKILL -> verify)
    from .remote import remote_lifecycle

    rl = remote_lifecycle(config, machine_id)
    if rl:
        # real machine: pid/port checks and termination happen over SSH
        if not rl.pid_alive(dep.get("server_pid")):
            port_free = dep.get("port") is None or not rl.port_busy(dep["port"])
            term = {
                "ok": port_free,
                "detail": "pid not alive (already stopped)" if port_free else "pid gone but port still occupied",
                "pid": dep.get("server_pid"),
                "group_gone": True,
                "port_free": port_free,
            }
        else:
            term = rl.terminate(dep, graceful_timeout_s=graceful, kill_timeout_s=kill_t)
    elif not pid_alive(dep.get("server_pid")):
        # process already gone; port must still be free to consider it verified
        from ..runtime import port_busy

        port_free = dep.get("port") is None or not port_busy("127.0.0.1", dep["port"])
        term = {
            "ok": port_free,
            "detail": "pid not alive (already stopped)" if port_free else "pid gone but port still occupied",
            "pid": dep.get("server_pid"),
            "group_gone": True,
            "port_free": port_free,
        }
    else:
        term = terminate_verified(dep, graceful_timeout_s=graceful, kill_timeout_s=kill_t)

    if not term["ok"]:
        _mark_leak(registry, events, dep_id, target_id, machine_id, digest, dep, trace_id)
        raise ModelctlError(code="E_LEAK_SUSPECTED", message=f"could not verify termination of {dep_id}: {term['detail']}; reservation retained", target=target_id, machine=machine_id)

    # 3. release GPU reservations owned by this deployment
    released = release_for_owner(gpu_uuids, dep_id)
    # 4. mark STOPPED
    now = utc_now()
    conn = registry.conn()
    conn.execute("UPDATE deployments SET state='STOPPED', stopped_at=? WHERE deployment_id=?", (now, dep_id))
    conn.commit()
    events.emit("SERVICE_STOPPED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="terminated", details=term, trace_id=trace_id)
    events.emit("GPU_RELEASE_VERIFIED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="ok", details={"released": released}, trace_id=trace_id)
    events.emit("DEPLOYMENT_STOPPED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="stopped", trace_id=trace_id)

    return {"ok": True, "target": target_id, "deployment_id": dep_id, "state": "STOPPED", "pid": dep.get("server_pid"), "trace_id": trace_id, "simulation": True, "backend": "local-simulation"}


def _mark_leak(registry: Registry, events: EventLog, dep_id: str, target_id: str, machine_id: str, digest: str, dep: dict[str, Any], trace_id: str) -> None:
    registry.upsert_deployment(dep_id, target_id, machine_id, digest, dep["artifact_id"], dep.get("artifact_fingerprint"), "LEAK_SUSPECTED", dep["supervisor_unit"], server_pid=dep.get("server_pid"), port=dep.get("port"), lease_expires_at=dep.get("lease_expires_at"))
    events.emit("LEAK_SUSPECTED", target_id=target_id, deployment_id=dep_id, machine_id=machine_id, result="leak", trace_id=trace_id)