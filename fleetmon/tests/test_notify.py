import pytest

from fleetmon import notify
from fleetmon.config import ConfigError
from fleetmon.database import Database


def test_outbox_digest_respects_the_minute_delivery_cap(tmp_path):
    db = Database(tmp_path / "notify.db")
    sent = []

    def poster(_url, payload):
        sent.append(payload)
        return "ok"

    try:
        for index in range(6):
            db.enqueue_notification(f"incident:{index}",
                                    {"event": "gpu_free", "target": f"gpu{index}"}, 100.0)
        assert notify.dispatch_outbox(db, "https://example.invalid/notify", poster, 100.0) == 1
        assert len(sent) == 1 and sent[0]["event"] == "notification_digest"
        db.enqueue_notification("incident:later", {"event": "gpu_free", "target": "later"}, 101.0)
        assert notify.dispatch_outbox(db, "https://example.invalid/notify", poster, 101.0) == 0
        assert len(sent) == 1
        assert notify.dispatch_outbox(db, "https://example.invalid/notify", poster, 161.0) == 1
        assert len(sent) == 2
    finally:
        db.close()


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


def test_notification_projection_drops_unapproved_fields_and_secrets():
    projected = notify.safe_payload(
        {
            "event": "poll_failures",
            "target": "gpu1",
            "failures": 4,
            "error": "ssh -i /secret/key TOKEN=abc",
            "command": "full command",
            "token": "abc",
        }
    )
    assert projected == {
        "event": "poll_failures",
        "target": "gpu1",
        "failures": 4,
        "error": "unknown",
    }


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


def _health_cycle(failures, last_error, skew, notified, poster=None):
    """One hub cycle: evaluate, deliver, persist exactly like the service."""

    events, updated = notify.evaluate_host_health(failures, last_error, skew, notified)
    if not events:
        return events, updated  # the service persists the (possibly cleared) map
    delivered, _retry = notify.notify_host_events(
        "ada1",
        events,
        updated,
        0.0,
        100.0,
        "https://ntfy/topic",
        poster or (lambda url, payload: "ok"),
    )
    return events, delivered


def test_health_poll_failure_streak_fires_once_and_rearms_after_success():
    calls = []
    events, notified = _health_cycle(
        3,
        "transport",
        None,
        {},
        lambda u, p: calls.append(p) or "ok",
    )
    assert [e["event"] for e in events] == ["poll_failures"]
    assert events[0]["failures"] == 3 and events[0]["error"] == "transport"
    assert notified["poll_failures"] == "sent" and len(calls) == 1

    # A fourth failure while already notified emits and delivers nothing new.
    events, notified = _health_cycle(4, "transport", None, notified)
    assert events == [] and len(calls) == 1

    # Success clears the streak and re-arms the alert.
    events, notified = _health_cycle(0, None, None, notified)
    assert events == [] and "poll_failures" not in notified
    events, notified = _health_cycle(
        3, "timeout", None, notified, lambda u, p: calls.append(p) or "ok"
    )
    assert [e["event"] for e in events] == ["poll_failures"]
    assert events[0]["error"] == "timeout" and len(calls) == 2


def test_health_failure_streak_below_threshold_never_fires():
    events, notified = _health_cycle(1, "transport", None, {})
    assert events == [] and notified == {}
    events, notified = _health_cycle(2, "transport", None, notified)
    assert events == [] and notified == {}
    # A recovered streak without reaching the threshold leaves nothing armed.
    events, notified = _health_cycle(0, None, None, notified)
    assert events == [] and notified == {}


