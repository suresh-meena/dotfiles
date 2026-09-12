from __future__ import annotations

import pytest

from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from research_kb.service.context import open_service
from research_kb.service.work import project_progress, readiness_many
from tests.conftest import apply_auto


def capture_work(kb: ResearchKB, *, title="Task", criteria=None, priority="high"):
    return apply_auto(
        kb,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "work",
                    "subkind": "task",
                    "title": title,
                    "record_state": "active",
                    "state_json": {
                        "subkind": "task",
                        "objective": f"Objective for {title}",
                        "work_state": "open",
                        "priority": priority,
                        "priority_reason": "Blocks the milestone.",
                        "completion_criteria": criteria or ["criterion one"],
                        "owner": "unassigned",
                        "required_inputs": [],
                    },
                },
            }
        ],
    )


def capture_claim(kb: ResearchKB, *, title="Claim", statement="X holds."):
    return apply_auto(
        kb,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "claim",
                    "subkind": "claim",
                    "title": title,
                    "record_state": "active",
                    "state_json": {
                        "subkind": "claim",
                        "statement": statement,
                        "domain_applicability": {"regime": "test"},
                        "evidence_criteria": [{"criterion": "analytical derivation"}],
                    },
                },
            }
        ],
    )


def test_work_state_transitions_and_completion_evidence(api: ResearchKB):
    work = capture_work(api)
    object_id = work["created"][0]["object_id"]
    started = api.propose(
        operations=[
            {
                "op": "set_work_state",
                "payload": {"ref": {"object_id": object_id, "revision": 1}, "to_state": "in_progress"},
            }
        ],
        auto_apply=True,
    )
    assert started["status"] == "applied"
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "set_work_state",
                    "payload": {"ref": {"object_id": object_id, "revision": 2}, "to_state": "done"},
                }
            ],
            auto_apply=True,
        )
    assert error.value.code == "BLOCKED"
    done = api.propose(
        operations=[
            {
                "op": "set_work_state",
                "payload": {
                    "ref": {"object_id": object_id, "revision": 2},
                    "to_state": "done",
                    "completion_report": {"reported_by": "user", "summary": "criteria checked"},
                },
            }
        ],
        auto_apply=True,
    )
    assert done["status"] == "applied"


def test_completed_command_alone_cannot_close_task(api: ResearchKB):
    work = capture_work(api)
    object_id = work["created"][0]["object_id"]
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "set_work_state",
                    "payload": {"ref": {"object_id": object_id, "revision": 1}, "to_state": "in_progress"},
                }
            ],
            auto_apply=True,
        )
        api.propose(
            operations=[
                {
                    "op": "set_work_state",
                    "payload": {
                        "ref": {"object_id": object_id, "revision": 2},
                        "to_state": "done",
                        "completion_report": 0,
                    },
                }
            ],
            auto_apply=True,
        )
    assert error.value.code in ("BLOCKED", "SCHEMA_VALIDATION_FAILED")


def test_dependency_conditions_and_reopening(api: ResearchKB):
    prerequisite = capture_work(api, title="Prerequisite")
    dependent = capture_work(api, title="Dependent")
    prereq_id = prerequisite["created"][0]["object_id"]
    dep_id = dependent["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "capture",
                "payload": {
                    "kind": "work",
                    "subkind": "task",
                    "title": "Prerequisite second revision",
                    "state_json": {
                        "subkind": "task",
                        "objective": "placeholder",
                        "work_state": "open",
                        "priority": "low",
                        "priority_reason": "test",
                        "completion_criteria": ["x"],
                        "owner": "unassigned",
                    },
                },
            },
            {
                "op": "link",
                "payload": {
                    "predicate": "depends_on",
                    "src_ref": {"object_id": dep_id},
                    "dst_ref": {"object_id": prereq_id},
                    "pin_mode": "tracking",
                    "qualifiers": {"condition": "done_with_review"},
                },
            },
        ],
        auto_apply=True,
    )
    api.propose(
        operations=[
            {
                "op": "set_work_state",
                "payload": {"ref": {"object_id": dep_id, "revision": 1}, "to_state": "in_progress"},
            }
        ],
        auto_apply=True,
    )
    readiness = readiness_many(api.ctx, [dep_id])[dep_id]
    assert readiness["blocked"] is True
    assert readiness["unsatisfied_dependencies"][0]["condition"] == "done_with_review"


