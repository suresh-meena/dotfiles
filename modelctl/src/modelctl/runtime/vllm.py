from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import json
from pathlib import Path
from typing import Any

import psutil

# Capability matrix per spec §12.2 (simplified)
SUPPORTED = {
    "0.6": {"serve_config": True, "health": True, "sleep": True},
    "0.7": {"serve_config": True, "health": True, "sleep": True},
    "0.8": {"serve_config": True, "health": True, "sleep": True},
    "0.9": {"serve_config": True, "health": True, "sleep": True},
}


class RuntimeAdapter:
    def detect_version(self, executable: str = "vllm") -> str | None:
        try:
            cp = subprocess.run([executable, "--version"], capture_output=True, timeout=5, text=True)
            out = (cp.stdout + cp.stderr).strip()
            # expected like "0.8.5"
            import re

            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out)
            if m:
                return m.group(1)
            return None
        except Exception:
            return None

    def validate_target(self, resolved: dict[str, Any]) -> None:
        # check tp size etc already validated; check port
        port = resolved.get("port")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            from ..errors import ModelctlError

            raise ModelctlError(code="E_CONFIG_INVALID", message=f"invalid port {port}")

    def generate_config(self, resolved: dict[str, Any], dest: Path) -> Path:
        """Generate vLLM YAML config per spec §12.3."""
        cfg = {
            "model": resolved["artifact"]["path"],
            "host": resolved.get("bind_host", "127.0.0.1"),
            "port": resolved.get("port", 8000),
            "served-model-name": resolved.get("served_model_name", resolved["model"]),
            "tensor-parallel-size": resolved.get("vllm", {}).get("tensor_parallel_size", 1),
            "max-model-len": resolved.get("vllm", {}).get("max_model_len", 4096),
            "gpu-memory-utilization": resolved.get("gpu_memory_utilization", 0.90),
            "dtype": resolved.get("vllm", {}).get("dtype", "auto"),
            "generation-config": "vllm",
            "enable-log-requests": False,
            "enable-log-outputs": False,
        }
        # merge allowed extra_args as separate? For YAML we keep only known; extra_args handled in launch spec
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.parent.chmod(0o700)
        except Exception:
            pass
        import yaml

        dest.write_text(yaml.safe_dump(cfg, sort_keys=True))
        try:
            dest.chmod(0o600)
        except Exception:
            pass
        return dest

    def build_launch_spec(self, resolved: dict[str, Any], config_yaml: Path) -> list[str]:
        # returns argv vector for ExecStart; never shell-interpolated
        vllm_args = ["vllm", "serve", "--config", str(config_yaml)]
        extra = resolved.get("vllm", {}).get("extra_args")
        if extra:
            # only allowlisted extra args (already validated as list of strings, no shell injection)
            vllm_args.extend(extra)
        return vllm_args

    def check_health(self, host: str = "127.0.0.1", port: int = 8000, timeout: float = 2.0) -> bool:
        import urllib.request
        import urllib.error

        url = f"http://{host}:{port}/health"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                # vLLM health returns 200 when healthy, 503 if engine dead per spec
                return r.status == 200
        except urllib.error.HTTPError as e:
            return e.code == 200
        except Exception:
            return False

    def check_model_identity(self, host: str = "127.0.0.1", port: int = 8000, expected: str | None = None, timeout: float = 2.0) -> bool:
        import urllib.request
        import urllib.error

        if not expected:
            return True
        url = f"http://{host}:{port}/v1/models"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.loads(r.read().decode())
                # data is {"data": [{"id": "qwen-72b", ...}]}
                models = data.get("data", []) if isinstance(data, dict) else []
                ids = {m.get("id") for m in models if isinstance(m, dict)}
                return expected in ids
        except Exception:
            return False

    def wait_ready(self, host: str, port: int, expected_model: str, deadline_s: float, initial_delay: float = 0.25, max_delay: float = 5.0) -> bool:
        import random
        import time

        deadline = time.time() + deadline_s
        delay = initial_delay
        while time.time() < deadline:
            if self.check_health(host, port) and self.check_model_identity(host, port, expected_model):
                return True
            # jitter
            time.sleep(delay + random.uniform(0, delay * 0.2))
            delay = min(max_delay, delay * 1.5)
        return False


def spawn_local_fake(served_model: str, port: int, log_file: Path, child: bool = False) -> subprocess.Popen:
    """Spawn the fake vLLM-compatible runtime as a new session leader.

    The process becomes its own session/process group leader so the pid can
    be used directly for process-group termination (killpg).
    """
    argv = [
        sys.executable,
        "-m",
        "modelctl.runtime.fake",
        "--port",
        str(port),
        "--served-model",
        served_model,
        "--log-file",
        str(log_file),
    ]
    if child:
        argv.append("--child")
    return subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def pid_alive(pid: int | None) -> bool:
    """True when a pid still exists. Fail-closed: unknown/error => False."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # exists but owned by another user; treat as alive (cannot prove dead)
        return True
    except Exception:
        return False


def _group_has_live_member(pgid: int) -> bool:
    """True when any non-zombie process belongs to the group.

    A zombie member is already dead (signals are no-ops on it); requiring its
    reap by an external parent would make verified termination impossible
    whenever the spawner stays alive. Unscannable members fail closed.
    """
    for proc in psutil.process_iter(attrs=["status"]):
        try:
            if os.getpgid(proc.pid) != pgid:
                continue
        except (ProcessLookupError, PermissionError, OSError):
            continue
        if proc.info.get("status") != psutil.STATUS_ZOMBIE:
            return True
    return False


def process_group_gone(pid: int | None) -> bool:
    """True only when the whole process group is proven gone.

    Any error other than ProcessLookupError (e.g. EPERM, EINVAL) means we
    cannot prove the group is gone, so we fail closed and report alive.
    Group members that are zombies count as gone: they are already dead.
    """
    if not pid or pid <= 0:
        return True
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    return not _group_has_live_member(pid)


def kill_group(pid: int | None, sig: int = signal.SIGTERM) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        return False


def port_busy(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
