from __future__ import annotations

from conftest import parse_json


def test_budget_status(run):
    data = parse_json(run("budget", "status"))
    assert data["soft_daily_usd"] == 5
    assert data["hard_daily_usd"] == 10


def test_queue_and_delegate_status(run):
    q = parse_json(run("queue", "status"))
    assert q["ok"] is True
    d = parse_json(run("delegate", "status"))
    assert d["ok"] is True
    assert isinstance(d["runs"], list)


def test_delegates_list_empty_ok(run):
    data = parse_json(run("delegates", "list"))
    assert data["ok"] is True
