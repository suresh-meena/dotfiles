import asyncio
import subprocess

from fleetq.clock import (ClockHealth, ClockProvider, _probe_chrony,
                          _probe_systemd_timesyncd)


def test_initially_unhealthy_and_read_does_not_probe():
    calls = []
    health = ClockHealth(probe=lambda: calls.append(True) or True)

    assert health.provider is ClockProvider.CHRONY
    assert health.read() is False
    assert calls == []


def test_successful_refresh_and_fresh_cached_read():
    calls = []
    health = ClockHealth(probe=lambda: calls.append(True) or True)

    assert asyncio.run(health.refresh()) is True
    assert health.read() is True
    assert calls == [True]


def test_failed_refresh_is_unhealthy():
    health = ClockHealth(probe=lambda: False)

    assert asyncio.run(health.refresh()) is False
    assert health.read() is False


def test_failed_refresh_revokes_previous_healthy_sample():
    responses = iter([True, False])
    health = ClockHealth(probe=lambda: next(responses))
    assert asyncio.run(health.refresh()) is True
    assert asyncio.run(health.refresh()) is False
    assert health.read() is False


def test_chrony_probe_uses_bounded_noninteractive_command(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert _probe_chrony() is True
    assert seen["argv"] == ["chronyc", "-n", "waitsync", "1", "0.5", "1000", "0.1"]
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert seen["kwargs"]["timeout"] == 3


def test_chrony_probe_timeout_is_unhealthy(monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    assert _probe_chrony() is False


def test_timesyncd_probe_requires_active_service_and_synchronized_clock(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 0, stdout=b"yes\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert _probe_systemd_timesyncd() is True
    assert seen[0][0] == ["systemctl", "is-active", "--quiet", "systemd-timesyncd.service"]
    assert seen[1][0] == ["timedatectl", "show", "--property=NTPSynchronized", "--value"]
    assert all(call[1]["timeout"] == 3 for call in seen)


def test_timesyncd_probe_fails_closed_for_inactive_unsynced_or_errors(monkeypatch):
    calls = []

    def inactive(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr(subprocess, "run", inactive)
    assert _probe_systemd_timesyncd() is False
    assert len(calls) == 1

    def unsynced(argv, **kwargs):
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 0, stdout=b"no\n")

    monkeypatch.setattr(subprocess, "run", unsynced)
    assert _probe_systemd_timesyncd() is False

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    assert _probe_systemd_timesyncd() is False


def test_timesyncd_provider_selects_its_probe():
    health = ClockHealth(ClockProvider.SYSTEMD_TIMESYNCD, probe=lambda: True)
    assert health.provider is ClockProvider.SYSTEMD_TIMESYNCD


def test_stale_sample_fails_closed():
    now = [10.0]
    health = ClockHealth(probe=lambda: True, monotonic=lambda: now[0], max_age=90)

    assert asyncio.run(health.refresh()) is True
    now[0] = 100.01
    assert health.read() is False
    now[0] = 9.0
    assert health.read() is False


def test_refresh_loop_can_be_cancelled():
    async def scenario():
        health = ClockHealth(probe=lambda: True, refresh_interval=3600)
        task = asyncio.create_task(health.run())
        await asyncio.sleep(0.02)
        assert health.read() is True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("run task did not propagate cancellation")

    asyncio.run(scenario())
