from __future__ import annotations

import pytest

from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from tests.conftest import apply_auto


def approve_and_apply(kb: ResearchKB, operations: list[dict]) -> dict:
    stored = kb.propose(operations=operations, persist=True)
    proposal_id = stored["proposal"]["proposal_id"]
    approval = kb.approve(proposal_id=proposal_id)
    return kb.apply(proposal_id=proposal_id, approval_token=approval["approval_token"])


def capture_claim(kb: ResearchKB, title="Manuscript claim") -> str:
    response = apply_auto(
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
                        "statement": "The measured exponent matches the analytic value.",
                        "domain_applicability": {"regime": "test"},
                        "evidence_criteria": [{"criterion": "independent numerical reproduction"}],
                    },
                },
            }
        ],
    )
    return response["created"][0]["object_id"]


def capture_run(kb: ResearchKB) -> tuple[str, str]:
    study = apply_auto(
        kb,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "study",
                    "subkind": "numerical",
                    "title": "Comparison study",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "numerical",
                        "question": "Does the exponent match?",
                        "protocol": {"method_profile": "numerical", "preprocessing": "none"},
                        "required_outputs": [{"name": "exponent"}],
                        "completion_criteria": ["fit recorded"],
                        "study_state": "active",
                        "executable": True,
                    },
                },
            }
        ],
    )
    study_id = study["created"][0]["object_id"]
    run = apply_auto(
        kb,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "run",
                    "subkind": "attempt",
                    "title": "Imported run attempt",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "attempt",
                        "study_ref": {"object_id": study_id, "revision": 1},
                        "attempt_no": 1,
                        "manifest": {"code_identity": "git:deadbeef"},
                        "status": "completed",
                        "validity": "valid",
                    },
                },
            }
        ],
    )
    return study_id, run["created"][0]["object_id"]


def test_selection_manifest_freezes_selection(api: ResearchKB):
    claim_id = capture_claim(api)
    target = apply_auto(
        api,
        [
            {
                "op": "register_artifact",
                "payload": {
                    "subkind": "manuscript_target",
                    "role": "figure-1",
                    "role_note": "Figure 1",
                    "content_identity": {"kind": "manifest", "value": "figure-1-inputs-v0"},
                    "assurance": "manifest",
                    "availability": "available",
                },
            }
        ],
    )
    target_id = target["created"][0]["object_id"]
    result = approve_and_apply(
        api,
        [
            {
                "op": "create_selection_manifest",
                "payload": {
                    "target_ref": {"object_id": target_id, "revision": 1},
                    "claim_refs": [{"object_id": claim_id, "revision": 1}],
                    "input_refs": [],
                    "inclusion_rule": "earliest attempt passing validity checks",
                    "generated_artifact_hashes": ["sha256:abc"],
                    "review_refs": [{"object_id": claim_id, "revision": 1}],
                },
            }
        ],
    )
    assert result["committed"] is True
    bundle = api.get([{"object_id": target_id}])["records"][0]
    manifest = bundle["state_json"]["selection_manifest"]
    assert manifest["inclusion_rule"] == "earliest attempt passing validity checks"
    assert manifest["frozen_seq"] >= 1
    links = bundle["links"]
    assert any(link["predicate"] == "included_in" for link in links)


def test_selection_with_favorable_outcome_requires_plan(api: ResearchKB):
    target = apply_auto(
        api,
        [
            {
                "op": "register_artifact",
                "payload": {
                    "subkind": "manuscript_target",
                    "role": "table-1",
                    "role_note": "Table 1",
                    "content_identity": {"kind": "manifest", "value": "table-v0"},
                    "assurance": "manifest",
                    "availability": "available",
                },
            }
        ],
    )
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "create_selection_manifest",
                    "payload": {
                        "target_ref": {"object_id": target["created"][0]["object_id"], "revision": 1},
                        "input_refs": [],
                        "inclusion_rule": "best metric",
                        "selection_mode": "best_metric",
                        "generated_artifact_hashes": ["sha256:x"],
                    },
                }
            ],
            persist=False,
        )
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"


def test_comparison_assessment_is_separate_from_run_validity(api: ResearchKB):
    _, run_id = capture_run(api)
    target = capture_claim(api, title="Comparison target")
    result = approve_and_apply(
        api,
        [
            {
                "op": "assess_comparison",
                "payload": {
                    "run_ref": {"object_id": run_id, "revision": 1},
                    "target_ref": {"object_id": target, "revision": 1},
                    "assessment": "eligible",
                    "dimensions": [
                        {
                            "name": "preprocessing",
                            "run_value": "none",
                            "target_value": "none",
                            "matched": True,
                        }
                    ],
                    "rationale": "Both revisions use the same preprocessing and protocol.",
                },
            }
        ],
    )
    assert result["committed"] is True
    run = api.get([{"object_id": run_id}])["records"][0]
    assert run["state_json"]["validity"] == "valid"
    assessment = run["state_json"]["comparison_assessments"][target]
    assert assessment["assessment"] == "eligible"


