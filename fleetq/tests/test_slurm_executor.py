import json
from types import SimpleNamespace

import pytest

from fleetq.executors.base import AttemptContext, LaunchKind
from fleetq.executors.slurm import SlurmExecutor
from fleetq.transport.fleetctl import FleetctlResult


class Transport:
    async def exec(self, target, argv, **kwargs):
        return FleetctlResult(outcome="ok", may_have_executed=False,
                              stdout=json.dumps({"result": "never_started", "reason": "no_fence"}))


class UnsafeRootTransport:
    async def exec(self, target, argv, **kwargs):
        return FleetctlResult(outcome="ok", may_have_executed=False, stdout='{"ok":false}')

    async def sync_push(self, *args, **kwargs):
        raise AssertionError("sync_push must not run after an unsafe root precheck")


@pytest.mark.asyncio
async def test_submit_budget_estimate_counts_controller_ping_and_sbatch():
    calls = []

    async def permit(target, op_class, rpc, cost):
        calls.append((target, op_class, rpc, cost))
        return True, "permit-test", None

    cfg = SimpleNamespace(nodes=[SimpleNamespace(
        id="cluster", backend="slurm", control_root="/shared/fleetq", fleetctl_target=None, site={})],
        controller={})
    executor = SlurmExecutor(Transport(), cfg, permit=permit)
    ctx = AttemptContext(attempt_id="att_0123456789abcdef01234567", job_id=1, n=1,
                         target="cluster", epoch=2, fleet_id="fleet-test", spec={},
                         spec_digest="sha256:test", launch_op_id="op-test")

    result = await executor.launch(ctx)

    assert result.kind is LaunchKind.NEVER_STARTED
    assert calls == [("cluster", "action", 2, {"rpc": 8, "sessions": 2, "bytes": 0})]


@pytest.mark.asyncio
async def test_stage_rejects_unrelated_root_before_transfer():
    async def permit(target, op_class, rpc, cost):
        return True, "permit-test", None

    cfg = SimpleNamespace(nodes=[SimpleNamespace(
        id="cluster", backend="slurm", control_root="/home/test/.ssh/cache",
        fleetctl_target=None, site={})], controller={})
    executor = SlurmExecutor(UnsafeRootTransport(), cfg, permit=permit)
    ctx = AttemptContext(attempt_id="att_0123456789abcdef01234567", job_id=1, n=1,
                         target="cluster", epoch=2, fleet_id="fleet-test", spec={},
                         spec_digest="sha256:test", launch_op_id="op-test")

    result = await executor.stage(ctx)

    assert result.ok is False and result.reason == "unsafe_control_root"
    assert result.retryable is False
