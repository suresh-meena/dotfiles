from __future__ import annotations

from typing import Any

# Per spec §9.5
GPU_FREE = "FREE"
GPU_OWNED_SAME = "OWNED_BY_SAME_DEPLOYMENT"
GPU_RESERVED_OTHER = "RESERVED_BY_OTHER_MODELCTL_DEPLOYMENT"
GPU_BUSY_FOREIGN = "BUSY_FOREIGN"
GPU_UNKNOWN = "UNKNOWN"


def classify_gpu(*, index: int, uuid: str, compute_processes: list[dict[str, Any]], reserved_by: str | None, owned_deployment: str | None) -> str:
    if reserved_by and reserved_by != owned_deployment:
        return GPU_RESERVED_OTHER
    if compute_processes:
        # if any process, check if owned by same deployment (we can't know PID ownership without cgroup, so treat as BUSY_FOREIGN unless we prove ownership)
        # For local simulation, if owned_deployment is set and we have processes, assume OWNED_SAME if reservation matches
        if owned_deployment and reserved_by == owned_deployment:
            return GPU_OWNED_SAME
        return GPU_BUSY_FOREIGN
    if reserved_by == owned_deployment and owned_deployment:
        return GPU_OWNED_SAME
    if not compute_processes and not reserved_by:
        return GPU_FREE
    return GPU_UNKNOWN


def preflight_gpus(gpus: list[int], gpu_states: dict[int, str]) -> dict[int, str]:
    return {i: gpu_states.get(i, GPU_UNKNOWN) for i in gpus}
