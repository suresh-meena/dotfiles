from __future__ import annotations

import json

import pytest

from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from research_kb.service.context import open_service
from tests.conftest import apply_auto


def enable_execution(project_root, adapters=("import_only",)):
    from research_kb.config import load_policy, save_policy

    routing = load_routing(project_root)
    policy = load_policy(routing.state_dir)
    policy["execution"]["enabled"] = True
    policy["execution"]["adapters"] = list(adapters)
    save_policy(routing.state_dir, policy)


def load_routing(root):
    from research_kb.config import load_routing as load

    routing = load(root)
    assert routing is not None
    return routing


def capture_executable_study(api: ResearchKB, *, resources=None):
    study = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "study",
                    "subkind": "numerical",
                    "title": "Resolution study",
                    "state_json": {
                        "subkind": "numerical",
                        "question": "How does the observable scale with spacing?",
                        "protocol": {"method_profile": "numerical", "protocol_revision": "p1"},
                        "required_outputs": [{"name": "observable"}],
                        "completion_criteria": ["all required slots covered"],
                        "study_state": "planned",
                        "executable": True,
                        "trial_design": {
                            "factors": {"a": [0.1, 0.05]},
                            "constraints": [],
                        },
                    },
                },
            }
        ],
    )
    return study["created"][0]["object_id"]


def capture_resource(api: ResearchKB, *, capacity=1):
    resource = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "resource",
                    "subkind": "machine",
                    "title": "Test machine",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "machine",
                        "machine_id": "test-host",
                        "capabilities": {"cpu": 8},
                        "capacity": capacity,
                        "admin_state": "enabled",
                        "limitations": ["cpu only"],
                    },
                },
            }
        ],
    )
    return resource["created"][0]["object_id"]


def test_execution_disabled_by_default(api: ResearchKB):
    with pytest.raises(KBError) as error:
        api.execute(
            operation="execute_prepare",
            payload={"study_ref": {"object_id": "x"}, "trial": {"conditions": {}}},
        )
    assert error.value.code == "CAPABILITY_UNAVAILABLE"


def test_trial_design_expansion_cap():
    from research_kb.execution.slots import expand_design

    rows = expand_design({"factors": {"a": [1, 2], "b": ["x", "y"]}})
    assert len(rows) == 4
    with pytest.raises(KBError):
        expand_design({"factors": {"a": list(range(200)), "b": list(range(200))}}, cap=100)
    constrained = expand_design(
        {
            "factors": {"a": [1, 2], "b": ["x", "y"]},
            "constraints": [{"field": "b", "op": "in", "values": ["x"]}],
        }
    )
    assert len(constrained) == 2


def test_prepare_launch_reconcile_flow(project_root, api: ResearchKB, contributor: ResearchKB):
    enable_execution(project_root)
    study_id = capture_executable_study(api)
    resource_id = capture_resource(api)
    with ResearchKB(open_service(project_root=project_root, actor_id="admin")) as admin:
        proposal = admin.propose(
            operations=[
                {
                    "op": "execute_prepare",
                    "payload": {
                        "study_ref": {"object_id": study_id},
                        "trial": {
                            "conditions": {"a": 0.1},
                            "method_profile": "numerical",
                            "adapter": "import_only",
                            "code_identity": "git:deadbeef",
                            "environment_identity": "python-3.14",
                            "inputs_identity": "dataset-sha256:abc",
                            "resources": [{"resource_id": resource_id, "ttl_seconds": 3600}],
                        },
                    },
                }
            ],
            persist=True,
        )
        assert proposal["proposal"]["requires_approval"] is True
        approval = admin.approve(proposal_id=proposal["proposal"]["proposal_id"])
        applied = admin.apply(
            proposal_id=proposal["proposal"]["proposal_id"],
            approval_token=approval["approval_token"],
        )
        assert applied["committed"] is True
        prepared = applied["result"]["details"]["execute_prepare"][0]
        execution_id = prepared["execution_id"]
        assert prepared["phase"] == "prepared"
        assert prepared["leases"][0]["state"] == "active"
        launch_proposal = admin.propose(
            operations=[
                {
                    "op": "execute_launch",
                    "payload": {"execution_id": execution_id, "adapter": "import_only"},
                }
            ],
            persist=True,
        )
        launch_approval = admin.approve(proposal_id=launch_proposal["proposal"]["proposal_id"])
        launched = admin.apply(
            proposal_id=launch_proposal["proposal"]["proposal_id"],
            approval_token=launch_approval["approval_token"],
        )
        assert launched["result"]["details"]["execute_launch"][0]["phase"] == "accepted"
        reconcile_proposal = admin.propose(
            operations=[
                {
                    "op": "execute_reconcile",
                    "payload": {
                        "execution_id": execution_id,
                        "phase": "terminal",
                        "payload": {
                            "status": "completed",
                            "validity": "valid",
                            "termination_verified": True,
                        },
                        "external_ref": "import:done",
                    },
                }
            ],
            persist=True,
        )
        reconciled = admin.apply(proposal_id=reconcile_proposal["proposal"]["proposal_id"])
        detail = reconciled["result"]["details"]["execute_reconcile"][0]
        assert detail["phase"] == "terminal"
        leases = admin.ctx.conn.execute(
            "SELECT state FROM resource_leases WHERE owner_execution_id = ?", (execution_id,)
        ).fetchall()
        assert all(lease["state"] == "released" for lease in leases)
        assert detail["partial_outputs_preserved"] is False


