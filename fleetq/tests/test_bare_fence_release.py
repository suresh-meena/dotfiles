"""Release uses the epoch actually accepted by each remote node."""

import asyncio
import json
from types import SimpleNamespace

from fleetq.executors.bare import BareExecutor
from fleetq.transport.fleetctl import FleetctlResult


def test_release_uses_accepted_fence_epoch_per_target():
    class Transport:
        def __init__(self):
            self.calls = []

        async def exec(self, target, argv, *, timeout, mutation, expected_role):
            assert expected_role == "workstation"
            self.calls.append((target, argv, mutation))
            if "fence" in argv:
                body = {"schema": "fq-node/v1", "ok": True, "accepted": True,
                        "highest_epoch_seen": 0, "attempts": []}
            else:
                body = {"schema": "fq-node/v1", "ok": True, "released": True}
            return FleetctlResult("ok", False, stdout=json.dumps(body))

    transport = Transport()
    nodes = [SimpleNamespace(id=name, backend="bare", control_root=f"/control/{name}",
                             fleetctl_target=name) for name in ("ws1", "ws2")]
    executor = BareExecutor(transport, SimpleNamespace(nodes=nodes))

    async def exercise():
        assert (await executor.fence("ws1", epoch=3, fleet_id="fleet-a")).accepted
        assert (await executor.fence("ws2", epoch=4, fleet_id="fleet-a")).accepted
        assert (await executor.release("ws1", "att_0123456789abcdef01234567")).released
        assert (await executor.release("ws2", "att_0123456789abcdef01234567")).released

    asyncio.run(exercise())
    releases = [(target, argv, mutation) for target, argv, mutation in transport.calls if "release" in argv]
    assert [argv[argv.index("--epoch") + 1] for _, argv, _ in releases] == ["3", "4"]
    assert all(mutation for _, _, mutation in releases)


def test_cache_pin_release_uses_explicit_epoch_and_command():
    class Transport:
        def __init__(self):
            self.calls = []

        async def exec(self, target, argv, *, timeout, mutation, expected_role):
            assert expected_role == "workstation"
            self.calls.append((target, argv, mutation))
            body = {"schema": "fq-node/v1", "ok": True, "released": True}
            return FleetctlResult("ok", False, stdout=json.dumps(body))

    transport = Transport()
    node = SimpleNamespace(id="ws1", backend="bare", control_root="/control/ws1", fleetctl_target="ws1")
    executor = BareExecutor(transport, SimpleNamespace(nodes=[node]))

    async def exercise():
        assert await executor.release_cache_pin("ws1", "att_0123456789abcdef01234567", epoch=8)

    asyncio.run(exercise())
    target, argv, mutation = transport.calls[0]
    assert target == "ws1" and mutation
    assert "cache-release" in argv
    assert argv[argv.index("--epoch") + 1] == "8"
