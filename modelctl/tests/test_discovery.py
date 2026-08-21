from __future__ import annotations

from conftest import parse_json


def test_machines_list(run):
    data = parse_json(run("machines", "list"))
    ids = [m["machine_id"] for m in data["machines"]]
    assert "gpu-a" in ids


def test_targets_list(run):
    data = parse_json(run("targets", "list"))
    tids = [t["target_id"] for t in data["targets"]]
    assert "qwen-72b@gpu-a" in tids


def test_models_list(run):
    data = parse_json(run("models", "list"))
    assert any(m["model"] == "qwen-72b" for m in data["models"])


def test_inventory_sync_and_list(run):
    data = parse_json(run("inventory", "sync", "--machine", "gpu-a"))
    assert data["verified"] >= 1
    arts = parse_json(run("inventory", "list"))["artifacts"]
    assert any(a["current_status"] == "AVAILABLE" for a in arts)
