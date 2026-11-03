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


def test_timeout_still_applies_after_stdout_closes():
    async def go():
        code = "import os,time; os.close(1); time.sleep(5)"
        return await run_command([sys.executable, "-c", code], timeout=0.05)

    result = asyncio.run(asyncio.wait_for(go(), 3))
    assert result.timed_out


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


def test_a_machine_is_never_called_more_than_its_hourly_cap():
    async def scenario():
        controller = PollController(concurrency=1, launch_interval=0, max_calls_per_hour=3)
        argv = [sys.executable, "-c", "pass"]
        # "host1" and "host1:squeue" are the same machine, so they share one allowance.
        results = [
            await controller.poll("host1" if i % 2 else "host1:squeue", argv) for i in range(5)
        ]
        other = await controller.poll("host2", argv)
        return controller, results, other

    controller, results, other = asyncio.run(scenario())
    assert [result is not None for result in results] == [True, True, True, False, False]
    assert other is not None, "another machine keeps its own allowance"
    assert controller.rate_limited == {"host1": 2}
