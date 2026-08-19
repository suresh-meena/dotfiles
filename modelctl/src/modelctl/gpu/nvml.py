from __future__ import annotations

import subprocess
import json
from typing import Any


def query_via_nvidia_smi() -> dict[str, Any]:
    """Best-effort local GPU query. Prefers NVML if available, falls back to nvidia-smi parsing (with warning)."""
    # Try pynvml if installed
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        gpus = []
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            uuid = pynvml.nvmlDeviceGetUUID(h).decode() if isinstance(pynvml.nvmlDeviceGetUUID(h), bytes) else str(pynvml.nvmlDeviceGetUUID(h))
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            procs = pynvml.nvmlDeviceGetComputeRunningProcesses(h)
            # procs are objects with pid and usedGpuMemory
            proc_list = []
            for p in procs:
                pid = getattr(p, "pid", None)
                mem_used = getattr(p, "usedGpuMemory", None)
                proc_list.append({"pid": pid, "usedGpuMemory": mem_used})
            gpus.append({"index": i, "uuid": uuid, "total_memory": mem.total, "used_memory": mem.used, "free_memory": mem.free, "compute_processes": proc_list})
        pynvml.nvmlShutdown()
        return {"backend": "nvml", "gpus": gpus}
    except Exception:
        pass
    # fallback to nvidia-smi (not guaranteed stable, per NVIDIA docs)
    try:
        cp = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            timeout=5,
        )
        if cp.returncode != 0:
            return {"backend": "none", "gpus": [], "error": cp.stderr.decode(errors="ignore")[:500]}
        gpus = []
        for line in cp.stdout.decode().strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({"index": int(parts[0]), "uuid": parts[1], "total_memory_mib": int(parts[2]), "used_memory_mib": int(parts[3]), "free_memory_mib": int(parts[4]), "compute_processes": []})
        # also query compute processes via csv
        try:
            cp2 = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"],
                capture_output=True,
                timeout=5,
            )
            if cp2.returncode == 0:
                # attach generically (no per-GPU mapping in fallback)
                pass
        except Exception:
            pass
        return {"backend": "nvidia-smi-fallback", "gpus": gpus, "warning": "nvidia-smi output not guaranteed stable; prefer NVML"}
    except FileNotFoundError:
        return {"backend": "none", "gpus": [], "error": "nvidia-smi not found and pynvml unavailable"}
    except Exception as e:
        return {"backend": "none", "gpus": [], "error": str(e)[:500]}
