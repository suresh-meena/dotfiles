from __future__ import annotations

from conftest import parse_json


def test_help_lists_groups(run):
    res = run("--help")
    for name in ("start", "stop", "status", "inventory", "machines", "targets", "models", "delegate", "delegates", "queue", "budget", "gpu"):
        assert name in res.output


def test_version(run):
    data = parse_json(run("version"))
    assert data["ok"] is True
    assert "version" in data


def test_config_validate(run):
    data = parse_json(run("config", "validate"))
    assert data["ok"] is True


def test_config_resolve(run):
    data = parse_json(run("config", "resolve", "--target", "qwen-72b@gpu-a"))
    assert data["ok"] is True
    assert data["config_digest"]


def test_error_envelope_shape(run):
    res = run("--json", "config", "resolve", "--target", "nope@gpu-a", expect_success=False)
    assert res.exit_code == 1
    data = parse_json(res)
    assert data["ok"] is False
    assert data["code"] == "E_TARGET_NOT_FOUND"
    assert "suggested_action" in data
