"""Bounded GPU-free notifications built from committed samples only.

Notifications reuse the samples the hub already stores; they add zero remote
polls. Availability is classified from the latest TWO DISTINCT consecutive
host observations: receipt freshness, boot identity, a supported non-MIG GPU,
error-free NVML telemetry, zero compute processes, and zero utilization in
both observations. Ordinary top-process-list truncation never disqualifies
GPU telemetry; actual NVML errors or a missing GPU row break the chain.
Delivery is one bounded HTTP POST per transition; failures back off instead
of retrying until success. The same classifier backs the idle-GPU API, the
host GPU rows, and the overview summary.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, TypeGuard

GPU_FREE_OBSERVATIONS = 2
GPU_IDLE = "idle"
GPU_BUSY = "busy"
GPU_UNKNOWN = "unknown"
GPU_STALE = "stale"
MAX_NOTIFY_URL_BYTES = 512
NOTIFY_TIMEOUT_SECONDS = 5.0
NOTIFY_RETRY_SECONDS = 300.0
MAX_PAYLOAD_BYTES = 2048
MAX_TRACKED_GPUS = 32


def parse_notify_url(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("notify URL must be a string")
    value = value.strip()
    if not value:
        return None
    if len(value.encode("utf-8")) > MAX_NOTIFY_URL_BYTES:
        raise ValueError("notify URL is too long")
    if not value.startswith(("http://", "https://")):
        raise ValueError("notify URL must be http(s)")
    return value


def default_poster(url: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body[:MAX_PAYLOAD_BYTES],
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=NOTIFY_TIMEOUT_SECONDS) as resp:
            if resp.status < 200 or resp.status >= 300:
                return "notify_http_error"
    except (urllib.error.URLError, OSError, ValueError):
        return "notify_unreachable"
    return "ok"


def _numeric(value: Any) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _activity_reason(gpu: dict[str, Any]) -> str | None:
    """Return why a trusted GPU row shows activity, or None when idle."""

    processes = gpu.get("compute_process_count")
    utilization = gpu.get("utilization")
    busy_processes = _numeric(processes) and processes > 0
    busy_utilization = _numeric(utilization) and utilization > 0
    if busy_processes and busy_utilization:
        return "compute_processes_and_utilization"
    if busy_processes:
        return "compute_processes"
    if busy_utilization:
        return "utilization"
    return None


def _untrusted_reason(observation: dict[str, Any]) -> str | None:
    """Return why one observation cannot support a trustworthy decision.

    Host process-list truncation is deliberately absent: it says nothing
    about NVML completeness. Only actual NVML evidence disqualifies.
    """

    if not observation.get("nvml_supported"):
        return "nvml_unsupported"
    if observation.get("nvml_error"):
        return "nvml_error"
    gpu = observation.get("gpu")
    if not isinstance(gpu, dict):
        return "missing_gpu_row"
    if gpu.get("error"):
        return "gpu_error"
    if not gpu.get("supported"):
        return "gpu_unsupported"
    if gpu.get("mig_detected"):
        return "mig_detected"
    if not _numeric(gpu.get("utilization")) or not _numeric(
        gpu.get("compute_process_count")
    ):
        return "incomplete_telemetry"
    return None


def classify_gpu(
    newest: dict[str, Any] | None,
    older: dict[str, Any] | None,
    *,
    now: float | None = None,
    freshness_seconds: float | None = None,
) -> tuple[str, str]:
    """Classify one GPU from the two latest consecutive observations.

    Each observation carries host receipt context (``received_at``,
    ``boot_id``, ``nvml_supported``, ``nvml_error``) plus this GPU's row as
    ``gpu`` (or None when the GPU is absent from that observation). Returns
    ``(availability, reason)`` where availability is idle, busy, or unknown.
    """

    if not isinstance(newest, dict) or not newest:
        return GPU_UNKNOWN, "no_observation"
    if now is not None and freshness_seconds is not None and freshness_seconds > 0:
        received = newest.get("received_at")
        if not _numeric(received) or not 0 <= now - received <= freshness_seconds:
            return GPU_UNKNOWN, "stale_receipt"
    reason = _untrusted_reason(newest)
    if reason is not None:
        return GPU_UNKNOWN, reason
    activity = _activity_reason(newest["gpu"])
    if activity is not None:
        return GPU_BUSY, activity
    if not isinstance(older, dict) or not older:
        return GPU_UNKNOWN, "awaiting_second_observation"
    received, previous = newest.get("received_at"), older.get("received_at")
    if not _numeric(received) or not _numeric(previous) or received <= previous:
        return GPU_UNKNOWN, "non_distinct_observations"
    if (
        now is not None
        and freshness_seconds is not None
        and not 0 <= now - previous <= freshness_seconds
    ):
        return GPU_UNKNOWN, "older_observation_stale"
    if newest.get("boot_id") in (None, "", "unknown"):
        return GPU_UNKNOWN, "boot_unknown"
    if newest.get("boot_id") != older.get("boot_id"):
        return GPU_UNKNOWN, "boot_changed"
    reason = _untrusted_reason(older)
    if reason is not None:
        return GPU_UNKNOWN, reason
    if _activity_reason(older["gpu"]) is not None:
        return GPU_BUSY, "recent_activity"
    return GPU_IDLE, "consecutive_idle_observations"


def classify_gpu_slots(
    slots: list[dict[str, Any] | None] | None,
    *,
    now: float | None = None,
    freshness_seconds: float | None = None,
) -> tuple[str, str]:
    """Classify from newest-first observation slots (missing slots are None)."""

    padded = list(slots or [])[:GPU_FREE_OBSERVATIONS]
    padded += [None] * (GPU_FREE_OBSERVATIONS - len(padded))
    return classify_gpu(
        padded[0], padded[1], now=now, freshness_seconds=freshness_seconds
    )


def gpu_flag(availability: Any) -> str:
    """Map one classifier verdict onto the hardened three-value flag set.

    busy and idle pass through unchanged; every verdict that cannot be
    confirmed from fresh, trusted observations — stale receipts, missing
    data, NVML errors, host unreachable — is surfaced as stale.  The flag is
    always exactly one of busy, idle, or stale, so a reader never has to
    interpret a fourth state.
    """

    if availability == GPU_BUSY:
        return GPU_BUSY
    if availability == GPU_IDLE:
        return GPU_IDLE
    return GPU_STALE


def evaluate_gpu_free(
    recent: dict[str, list[dict[str, Any] | None]],
    counts: dict[str, int],
    notified: dict[str, Any] | None = None,
    *,
    now: float | None = None,
    freshness_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    """Return (events, new_counts, new_notified) for per-GPU free tracking.

    ``recent`` maps GPU UUID to the newest-first slots of the latest two
    consecutive observations for the target; a slot whose observation lacks
    the GPU row carries ``gpu=None`` so the chain breaks. Two distinct
    consecutive idle observations fire immediately instead of requiring a
    third observation. Any non-idle outcome resets the chain and rearms the
    sent flag so the next idle transition notifies again.
    """

    events: list[dict[str, Any]] = []
    new_counts: dict[str, int] = {}
    updated_notified = {k: v for k, v in (notified or {}).items() if k in recent}
    for uuid_key, samples in recent.items():
        availability, _reason = classify_gpu_slots(
            samples, now=now, freshness_seconds=freshness_seconds
        )
        if availability != GPU_IDLE:
            new_counts[uuid_key] = 0
            updated_notified.pop(uuid_key, None)
            continue
        new_counts[uuid_key] = GPU_FREE_OBSERVATIONS
        if updated_notified.get(uuid_key) == "sent":
            continue
        gpu = {}
        for slot in list(samples or [])[:1]:
            if isinstance(slot, dict) and isinstance(slot.get("gpu"), dict):
                gpu = slot["gpu"]
        events.append(
            {
                "event": "gpu_free",
                "gpu_uuid": uuid_key,
                "gpu_index": gpu.get("idx"),
                "model": gpu.get("model"),
                "observations": GPU_FREE_OBSERVATIONS,
            }
        )
    return events, new_counts, updated_notified


def notify_target(
    target: str,
    events: list[dict[str, Any]],
    notified: dict[str, Any],
    next_retry: float,
    now: float,
    url: str,
    poster: Callable[[str, dict[str, Any]], str] = default_poster,
) -> tuple[dict[str, Any], float]:
    """Deliver at most one POST per GPU transition, honoring backoff.

    Returns (updated notified map, updated next_retry timestamp). A failed
    delivery backs off ``NOTIFY_RETRY_SECONDS`` and is retried on a later
    cycle while the GPU remains free; it never blocks polling.
    """

    if now < next_retry:
        return notified, next_retry
    updated = dict(notified)
    retry = next_retry
    for event in events:
        uuid_key = event["gpu_uuid"]
        if updated.get(uuid_key) == "sent":
            continue
        payload = {"target": target, **event}
        result = poster(url, payload)
        if result == "ok":
            updated[uuid_key] = "sent"
        else:
            updated[uuid_key] = result
            retry = now + NOTIFY_RETRY_SECONDS
            break
    return updated, retry


def prune_tracking(
    counts: dict[str, int], notified: dict[str, Any]
) -> tuple[dict[str, int], dict[str, Any]]:
    """Keep tracking maps bounded to MAX_TRACKED_GPUS newest-agnostic."""

    if len(counts) > MAX_TRACKED_GPUS:
        counts = dict(sorted(counts.items())[:MAX_TRACKED_GPUS])
    if len(notified) > MAX_TRACKED_GPUS:
        notified = dict(sorted(notified.items())[:MAX_TRACKED_GPUS])
    return counts, notified