def test_health_clock_drift_fires_once_and_rearms_within_threshold():
    events, notified = _health_cycle(0, None, 2411.9, {})
    assert [e["event"] for e in events] == ["clock_drift"]
    assert events[0]["skew_seconds"] == 2411.9
    assert notified["clock_drift"] == "sent"

    # Drift persists (even worsening): exactly one notification.
    events, notified = _health_cycle(0, None, -2500.0, notified)
    assert events == []

    # No new sample (failed poll) must neither fire nor clear the flag.
    events, notified = _health_cycle(0, None, None, notified)
    assert events == [] and notified.get("clock_drift") == "sent"

    # Back within the threshold clears and re-arms.
    events, notified = _health_cycle(0, None, 119.9, notified)
    assert events == [] and "clock_drift" not in notified
    events, _ = _health_cycle(0, None, -120.0, notified)
    assert [e["event"] for e in events] == ["clock_drift"]


def test_health_boundary_is_inclusive_and_events_can_combine():
    events, _ = notify.evaluate_host_health(
        notify.POLL_FAILURES_NOTIFY, "timeout", -notify.CLOCK_DRIFT_SECONDS, {}
    )
    assert [e["event"] for e in events] == ["poll_failures", "clock_drift"]


def test_health_failed_delivery_retries_on_a_later_cycle():
    attempts = []

    def failing(url, payload):
        attempts.append(payload)
        return "notify_unreachable"

    events, notified = _health_cycle(3, "transport", None, {}, failing)
    assert notified["poll_failures"] == "notify_unreachable"
    first_attempt = len(attempts)

    # While backed off, nothing is re-sent; after the window, delivery retries.
    notified, retry = notify.notify_host_events(
        "ada1",
        events,
        notified,
        retry if (retry := 100.0 + notify.NOTIFY_RETRY_SECONDS) else 0.0,
        150.0,
        "https://ntfy/topic",
        failing,
    )
    assert len(attempts) == first_attempt  # still inside the backoff window
    notified, _retry = notify.notify_host_events(
        "ada1",
        events,
        notified,
        retry,
        retry + 1.0,
        "https://ntfy/topic",
        lambda u, p: attempts.append(p) or "ok",
    )
    assert notified["poll_failures"] == "sent" and len(attempts) == first_attempt + 1


def test_host_delivery_sends_once_and_backs_off_on_failure():
    calls = []

    def poster(url, payload):
        calls.append(payload)
        return "ok"

    events = [{"event": "clock_drift", "skew_seconds": 2400.0}]
    notified, retry = notify.notify_host_events(
        "ada1", events, {}, 0.0, 100.0, "https://ntfy/topic", poster
    )
    assert notified["clock_drift"] == "sent" and retry == 0.0 and len(calls) == 1
    assert calls[0] == {
        "target": "ada1",
        "event": "clock_drift",
        "skew_seconds": 2400.0,
    }

    def failing(url, payload):
        return "notify_unreachable"

    events = [
        {"event": "poll_failures", "failures": 3, "error": "transport"},
        {"event": "clock_drift", "skew_seconds": 2400.0},
    ]
    notified, retry = notify.notify_host_events(
        "ada1", events, {}, 0.0, 100.0, "https://ntfy/topic", failing
    )
    assert notified["poll_failures"] == "notify_unreachable"
    assert "clock_drift" not in notified  # delivery stops after first failure
    assert retry == 100.0 + notify.NOTIFY_RETRY_SECONDS
    notified, retry = notify.notify_host_events(
        "ada1", events, notified, retry, retry + 1, "https://ntfy/topic", failing
    )
    assert notified["poll_failures"] == "notify_unreachable"
    assert "clock_drift" not in notified  # the failed event is retried first
    notified, retry = notify.notify_host_events(
        "ada1", events, notified, retry, retry + 1, "https://ntfy/topic", poster
    )
    assert notified["poll_failures"] == "sent"
    assert notified["clock_drift"] == "sent"  # later events follow in the same pass


def test_host_delivery_honors_backoff_window():
    def poster(url, payload):
        raise AssertionError("must not deliver while backed off")

    notified, retry = notify.notify_host_events(
        "ada1",
        [{"event": "poll_failures", "failures": 3, "error": "transport"}],
        {},
        200.0,
        150.0,
        "https://ntfy/topic",
        poster,
    )
    assert notified == {} and retry == 200.0
