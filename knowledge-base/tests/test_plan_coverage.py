from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from research_kb.service.context import initialize_project, open_service
from tests.conftest import apply_auto, capture_definition


def enable_policy(project_root: Path, mutate) -> None:
    from research_kb.config import load_policy, save_policy

    routing = load_routing(project_root)
    policy = load_policy(routing.state_dir)
    mutate(policy)
    save_policy(routing.state_dir, policy)


def load_routing(root: Path):
    from research_kb.config import load_routing as load

    routing = load(root)
    assert routing is not None
    return routing


def refresh(api: ResearchKB) -> None:
    from research_kb.config import load_policy, policy_hash

    api.ctx.policy = load_policy(api.ctx.state_dir)
    api.ctx.policy_revision = policy_hash(api.ctx.policy)


def test_citations_do_not_transfer_silently_and_can_be_reaffirmed(api: ResearchKB, tmp_path: Path):
    source_file = tmp_path / "cited.md"
    source_file.write_text("# Findings\n\nThe cited statement is exact.\n", encoding="utf-8")
    source = apply_auto(
        api,
        [
            {
                "op": "register_source",
                "payload": {
                    "subkind": "note",
                    "title": "Cited note",
                    "version": "v1",
                    "identity_assurance": "content_sha256",
                    "preservation": "local_copy_allowed",
                    "captured_path": str(source_file),
                    "anchors": [
                        {
                            "anchor_kind": "markdown_text",
                            "locator": {"line_start": 3, "line_end": 3},
                            "coordinate_system": "line_range_1based",
                            "excerpt": "The cited statement is exact.",
                        }
                    ],
                },
            }
        ],
    )
    anchor_id = source["details"]["register_source"][0]["anchors"][0]["anchor_id"]
    observation = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "observation",
                    "title": "Cited observation",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "observation",
                        "observation": "The cited statement is exact.",
                        "conditions": {},
                    },
                    "source_anchor_ids": [anchor_id],
                },
            }
        ],
    )
    object_id = observation["created"][0]["object_id"]
    original = api.get([{"object_id": object_id}])["records"][0]
    citation_id = original["citations"][0]["citation_id"]
    revised = api.propose(
        operations=[
            {
                "op": "revise",
                "payload": {
                    "ref": {"object_id": object_id, "revision": 1},
                    "title": "Reworded observation",
                },
            }
        ],
        auto_apply=True,
    )
    assert revised["status"] == "applied"
    after = api.get([{"object_id": object_id}])["records"][0]
    assert after["citations"] == []
    reaffirmed = api.propose(
        operations=[
            {
                "op": "revise",
                "payload": {
                    "ref": {"object_id": object_id, "revision": 2},
                    "title": "Reworded and reaffirmed",
                    "reaffirm_citation_ids": [citation_id],
                },
            }
        ],
        auto_apply=True,
    )
    assert reaffirmed["status"] == "applied"
    final = api.get([{"object_id": object_id}])["records"][0]
    assert len(final["citations"]) == 1
    assert "reaffirmed" in final["citations"][0]["note"]


def test_effective_at_filters_applicability(api: ResearchKB):
    bounded = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "definition",
                    "title": "Bounded applicability marker",
                    "record_state": "active",
                    "effective_from": "2020-01-01T00:00:00Z",
                    "effective_to": "2021-01-01T00:00:00Z",
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "Applies only during 2020.",
                        "symbol": "bounded",
                        "namespace": "test",
                    },
                },
            }
        ],
    )
    bounded_id = bounded["created"][0]["object_id"]
    unknown = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "definition",
                    "title": "Unknown applicability marker",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "Interval unknown.",
                        "symbol": "unknownness",
                        "namespace": "test",
                    },
                },
            }
        ],
    )
    unknown_id = unknown["created"][0]["object_id"]
    inside = api.search(query="applicability marker", effective_at="2020-06-01T00:00:00Z")
    inside_ids = {candidate["object_id"] for candidate in inside["result"]["candidates"]}
    assert bounded_id in inside_ids
    assert unknown_id in inside_ids
    outside = api.search(query="applicability marker", effective_at="2022-01-01T00:00:00Z")
    outside_ids = {candidate["object_id"] for candidate in outside["result"]["candidates"]}
    assert bounded_id not in outside_ids
    assert unknown_id in outside_ids
    lookup = api.context(
        mode="lookup", focus_refs=[unknown_id], effective_at="2022-01-01T00:00:00Z"
    )
    assert lookup["result"]["records"][0]["effective_unknown"] is True
    lookup_bounded = api.context(
        mode="lookup", focus_refs=[bounded_id], effective_at="2022-01-01T00:00:00Z"
    )
    assert any("not_applicable" in item for item in lookup_bounded["result"]["missing"])


