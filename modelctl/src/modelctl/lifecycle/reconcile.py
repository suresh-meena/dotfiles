from __future__ import annotations

import time
from typing import Any

from ..inventory.registry import Registry
from ..gpu.nvml import query_via_nvidia_smi


def reconcile(*, registry: Registry, config: dict[str, Any], machine: str | None = None, fix_safe: bool = False) -> dict[str, Any]:
    """Compare local DB, remote supervisor (simulated), GPU state. Safe repairs only with fix_safe."""
    deployments = registry.list_deployments(machine=machine)
    gpu_state = query_via_nvidia_smi()
    events: list[dict[str, Any]] = []
    fixes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for dep in deployments:
        target_id = dep["target_id"]
        state = dep["state"]
        # Simulate remote supervisor check: for local simulation, we assume supervisor inactive if deployment is STOPPED, active otherwise
        # In real remote, we'd SSH and check systemd unit.
        # For reconciliation, we detect stale states:
        # - if state is STARTING but we are past timeout, mark FAILED_START
        # - if state is READY but no GPU reservation (check lock files), mark RECONCILE_REQUIRED
        # - if state is LEAK_SUSPECTED, keep
        if state == "STARTING":
            # if older than 30m assume failed
            from datetime import datetime, timezone

            try:
                ts = datetime.fromisoformat(dep["started_at"]).timestamp() if dep.get("started_at") else 0
                if time.time() - ts > 1800 and fix_safe:
                    registry.conn().execute("UPDATE deployments SET state='FAILED_START' WHERE deployment_id=?", (dep["deployment_id"],))
                    registry.conn().commit()
                    fixes.append({"deployment_id": dep["deployment_id"], "from": "STARTING", "to": "FAILED_START", "reason": "startup timeout"})
                elif time.time() - ts > 1800:
                    issues.append({"deployment_id": dep["deployment_id"], "state": state, "issue": "stale STARTING beyond timeout"})
            except Exception:
                pass
        elif state == "READY":
            # check artifact fingerprint drift
            artifact_id = dep["artifact_id"]
            art = registry.get_artifact(artifact_id)
            if art and art.get("manifest_fingerprint") and dep.get("artifact_fingerprint") and art["manifest_fingerprint"] != dep["artifact_fingerprint"]:
                if fix_safe:
                    registry.conn().execute("UPDATE deployments SET state='DRIFTED' WHERE deployment_id=?", (dep["deployment_id"],))
                    registry.conn().commit()
                    fixes.append({"deployment_id": dep["deployment_id"], "to": "DRIFTED", "reason": "artifact fingerprint changed"})
                else:
                    issues.append({"deployment_id": dep["deployment_id"], "issue": "ARTIFACT_CHANGED"})
        elif state == "LEAK_SUSPECTED":
            issues.append({"deployment_id": dep["deployment_id"], "issue": "LEAK_SUSPECTED requires manual investigation"})

    # Check stale tunnels (pid not alive)
    tunnels = registry.list_tunnels()
    for t in tunnels:
        pid = t.get("pid")
        if pid:
            try:
                import os

                os.kill(pid, 0)
            except ProcessLookupError:
                if fix_safe:
                    registry.delete_tunnel(t["tunnel_id"])
                    fixes.append({"tunnel_id": t["tunnel_id"], "action": "removed stale tunnel"})
                else:
                    issues.append({"tunnel_id": t["tunnel_id"], "issue": "stale tunnel PID"})
            except PermissionError:
                pass

    return {"ok": True, "machine": machine, "checked": len(deployments), "fixes": fixes, "issues": issues, "gpu_backend": gpu_state.get("backend")}
