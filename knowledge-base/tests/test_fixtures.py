from __future__ import annotations

import json
from pathlib import Path

from research_kb.domain.canonical import normalize_alias
from research_kb.service.api import ResearchKB

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = FIXTURES / "synthetic_project.json"
GOLD = FIXTURES / "retrieval_gold.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def alias_map(kb: ResearchKB) -> dict[str, str]:
    rows = kb.ctx.conn.execute(
        "SELECT alias_text, object_id FROM aliases WHERE project_id = ?", (kb.ctx.project_id,)
    ).fetchall()
    return {normalize_alias(row["alias_text"]): row["object_id"] for row in rows}


def substitute_refs(operations: list[dict], aliases: dict[str, str]) -> list[dict]:
    resolved = []
    for operation in operations:
        payload = json.loads(json.dumps(operation["payload"]))
        for field in ("src_ref", "dst_ref"):
            ref = payload.get(field)
            if isinstance(ref, dict) and "alias" in ref:
                ref = {"object_id": aliases[normalize_alias(ref["alias"])]}
            payload[field] = ref
        resolved.append({"op": operation["op"], "payload": payload})
    return resolved


def test_fixture_is_not_imported_silently(api: ResearchKB):
    count = api.ctx.conn.execute(
        "SELECT COUNT(*) AS count FROM objects WHERE project_id = ?", (api.ctx.project_id,)
    ).fetchone()["count"]
    assert count == 1


def test_fixture_records_import_and_are_retrievable(api: ResearchKB):
    fixture = load_fixture()
    records = fixture["batches"][0]["operations"]
    response = api.propose(operations=records, reason="Explicit synthetic fixture import.", auto_apply=True)
    assert response["status"] == "applied"
    assert len(response["created"]) == len(records)
    aliases = alias_map(api)
    links = substitute_refs(fixture["batches"][1]["operations"], aliases)
    link_response = api.propose(operations=links, reason="Fixture links.", auto_apply=True)
    assert link_response["status"] == "applied"


def test_retrieval_gold_set_recall(api: ResearchKB):
    fixture = load_fixture()
    records = fixture["batches"][0]["operations"]
    api.propose(operations=records, reason="Explicit synthetic fixture import.", auto_apply=True)
    aliases = alias_map(api)
    links = substitute_refs(fixture["batches"][1]["operations"], aliases)
    api.propose(operations=links, reason="Fixture links.", auto_apply=True)
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    assert 40 <= len(gold["questions"]) <= 60
    hits = 0
    misses = []
    for question in gold["questions"]:
        result = api.search(query=question["query"], limit=10)
        found = {candidate["object_id"] for candidate in result["result"]["candidates"]}
        expected = {aliases[normalize_alias(alias)] for alias in question["expected_aliases"]}
        if expected.issubset(found):
            hits += 1
        else:
            misses.append(question["id"])
    recall = hits / len(gold["questions"])
    assert recall >= 0.9, f"recall={recall:.2f} misses={misses}"