def test_lease_exclusivity_and_quarantine(project_root, api: ResearchKB):
    enable_execution(project_root)
    resource_id = capture_resource(api)
    from research_kb.errors import KBError as Error
    from research_kb.execution import leases
    from research_kb.storage.db import write_tx

    with write_tx(api.ctx.conn):
        first = leases.acquire_lease(
            api.ctx.conn,
            project_id=api.ctx.project_id,
            resource_id=resource_id,
            execution_id="exec-1",
            seq=api.ctx.latest_seq(),
            ttl_seconds=1,
        )
    with pytest.raises(Error) as error:
        with write_tx(api.ctx.conn):
            leases.acquire_lease(
                api.ctx.conn,
                project_id=api.ctx.project_id,
                resource_id=resource_id,
                execution_id="exec-2",
                seq=api.ctx.latest_seq(),
                ttl_seconds=1,
            )
    assert error.value.code == "BLOCKED"
    with write_tx(api.ctx.conn):
        api.ctx.conn.execute(
            "UPDATE resource_leases SET expires_at = '2000-01-01T00:00:00.000000Z' WHERE lease_id = ?",
            (first["lease_id"],),
        )
        quarantined = leases.quarantine_expired(api.ctx.conn, api.ctx.project_id)
        assert first["lease_id"] in quarantined
        released = leases.release_lease(
            api.ctx.conn,
            project_id=api.ctx.project_id,
            lease_id=first["lease_id"],
            seq=api.ctx.latest_seq(),
            verified_termination=False,
        )
        assert released["state"] == "quarantined"


def test_coverage_counts_required_slots(project_root, api: ResearchKB):
    enable_execution(project_root)
    study_id = capture_executable_study(api)
    from research_kb.execution.slots import coverage

    study_row = api.ctx.conn.execute(
        "SELECT current_revision FROM objects WHERE object_id = ?", (study_id,)
    ).fetchone()
    rows = coverage(api.ctx.conn, api.ctx.project_id, study_id, study_row["current_revision"])
    assert rows["denominator"] == 0


def refresh_policy(api: ResearchKB) -> None:
    from research_kb.config import load_policy, policy_hash

    api.ctx.policy = load_policy(api.ctx.state_dir)
    api.ctx.policy_revision = policy_hash(api.ctx.policy)


def _direct_execution(api: ResearchKB, operation: str, payload: dict) -> dict:
    from research_kb.service.operations import apply_operations

    batch = apply_operations(
        api.ctx,
        operations=[{"op": operation, "payload": payload}],
        reason="failure-injection test",
        action="proposal_apply",
    )
    return batch.details[operation][0]


