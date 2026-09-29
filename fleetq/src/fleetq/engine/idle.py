"""Conservative shared-node GPU availability from independent observations (§2.2, §7).

A shared GPU is placeable only when the observation history *proves* it has
been idle, never merely when nothing contradicts it:

* no unsupported, incomplete, errored or ambiguous metric in the window;
  unknown is never zero;
* at least ``min_samples`` **distinct** sample ids across ``history_s``
  seconds, with no gap longer than ``max_gap_s`` (re-reading one cached sample
  is still one observation; a feed's response time is not a sample time);
* the newest sample is no older than ``max_age_s``, and the feed transport
  itself answered within ``feed_stale_s``;
* one boot throughout;
* zero compute processes, memory within the GPU's *measured* baseline, and
  utilization within the profile, in every sample.

A controller restart starts with empty history, so shared placement waits for
fresh observations to rebuild it (§7). This module decides nothing about
exclusive nodes, whose immediate launch gate is enough.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

DEFAULTS = {"history_s": 120.0, "min_samples": 3, "max_gap_s": 65.0, "max_age_s": 75.0,
            "max_util": 5.0, "feed_stale_s": 30.0}
FUTURE_SAMPLE_TOLERANCE_S = 5.0


@dataclass(frozen=True)
class GpuSample:
    sample_id: str
    sample_time: float            # when the GPU was measured (epoch seconds)
    boot_id: str | None
    supported: bool               # every required metric supported
    complete: bool                # process enumeration complete (no truncation, no namespace ambiguity)
    process_count: int | None
    mem_used_mib: float | None
    util: float | None
    error: str | None = None


class _FeedSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    sample_id: StrictStr = Field(min_length=1, max_length=128)
    sample_time: StrictInt | float
    boot_id: StrictStr | None
    supported: StrictBool
    complete: StrictBool
    process_count: StrictInt | None
    mem_used_mib: StrictInt | float | None
    utilization: StrictInt | float | None
    error: StrictStr | None = None


class _FeedGpu(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    uuid: StrictStr = Field(pattern=r"^GPU-", min_length=5, max_length=128)
    samples: list[_FeedSample] = Field(max_length=32)


class _FeedHost(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: StrictStr = Field(min_length=1, max_length=128)
    state: StrictStr | None = None
    gpus: list[_FeedGpu] = Field(max_length=64)


class _CapacityFeed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    schema_version: StrictStr = Field(alias="schema")
    generated_at: StrictInt | float
    hosts: list[_FeedHost] = Field(max_length=256)


@dataclass
class IdleHistory:
    """Per-(node, GPU) observation windows, fed by the fleetmon capacity feed."""

    policies: dict[str, dict[str, Any]] = field(default_factory=dict)      # node -> gpu_profile
    _samples: dict[tuple[str, str], deque] = field(default_factory=dict)
    _feed_ok_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    clock: Any = time.time

    def policy(self, node_id: str) -> dict[str, Any]:
        return {**DEFAULTS, **(self.policies.get(node_id) or {})}

    def record_feed_success(self, at: float | None = None) -> None:
        with self._lock:
            self._feed_ok_at = at if at is not None else self.clock()

    def add(self, node_id: str, uuid: str, sample: GpuSample) -> bool:
        """Add a sample; returns False for a duplicate or out-of-order one (§9)."""
        with self._lock:
            window = self._samples.setdefault((node_id, uuid), deque(maxlen=64))
            if any(s.sample_id == sample.sample_id for s in window):
                return False                   # a cached re-read is not a new observation
            if window and sample.sample_time <= window[-1].sample_time:
                return False                   # stale/out-of-order: never progresses state
            window.append(sample)
            return True

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._feed_ok_at = None

    def expire_missing(self, present: set[tuple[str, str]]) -> None:
        """A fresh feed omitting a host/GPU revokes its previous idle history."""
        with self._lock:
            for key in set(self._samples) - present:
                del self._samples[key]

    def verdict(self, node_id: str, uuid: str) -> tuple[bool, str]:
        pol = self.policy(node_id)
        now = self.clock()
        with self._lock:
            feed_ok_at = self._feed_ok_at
            samples = list(self._samples.get((node_id, uuid), ()))
        if feed_ok_at is None or now - feed_ok_at > pol["feed_stale_s"]:
            return False, "feed_stale"
        if len(samples) < pol["min_samples"]:
            return False, "insufficient_history"
        newest = samples[-1]
        if newest.sample_time > now + FUTURE_SAMPLE_TOLERANCE_S:
            return False, "sample_from_future"
        if now - newest.sample_time > pol["max_age_s"]:
            return False, "sample_stale"
        # Select the contiguous tail ending at the newest observation and walk
        # backward until it covers the full history duration. The oldest
        # sample can naturally be older than history_s relative to wall time:
        # freshness is checked against the newest sample separately.
        tail = [newest]
        for sample in reversed(samples[:-1]):
            if tail[-1].sample_time - sample.sample_time > pol["max_gap_s"]:
                break
            tail.append(sample)
            if newest.sample_time - sample.sample_time >= pol["history_s"]:
                break
        window = list(reversed(tail))
        if len(window) < pol["min_samples"]:
            return False, "insufficient_history"
        if newest.sample_time - window[0].sample_time < pol["history_s"]:
            return False, "history_too_short"
        if len({s.boot_id for s in window}) != 1 or window[-1].boot_id in (None, "", "unknown"):
            return False, "boot_changed_or_unknown"
        # The backward walk stopped at a hole if there was not enough history;
        # this validates every interval in the selected coverage tail.
        for a, b in zip(window, window[1:]):
            if b.sample_time - a.sample_time > pol["max_gap_s"]:
                return False, "history_gap"
        baseline = float((pol.get("baselines") or {}).get(uuid, pol.get("default_baseline_mib", 0)))
        for s in window:
            if s.error:
                return False, f"gpu_error:{s.error}"
            if not s.supported or s.mem_used_mib is None or s.util is None:
                return False, "metric_unsupported"
            if not s.complete or s.process_count is None:
                return False, "process_visibility_incomplete"
            if s.process_count > 0:
                return False, "compute_process_present"
            if s.mem_used_mib > baseline:
                return False, "memory_above_baseline"
            if s.util > pol["max_util"]:
                return False, "utilization_above_profile"
        return True, "idle_history_clean"

    # SharedCapacity protocol, used by placement.
    def placeable_gpus(self, node_id: str, uuids: list[str]) -> tuple[list[str], dict[str, str]]:
        ok, reasons = [], {}
        for uuid in uuids:
            good, why = self.verdict(node_id, uuid)
            if good:
                ok.append(uuid)
            else:
                reasons[uuid] = why
        return ok, reasons


def ingest_capacity_feed(history: IdleHistory, feed: dict[str, Any]) -> int:
    """Load one ``fleetmon.capacity/v1`` document; returns new samples added.

    Only the schema version this module understands is accepted; anything
    else leaves the history untouched, so shared placement fails closed (§7).
    """
    if not isinstance(feed, dict) or feed.get("schema") != "fleetmon.capacity/v1":
        return 0
    try:
        document = _CapacityFeed.model_validate(feed)
        if document.schema_version != "fleetmon.capacity/v1":
            return 0
        seen_hosts: set[str] = set()
        staged: list[tuple[str, str, GpuSample]] = []
        seen_gpus: set[tuple[str, str]] = set()
        for host in document.hosts:
            if host.target in seen_hosts:
                return 0
            seen_hosts.add(host.target)
            if host.state not in (None, "live", "partial"):
                continue
            for gpu in host.gpus:
                key = (host.target, gpu.uuid)
                if key in seen_gpus:
                    return 0
                seen_gpus.add(key)
                sample_ids: set[str] = set()
                previous_time: float | None = None
                for s in gpu.samples:
                    sample_time = float(s.sample_time)
                    if s.sample_id in sample_ids or (previous_time is not None and sample_time <= previous_time):
                        return 0
                    sample_ids.add(s.sample_id)
                    previous_time = sample_time
                    if s.process_count is not None and s.process_count < 0:
                        return 0
                    staged.append((host.target, gpu.uuid, GpuSample(
                        sample_id=s.sample_id, sample_time=sample_time, boot_id=s.boot_id,
                        supported=s.supported, complete=s.complete, process_count=s.process_count,
                        mem_used_mib=float(s.mem_used_mib) if s.mem_used_mib is not None else None,
                        util=float(s.utilization) if s.utilization is not None else None, error=s.error)))
    except (ValidationError, TypeError, ValueError, OverflowError):
        return 0
    history.expire_missing(seen_gpus)
    added = 0
    for node, uuid, sample in staged:
        added += history.add(node, uuid, sample)
    history.record_feed_success()
    return added
