"""Full lifecycle cycle against the local-simulation runtime: sync → start → status → ps → logs → stop."""

from __future__ import annotations

from conftest import parse_json


def test_lifecycle_cycle(run):
    parse_json(run("inventory", "sync", "--machine", "gpu-a"))
    started = parse_json(run("start", "qwen-72b", "--machine", "gpu-a"))
    assert started["ok"] is True
    assert started["state"] == "READY"
    assert started["endpoint"].startswith("http://127.0.0.1:")
    assert started["simulation"] is True

    st = parse_json(run("status", "qwen-72b", "--machine", "gpu-a"))
    assert st["STATE"] == "READY"
    assert st["VLLM"] == "healthy"

    ps = parse_json(run("ps"))
    assert any(t["target_id"] == "qwen-72b@gpu-a" and t["live"] for t in ps["targets"])

    logs = parse_json(run("logs", "qwen-72b", "--machine", "gpu-a"))
    assert "listening" in logs["log"]

    stopped = parse_json(run("stop", "qwen-72b", "--machine", "gpu-a"))
    assert stopped["state"] == "STOPPED"

    # idempotent stop
    again = parse_json(run("stop", "qwen-72b", "--machine", "gpu-a"))
    assert again["idempotent"] is True


def test_status_unknown_target(run):
    res = run("--json", "status", "ghost", "--machine", "gpu-a", expect_success=False)
    assert res.exit_code == 1
    assert parse_json(res)["code"] == "E_TARGET_NOT_FOUND"
