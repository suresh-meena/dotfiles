import asyncio
import sys
from contextlib import suppress

from fleetmon.poller import PollController, run_command


def test_overflow_kills_without_deadlock():
    async def go():
        return await run_command(
            [sys.executable, "-c", 'print("x"*1000000)'], stdout_limit=32
        )

    result = asyncio.run(asyncio.wait_for(go(), 3))
    assert result.overflow and len(result.stdout) == 32


def test_timeout_kills_group():
    async def go():
        return await run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05
        )

    assert asyncio.run(asyncio.wait_for(go(), 3)).timed_out


def test_kill_switch():
    async def go():
        c = PollController()
        c.polling_enabled = False
        return await c.poll("x", ["false"])

    assert asyncio.run(go()) is None


def test_timeout_still_applies_after_stdout_closes():
    async def go():
        code = "import os,time; os.close(1); time.sleep(5)"
        return await run_command([sys.executable, "-c", code], timeout=0.05)

    result = asyncio.run(asyncio.wait_for(go(), 3))
    assert result.timed_out


def test_kill_switch_is_rechecked_immediately_before_launch():
    async def go():
        controller = PollController(concurrency=1, launch_interval=0.1)
        controller._last_launch = asyncio.get_running_loop().time()
        task = asyncio.create_task(
            controller.poll("gpu1", [sys.executable, "-c", "raise SystemExit(9)"])
        )
        await asyncio.sleep(0.01)
        controller.disabled_targets.add("gpu1")
        return await task

    assert asyncio.run(go()) is None


def test_cancellation_kills_group_with_grandchild_holding_pipes():
    async def go():
        loop = asyncio.get_running_loop()
        code = (
            "import subprocess, sys, time;"
            "subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(30)']);"
            "time.sleep(30)"
        )
        task = asyncio.create_task(
            run_command([sys.executable, "-c", code], timeout=30)
        )
        await asyncio.sleep(1.0)
        started = loop.time()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return loop.time() - started

    elapsed = asyncio.run(asyncio.wait_for(go(), 15))
    assert elapsed < 5


def test_cancellation_completes_when_grandchild_survives_child():
    async def go():
        loop = asyncio.get_running_loop()
        code = (
            "import os, subprocess, sys, time;"
            "grandchild = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(30)'],"
            "start_new_session=True);"
            "print(grandchild.pid, flush=True);"
            "os.close(1);"
            "time.sleep(30)"
        )
        task = asyncio.create_task(
            run_command([sys.executable, "-c", code], timeout=30)
        )
        await asyncio.sleep(1.0)
        started = loop.time()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return loop.time() - started

    elapsed = asyncio.run(asyncio.wait_for(go(), 15))
    assert elapsed < 5
