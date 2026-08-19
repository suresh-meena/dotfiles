from __future__ import annotations

import subprocess
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

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
        url = f"http://{host}:{port}/health"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                body = r.read().decode()
                # vLLM health returns 200 when healthy, 503 if engine dead per spec
                return r.status == 200
        except urllib.error.HTTPError as e:
            return e.code == 200
        except Exception:
            return False

    def check_model_identity(self, host: str = "127.0.0.1", port: int = 8000, expected: str | None = None, timeout: float = 2.0) -> bool:
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
