"""Fleetmon telemetry cache. Reads never fetch or wake the controller."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from ..errors import FqError

STALE_S = 30.0


class _Metrics(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)


class _Gpu(_Metrics):
    uuid: StrictStr = Field(min_length=1, max_length=256)
    index: StrictInt = Field(ge=0)
    model: StrictStr | None = Field(max_length=256)
    utilization_fraction: float | None = Field(ge=0, le=1)
    vram_used_bytes: StrictInt | None = Field(ge=0)
    vram_total_bytes: StrictInt | None = Field(ge=0)
    supported: StrictBool


class _Host(_Metrics):
    target: StrictStr = Field(min_length=1, max_length=256)
    state: StrictStr = Field(min_length=1, max_length=64)
    sample_time: float | None = Field(ge=0)
    freshness_seconds: float = Field(gt=0, le=3 * 86400)
    cpu_busy_fraction: float | None = Field(ge=0, le=1)
    ram_used_bytes: StrictInt | None = Field(ge=0)
    ram_total_bytes: StrictInt | None = Field(ge=0)
    gpus: list[_Gpu] = Field(max_length=64)


class _Feed(_Metrics):
    schema_: Literal["fleetmon.usage/v1"] = Field(alias="schema")
    generated_at: float = Field(ge=0)
    hosts: list[_Host] = Field(max_length=256)


class UsageCache:
    def __init__(self, *, configured: bool = False):
        self.configured = configured
        self.document: dict | None = None
        self.fetched_at: float | None = None
        self.fetched_monotonic: float | None = None
        self.failed = False

    def update(self, document: dict) -> None:
        parsed = _Feed.model_validate(document)
        now = time.time()
        if parsed.generated_at > now + 5 or any(
            h.sample_time is not None and h.sample_time > min(now, parsed.generated_at) + 5
            for h in parsed.hosts
        ):
            raise ValueError("usage feed timestamp is in the future")
        targets = [h.target for h in parsed.hosts]
        if len(set(targets)) != len(targets):
            raise ValueError("usage feed has duplicate targets")
        for host in parsed.hosts:
            if len({g.uuid for g in host.gpus}) != len(host.gpus):
                raise ValueError("usage feed has duplicate GPUs")
            for gpu in host.gpus:
                if not gpu.supported:
                    gpu.utilization_fraction = gpu.vram_used_bytes = gpu.vram_total_bytes = None
        self.document = parsed.model_dump(by_alias=True)
        self.fetched_at = now
        self.fetched_monotonic = time.monotonic()
        self.failed = False

    def view(self, machine: str | None = None) -> dict:
        if not self.configured:
            raise FqError("feature_unavailable", "usage requires [daemon] fleetmon_feed_url on numpi")
        if self.document is None:
            raise FqError("not_ready", "Fleetmon usage cache is not available yet",
                          hint="Check Fleetmon and upgrade it if /api/feed/v1/usage is missing.")
        now = time.time()
        cache_age = max(0.0, time.monotonic() - self.fetched_monotonic)
        feed_age = max(0.0, now - self.document["generated_at"])
        source_state = "unavailable" if self.failed else "stale" if max(cache_age, feed_age) > STALE_S else "live"
        hosts = []
        for host in self.document["hosts"]:
            if machine is not None and host["target"] != machine:
                continue
            host = {**host, "gpus": [dict(g) for g in host["gpus"]]}
            sample = host["sample_time"]
            age = max(0.0, now - sample) if sample is not None else None
            stale = (host["state"] not in {"live", "partial"} or source_state != "live"
                     or age is None or age > host["freshness_seconds"])
            host["sample_age_s"] = round(age, 1) if age is not None else None
            host["stale"] = stale
            if host["state"] in {"live", "partial"} and stale:
                host["state"] = "stale" if sample is not None else "unknown"
            hosts.append(host)
        if machine is not None and not hosts:
            raise FqError("not_found", f"machine {machine!r} is not in Fleetmon's cached inventory")
        return {"schema": "fq.usage/v1", "ok": True,
                "source": {"state": source_state, "generated_at": self.document["generated_at"],
                           "cached_at": self.fetched_at, "cache_age_s": round(cache_age, 1)},
                "hosts": hosts}
