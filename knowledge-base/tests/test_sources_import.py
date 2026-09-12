from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from tests.conftest import apply_auto


def make_source_file(tmp_path: Path) -> Path:
    path = tmp_path / "paper.md"
    path.write_text(
        "# Results\n\n"
        "The finite-size correction is linear in 1/L for L >= 16.\n\n"
        "## Methods\n\n"
        "We used a fixed seed and a 64^3 lattice.\n",
        encoding="utf-8",
    )
    return path


def register_source_with_citation(api: ResearchKB, tmp_path: Path):
    path = make_source_file(tmp_path)
    source = apply_auto(
        api,
        [
            {
                "op": "register_source",
                "payload": {
                    "subkind": "paper",
                    "title": "Example paper",
                    "author": "A. Author",
                    "external_id": "arXiv:0000.00000",
                    "version": "v2",
                    "identity_assurance": "content_sha256",
                    "preservation": "local_copy_allowed",
                    "captured_path": str(path),
                    "anchors": [
                        {
                            "anchor_kind": "markdown_text",
                            "locator": {"heading_path": ["Results"], "line_start": 3, "line_end": 3},
                            "coordinate_system": "line_range_1based",
                            "excerpt": "The finite-size correction is linear in 1/L for L >= 16.",
                            "status": "ok",
                        }
                    ],
                },
            }
        ],
    )
    source_id = source["created"][0]["object_id"]
    anchors = source["details"]["register_source"][0]["anchors"]
    anchor_id = anchors[0]["anchor_id"]
    observation = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "observation",
                    "title": "Linear correction observed",
                    "state_json": {
                        "subkind": "observation",
                        "observation": "The correction is linear in 1/L for L >= 16.",
                        "conditions": {"method": "paper reading"},
                    },
                    "source_anchor_ids": [anchor_id],
                },
            }
        ],
    )
    return source_id, anchor_id, observation["created"][0]["object_id"]


def test_source_registration_extraction_and_anchors(api: ResearchKB, tmp_path: Path):
    source_id, anchor_id, observation_id = register_source_with_citation(api, tmp_path)
    bundle = api.get([{"object_id": source_id}])["records"][0]
    assert bundle["state_json"]["identity_assurance"] == "content_sha256"
    assert bundle["state_json"]["blob_hash"]
    extraction = api.ctx.conn.execute(
        "SELECT * FROM source_extractions WHERE source_object_id = ?", (source_id,)
    ).fetchone()
    assert extraction["status"] == "extracted"
    anchors = api.ctx.conn.execute(
        "SELECT * FROM source_anchors WHERE source_object_id = ?", (source_id,)
    ).fetchall()
    assert len(anchors) >= 1
    observation = api.get([{"object_id": observation_id}])["records"][0]
    assert observation["citations"][0]["anchor_id"] == anchor_id


def test_source_read_returns_frozen_span(api: ResearchKB, tmp_path: Path):
    source_id, anchor_id, _ = register_source_with_citation(api, tmp_path)
    result = api.context(mode="source_read", focus_refs=[{"anchor_id": anchor_id}])
    span = result["result"]["records"][0]
    assert "finite-size correction" in span["excerpt"]
    assert span["source_object_id"] == source_id


def test_citation_to_missing_anchor_rejected(api: ResearchKB):
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "capture",
                    "payload": {
                        "kind": "knowledge",
                        "subkind": "observation",
                        "title": "Bad citation",
                        "state_json": {
                            "subkind": "observation",
                            "observation": "x",
                            "conditions": {},
                        },
                        "source_anchor_ids": ["00000000-0000-0000-0000-000000000000"],
                    },
                }
            ],
            auto_apply=True,
        )
    assert error.value.code == "REFERENCE_UNRESOLVED"


def test_import_is_resumable_and_deduplicated(api: ResearchKB, tmp_path: Path):
    path = make_source_file(tmp_path)
    items = [
        {
            "external_id": "doc-1",
            "version": "v1",
            "title": "Imported document",
            "subkind": "note",
            "captured_path": str(path),
        }
    ]
    first = api.import_items(connector_namespace="local_file", items=items, request_id="import-1")
    assert first["status"] == "complete"
    assert first["result"]["outcomes"][0]["status"] == "created"
    second = api.import_items(connector_namespace="local_file", items=items, request_id="import-2")
    assert second["result"]["outcomes"][0]["status"] == "duplicate_reused"
    receipts = api.ctx.conn.execute("SELECT * FROM import_receipts").fetchall()
    assert len(receipts) == 1


def test_import_reports_missing_file_without_inventing_source(api: ResearchKB):
    result = api.import_items(
        connector_namespace="local_file",
        items=[{"external_id": "missing", "version": "v1", "captured_path": "/does/not/exist.md"}],
        request_id="import-missing",
    )
    assert result["result"]["failures"]
    assert result["result"]["created"] == []


def test_anchor_hash_is_retained(api: ResearchKB, tmp_path: Path):
    source_id, anchor_id, _ = register_source_with_citation(api, tmp_path)
    anchor = api.ctx.conn.execute(
        "SELECT excerpt, excerpt_hash FROM source_anchors WHERE anchor_id = ?", (anchor_id,)
    ).fetchone()
    import hashlib

    assert hashlib.sha256(anchor["excerpt"].encode("utf-8")).hexdigest() == anchor["excerpt_hash"]
