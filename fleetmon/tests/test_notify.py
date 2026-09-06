import pytest

from fleetmon import notify
from fleetmon.config import ConfigError


def gpu_row(utilization=0.0, processes=0, error=None, **extra):
    row = {
        "idx": 0,
        "model": "RTX",
        "utilization": utilization,
        "compute_process_count": processes,
        "supported": 1,
        "error": error,
        "mig_detected": 0,
    }
    row.update(extra)
    return row


def observation(
    gpu=None,
    *,
    received_at=1000.0,
    boot_id="boot-1",
    nvml_supported=1,
    nvml_error=None,
):
    return {
        "received_at": received_at,
        "boot_id": boot_id,
        "nvml_supported": nvml_supported,
        "nvml_error": nvml_error,
        "gpu": gpu,
    }


def test_url_parsing_is_bounded_and_http_only():
    assert notify.parse_notify_url(None) is None
    assert notify.parse_notify_url("  ") is None
    assert notify.parse_notify_url("https://host/topic") == "https://host/topic"
    with pytest.raises(ValueError):
        notify.parse_notify_url("ftp://host/topic")
    with pytest.raises(ValueError):
        notify.parse_notify_url(123)
    with pytest.raises(ValueError):
        notify.parse_notify_url("https://host/" + "x" * 600)


def test_classify_idle_requires_two_trusted_consecutive_idle_observations():
    newest = observation(gpu_row())
    assert notify.classify_gpu(newest, None) == (
        "unknown",
        "awaiting_second_observation",
    )
    assert notify.classify_gpu(None, None) == ("unknown", "no_observation")
    older = observation(gpu_row(), received_at=999.0)
    assert notify.classify_gpu(newest, older) == (
        "idle",
        "consecutive_idle_observations",
    )
    assert notify.classify_gpu_slots([newest, older]) == (
        "idle",
        "consecutive_idle_observations",
    )


def test_classify_busy_reasons_come_from_the_newest_trusted_row():
    assert notify.classify_gpu(
        observation(gpu_row(utilization=0.5, processes=1)),
        observation(gpu_row()),
    ) == ("busy", "compute_processes_and_utilization")
    assert notify.classify_gpu(
        observation(gpu_row(processes=2)), observation(gpu_row())
    ) == ("busy", "compute_processes")
    assert notify.classify_gpu(
        observation(gpu_row(utilization=0.2)), observation(gpu_row())
    ) == ("busy", "utilization")
    # Newest idle but the previous observation still shows activity.
    assert notify.classify_gpu(
        observation(gpu_row()), observation(gpu_row(utilization=1.0), received_at=999.0)
    ) == ("busy", "recent_activity")


def test_classify_stale_receipt_is_unknown_regardless_of_telemetry():
    result = notify.classify_gpu(
        observation(gpu_row(), received_at=1000.0),
        observation(gpu_row(), received_at=900.0),
        now=1130.0,
        freshness_seconds=120.0,
    )
    assert result == ("unknown", "stale_receipt")
    fresh = notify.classify_gpu(
        observation(gpu_row(), received_at=1000.0),
        observation(gpu_row(), received_at=900.0),
        now=1050.0,
        freshness_seconds=120.0,
    )
    assert fresh == ("unknown", "older_observation_stale")


def test_classify_boot_change_and_nvml_evidence_break_the_chain():
    idle = observation(gpu_row())
    other_boot = observation(gpu_row(), received_at=999.0, boot_id="boot-2")
    assert notify.classify_gpu(idle, other_boot) == ("unknown", "boot_changed")
    assert notify.classify_gpu(
        observation(gpu_row(), nvml_error="gpu_processes_truncated"), idle
    ) == ("unknown", "nvml_error")
    assert notify.classify_gpu(observation(gpu_row(), nvml_supported=0), idle) == (
        "unknown",
        "nvml_unsupported",
    )
    assert notify.classify_gpu(
        observation(gpu_row(error="uuid_unavailable")), idle
    ) == ("unknown", "gpu_error")
    assert notify.classify_gpu(observation(gpu_row(mig_detected=1)), idle) == (
        "unknown",
        "mig_detected",
    )
    assert notify.classify_gpu(observation(gpu_row(supported=0)), idle) == (
        "unknown",
        "gpu_unsupported",
    )
    assert notify.classify_gpu(observation(None), idle) == (
        "unknown",
        "missing_gpu_row",
    )
    assert notify.classify_gpu(observation(gpu_row(utilization=None)), idle) == (
        "unknown",
        "incomplete_telemetry",
    )


def test_gpu_flag_maps_every_verdict_onto_the_hardened_trio():
    assert notify.gpu_flag(notify.GPU_BUSY) == notify.GPU_BUSY
    assert notify.gpu_flag(notify.GPU_IDLE) == notify.GPU_IDLE
    # Every non-confirmed verdict is stale, never a fourth state.
    for verdict in (
        notify.GPU_UNKNOWN,
        "stale_receipt",
        "host_unavailable",
        "nvml_error",
        "boot_changed",
        None,
        "surprise-state",
    ):
        assert notify.gpu_flag(verdict) == notify.GPU_STALE