def test_dependency_cycle_rejected(api: ResearchKB):
    first = capture_work(api, title="A")
    second = capture_work(api, title="B")
    first_id = first["created"][0]["object_id"]
    second_id = second["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "link",
                "payload": {
                    "predicate": "depends_on",
                    "src_ref": {"object_id": first_id},
                    "dst_ref": {"object_id": second_id},
                    "pin_mode": "tracking",
                    "qualifiers": {"condition": "done"},
                },
            }
        ],
        auto_apply=True,
    )
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "link",
                    "payload": {
                        "predicate": "depends_on",
                        "src_ref": {"object_id": second_id},
                        "dst_ref": {"object_id": first_id},
                        "pin_mode": "tracking",
                        "qualifiers": {"condition": "done"},
                    },
                }
            ],
            auto_apply=True,
        )
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"


def test_progress_counts_scope_and_denominator(api: ResearchKB):
    first = capture_work(api, title="One", criteria=["a", "b"])
    second = capture_work(api, title="Two", criteria=["c"])
    progress = project_progress(api.ctx)
    assert progress["criteria"]["denominator"] == 3
    assert progress["criteria"]["numerator"] == 0
    assert len(progress["items"]) == 2
    first_id = first["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "set_work_state",
                "payload": {
                    "ref": {"object_id": first_id, "revision": 1},
                    "to_state": "done",
                    "completion_report": {"summary": "done"},
                },
            }
        ],
        auto_apply=True,
    )
    after = project_progress(api.ctx)
    assert after["criteria"]["numerator"] == 2


def test_next_work_ordering(api: ResearchKB):
    blocker_task = capture_work(api, title="Diagnose")
    other_task = capture_work(api, title="Routine", priority="low")
    issue = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "issue",
                    "title": "Critical issue",
                    "state_json": {
                        "subkind": "issue",
                        "affected_scope": {"x": 1},
                        "severity": "critical",
                        "effect": "blocks work",
                        "blocking_operations": ["analysis"],
                        "resolution_criterion": "diagnostic check passes",
                        "status": "open",
                    },
                },
            }
        ],
    )
    issue_id = issue["created"][0]["object_id"]
    blocker_id = blocker_task["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "link",
                "payload": {
                    "predicate": "resolves",
                    "src_ref": {"object_id": blocker_id},
                    "dst_ref": {"object_id": issue_id},
                    "pin_mode": "pinned",
                },
            }
        ],
        auto_apply=True,
    )
    from research_kb.service.work import next_work

    recommendations = next_work(api.ctx, limit=5)
    assert recommendations[0]["object_id"] == blocker_id
    assert recommendations[0]["ordering_reason"] == "resolves_critical_blocker"


def test_evidence_assessment_requires_reviewer_and_blocks(
    api: ResearchKB, reviewer: ResearchKB, contributor: ResearchKB
):
    claim = capture_claim(api)
    claim_id = claim["created"][0]["object_id"]
    observation = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "observation",
                    "title": "Instrument reading",
                    "state_json": {
                        "subkind": "observation",
                        "observation": "The value matches within tolerance.",
                        "conditions": {"run": 1},
                    },
                },
            }
        ],
    )
    observation_id = observation["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "link",
                "payload": {
                    "predicate": "supports",
                    "src_ref": {"object_id": observation_id, "revision": 1},
                    "dst_ref": {"object_id": claim_id, "revision": 1},
                },
            }
        ],
        auto_apply=True,
    )
    issue = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "issue",
                    "title": "Open blocker",
                    "state_json": {
                        "subkind": "issue",
                        "affected_scope": {"x": 1},
                        "severity": "critical",
                        "effect": "applies to the claim",
                        "blocking_operations": ["paper-ready assessment"],
                        "resolution_criterion": "normalization resolved",
                        "status": "open",
                    },
                },
            }
        ],
    )
    issue_id = issue["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "link",
                "payload": {
                    "predicate": "blocks",
                    "src_ref": {"object_id": issue_id, "revision": 1},
                    "dst_ref": {"object_id": claim_id, "revision": 1},
                },
            }
        ],
        auto_apply=True,
    )
    with pytest.raises(KBError) as error:
        contributor.propose(
            operations=[
                {
                    "op": "assess_evidence",
                    "payload": {
                        "ref": {"object_id": claim_id, "revision": 1},
                        "evidence_state": "supported",
                        "review_state": "reviewed",
                        "rationale": "Looks conclusive.",
                        "assessed_revision": 1,
                    },
                }
            ],
            persist=False,
        )
    assert error.value.code == "PERMISSION_DENIED"
    stored = reviewer.propose(
        operations=[
            {
                "op": "assess_evidence",
                "payload": {
                    "ref": {"object_id": claim_id, "revision": 1},
                    "evidence_state": "supported",
                    "review_state": "reviewed",
                    "rationale": "Looks conclusive.",
                    "assessed_revision": 1,
                },
            }
        ],
        persist=True,
    )
    proposal_id = stored["proposal"]["proposal_id"]
    with pytest.raises(KBError) as error:
        reviewer.apply(proposal_id=proposal_id)
    assert error.value.code == "APPROVAL_REQUIRED"
    approval = reviewer.approve(proposal_id=proposal_id)
    with pytest.raises(KBError) as error:
        reviewer.apply(proposal_id=proposal_id, approval_token=approval["approval_token"])
    assert error.value.code == "BLOCKED"


