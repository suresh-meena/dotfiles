"""Real remote vLLM lifecycle over SSH: launch, probe, promote, terminate.

Fail-closed: every claim (STARTING/READY/STOPPED) is backed by an actual
remote check (pid alive, port listening, /health 200, /v1/models identity).
"""
from __future__ import annotations

import shlex
import time
from typing import Any

from ..errors import ModelctlError
from ..transport.ssh import SSHTransport


def machine_ssh_cfg(config: dict[str, Any], machine_id: str) -> dict[str, Any]:
    return (config.get("machines", {}).get(machine_id, {}) or {}).get("ssh", {}) or {}


def is_remote_machine(config: dict[str, Any], machine_id: str) -> bool:
    """Real (non-simulation) machine: has an ssh host that is not example.internal."""
    host = machine_ssh_cfg(config, machine_id).get("host")
    return bool(host) and "example.internal" not in host


def remote_lifecycle(config: dict[str, Any], machine_id: str) -> "RemoteLifecycle | None":
    if not is_remote_machine(config, machine_id):
        return None
    return RemoteLifecycle(machine_ssh_cfg(config, machine_id))


class RemoteLifecycle:
    def __init__(self, ssh_cfg: dict[str, Any]):
        self.t = SSHTransport(
            ssh_cfg.get("host"), ssh_cfg.get("user"), ssh_cfg.get("port"), ssh_cfg.get("password_file")
        )

    # --- probes -------------------------------------------------------------

    def pid_alive(self, pid: int | None) -> bool:
        if not pid or int(pid) <= 0:
            return False
        cp = self.t.run(
            ["sh", "-c", f"kill -0 {int(pid)} 2>/dev/null && echo ALIVE || echo DEAD"], timeout=20
        )
        return b"ALIVE" in cp.stdout

    def port_busy(self, port: int | None) -> bool:
        if not port:
            return False
        cp = self.t.run(
            ["sh", "-c", f"ss -tlnH 2>/dev/null | grep -q ':{int(port)} ' && echo BUSY || echo FREE"],
            timeout=20,
        )
        return b"BUSY" in cp.stdout

    def health(self, port: int, served_model: str | None = None) -> tuple[bool, bool]:
        """Returns (healthy, identity_ok). healthy = /health 200; identity = served model listed."""
        cp = self.t.run(
            ["sh", "-c",
             f"curl -s -o /dev/null -m 4 -w '%{{http_code}}' http://127.0.0.1:{int(port)}/health; echo; "
             f"curl -s -m 4 http://127.0.0.1:{int(port)}/v1/models"],
            timeout=30,
        )
        out = cp.stdout.decode(errors="ignore")
        first, _, rest = out.partition("\n")
        healthy = first.strip() == "200"
        identity = healthy and (not served_model or served_model in rest)
        return healthy, identity

    # --- state files & launch ----------------------------------------------

    def mkdir_p(self, path: str) -> None:
        cp = self.t.run(["sh", "-c", f"mkdir -p {shlex.quote(path)}"], timeout=20)
        if cp.returncode != 0:
            raise ModelctlError(code="E_INTERNAL", message=f"could not create remote dir {path}: {cp.stderr.decode(errors='ignore')[:200]}")

    def write_file(self, path: str, content: str) -> None:
        cp = self.t.run(["sh", "-c", f"cat > {shlex.quote(path)}"], timeout=30, input_data=content.encode())
        if cp.returncode != 0:
            raise ModelctlError(
                code="E_INTERNAL",
                message=f"could not write remote file {path}: {cp.stderr.decode(errors='ignore')[:300]}",
            )

    def log_tail(self, path: str, n: int = 40) -> str:
        cp = self.t.run(["sh", "-c", f"tail -n {int(n)} {shlex.quote(path)} 2>/dev/null"], timeout=20)
        return cp.stdout.decode(errors="ignore")

    def launch_detached(self, script_path: str, log_path: str) -> int:
        cmd = (
            f"setsid nohup bash {shlex.quote(script_path)} > {shlex.quote(log_path)} 2>&1 < /dev/null & echo $!"
        )
        cp = self.t.run(["sh", "-c", cmd], timeout=30)
        try:
            return int(cp.stdout.strip().splitlines()[-1])
        except Exception:
            raise ModelctlError(
                code="E_START_EXITED",
                message=f"could not obtain remote pid: stdout={cp.stdout[:100]!r} stderr={cp.stderr.decode(errors='ignore')[:200]}",
            )

    def build_launch_script(self, resolved: dict[str, Any], remote_yaml: str, remote_log: str) -> str:
        activate = (resolved.get("machine_runtime", {}) or {}).get("activate")
        extra = (resolved.get("vllm", {}) or {}).get("extra_args") or []
        lines = ["#!/bin/bash", "set -uo pipefail"]
        if activate:
            lines.append(f"source {shlex.quote(str(activate))}")
        argv = ["vllm", "serve", "--config", remote_yaml, *[str(a) for a in extra]]
        lines.append("exec " + " ".join(shlex.quote(a) for a in argv))
        return "\n".join(lines) + "\n"

    # --- termination ---------------------------------------------------------

    def terminate(self, dep: dict[str, Any], *, graceful_timeout_s: float = 30.0, kill_timeout_s: float = 15.0) -> dict[str, Any]:
        """SIGTERM group -> wait -> SIGKILL group -> verify pid gone AND port free."""
        pid = int(dep.get("server_pid") or 0)
        port = dep.get("port")
        if not self.pid_alive(pid):
            port_free = port is None or not self.port_busy(port)
            return {
                "ok": port_free,
                "detail": "pid not alive (already stopped)" if port_free else "pid gone but port still occupied",
                "pid": pid, "group_gone": True, "port_free": port_free,
            }
        self.t.run(["sh", "-c", f"kill -TERM -{pid} 2>/dev/null; true"], timeout=20)
        if self._wait_gone(pid, port, graceful_timeout_s):
            return {"ok": True, "detail": f"terminated via SIGTERM (pgid {pid})", "pid": pid, "group_gone": True, "port_free": True}
        self.t.run(["sh", "-c", f"kill -KILL -{pid} 2>/dev/null; true"], timeout=20)
        if self._wait_gone(pid, port, kill_timeout_s):
            return {"ok": True, "detail": f"terminated via SIGKILL (pgid {pid})", "pid": pid, "group_gone": True, "port_free": True}
        gone = not self.pid_alive(pid)
        free = port is None or not self.port_busy(port)
        detail = "process group still present after SIGKILL" if not gone else "port still occupied"
        return {"ok": gone and free, "detail": detail, "pid": pid, "group_gone": gone, "port_free": free}

    def _wait_gone(self, pid: int, port: int | None, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if not self.pid_alive(pid) and (port is None or not self.port_busy(port)):
                return True
            time.sleep(2.0)
        return False
