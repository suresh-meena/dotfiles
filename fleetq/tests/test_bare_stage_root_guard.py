import asyncio
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path

import pytest

from fleetq.executors.bare import BareExecutor, NODE_LAUNCHER
from fleetq.executors.base import AttemptContext
from fleetq.transport.fleetctl import FleetctlResult


def _ctx(target="ws"):
    return AttemptContext(
        attempt_id="att_0123456789abcdef01234567", job_id=1, n=1, target=target,
        epoch=1, fleet_id="fleet-a", spec={"command": {"argv": ["true"]}, "env": {},
                                            "control": {"kill_grace_s": 1}, "workdir": {}},
        spec_digest="digest", launch_op_id="launch", resources={"mem_mb": 128, "cpus": 1, "time_s": 10},
    )


@pytest.mark.parametrize("root_kind", ["sentinel", "home"])
def test_stage_root_precheck_blocks_sync_push(tmp_path, monkeypatch, root_kind):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if root_kind == "sentinel":
        root = tmp_path / "existing-sentinel"
        root.mkdir()
        (root / "important.txt").write_text("keep")
    else:
        root = home

    class Transport:
        def __init__(self):
            self.sync_calls = []

        async def exec(self, target, argv, *, timeout, mutation, expected_role):
            assert expected_role == "workstation"
            assert argv[:3] == ["sh", "-c", argv[2]]
            return FleetctlResult("ok", False, stdout=json.dumps({"ok": False, "error": "unsafe_root"}))

        async def sync_push(self, *args, **kwargs):
            self.sync_calls.append((args, kwargs))
            return FleetctlResult("ok", False)

    transport = Transport()
    node = SimpleNamespace(id="ws", backend="bare", control_root=str(root), fleetctl_target="ws")
    executor = BareExecutor(transport, SimpleNamespace(nodes=[node]))
    result = asyncio.run(executor.stage(_ctx()))

    assert not result.ok and not result.retryable
    assert result.reason == "unsafe_control_root"
    assert transport.sync_calls == []
    if root_kind == "sentinel":
        assert (root / "important.txt").read_text() == "keep"


def test_node_launcher_disables_bytecode_before_import():
    assert "sys.dont_write_bytecode = True" in NODE_LAUNCHER
    assert "pycache_prefix" not in NODE_LAUNCHER
    root = "/control"
    shim = f"{root}/bin/fq-node"
    node = SimpleNamespace(id="ws", backend="bare", control_root=root, fleetctl_target="ws")
    executor = BareExecutor(object(), SimpleNamespace(nodes=[node]))
    argv = executor._shim("ws", "status", "att_1")
    assert argv[4:] == ["--root", root, "status", "att_1"]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shim.py"
        path.write_text("import json,sys\ndef main():\n print(json.dumps(sys.argv)); return 0\n")
        result = subprocess.run([sys.executable, "-c", NODE_LAUNCHER, str(path), "--root", root, "status"],
                                check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == [str(path), "--root", root, "status"]