def test_unknown_comparison_dimension_needs_review(api: ResearchKB):
    _, run_id = capture_run(api)
    target = capture_claim(api, title="Unknown dimension target")
    result = approve_and_apply(
        api,
        [
            {
                "op": "assess_comparison",
                "payload": {
                    "run_ref": {"object_id": run_id, "revision": 1},
                    "target_ref": {"object_id": target, "revision": 1},
                    "assessment": "needs_review",
                    "dimensions": [
                        {"name": "precision", "run_value": None, "target_value": "fp64", "matched": False}
                    ],
                    "rationale": "The run precision is not recorded.",
                },
            }
        ],
    )
    assert result["committed"] is True


def test_study_conclusion_requires_evidence(api: ResearchKB):
    study_id, _ = capture_run(api)
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "set_study_state",
                    "payload": {"ref": {"object_id": study_id, "revision": 1}, "to_state": "concluded"},
                }
            ],
            auto_apply=True,
        )
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"
    applied = api.propose(
        operations=[
            {
                "op": "set_study_state",
                "payload": {
                    "ref": {"object_id": study_id, "revision": 1},
                    "to_state": "abandoned",
                    "reason": "switching protocols",
                },
            }
        ],
        auto_apply=True,
    )
    assert applied["status"] == "applied"


def test_resolve_issue_records_criterion(api: ResearchKB):
    issue = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "issue",
                    "title": "Resolution issue",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "issue",
                        "affected_scope": {"x": 1},
                        "severity": "high",
                        "effect": "blocks the assessment",
                        "blocking_operations": ["paper-ready assessment"],
                        "resolution_criterion": "A pinned definition resolves the normalization.",
                        "status": "open",
                    },
                },
            }
        ],
    )
    issue_id = issue["created"][0]["object_id"]
    resolution = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "definition",
                    "title": "Resolving definition",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "The normalization used for the comparison.",
                        "symbol": "N",
                        "namespace": "test",
                    },
                },
            }
        ],
    )
    resolution_id = resolution["created"][0]["object_id"]
    result = approve_and_apply(
        api,
        [
            {
                "op": "resolve_issue",
                "payload": {
                    "ref": {"object_id": issue_id, "revision": 1},
                    "resolution_ref": {"object_id": resolution_id, "revision": 1},
                    "criterion_met": "A pinned definition resolves the normalization.",
                    "rationale": "Definition recorded and pinned.",
                },
            }
        ],
    )
    assert result["committed"] is True
    bundle = api.get([{"object_id": issue_id}])["records"][0]
    assert bundle["state_json"]["status"] == "resolved"
    status = api.status()
    assert not any(blocker["object_id"] == issue_id for blocker in status["result"]["blockers"])


def test_acknowledge_review_flag_requires_rationale(api: ResearchKB):
    from tests.test_work_evidence import capture_claim as capture_claim_simple
    from tests.conftest import capture_definition

    definition = capture_definition(api)
    definition_id = definition["created"][0]["object_id"]
    derivation = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "derivation",
                    "title": "Flagged derivation",
                    "record_state": "active",
                    "body_md": "Step one. Step two.",
                    "state_json": {
                        "subkind": "derivation",
                        "statement": "Uses the definition.",
                        "assumptions": ["spacing definition"],
                    },
                },
            }
        ],
    )
    derivation_id = derivation["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "link",
                "payload": {
                    "predicate": "assumes",
                    "src_ref": {"object_id": derivation_id, "revision": 1},
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
                        "meaning": "Updated spacing definition.",
                        "symbol": "a",
                        "namespace": "lattice",
                    },
                },
            }
        ],
        auto_apply=True,
    )
    flags = api.ctx.conn.execute(
        "SELECT flag_id FROM review_flags WHERE target_object_id = ? AND status = 'open'",
        (derivation_id,),
    ).fetchall()
    assert flags
    result = api.propose(
        operations=[
            {
                "op": "acknowledge_flag",
                "payload": {"flag_id": flags[0]["flag_id"], "rationale": "Rechecked the derivation."},
            }
        ],
        auto_apply=True,
    )
    assert result["status"] == "applied"
    status_row = api.ctx.conn.execute(
        "SELECT status FROM review_flags WHERE flag_id = ?", (flags[0]["flag_id"],)
    ).fetchone()
    assert status_row["status"] == "acknowledged"


def test_provenance_gate_fails_closed_for_paper_critical(api: ResearchKB):
    from research_kb.domain.provenance import check_provenance

    with pytest.raises(KBError) as error:
        check_provenance(
            "numerical",
            {"code_identity": "git:abc"},
            intended_use="paper_critical",
        )
    assert error.value.code == "PROVENANCE_INCOMPLETE"
    result = check_provenance(
        "theoretical",
        {"assumptions": ["A1"]},
        intended_use="paper_critical",
        deterministic_declared=True,
    )
    assert result.complete is True
    assert any("Deterministic" in note for note in result.notes)
