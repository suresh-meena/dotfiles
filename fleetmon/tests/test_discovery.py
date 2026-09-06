import asyncio
import json
from types import SimpleNamespace

import pytest

import fleetmon.discovery as discovery_module
from fleetmon.discovery import (
    MAX_TARGETS,
    Inventory,
    Protocol,
    discover,
    discover_async,
    parse_inventory,
)
from fleetmon.poller import PollResult


def test_roles_and_protocols_are_safe():
    inv = Inventory(
        parse_inventory(
            [
                {"name": "w", "role": "workstation", "protocol": "p", "enabled": True},
                {"name": "b", "role": "bridge", "protocol": "p", "enabled": True},
                {"name": "l", "role": "login", "protocol": "s", "enabled": True},
            ]
        ),
        {"p": Protocol("p", "direct"), "s": Protocol("s", "slurm")},
    )
    assert [x.name for x in inv.direct_targets] == ["w"]
    assert [x.name for x in inv.scheduler_targets] == ["l"]


def test_discover_resolves_distinct_protocols():
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[0] == "list":
            return [
                {"name": "a", "role": "compute", "protocol": "p"},
                {"name": "b", "role": "compute", "protocol": "p"},
            ]
        return {"name": "p", "kind": "direct"}

    result = discover(runner=run)
    assert len(calls) == 2 and result.direct_targets[1].name == "b"


def test_async_discovery_is_bounded_and_machine_readable(monkeypatch):
    calls = []

    async def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:3] == ["list", "--json"]:
            payload = [{"name": "gpu1", "role": "compute", "protocol": "direct"}]
        else:
            payload = {"name": "direct", "kind": "direct"}
        return PollResult(0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(discovery_module, "run_command", run)
    result = asyncio.run(discover_async("/usr/bin/fleetctl"))
    assert [target.name for target in result.direct_targets] == ["gpu1"]
    assert all(call[1]["stdout_limit"] == 1024 * 1024 for call in calls)


@pytest.mark.parametrize(
    "row",
    [
        {"name": "x", "role": "compute", "protocol": "p", "enabled": "yes"},
        {"name": "x", "role": "compute", "protocol": "p", "tags": [1]},
    ],
)
def test_inventory_rejects_ambiguous_types(row):
    with pytest.raises(ValueError):
        parse_inventory([row])


def test_inventory_target_and_tag_counts_are_bounded():
    target = {"name": "x", "role": "compute", "protocol": "p"}
    assert MAX_TARGETS == 256
    with pytest.raises(ValueError, match="target count"):
        parse_inventory(
            [dict(target, name=str(index)) for index in range(MAX_TARGETS + 1)]
        )
    with pytest.raises(ValueError, match="tags"):
        parse_inventory([dict(target, tags=[str(index) for index in range(65)])])


def test_zero_resolution_budget_records_targets_without_admitting_them():
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[0] == "list":
            return [
                {"name": f"t{index}", "role": "compute", "protocol": f"p{index}"}
                for index in range(10)
            ]
        return {"name": argv[2], "kind": "direct"}

    result = discover(runner=run, resolution_budget=0.0)
    assert len(calls) == 1
    assert len(result.targets) == 10
    assert result.protocols == {}
    assert result.direct_targets == []


def test_exhausted_resolution_budget_stops_midway(monkeypatch):
    clock = {"now": 0.0}

    def monotonic():
        return clock["now"]

    def run(argv):
        if argv[0] == "list":
            return [
                {"name": f"t{index}", "role": "compute", "protocol": f"p{index}"}
                for index in range(10)
            ]
        clock["now"] += 1.0
        return {"name": argv[2], "kind": "direct"}

    monkeypatch.setattr(discovery_module, "time", SimpleNamespace(monotonic=monotonic))
    result = discover(runner=run, resolution_budget=5.0)
    assert set(result.protocols) == {f"p{index}" for index in range(5)}
    assert len(result.direct_targets) == 5


def test_async_resolution_budget_exhaustion_returns_valid_inventory(monkeypatch):
    calls = []

    async def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["list", "--json"]:
            payload = [
                {"name": f"gpu{index}", "role": "compute", "protocol": f"p{index}"}
                for index in range(3)
            ]
        else:
            payload = {"name": argv[2], "kind": "direct"}
        return PollResult(0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(discovery_module, "run_command", run)
    result = asyncio.run(discover_async("/usr/bin/fleetctl", resolution_budget=0.0))
    assert len(calls) == 1
    assert [target.name for target in result.targets] == ["gpu0", "gpu1", "gpu2"]
    assert result.direct_targets == []