def test_gpu_free_fires_on_two_observations_without_a_third():
    uuid = "GPU-x"
    # Only one observation so far: no event, chain restarted.
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [observation(gpu_row()), None]}, {}
    )
    assert events == [] and counts[uuid] == 0
    # The second distinct consecutive idle observation fires immediately.
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [observation(gpu_row()), observation(gpu_row(), received_at=999.0)]},
        counts,
    )
    assert len(events) == 1 and events[0]["gpu_uuid"] == uuid
    assert events[0]["observations"] == 2
    assert counts[uuid] == notify.GPU_FREE_OBSERVATIONS == 2
    assert notified == {}


def test_unknown_breaks_the_chain_and_never_alerts():
    uuid = "GPU-x"
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [observation(gpu_row()), None]}, {uuid: 2}
    )
    assert events == [] and counts[uuid] == 0
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [observation(gpu_row()), observation(gpu_row(error="nvml"))]}, counts
    )
    assert events == [] and counts[uuid] == 0
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [None, observation(gpu_row())]}, counts
    )
    assert events == [] and counts[uuid] == 0
    # A missing GPU row in the newest observation (e.g. an NVML failure with
    # a stored empty GPU list) must break the chain, not borrow history.
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [observation(None), observation(gpu_row())]}, counts
    )
    assert events == [] and counts[uuid] == 0


def test_busy_gpu_resets_rearms_and_realerts_later():
    uuid = "GPU-x"
    # An already-delivered transition stays quiet while the GPU stays idle.
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [observation(gpu_row()), observation(gpu_row(), received_at=999.0)]},
        {uuid: 2},
        {uuid: "sent"},
    )
    assert events == [] and notified == {uuid: "sent"}
    # Becoming busy resets the chain and rearms the sent flag.
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [observation(gpu_row(utilization=5.0)), observation(gpu_row())]},
        counts,
        notified,
    )
    assert events == [] and counts[uuid] == 0
    assert uuid not in notified
    # Two fresh consecutive idle observations alert again.
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [observation(gpu_row()), observation(gpu_row(), received_at=999.0)]},
        counts,
        notified,
    )
    assert len(events) == 1


def test_sent_flag_survives_idle_cycles_and_is_not_rearmed_by_them():
    uuid = "GPU-x"
    events, counts, notified = notify.evaluate_gpu_free(
        {uuid: [observation(gpu_row()), observation(gpu_row(), received_at=999.0)]},
        {},
        {uuid: "sent"},
    )
    assert events == [] and counts[uuid] == 2 and notified == {uuid: "sent"}


def test_delivery_sends_once_and_backs_off_on_failure():
    uuid = "GPU-x"
    event = {
        "event": "gpu_free",
        "gpu_uuid": uuid,
        "gpu_index": 0,
        "model": "RTX",
        "observations": 2,
    }
    calls = []

    def poster(url, payload):
        calls.append(payload)
        return "ok"

    notified, retry = notify.notify_target(
        "host", [event], {}, 0.0, 100.0, "https://ntfy/topic", poster
    )
    assert notified[uuid] == "sent" and retry == 0.0 and len(calls) == 1
    assert calls[0]["target"] == "host"

    def failing(url, payload):
        return "notify_unreachable"

    notified, retry = notify.notify_target(
        "host", [event], {}, 0.0, 100.0, "https://ntfy/topic", failing
    )
    assert notified[uuid] == "notify_unreachable"
    assert retry == 100.0 + notify.NOTIFY_RETRY_SECONDS
    notified, retry = notify.notify_target(
        "host", [event], notified, retry, 150.0, "https://ntfy/topic", failing
    )
    assert notified[uuid] == "notify_unreachable"
    notified, retry = notify.notify_target(
        "host", [event], notified, retry, retry + 1, "https://ntfy/topic", poster
    )
    assert notified[uuid] == "sent"


def test_tracking_maps_are_bounded():
    counts = {f"GPU-{i}": 1 for i in range(64)}
    notified = {f"GPU-{i}": "sent" for i in range(64)}
    counts, notified = notify.prune_tracking(counts, notified)
    assert len(counts) <= notify.MAX_TRACKED_GPUS
    assert len(notified) <= notify.MAX_TRACKED_GPUS


def test_notify_url_comes_only_from_the_environment(monkeypatch):
    from fleetmon.config import _notify_url_from_environment

    monkeypatch.delenv("FLEETMON_NOTIFY_URL", raising=False)
    assert _notify_url_from_environment() is None
    monkeypatch.setenv("FLEETMON_NOTIFY_URL", "https://ntfy/topic")
    assert _notify_url_from_environment() == "https://ntfy/topic"
    monkeypatch.setenv("FLEETMON_NOTIFY_URL", "gopher://bad")
    with pytest.raises(ConfigError):
        _notify_url_from_environment()


def test_config_errors_never_contain_the_url(monkeypatch):
    from fleetmon.config import _notify_url_from_environment

    secret = "gopher://secret-value-leak"
    monkeypatch.setenv("FLEETMON_NOTIFY_URL", secret)
    with pytest.raises(ConfigError) as excinfo:
        _notify_url_from_environment()
    assert secret not in str(excinfo.value)