def test_paper_readiness_requires_current_review(api: ResearchKB, reviewer: ResearchKB):
    claim = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "claim",
                    "subkind": "claim",
                    "title": "Readiness claim",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "claim",
                        "statement": "X holds.",
                        "domain_applicability": {"regime": "test"},
                        "evidence_criteria": [{"criterion": "analytical derivation"}],
                    },
                },
            }
        ],
    )
    claim_id = claim["created"][0]["object_id"]
    before = api.verify(scope=claim_id, checks=["readiness"])
    readiness = next(
        finding for finding in before["result"]["findings"] if finding["check"] == "readiness"
    )
    assert readiness["details"]["ready"] is False
    assert any("No evidence assessment" in reason for reason in readiness["details"]["reasons"])
    reviewer.propose(
        operations=[
            {
                "op": "assess_evidence",
                "payload": {
                    "ref": {"object_id": claim_id, "revision": 1},
                    "evidence_state": "supported",
                    "review_state": "reviewed",
                    "rationale": "Derivation checked against the stated assumptions.",
                    "assessed_revision": 1,
                },
            }
        ],
        persist=True,
    )
    proposals = api.proposals(status="stored")["proposals"]
    proposal_id = proposals[-1]["proposal_id"]
    approval = api.approve(proposal_id=proposal_id)
    applied = api.apply(proposal_id=proposal_id, approval_token=approval["approval_token"])
    assert applied["committed"] is True
    assert applied["result"]["readback"]
    after = api.verify(scope=claim_id, checks=["readiness"])
    readiness = next(
        finding for finding in after["result"]["findings"] if finding["check"] == "readiness"
    )
    assert readiness["details"]["ready"] is True


def test_import_roots_are_enforced(project_root: Path, api: ResearchKB, tmp_path: Path):
    from tests.conftest import add_actor

    outside = tmp_path / "outside.md"
    outside.write_text("outside root\n", encoding="utf-8")
    inside = project_root / "inside.md"
    inside.write_text("inside root\n", encoding="utf-8")
    enable_policy(project_root, lambda policy: policy["capture"].update(allowed_import_roots=[str(project_root)]))
    refresh(api)
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "register_source",
                    "payload": {
                        "subkind": "note",
                        "title": "Outside",
                        "version": "v1",
                        "identity_assurance": "content_sha256",
                        "captured_path": str(outside),
                    },
                }
            ],
            persist=False,
        )
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"
    accepted = api.propose(
        operations=[
            {
                "op": "register_source",
                "payload": {
                    "subkind": "note",
                    "title": "Inside",
                    "version": "v1",
                    "identity_assurance": "content_sha256",
                    "captured_path": str(inside),
                },
            }
        ],
        auto_apply=True,
    )
    assert accepted["status"] == "applied"


def test_policy_unknown_keys_are_rejected(project_root: Path):
    from research_kb.config import default_policy

    routing = load_routing(project_root)
    policy_file = routing.state_dir / "policy.json"
    policy = default_policy()
    policy["retrieval"]["unexpected_key"] = True
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(KBError) as error:
        open_service(project_root=project_root, actor_id="admin")
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"