def test_counterevidence_preserved_in_claim_evidence(api: ResearchKB):
    claim = capture_claim(api)
    claim_id = claim["created"][0]["object_id"]
    support = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "observation",
                    "title": "Supporting observation",
                    "state_json": {
                        "subkind": "observation",
                        "observation": "supports",
                        "conditions": {},
                    },
                },
            }
        ],
    )
    contradiction = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "negative_result",
                    "title": "Contradicting negative result",
                    "state_json": {
                        "subkind": "negative_result",
                        "investigated_domain": {"L": [8, 12]},
                        "result": "No effect observed in this range.",
                        "detection_limits": "Sensitivity insufficient below L=8.",
                    },
                },
            }
        ],
    )
    api.propose(
        operations=[
            {
                "op": "link",
                "payload": {
                    "predicate": "supports",
                    "src_ref": {"object_id": support["created"][0]["object_id"], "revision": 1},
                    "dst_ref": {"object_id": claim_id, "revision": 1},
                },
            },
            {
                "op": "link",
                "payload": {
                    "predicate": "contradicts",
                    "src_ref": {"object_id": contradiction["created"][0]["object_id"], "revision": 1},
                    "dst_ref": {"object_id": claim_id, "revision": 1},
                },
            },
        ],
        auto_apply=True,
    )
    result = api.context(mode="claim_evidence", focus_refs=[claim_id])
    roles = {record.get("role") for record in result["result"]["records"]}
    assert "counterevidence" in roles
    assert "evidence" in roles


def test_review_flags_created_on_upstream_change(api: ResearchKB):
    definition = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "definition",
                    "title": "Base definition",
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "Base.",
                        "symbol": "b",
                        "namespace": "test",
                    },
                },
            }
        ],
    )
    derived = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "derivation",
                    "title": "Dependent derivation",
                    "body_md": "Step 1. Step 2.",
                    "state_json": {
                        "subkind": "derivation",
                        "statement": "Result from the base definition.",
                        "assumptions": ["base definition"],
                    },
                },
            }
        ],
    )
    definition_id = definition["created"][0]["object_id"]
    derived_id = derived["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "link",
                "payload": {
                    "predicate": "assumes",
                    "src_ref": {"object_id": derived_id, "revision": 1},
                    "dst_ref": {"object_id": definition_id, "revision": 1},
                },
            }
        ],
        auto_apply=True,
    )
    api.propose(
        operations=[
            {
                "op": "revise",
                "payload": {
                    "ref": {"object_id": definition_id, "revision": 1},
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "Changed base definition.",
                        "symbol": "b",
                        "namespace": "test",
                    },
                },
            }
        ],
        auto_apply=True,
    )
    flags = api.ctx.conn.execute(
        "SELECT * FROM review_flags WHERE project_id = ? AND target_object_id = ?",
        (api.ctx.project_id, derived_id),
    ).fetchall()
    assert flags
    assert flags[0]["status"] == "open"
