"""Model override chain: task model_ref > CLI --model > config role model > bin default."""

from __future__ import annotations

import pytest

from conftest import parse_json
from modelctl.delegation.config import DelegationCfg
from modelctl.delegation.router import deterministic_select
from modelctl.errors import ModelctlError
from modelctl.inventory.registry import Registry


@pytest.fixture
def reg(tmp_path):
    r = Registry(db_path=tmp_path / "routing.db")
    r.upsert_delegate_model("opencode-go/deepseek-v4-flash", "opencode-go", "deepseek-v4-flash", "driver", True, "AVAILABLE")
    r.upsert_delegate_model("opencode-go/hy3", "opencode-go", "hy3", "worker", True, "AVAILABLE")
    r.upsert_delegate_model("opencode-go/other", "opencode-go", "other", "unclassified", False, "AVAILABLE")
    return r


def test_explicit_model_wins_even_across_bins(reg):
    sel = deterministic_select(registry=reg, requested_bin="worker", requested_model="opencode-go/deepseek-v4-flash")
    assert sel["model_ref"] == "opencode-go/deepseek-v4-flash"


def test_unknown_model_fails_closed(reg):
    with pytest.raises(ModelctlError) as e:
        deterministic_select(registry=reg, requested_bin="worker", requested_model="opencode-go/nope")
    assert e.value.code == "E_DELEGATE_MODEL_UNAVAILABLE"


def test_disabled_model_refused(reg):
    with pytest.raises(ModelctlError) as e:
        deterministic_select(registry=reg, requested_bin="worker", requested_model="opencode-go/other")
    assert e.value.code == "E_DELEGATION_POLICY_DENIED"


def test_unavailable_model_refused(tmp_path):
    r = Registry(db_path=tmp_path / "u.db")
    r.upsert_delegate_model("opencode-go/down", "opencode-go", "down", "worker", True, "UNAVAILABLE")
    with pytest.raises(ModelctlError) as e:
        deterministic_select(registry=r, requested_bin="worker", requested_model="opencode-go/down")
    assert e.value.code == "E_DELEGATE_MODEL_UNAVAILABLE"


def test_config_role_model():
    cfg = DelegationCfg.from_config({"delegation": {"roles": {"worker": {"model": "opencode-go/hy3-mini"}}}})
    assert cfg.role("worker").model == "opencode-go/hy3-mini"
    assert cfg.role("driver").model is None


def test_cli_assign_and_list(run):
    res = run("--json", "delegates", "assign", "opencode-go/x", "--bin", "worker", expect_success=False)
    assert parse_json(res)["code"] == "E_DELEGATE_MODEL_UNAVAILABLE"

    from modelctl.inventory.registry import default_db

    reg = Registry(db_path=default_db())
    reg.upsert_delegate_model("opencode-go/x", "opencode-go", "x", "unclassified", False, "AVAILABLE")

    data = parse_json(run("delegates", "assign", "opencode-go/x", "--bin", "worker"))
    assert data == {"ok": True, "model_ref": "opencode-go/x", "bin": "worker", "enabled": True}

    listed = parse_json(run("delegates", "list", "--bin", "worker"))["models"]
    assert any(m["model_ref"] == "opencode-go/x" and m["enabled"] for m in listed)