def test_cli_rejects_duplicate_json_keys(project_root: Path, capsys):
    from research_kb.clients.cli import main as cli_main

    duplicate = project_root / "ops.json"
    duplicate.write_text('{"operations": [], "operations": []}', encoding="utf-8")
    code = cli_main(
        ["--json", "--actor", "admin", "--root", str(project_root), "propose", "--file", str(duplicate)]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["error"]["code"] == "SCHEMA_VALIDATION_FAILED"


def test_excerpt_anchor_is_located_in_extraction(api: ResearchKB, tmp_path: Path):
    source_file = tmp_path / "anchor.md"
    source_file.write_text(
        "# Intro\n\nSome preamble.\n\n## Result\nThe located sentence is here.\n",
        encoding="utf-8",
    )
    source = apply_auto(
        api,
        [
            {
                "op": "register_source",
                "payload": {
                    "subkind": "note",
                    "title": "Anchor note",
                    "version": "v1",
                    "identity_assurance": "content_sha256",
                    "captured_path": str(source_file),
                    "anchors": [{"excerpt": "The located sentence is here."}],
                },
            }
        ],
    )
    anchor_id = source["details"]["register_source"][0]["anchors"][0]["anchor_id"]
    anchor = api.ctx.conn.execute(
        "SELECT locator_json, coordinate_system FROM source_anchors WHERE anchor_id = ?",
        (anchor_id,),
    ).fetchone()
    locator = json.loads(anchor["locator_json"])
    assert locator.get("line_start")
    assert anchor["coordinate_system"] == "line_range_1based"


def test_chunker_respects_target_and_maximum(api: ResearchKB):
    from research_kb.ingestion.extract import CHUNK_MAX_TOKENS, CHUNK_TARGET_TOKENS, chunk_markdown

    paragraphs = "\n\n".join(
        " ".join(f"token{index}_{word}" for word in range(120)) for index in range(12)
    )
    sections = chunk_markdown(paragraphs)
    assert len(sections) >= 2
    for section in sections[:-1]:
        assert len(section["body"].split()) <= CHUNK_MAX_TOKENS
    assert any(
        len(section["body"].split()) >= CHUNK_TARGET_TOKENS for section in sections[:-1]
    )


def test_artifact_results_contract_is_validated(api: ResearchKB):
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "capture",
                    "payload": {
                        "kind": "artifact",
                        "subkind": "analysis_result",
                        "title": "Bad results",
                        "record_state": "active",
                        "state_json": {
                            "subkind": "analysis_result",
                            "role": "fit",
                            "content_identity": {"kind": "manifest", "value": "x"},
                            "assurance": "manifest",
                            "availability": "available",
                            "results": {
                                "results": [{"name": "exponent", "value": 1.0, "condition": "c"}]
                            },
                        },
                    },
                }
            ],
            persist=False,
        )
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"


def test_gc_dry_run_and_permission(project_root: Path, capsys, api: ResearchKB):
    from research_kb.clients.cli import main as cli_main

    code = cli_main(
        ["--json", "--actor", "admin", "--root", str(project_root), "gc", "--grace", "0"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "dry_run"


def test_tokenizer_handles_literals_and_notation(api: ResearchKB):
    markers = {
        "alpha-beta coupling": "hyphen-marker",
        "x_1 field": "underscore-marker",
        "1e-3 tolerance": "scientific-marker",
        "gamma/2 ratio": "slash-marker",
    }
    created: dict[str, str] = {}
    for title, alias in markers.items():
        response = apply_auto(
            api,
            [
                {
                    "op": "capture",
                    "payload": {
                        "kind": "knowledge",
                        "subkind": "definition",
                        "title": title,
                        "record_state": "active",
                        "state_json": {
                            "subkind": "definition",
                            "meaning": f"Fixture meaning for {title}.",
                            "symbol": alias,
                            "namespace": "tokenizer",
                        },
                        "aliases": [alias],
                    },
                }
            ],
        )
        created[title] = response["created"][0]["object_id"]
    expected = {
        "alpha-beta": "alpha-beta coupling",
        "x_1": "x_1 field",
        "1e-3": "1e-3 tolerance",
        "gamma/2": "gamma/2 ratio",
    }
    for query, title in expected.items():
        result = api.search(query=query, limit=5)
        found = {candidate["object_id"] for candidate in result["result"]["candidates"]}
        assert created[title] in found, (query, found)
