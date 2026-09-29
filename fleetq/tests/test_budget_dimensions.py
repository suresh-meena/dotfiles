"""Central permits charge RPCs, control sessions, and transfer bytes atomically."""

from __future__ import annotations

import json

import pytest

from fleetq import budget
from fleetq.db.store import Store
from fleetq.errors import FqError
from fleetq.util import utcnow


@pytest.fixture()
def store(tmp_path):
    db = Store(tmp_path / "budget.db")
    db.open()
    cfg = {"budget": {
        "aliases": ["login-a"], "monitor_per_minute": 20, "action_per_minute": 20,
        "transfer_per_minute": 20, "burst": 20,
        "max_sessions_per_operation": 2, "max_bytes_per_transfer": 1000,
        "sessions_per_minute": 20, "sessions_burst": 20,
        "bytes_per_minute": 1000, "bytes_burst": 1000,
        "action_session_reserve": 2,
    }}
    db.run_sync(lambda c: c.execute(
        "INSERT INTO nodes(id,backend,enabled,config_json,updated_at) VALUES(?,?,?,?,?)",
        ("site", "slurm", 1, json.dumps(cfg), utcnow())))
    yield db
    db.close()


def grant(store, op="monitor", **cost):
    return store.run_sync(lambda c: budget.grant_permit(
        c, cluster="login-a", op_class=op, cost=cost or {"rpc": 1}, caller="test"))


def test_alias_uses_canonical_key_and_records_all_cost_dimensions(store):
    result = grant(store, "transfer", rpc=3, sessions=2, bytes=75)
    assert result["granted"] and result["cluster"] == "site"
    rows = store.run_sync(lambda c: budget.usage(c, "0000-01-01T00:00:00Z"))
    assert rows == [{"target": "site", "op_class": "transfer", "calls": 1,
                     "rpc": 3, "sessions": 2, "bytes": 75}]
    permit = store.run_sync(lambda c: budget.redeem_permit(
        c, result["permit_id"], cluster="login-a", op_class="transfer",
        cost={"rpc": 3, "sessions": 2, "bytes": 75}))
    assert permit["valid"] and permit["canonical"] == "site"


def test_cost_dimensions_are_bounded_and_byte_cost_requires_transfer(store):
    with pytest.raises(FqError):
        grant(store, "monitor", sessions=0)
    with pytest.raises(FqError):
        grant(store, "monitor", rpc=True)
    assert grant(store, "monitor", bytes=1)["reason"] == "transfer_cap"
    assert grant(store, "transfer", bytes=1001)["reason"] == "transfer_cap"


def test_non_action_cannot_spend_the_reserved_action_sessions(store):
    # Leave exactly the configured reserve available through repeated actions.
    for _ in range(9):
        assert grant(store, "action", sessions=2)["granted"]
    denied = grant(store, "monitor", sessions=1)
    assert denied["granted"] is False
    assert denied["reason"] == "action_session_reserve"


def test_denied_multidimension_grant_does_not_spend_rpc_or_sessions(store):
    # First consume enough bytes that this transfer is denied only on bytes.
    assert grant(store, "transfer", rpc=1, sessions=1, bytes=950)["granted"]
    before = store.run_sync(lambda c: (
        c.execute("SELECT tokens FROM budget_buckets WHERE cluster='site' AND op_class='transfer'").fetchone()[0],
        c.execute("SELECT tokens FROM budget_dimension_buckets WHERE cluster='site' AND dimension='sessions'").fetchone()[0]))
    denied = grant(store, "transfer", rpc=4, sessions=2, bytes=100)
    assert denied["granted"] is False and denied["reason"] == "byte_budget_exhausted"
    after = store.run_sync(lambda c: (
        c.execute("SELECT tokens FROM budget_buckets WHERE cluster='site' AND op_class='transfer'").fetchone()[0],
        c.execute("SELECT tokens FROM budget_dimension_buckets WHERE cluster='site' AND dimension='sessions'").fetchone()[0]))
    assert after == before
