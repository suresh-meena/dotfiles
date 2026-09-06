"""Bounded asynchronous subprocess polling."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


def _configure_child_watcher() -> None:
    """Use the single-loop SIGCHLD watcher on Python 3.10/3.11 Unix.

    Fleetmon has one event loop. Avoiding the threaded watcher also avoids a
    known failure mode where subprocess transports outlive loop shutdown.
    """

    if sys.platform == "win32" or sys.version_info >= (3, 12):
        return
    policy = asyncio.get_event_loop_policy()
    watcher = policy.get_child_watcher()
    if isinstance(watcher, asyncio.ThreadedChildWatcher):
        policy.set_child_watcher(asyncio.SafeChildWatcher())


_configure_child_watcher()


@dataclass(frozen=True)
class PollResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    overflow: bool = False


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Drain one pipe while retaining at most ``limit`` bytes."""

    data = bytearray()
    while True:
        chunk = await stream.read(min(65_536, limit - len(data) + 1))
        if not chunk:
            return bytes(data), False
        data.extend(chunk)
        if len(data) > limit:
            return bytes(data[:limit]), True


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


async def run_command(
    argv: list[str],
    timeout: float = 20,
    stdout_limit: int = 262_144,
    stderr_limit: int = 65_536,
) -> PollResult:
    """Run explicit argv with a wall timeout and hard output caps.

    A process is killed as soon as either pipe exceeds its limit. The timeout
    continues to apply if one pipe closes early while the process remains alive.
    """

    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("argv must be a non-empty string list")
    if timeout <= 0 or stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("timeout and output limits must be positive")

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_task = asyncio.create_task(_read_limited(process.stdout, stdout_limit))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, stderr_limit))
    wait_task = asyncio.create_task(process.wait())
    pending: set[asyncio.Task[Any]] = {
        stdout_task,
        stderr_task,
        wait_task,
    }
    timed_out = False
    overflow = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    try:
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            done, pending = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                timed_out = True
                break
            for task in done:
                if task is stdout_task or task is stderr_task:
                    _payload, exceeded = task.result()
                    overflow = overflow or exceeded
            if overflow:
                break

        if timed_out or overflow:
            _kill_process_group(process)

            # Killing the group should close both pipes. Bound even this cleanup
            # path for Python versions whose subprocess transports close slowly.
            _done, cleanup_pending = await asyncio.wait(
                {stdout_task, stderr_task, wait_task}, timeout=1.0
            )
            for task in cleanup_pending:
                task.cancel()
            await asyncio.gather(
                stdout_task, stderr_task, wait_task, return_exceptions=True
            )
        else:
            await asyncio.gather(stdout_task, stderr_task, wait_task)
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()
        # Let asyncio deliver final pipe connection_lost callbacks before a
        # short-lived caller closes its event loop (notably Python 3.10).
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        _kill_process_group(process)
        # Bound this path exactly like the timeout path: a grandchild holding
        # the pipes must not extend cancellation cleanup beyond one second.
        # Let the direct child's exit settle first so it is reaped while the
        # loop is still alive, then cancel whatever is still stuck.
        _done, cleanup_pending = await asyncio.wait(
            {stdout_task, stderr_task, wait_task}, timeout=1.0
        )
        for task in cleanup_pending:
            task.cancel()
        await asyncio.gather(
            stdout_task, stderr_task, wait_task, return_exceptions=True
        )
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()
        await asyncio.sleep(0)
        raise

    stdout = stdout_task.result()[0] if not stdout_task.cancelled() else b""
    stderr = stderr_task.result()[0] if not stderr_task.cancelled() else b""
    return PollResult(
        process.returncode,
        stdout,
        stderr,
        timed_out=timed_out,
        overflow=overflow,
    )


class PollController:
    """Enforce fleet-wide concurrency, launch rate, and one poll per target."""

    def __init__(self, concurrency: int = 2, launch_interval: float = 2.0):
        if concurrency < 1 or launch_interval < 0:
            raise ValueError("invalid poll controller limits")
        self.sem = asyncio.Semaphore(concurrency)
        self.launch_interval = launch_interval
        self.polling_enabled = True
        self.disabled_targets: set[str] = set()
        self._active: set[str] = set()
        self._launch_lock = asyncio.Lock()
        self._last_launch = 0.0

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def poll(
        self,
        target: str,
        argv: list[str],
        *,
        timeout: float = 20.0,
        stdout_limit: int = 262_144,
        stderr_limit: int = 65_536,
    ) -> PollResult | None:
        if not self.polling_enabled or target in self.disabled_targets:
            return None
        if target in self._active:
            return None

        self._active.add(target)
        try:
            async with self.sem:
                if not self.polling_enabled or target in self.disabled_targets:
                    return None
                async with self._launch_lock:
                    delay = self.launch_interval - (
                        asyncio.get_running_loop().time() - self._last_launch
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    # This is the final kill-switch check before process creation.
                    if not self.polling_enabled or target in self.disabled_targets:
                        return None
                    self._last_launch = asyncio.get_running_loop().time()

                return await run_command(
                    argv,
                    timeout=timeout,
                    stdout_limit=stdout_limit,
                    stderr_limit=stderr_limit,
                )
        finally:
            self._active.discard(target)


def backoff_seconds(failures: int) -> int:
    return [0, 120, 300, 600, 1200][min(max(failures, 0), 4)]
