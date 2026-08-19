from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..inventory.registry import Registry, utc_now
from ..gpu.reservations import cleanup_stale
from ..gpu.nvml import query_via_nvidia_smi
from .procs import deployment_live, lease_expired


def reconcile(*, registry: Registry, config: dict[str, Any], machine: str | None = None, fix_safe: bool = False) -> dict[str, Any]:
    """Compare local DB state against live processes, leases and GPU
    reservations. Safe repairs only with fix_safe."""
    deployments = registry.list_deployments(machine=machine)
    events: list[dict[str, Any]] = []
    fixes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for dep in deployments:
        dep_id = dep["deployment_id"]
        state = dep["state"]

        if state in ("READY", "STARTING", "DRAINING", "STOPPING"):
            live = deployment_live(dep)
            if not live and state == "READY":
                # live-state drift: recorded READY but no process/port
                if fix_safe:
                    conn = registry.conn()
                    conn.execute("UPDATE deployments SET state='STOPPED', stopped_at=? WHERE deployment_id=?", (utc_now(), dep_id))
                    conn.commit()
                    fixes.append({"deployment_id": dep_id, "from": state, "to": "STOPPED", "reason": "no live process"})
                else:
                    issues.append({"deployment_id": dep_id, "state": state, "issue": "recorded READY but no live process/port"})
            elif not live and state == "STARTING":
                if fix_safe:
                    conn = registry.conn()
                    conn.execute("UPDATE deployments SET state='FAILED_START' WHERE deployment_id=?", (dep_id,))
                    conn.commit()
                    fixes.append({"deployment_id": dep_id, "from": "STARTING", "to": "FAILED_START", "reason": "server process gone"})
                else:
                    issues.append({"deployment_id": dep_id, "state": state, "issue": "STARTING but server process not alive"})

        # lease expiry
        if state in ("READY", "STARTING") and lease_expired(dep):
            if fix_safe:
                if dep["target_id"] in config.get("targets", {}):
                    from .stop import stop_target

                    try:
                        stop_target(registry=registry, config=config, target_id=dep["target_id"], force=False)
                        fixes.append({"deployment_id": dep_id, "from": state, "to": "STOPPED", "reason": "lease expired"})
                    except Exception as e:
                        issues.append({"deployment_id": dep_id, "state": state, "issue": f"lease expired but stop failed: {str(e)[:200]}"})
                else:
                    issues.append({"deployment_id": dep_id, "state": state, "issue": "lease expired (no config to stop cleanly)"})
            else:
                issues.append({"deployment_id": dep_id, "state": state, "issue": "lease expired"})

        if state == "LEAK_SUSPECTED":
            issues.append({"deployment_id": dep_id, "issue": "LEAK_SUSPECTED requires manual investigation"})

    # stale tunnels (pid not alive)
    tunnels = registry.list_tunnels()
    for t in tunnels:
        pid = t.get("pid")
        if pid:
            from ..runtime import pid_alive

            if not pid_alive(pid):
                if fix_safe:
                    registry.delete_tunnel(t["tunnel_id"])
                    fixes.append({"tunnel_id": t["tunnel_id"], "action": "removed stale tunnel"})
                else:
                    issues.append({"tunnel_id": t["tunnel_id"], "issue": "stale tunnel PID"})

    # stale GPU reservations (dead owner or aged in-flight)
    if fix_safe:
        removed = cleanup_stale(max_age_s=300)
        for r in removed:
            fixes.append({"gpu_uuid": r["gpu_uuid"], "action": "removed stale reservation", "owner": r.get("owner")})

    gpu_state = query_via_nvidia_smi()
    return {"ok": True, "machine": machine, "checked": len(deployments), "fixes": fixes, "issues": issues, "gpu_backend": gpu_state.get("backend")}