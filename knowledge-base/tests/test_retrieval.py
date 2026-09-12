from __future__ import annotations

import pytest

from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from tests.conftest import apply_auto, capture_definition


def test_search_finds_terms_and_reports_completeness(api: ResearchKB):
    capture_definition(api)
    result = api.search(query="lattice")
    assert result["status"] == "ok"
    titles = [item.get("title") for item in result["result"]["candidates"]]
    assert "Spacing" in titles
    assert result["result"]["completeness"]["search_exhaustive"] is False


def test_search_empty_does_not_claim_absence(api: ResearchKB):
    result = api.search(query="definitely_absent_term_xyz")
    assert result["result"]["candidates"] == []
    assert result["result"]["completeness"]["search_exhaustive"] is False


def test_fts_quoted_literals_and_special_characters(api: ResearchKB):
    apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "definition",
                    "title": "Notation variants",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "Time step notation.",
                        "symbol": "dt",
                        "namespace": "numerics",
                        "aliases": ["Δt", "spacing"],
                    },
                },
            }
        ],
    )
    result = api.search(query="dt")
    assert result["result"]["candidates"]
    quoted = api.search(query='field:"value"')
    assert quoted["status"] == "ok"


def test_history_as_of_does_not_leak_later_corrections(api: ResearchKB):
    response = capture_definition(api)
    object_id = response["created"][0]["object_id"]
    before_cursor = api.capabilities()["snapshot"]["cursor"]
    apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "definition",
                    "title": "Unrelated later capture",
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "Later.",
                        "symbol": "z",
                        "namespace": "numerics",
                    },
                },
            }
        ],
    )
    api.propose(
        operations=[
            {
                "op": "revise",
                "payload": {"ref": {"object_id": object_id, "revision": 1}, "title": "Corrected spacing"},
            }
        ],
        auto_apply=True,
    )
    historical = api.get([{"object_id": object_id}])
    assert historical["records"][0]["title"] == "Corrected spacing"
    context = api.context(mode="history", focus_refs=[object_id], as_of_cursor=before_cursor)
    history = context["result"]["records"][0]["history"]
    assert [entry["revision"] for entry in history] == [1]
    assert context["result"]["snapshot"]["historical"] is True


def test_changes_pagination_and_acknowledgment(api: ResearchKB):
    baseline = api.capabilities()["snapshot"]["cursor"]
    for index in range(3):
        apply_auto(
            api,
            [
                {
                    "op": "capture",
                    "payload": {
                        "kind": "knowledge",
                        "subkind": "idea",
                        "title": f"Idea {index}",
                        "state_json": {"subkind": "idea", "proposal": f"Proposal {index}."},
                    },
                }
            ],
        )
    page = api.changes(after=baseline, limit=2)
    assert len(page["result"]["events"]) == 2
    assert page["result"]["completeness"]["truncated"] is True
    next_page = api.changes(page_cursor=page["result"]["next_page_cursor"], limit=2)
    assert next_page["result"]["events"]
    acknowledged = api.changes(after=baseline, acknowledge=True)
    assert acknowledged["result"]["acknowledged_seq"] is not None


def test_context_question_expands_evidence(api: ResearchKB):
    claim = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "claim",
                    "subkind": "claim",
                    "title": "Tolerance claim",
                    "state_json": {
                        "subkind": "claim",
                        "statement": "The extrapolated value agrees within tolerance.",
                        "domain_applicability": {"regime": "test"},
                        "evidence_criteria": [{"criterion": "independent reproduction"}],
                    },
                },
            }
        ],
    )
    claim_id = claim["created"][0]["object_id"]
    observation = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "observation",
                    "title": "Reproduction observation",
                    "state_json": {
                        "subkind": "observation",
                        "observation": "Independent reproduction agrees within tolerance.",
                        "conditions": {"seed": 1},
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
                    "src_ref": {"object_id": observation["created"][0]["object_id"], "revision": 1},
                    "dst_ref": {"object_id": claim_id, "revision": 1},
                },
            }
        ],
        auto_apply=True,
    )
    result = api.context(mode="question", query="tolerance")
    roles = {record.get("role") for record in result["result"]["records"]}
    assert "search_hit" in roles
    assert "evidence" in roles


def test_status_reports_blockers(api: ResearchKB):
    apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "issue",
                    "title": "Open question",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "issue",
                        "affected_scope": {"x": 1},
                        "severity": "critical",
                        "effect": "blocks publication",
                        "blocking_operations": ["paper"],
                        "resolution_criterion": "source located",
                        "status": "open",
                    },
                },
            }
        ],
    )
    result = api.status()
    assert result["result"]["blockers"]
