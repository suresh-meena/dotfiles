from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import uuid
from typing import Any

from .inventory.registry import Registry


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TunnelManager:
    def __init__(self, registry: Registry):
        self.registry = registry

    def connect(self, *, target_id: str, machine_id: str, ssh_host: str, ssh_user: str | None, ssh_port: int | None, remote_port: int, trace_id: str | None = None) -> dict[str, Any]:
        trace_id = trace_id or uuid.uuid4().hex[:8]
        # Check existing tunnel
        existing = [t for t in self.registry.list_tunnels() if t["target_id"] == target_id and t["state"] == "ACTIVE"]
        if existing:
            t = existing[0]
            return {"ok": True, "target": target_id, "local": f"http://127.0.0.1:{t['local_port']}/v1", "local_port": t["local_port"], "remote_port": t["remote_port"], "tunnel_id": t["tunnel_id"], "idempotent": True, "trace_id": trace_id}
        local_port = find_free_port()
        tunnel_id = f"tun-{uuid.uuid4().hex[:8]}"
        # Start SSH tunnel: ssh -N -L local:127.0.0.1:remote host
        ssh_cmd = ["ssh", "-N", "-L", f"{local_port}:127.0.0.1:{remote_port}"]
        if ssh_port:
            ssh_cmd += ["-p", str(ssh_port)]
        ssh_cmd.append(f"{ssh_user}@{ssh_host}" if ssh_user else ssh_host)
        # For local simulation (host == localhost or example.internal without real SSH), we skip actual ssh and just reserve port
        # We detect if ssh_host contains "example.internal" -> simulation mode
        pid: int | None = None
        state = "ACTIVE"
        if "example.internal" in ssh_host or ssh_host in ("localhost", "127.0.0.1"):
            # simulation: no real tunnel process, just record
            pid = None
        else:
            try:
                proc = subprocess.Popen(ssh_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                pid = proc.pid
                # brief check
                time.sleep(0.3)
                if proc.poll() is not None:
                    raise RuntimeError(f"ssh tunnel failed with exit {proc.poll()}")
            except Exception as e:
                from .errors import ModelctlError

                raise ModelctlError(code="E_TUNNEL_FAILED", message=f"tunnel failed: {e}", target=target_id, machine=machine_id)
        self.registry.upsert_tunnel(tunnel_id, target_id, machine_id, local_port, remote_port, pid, state)
        return {"ok": True, "target": target_id, "local": f"http://127.0.0.1:{local_port}/v1", "local_port": local_port, "remote_port": remote_port, "tunnel_id": tunnel_id, "pid": pid, "trace_id": trace_id}

    def disconnect(self, *, target_id: str | None = None, machine_id: str | None = None, tunnel_id: str | None = None) -> dict[str, Any]:
        tunnels = self.registry.list_tunnels()
        to_close = []
        for t in tunnels:
            if tunnel_id and t["tunnel_id"] != tunnel_id:
                continue
            if target_id and t["target_id"] != target_id:
                continue
            if machine_id and t["machine_id"] != machine_id:
                continue
            if t["state"] != "ACTIVE":
                continue
            to_close.append(t)
        if not to_close:
            return {"ok": True, "closed": 0, "idempotent": True}
        closed = 0
        for t in to_close:
            pid = t.get("pid")
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
                # wait briefly
                for _ in range(10):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                else:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except Exception:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except Exception:
                            pass
            self.registry.delete_tunnel(t["tunnel_id"])
            closed += 1
        return {"ok": True, "closed": closed}

    def endpoint(self, *, target_id: str) -> dict[str, Any]:
        for t in self.registry.list_tunnels():
            if t["target_id"] == target_id and t["state"] == "ACTIVE":
                return {"ok": True, "target": target_id, "local": f"http://127.0.0.1:{t['local_port']}/v1", "local_port": t["local_port"], "remote_port": t["remote_port"], "tunnel_id": t["tunnel_id"]}
        return {"ok": False, "code": "E_TUNNEL_FAILED", "message": f"no active tunnel for {target_id}", "target": target_id}
