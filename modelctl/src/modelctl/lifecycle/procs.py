from __future__ import annotations

import signal
import time
from datetime import datetime, timezone
from typing import Any

from ..inventory.registry import Registry
from ..runtime.vllm import RuntimeAdapter, kill_group, pid_alive, port_busy, process_group_gone
from .lease import parse_ttl


def compute_lease_expiry(ttl: str | None) -> str | None:
    """ISO-8601 expiry for a --ttl value, or None for persistent deployments."""
    if not ttl:
        return None
    seconds = parse_ttl(ttl)
    return datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat()


def lease_expired(dep: dict[str, Any]) -> bool:
    exp = dep.get("lease_expires_at")
    if not exp:
        return False
    try:
        ts = datetime.fromisoformat(exp).timestamp()
    except Exception:
        # unparseable lease => fail closed
        return True
    return time.time() > ts


def deployment_live(dep: dict[str, Any], *, require_port: bool = True) -> bool:
    """A deployment is live when its recorded server pid is alive and (when a
    port is known) something is listening on that port."""
    if not pid_alive(dep.get("server_pid")):
        return False
    port = dep.get("port")
    if port and require_port:
        return port_busy("127.0.0.1", port)
    return True


def wait_for_termination(pid: int | None, timeout_s: float, port: int | None = None) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if process_group_gone(pid):
            if port is None or not port_busy("127.0.0.1", port):
                return True
        time.sleep(0.25)
    return False


def terminate_verified(dep: dict[str, Any], *, graceful_timeout_s: float = 30.0, kill_timeout_s: float = 15.0) -> dict[str, Any]:
    """SIGTERM -> graceful wait -> SIGKILL -> verified process-group/port check.

    Returns {"ok", "detail", "pid", "group_gone", "port_free"}. Verification
    is mandatory; ok is True only when the group is proven gone and the port
    is free.
    """
    pid = dep.get("server_pid")
    port = dep.get("port")
    if not pid_alive(pid):
        return {
            "ok": True,
            "detail": "pid not alive (already stopped)",
            "pid": pid,
            "group_gone": True,
            "port_free": port is None or not port_busy("127.0.0.1", port),
        }
    kill_group(pid, signal.SIGTERM)
    if wait_for_termination(pid, graceful_timeout_s, port):
        return {"ok": True, "detail": f"terminated via SIGTERM (pgid {pid})", "pid": pid, "group_gone": True, "port_free": True}
    kill_group(pid, signal.SIGKILL)
    if wait_for_termination(pid, kill_timeout_s, port):
        return {"ok": True, "detail": f"terminated via SIGKILL (pgid {pid})", "pid": pid, "group_gone": True, "port_free": True}
    gone = process_group_gone(pid)
    free = port is None or not port_busy("127.0.0.1", port)
    detail = "process group still present after SIGKILL" if not gone else "port still occupied"
    return {"ok": gone and free, "detail": detail, "pid": pid, "group_gone": gone, "port_free": free}


def finish_pending_start(registry: Registry, dep: dict[str, Any], *, host: str, port: int, served_model: str, deadline_s: float, runtime: Any = None) -> tuple[bool, str]:
    """Wait for a STARTING deployment's server to become healthy and mark it READY."""
    if not pid_alive(dep.get("server_pid")):
        return False, "server pid not alive"
    adapter = runtime if runtime is not None else RuntimeAdapter()
    if adapter.wait_ready(host, port, served_model, deadline_s):
        from ..inventory.registry import utc_now

        conn = registry.conn()
        conn.execute("UPDATE deployments SET state='READY', ready_at=? WHERE deployment_id=?", (utc_now(), dep["deployment_id"]))
        conn.commit()
        return True, "healthy"
    return False, "health/identity check failed"
