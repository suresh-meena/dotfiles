"""Shared-node idle history (§2.2): every row of the idle table, plus cache/replay traps."""

from __future__ import annotations

import copy

import pytest

from fleetq.engine.idle import GpuSample, IdleHistory, ingest_capacity_feed


class Clock:
    def __init__(self, t: float = 10_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _s(i, t, *, procs=0, mem=0.0, util=0.0, boot="b1", supported=True, complete=True, error=None):
    return GpuSample(f"s{i}", t, boot, supported, complete, procs, mem, util, error)


@pytest.fixture()
def hist():
    clock = Clock()
    h = IdleHistory(policies={"ws": {"baselines": {"GPU-disp": 400}}}, clock=clock)
    h.record_feed_success()
    return h, clock


def _fill(h, clock, uuid="GPU-a", n=4, step=40.0, **kw):
    for i in range(n):
        h.add("ws", uuid, _s(i, clock.t - (n - 1 - i) * step, **kw))


def test_clean_sustained_history_is_placeable(hist):
    h, clock = hist
    _fill(h, clock)
    assert h.verdict("ws", "GPU-a") == (True, "idle_history_clean")


@pytest.mark.parametrize("kw, why", [
    ({"procs": 1}, "compute_process_present"),
    ({"mem": 20000.0}, "memory_above_baseline"),           # idle Jupyter kernel holding memory
    ({"util": 30.0}, "utilization_above_profile"),
    ({"supported": False}, "metric_unsupported"),          # unknown is never zero
    ({"complete": False}, "process_visibility_incomplete"),  # e.g. PID namespaces
    ({"error": "xid79"}, "gpu_error:xid79"),
])
def test_any_bad_sample_blocks(hist, kw, why):
    h, clock = hist
    _fill(h, clock, **kw)
    assert h.verdict("ws", "GPU-a") == (False, why)


def test_a_single_busy_sample_in_the_window_blocks(hist):
    """A labmate's loop between runs: idle now, busy 40 s ago."""
    h, clock = hist
    h.add("ws", "GPU-a", _s(0, clock.t - 130))
    h.add("ws", "GPU-a", _s(1, clock.t - 90, procs=1, mem=9000))
    h.add("ws", "GPU-a", _s(2, clock.t - 50))
    h.add("ws", "GPU-a", _s(3, clock.t - 5))
    assert h.verdict("ws", "GPU-a") == (False, "compute_process_present")


def test_display_gpu_uses_measured_baseline(hist):
    h, clock = hist
    _fill(h, clock, uuid="GPU-disp", mem=350.0)
    assert h.verdict("ws", "GPU-disp")[0] is True
    _fill(h, clock, uuid="GPU-a", mem=350.0)        # no measured baseline -> zero allowance
    assert h.verdict("ws", "GPU-a") == (False, "memory_above_baseline")


def test_cached_resamples_never_create_history(hist):
    h, clock = hist
    one = _s(0, clock.t - 5)
    for _ in range(10):
        h.add("ws", "GPU-a", one)                    # the same sample re-read ten times
    assert h.verdict("ws", "GPU-a") == (False, "insufficient_history")


def test_out_of_order_sample_is_ignored(hist):
    h, clock = hist
    _fill(h, clock)
    assert h.add("ws", "GPU-a", _s(99, clock.t - 1000, procs=5)) is False
    assert h.verdict("ws", "GPU-a")[0] is True


def test_gap_in_history_blocks(hist):
    h, clock = hist
    h.add("ws", "GPU-a", _s(0, clock.t - 130))
    h.add("ws", "GPU-a", _s(1, clock.t - 118))
    h.add("ws", "GPU-a", _s(2, clock.t - 115))
    h.add("ws", "GPU-a", _s(3, clock.t - 5))          # 110 s hole: could have been busy
    assert h.verdict("ws", "GPU-a") == (False, "insufficient_history")


def test_too_short_history_blocks(hist):
    h, clock = hist
    for i in range(5):
        h.add("ws", "GPU-a", _s(i, clock.t - 10 + i))     # 5 samples within 10 s
    assert h.verdict("ws", "GPU-a") == (False, "history_too_short")


def test_stale_samples_and_stale_feed_block(hist):
    h, clock = hist
    _fill(h, clock)
    clock.t += 100
    assert h.verdict("ws", "GPU-a")[1] in ("sample_stale", "feed_stale", "insufficient_history")
    h2 = IdleHistory(clock=clock)
    _fill(h2, clock)
    assert h2.verdict("ws", "GPU-a") == (False, "feed_stale")     # feed never answered


def test_boot_change_resets_trust(hist):
    h, clock = hist
    h.add("ws", "GPU-a", _s(0, clock.t - 130, boot="b1"))
    h.add("ws", "GPU-a", _s(1, clock.t - 90, boot="b2"))
    h.add("ws", "GPU-a", _s(2, clock.t - 45, boot="b2"))
    h.add("ws", "GPU-a", _s(3, clock.t - 5, boot="b2"))
    assert h.verdict("ws", "GPU-a") == (False, "boot_changed_or_unknown")


def test_restart_clears_history(hist):
    h, clock = hist
    _fill(h, clock)
    h.reset()
    h.record_feed_success()
    assert h.verdict("ws", "GPU-a") == (False, "insufficient_history")


def test_feed_ingest_dedupes_and_rejects_unknown_schema(hist):
    h, clock = hist
    doc = {"schema": "fleetmon.capacity/v1", "generated_at": clock.t,
           "hosts": [{"target": "ws", "gpus": [{"uuid": "GPU-a", "samples": [
        {"sample_id": f"p{i}", "sample_time": clock.t - (3 - i) * 40, "boot_id": "b1", "supported": True,
         "complete": True, "process_count": 0, "mem_used_mib": 0, "utilization": 0} for i in range(4)]}]}]}
    assert ingest_capacity_feed(h, doc) == 4
    assert ingest_capacity_feed(h, doc) == 0            # same document again: nothing new
    assert h.verdict("ws", "GPU-a")[0] is True
    assert ingest_capacity_feed(h, {"schema": "fleetmon.capacity/v2"}) == 0


@pytest.mark.parametrize("level", ["document", "host", "gpu", "sample"])
def test_feed_rejects_unknown_v1_fields_without_refreshing_history(hist, level):
    h, clock = hist
    doc = {"schema": "fleetmon.capacity/v1", "generated_at": clock.t,
           "hosts": [{"target": "ws", "state": "live", "gpus": [{"uuid": "GPU-a", "samples": [
               {"sample_id": "s1", "sample_time": clock.t, "boot_id": "b1",
                "supported": True, "complete": True, "process_count": 0,
                "mem_used_mib": 0, "utilization": 0}
           ]}]}]}
    changed = copy.deepcopy(doc)
    objects = {"document": changed, "host": changed["hosts"][0],
               "gpu": changed["hosts"][0]["gpus"][0],
               "sample": changed["hosts"][0]["gpus"][0]["samples"][0]}
    objects[level]["unexpected_v1_field"] = "unsafe-new-meaning"
    assert ingest_capacity_feed(h, changed) == 0
    assert h.verdict("ws", "GPU-a") == (False, "insufficient_history")
    assert ingest_capacity_feed(h, doc) == 1


def test_placeable_gpus_reports_per_gpu_reasons(hist):
    h, clock = hist
    _fill(h, clock, uuid="GPU-a")
    _fill(h, clock, uuid="GPU-b", procs=1)
    ok, reasons = h.placeable_gpus("ws", ["GPU-a", "GPU-b", "GPU-c"])
    assert ok == ["GPU-a"]
    assert reasons == {"GPU-b": "compute_process_present", "GPU-c": "insufficient_history"}


def test_feed_generation_time_cannot_make_old_gpu_samples_fresh(hist):
    h, clock = hist
    old = [{"sample_id": f"old-{i}", "sample_time": clock.t - 300 + i * 40,
            "boot_id": "b1", "supported": True, "complete": True,
            "process_count": 0, "mem_used_mib": 0, "utilization": 0} for i in range(4)]
    doc = {"schema": "fleetmon.capacity/v1", "generated_at": clock.t,
           "hosts": [{"target": "ws", "gpus": [{"uuid": "GPU-a", "samples": old}]}]}
    assert ingest_capacity_feed(h, doc) == 4
    assert h.verdict("ws", "GPU-a") == (False, "sample_stale")


def test_invalid_feed_is_atomic_and_does_not_refresh_feed_freshness(hist):
    h, clock = hist
    before = h.verdict("ws", "GPU-a")
    duplicate_host = {"schema": "fleetmon.capacity/v1", "hosts": [
        {"target": "ws", "gpus": []}, {"target": "ws", "gpus": []}]}
    assert ingest_capacity_feed(h, duplicate_host) == 0
    assert h.verdict("ws", "GPU-a") == before
    clock.t += 31
    assert h.verdict("ws", "GPU-a") == (False, "feed_stale")


def test_unknown_sample_identity_or_missing_metric_is_not_idle(hist):
    h, clock = hist
    samples = [{"sample_id": f"x{i}", "sample_time": clock.t - (3-i)*40,
                "boot_id": "b1", "supported": True, "complete": True,
                "process_count": 0, "mem_used_mib": 0, "utilization": 0} for i in range(4)]
    samples[-1]["sample_id"] = samples[0]["sample_id"]
    doc = {"schema": "fleetmon.capacity/v1", "generated_at": clock.t, "hosts": [
        {"target": "ws", "gpus": [{"uuid": "GPU-a", "samples": samples}]}]}
    assert ingest_capacity_feed(h, doc) == 0
    assert h.verdict("ws", "GPU-a")[1] == "insufficient_history"


def test_omitted_host_or_gpu_invalidates_old_idle_evidence(hist):
    h, clock = hist
    _fill(h, clock)
    assert h.verdict("ws", "GPU-a")[0] is True
    # A newer complete feed that no longer mentions this node/GPU cannot leave
    # a previously clean window eligible for placement.
    doc = {"schema": "fleetmon.capacity/v1", "generated_at": clock.t,
           "hosts": [{"target": "other", "gpus": []}]}
    assert ingest_capacity_feed(h, doc) == 0
    assert h.verdict("ws", "GPU-a")[0] is False


def test_unavailable_host_does_not_refresh_idle_history(hist):
    h, clock = hist
    _fill(h, clock)
    assert h.verdict("ws", "GPU-a")[0] is True
    clock.t += 31
    doc = {"schema": "fleetmon.capacity/v1", "generated_at": clock.t, "hosts": [
        {"target": "ws", "state": "unavailable", "gpus": []}]}
    ingest_capacity_feed(h, doc)
    assert h.verdict("ws", "GPU-a") == (False, "insufficient_history")
