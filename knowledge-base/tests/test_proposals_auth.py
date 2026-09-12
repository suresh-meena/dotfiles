from __future__ import annotations

import pytest

from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from tests.conftest import apply_auto


def test_high_risk_proposal_requires_approval(api: ResearchKB, reviewer: ResearchKB):
    claim = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "claim",
                    "subkind": "claim",
                    "title": "Approval claim",
                    "state_json": {
                        "subkind": "claim",
                        "statement": "X holds.",
                        "domain_applicability": {},
                        "evidence_criteria": [{"criterion": "derivation"}],
                    },
                },
            }
        ],
    )
    claim_id = claim["created"][0]["object_id"]
    stored = reviewer.propose(
        operations=[
            {
                "op": "assess_evidence",
                "payload": {
                    "ref": {"object_id": claim_id, "revision": 1},
                    "evidence_state": "supported",
                    "review_state": "reviewed",
                    "rationale": "Derivation checked.",
                    "assessed_revision": 1,
                },
            }
        ],
        persist=True,
    )
    proposal = stored["proposal"]
    assert proposal["requires_approval"] is True
    assert proposal["persisted"] is True
    with pytest.raises(KBError) as error:
        reviewer.apply(proposal_id=proposal["proposal_id"])
    assert error.value.code == "APPROVAL_REQUIRED"
    approval = reviewer.approve(proposal_id=proposal["proposal_id"])
    applied = reviewer.apply(
        proposal_id=proposal["proposal_id"], approval_token=approval["approval_token"]
    )
    assert applied["committed"] is True
    claim_after = api.get([{"object_id": claim_id}])["records"][0]
    assert claim_after["state_json"]["assessment"]["evidence_state"] == "supported"


def test_approval_is_not_reusable(api: ResearchKB, reviewer: ResearchKB):
    claim = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "claim",
                    "subkind": "claim",
                    "title": "Single-use approval",
                    "state_json": {
                        "subkind": "claim",
                        "statement": "Y holds.",
                        "domain_applicability": {},
                        "evidence_criteria": [{"criterion": "derivation"}],
                    },
                },
            }
        ],
    )
    claim_id = claim["created"][0]["object_id"]
    stored = reviewer.propose(
        operations=[
            {
                "op": "assess_evidence",
                "payload": {
                    "ref": {"object_id": claim_id, "revision": 1},
                    "evidence_state": "provisional",
                    "review_state": "reviewed",
                    "rationale": "Initial.",
                    "assessed_revision": 1,
                },
            }
        ],
        persist=True,
    )
    proposal_id = stored["proposal"]["proposal_id"]
    approval = reviewer.approve(proposal_id=proposal_id)
    reviewer.apply(proposal_id=proposal_id, approval_token=approval["approval_token"])
    with pytest.raises(KBError) as error:
        reviewer.apply(proposal_id=proposal_id, approval_token=approval["approval_token"])
    assert error.value.code in ("APPROVAL_STALE", "SCHEMA_VALIDATION_FAILED")


def test_bound_version_change_invalidates_apply(api: ResearchKB):
    from tests.test_work_evidence import capture_work

    work = capture_work(api, title="Bound work")
    object_id = work["created"][0]["object_id"]
    stored = api.propose(
        operations=[
            {
                "op": "set_work_state",
                "payload": {"ref": {"object_id": object_id, "revision": 1}, "to_state": "in_progress"},
            }
        ],
        persist=True,
    )
    proposal_id = stored["proposal"]["proposal_id"]
    apply_auto(
        api,
        [
            {
                "op": "revise",
                "payload": {"ref": {"object_id": object_id, "revision": 1}, "title": "Changed work"},
            }
        ],
    )
    with pytest.raises(KBError) as error:
        api.apply(proposal_id=proposal_id)
    assert error.value.code in ("APPROVAL_STALE", "REVISION_CONFLICT")


def test_policy_change_requires_administer_and_approval(api: ResearchKB, reviewer: ResearchKB):
    from research_kb.config import default_policy

    policy = default_policy()
    operations = [{"op": "policy_change", "payload": {"profile": policy, "reason": "test"}}]
    with pytest.raises(KBError) as error:
        reviewer.propose(operations=operations, persist=False)
    assert error.value.code == "PERMISSION_DENIED"
    stored = api.propose(operations=operations, persist=True)
    assert stored["proposal"]["requires_approval"] is True
    approval = api.approve(proposal_id=stored["proposal"]["proposal_id"])
    applied = api.apply(
        proposal_id=stored["proposal"]["proposal_id"], approval_token=approval["approval_token"]
    )
    assert applied["committed"] is True


def test_unsupported_operation_rejected(api: ResearchKB):
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[{"op": "drop_everything", "payload": {}}],
            persist=False,
        )
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"


def test_cross_project_reference_rejected(api: ResearchKB, tmp_path):
    from research_kb.service.context import initialize_project, open_service

    other = tmp_path / "other"
    other.mkdir()
    initialize_project(other, "Other", actor_id="admin")
    other_kb = ResearchKB(open_service(project_root=other, actor_id="admin"))
    try:
        other_record = apply_auto(
            other_kb,
            [
                {
                    "op": "capture",
                    "payload": {
                        "kind": "knowledge",
                        "subkind": "idea",
                        "title": "Other idea",
                        "state_json": {"subkind": "idea", "proposal": "Only in the other project."},
                    },
                }
            ],
        )
        with pytest.raises(KBError) as error:
            api.propose(
                operations=[
                    {
                        "op": "link",
                        "payload": {
                            "predicate": "related_to",
                            "src_ref": {
                                "project_id": other_record["project_id"],
                                "object_id": other_record["created"][0]["object_id"],
                            },
                            "dst_ref": {"object_id": other_record["created"][0]["object_id"]},
                        },
                    }
                ],
                auto_apply=True,
            )
        assert error.value.code in ("PROJECT_MISMATCH", "REFERENCE_UNRESOLVED")
    finally:
        other_kb.close()


def test_tombstone_retains_history_and_flags_dependents(api: ResearchKB, reviewer: ResearchKB):
    from research_kb.storage.db import write_tx

    with write_tx(api.ctx.conn):
        from research_kb.storage.repo import ensure_actor

        ensure_actor(api.ctx.conn, "admin", kind="human", roles=["administrator"])
    definition = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "assumption",
                    "title": "Assumption to tombstone",
                    "state_json": {
                        "subkind": "assumption",
                        "assumption": "A holds.",
                        "domain": "test",
                        "consequences": ["B"],
                        "status": "holding",
                    },
                },
            }
        ],
    )
    definition_id = definition["created"][0]["object_id"]
    stored = api.propose(
        operations=[
            {
                "op": "tombstone",
                "payload": {"ref": {"object_id": definition_id, "revision": 1}, "reason": "withdrawn"},
            }
        ],
        persist=True,
    )
    proposal_id = stored["proposal"]["proposal_id"]
    approval = api.approve(proposal_id=proposal_id)
    result = api.apply(proposal_id=proposal_id, approval_token=approval["approval_token"])
    assert result["committed"] is True
    bundle = api.get([{"object_id": definition_id, "history": True}])["records"][0]
    assert bundle["record_state"] == "tombstoned"
    assert len(bundle["history"]) == 2
