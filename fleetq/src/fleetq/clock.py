"""Cached, fail-closed check that the host clock is synchronized."""

from __future__ import annotations

import asyncio
import subprocess
import time
from enum import Enum
from typing import Callable


class ClockProvider(str, Enum):
    CHRONY = "chrony"
    SYSTEMD_TIMESYNCD = "systemd-timesyncd"


def _probe_chrony() -> bool:
    try:
        result = subprocess.run(
            ["chronyc", "-n", "waitsync", "1", "0.5", "1000", "0.1"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _probe_systemd_timesyncd() -> bool:
    """Require both the timesyncd service and system clock sync state."""
    try:
        service = subprocess.run(
            ["systemctl", "is-active", "--quiet", "systemd-timesyncd.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        if service.returncode != 0:
            return False
        sync = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return sync.returncode == 0 and sync.stdout.strip().lower() == b"yes"


class ClockHealth:
    """Nonblocking cached clock health, refreshed asynchronously."""

    def __init__(
        self,
        provider: ClockProvider = ClockProvider.CHRONY,
        *,
        probe: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_age: float = 90.0,
        refresh_interval: float = 30.0,
    ) -> None:
        self.provider = ClockProvider(provider)
        if probe is not None:
            self._probe = probe
        elif self.provider is ClockProvider.CHRONY:
            self._probe = _probe_chrony
        else:
            self._probe = _probe_systemd_timesyncd
        self._monotonic = monotonic
        self._max_age = max_age
        self._refresh_interval = refresh_interval
        self._healthy = False
        self._checked_at: float | None = None

    def read(self) -> bool:
        checked_at = self._checked_at
        return bool(
            self._healthy
            and checked_at is not None
            and 0 <= self._monotonic() - checked_at <= self._max_age
        )

    async def refresh(self) -> bool:
        try:
            healthy = bool(await asyncio.to_thread(self._probe))
        except Exception:
            healthy = False
        self._healthy = healthy
        self._checked_at = self._monotonic()
        return self.read()

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self._refresh_interval)