def test_duplicate_terminal_receipt_is_deduplicated(project_root, api: ResearchKB):
    enable_execution(project_root)
    refresh_policy(api)
    study_id = capture_executable_study(api)
    prepared = _direct_execution(
        api,
        "execute_prepare",
        {
            "study_ref": {"object_id": study_id},
            "trial": {
                "conditions": {"a": 0.2},
                "method_profile": "numerical",
                "adapter": "import_only",
                "code_identity": "git:dd",
                "environment_identity": "env",
                "inputs_identity": "inputs",
            },
        },
    )
    execution_id = prepared["execution_id"]
    _direct_execution(
        api,
        "execute_launch",
        {"execution_id": execution_id, "adapter": "import_only"},
    )
    terminal = {
        "execution_id": execution_id,
        "phase": "terminal",
        "payload": {"status": "completed", "validity": "valid", "termination_verified": True},
        "external_ref": "import:done",
    }
    first = _direct_execution(api, "execute_reconcile", terminal)
    assert first["updated"]["revision"] == 2
    second = _direct_execution(api, "execute_reconcile", terminal)
    assert second["duplicate_receipt"] is True
    assert second["updated"] is None
    run = api.ctx.conn.execute(
        "SELECT current_revision FROM objects WHERE object_id = ?", (first["updated"]["object_id"],)
    ).fetchone()
    assert run["current_revision"] == 2


def test_ambiguous_receipt_keeps_lease_quarantined(project_root, api: ResearchKB):
    enable_execution(project_root)
    refresh_policy(api)
    study_id = capture_executable_study(api)
    resource_id = capture_resource(api)
    prepared = _direct_execution(
        api,
        "execute_prepare",
        {
            "study_ref": {"object_id": study_id},
            "trial": {
                "conditions": {"a": 0.3},
                "method_profile": "numerical",
                "adapter": "import_only",
                "code_identity": "git:ee",
                "environment_identity": "env",
                "inputs_identity": "inputs",
                "resources": [{"resource_id": resource_id}],
            },
        },
    )
    execution_id = prepared["execution_id"]
    _direct_execution(api, "execute_launch", {"execution_id": execution_id, "adapter": "import_only"})
    ambiguous = _direct_execution(
        api,
        "execute_reconcile",
        {
            "execution_id": execution_id,
            "phase": "ambiguous",
            "payload": {"reason": "submission timeout without idempotent support"},
        },
    )
    assert ambiguous["code"] == "EXECUTION_AMBIGUOUS"
    lease = api.ctx.conn.execute(
        "SELECT state FROM resource_leases WHERE owner_execution_id = ?", (execution_id,)
    ).fetchone()
    assert lease["state"] == "active"


def test_late_receipt_reconciles_lost_run_with_new_revision(project_root, api: ResearchKB):
    enable_execution(project_root)
    refresh_policy(api)
    study_id = capture_executable_study(api)
    prepared = _direct_execution(
        api,
        "execute_prepare",
        {
            "study_ref": {"object_id": study_id},
            "trial": {
                "conditions": {"a": 0.4},
                "method_profile": "numerical",
                "adapter": "import_only",
                "code_identity": "git:ff",
                "environment_identity": "env",
                "inputs_identity": "inputs",
            },
        },
    )
    execution_id = prepared["execution_id"]
    run_id = prepared["run"]["object_id"]
    _direct_execution(api, "execute_launch", {"execution_id": execution_id, "adapter": "import_only"})
    lost = _direct_execution(
        api,
        "execute_reconcile",
        {
            "execution_id": execution_id,
            "phase": "terminal",
            "payload": {"status": "lost", "validity": "unknown", "termination_note": "process absent"},
        },
    )
    assert lost["updated"]["revision"] == 2
    late = _direct_execution(
        api,
        "execute_reconcile",
        {
            "execution_id": execution_id,
            "phase": "terminal",
            "payload": {"status": "completed", "validity": "valid", "termination_verified": True},
            "external_ref": "import:late",
        },
    )
    assert late["updated"]["revision"] == 3
    run = api.get([{"object_id": run_id, "history": True}])["records"][0]
    assert [entry["revision"] for entry in run["history"]] == [1, 2, 3]
    assert run["state_json"]["status"] == "completed"
