from __future__ import annotations

import pytest

from modelctl.errors import ModelctlError
from modelctl.inventory.registry import Registry
from modelctl.tunnel import TunnelManager
from unittest.mock import MagicMock, patch


def _manager(tmp_path):
    return TunnelManager(Registry(tmp_path / "modelctl.db"))


def test_explicit_tunnel_port_is_stable_and_idempotent(tmp_path):
    manager = _manager(tmp_path)
    first = manager.connect(
        target_id="qwen38@rtx3090",
        machine_id="rtx3090",
        ssh_host="localhost",
        ssh_user=None,
        ssh_port=None,
        remote_port=8000,
        local_port=8000,
    )
    second = manager.connect(
        target_id="qwen38@rtx3090",
        machine_id="rtx3090",
        ssh_host="localhost",
        ssh_user=None,
        ssh_port=None,
        remote_port=8000,
        local_port=8000,
    )

    assert first["local_port"] == 8000
    assert second["idempotent"] is True
    assert second["local_port"] == 8000


def test_explicit_tunnel_port_cannot_be_shared(tmp_path):
    manager = _manager(tmp_path)
    manager.connect(
        target_id="qwen38@rtx3090",
        machine_id="rtx3090",
        ssh_host="localhost",
        ssh_user=None,
        ssh_port=None,
        remote_port=8000,
        local_port=8000,
    )

    with pytest.raises(ModelctlError) as exc:
        manager.connect(
            target_id="other@rtx3090",
            machine_id="rtx3090",
            ssh_host="localhost",
            ssh_user=None,
            ssh_port=None,
            remote_port=8001,
            local_port=8000,
        )
    assert "already owned" in exc.value.message


def test_dead_tunnel_is_replaced_and_binds_ipv4(tmp_path):
    manager = _manager(tmp_path)
    manager.registry.upsert_tunnel("dead", "qwen38@rtx3090", "rtx3090", 8000, 8000, 12345, "ACTIVE")
    process = MagicMock(pid=67890)
    process.poll.return_value = None
    with patch("modelctl.tunnel.os.kill", side_effect=ProcessLookupError), \
         patch("modelctl.tunnel.subprocess.Popen", return_value=process) as spawn, \
         patch("modelctl.tunnel.time.sleep"):
        result = manager.connect(target_id="qwen38@rtx3090", machine_id="rtx3090",
                                 ssh_host="gpu.test", ssh_user=None, ssh_port=None,
                                 remote_port=8000, local_port=8000)
    assert result["pid"] == 67890
    assert "127.0.0.1:8000:127.0.0.1:8000" in spawn.call_args.args[0]
    assert "ServerAliveInterval=15" in spawn.call_args.args[0]
    assert [t["tunnel_id"] for t in manager.registry.list_tunnels()] == [result["tunnel_id"]]
