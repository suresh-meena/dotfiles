from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from research_kb.service.context import open_service
from tests.conftest import apply_auto, add_actor


def capture_definition(kb: ResearchKB, *, request_id: str | None = None):
    return apply_auto(
        kb,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "definition",
                    "title": "Spacing",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "Grid spacing.",
                        "symbol": "a",
                        "namespace": "lattice",
                        "units_or_domain": "lattice units",
                        "source_or_convention": "project convention",
                    },
                    "aliases": ["lattice spacing"],
                },
            }
        ],
        request_id=request_id,
    )


def test_capture_and_get_round_trip(api: ResearchKB):
    response = capture_definition(api)
    object_id = response["created"][0]["object_id"]
    fetched = api.get([{"object_id": object_id}])
    record = fetched["records"][0]
    assert record["state_json"]["symbol"] == "a"
    assert record["revision"] == 1
    assert record["aliases"] == ["lattice spacing"]


def test_alias_resolution_and_ambiguity(api: ResearchKB):
    capture_definition(api)
    fetched = api.get([{"object_id": "lattice spacing"}])
    assert fetched["records"][0]["revision"] == 1
    capture_definition(api)
    with pytest.raises(KBError) as error:
        api.get([{"object_id": "lattice spacing"}])
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"
    assert len(error.value.details["candidates"]) == 2


def test_revision_conflict(api: ResearchKB):
    response = capture_definition(api)
    object_id = response["created"][0]["object_id"]
    first = api.propose(
        operations=[
            {
                "op": "revise",
                "payload": {
                    "ref": {"object_id": object_id, "revision": 1},
                    "title": "Spacing v2",
                },
            }
        ],
        reason="update symbol",
        auto_apply=True,
        persist=True,
    )
    assert first["status"] == "applied"
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "revise",
                    "payload": {
                        "ref": {"object_id": object_id, "revision": 1},
                        "title": "Stale update",
                    },
                }
            ],
            reason="stale",
            auto_apply=True,
            persist=True,
        )
    assert error.value.code == "REVISION_CONFLICT"


def test_expected_revision_conflict_raises(api: ResearchKB):
    from research_kb.service.operations import apply_operations
    from research_kb.service.objects import revise_object
    from research_kb.storage.db import write_tx

    response = capture_definition(api)
    object_id = response["created"][0]["object_id"]
    captured = api.ctx
    with pytest.raises(KBError) as error:
        with write_tx(captured.conn):
            from research_kb.service.objects import Mutation, create_object

            mutation = Mutation(ctx=captured, commit_seq=captured.latest_seq())
            revise_object(
                mutation,
                object_id=object_id,
                expected_revision=99,
                title="bad",
            )
    assert error.value.code == "REVISION_CONFLICT"


def test_history_is_insert_only(api: ResearchKB, project_root: Path):
    response = capture_definition(api)
    object_id = response["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "revise",
                "payload": {"ref": {"object_id": object_id, "revision": 1}, "title": "Spacing v2"},
            }
        ],
        reason="edit",
        auto_apply=True,
    )
    fetched = api.get([{"object_id": object_id, "history": True}])
    history = fetched["records"][0]["history"]
    assert [entry["revision"] for entry in history] == [1, 2]
    with pytest.raises(Exception):
        api.ctx.conn.execute(
            "UPDATE revisions SET title = 'rewritten' WHERE object_id = ?", (object_id,)
        )


def test_idempotent_replay_and_conflict(api: ResearchKB):
    request_id = "fixed-request-1"
    first = capture_definition(api, request_id=request_id)
    second = capture_definition(api, request_id=request_id)
    assert second["proposal"]["idempotent_replay"] is True
    assert second["project_id"] == first["project_id"]
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "capture",
                    "payload": {
                        "kind": "knowledge",
                        "subkind": "idea",
                        "title": "Different payload",
                        "state_json": {"subkind": "idea", "proposal": "Something else."},
                    },
                }
            ],
            request_id=request_id,
            auto_apply=True,
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_project_isolation(tmp_path: Path, project_root: Path):
    other_root = tmp_path / "other"
    other_root.mkdir()
    from research_kb.service.context import initialize_project

    initialize_project(other_root, "Other Project", actor_id="admin")
    first = ResearchKB(open_service(project_root=project_root, actor_id="admin"))
    second = ResearchKB(open_service(project_root=other_root, actor_id="admin"))
    try:
        response = capture_definition(first)
        object_id = response["created"][0]["object_id"]
        with pytest.raises(KBError):
            second.get([{"object_id": object_id}])
        with pytest.raises(KBError):
            second.propose(
                operations=[
                    {
                        "op": "link",
                        "payload": {
                            "predicate": "related_to",
                            "src_ref": {"object_id": object_id},
                            "dst_ref": {"object_id": object_id},
                        },
                    }
                ],
                auto_apply=True,
            )
    finally:
        first.close()
        second.close()


def test_missing_required_payload_field_rejected(api: ResearchKB):
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "capture",
                    "payload": {
                        "kind": "knowledge",
                        "subkind": "definition",
                        "title": "Incomplete",
                        "state_json": {"subkind": "definition", "meaning": "only meaning"},
                    },
                }
            ],
            persist=False,
        )
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"


def test_reader_cannot_capture(reader: ResearchKB):
    with pytest.raises(KBError) as error:
        reader.propose(
            operations=[
                {
                    "op": "capture",
                    "payload": {
                        "kind": "knowledge",
                        "subkind": "idea",
                        "title": "Reader attempt",
                        "state_json": {"subkind": "idea", "proposal": "Nope."},
                    },
                }
            ],
            auto_apply=True,
        )
    assert error.value.code == "PERMISSION_DENIED"


def test_occurred_at_unknown_not_invented(api: ResearchKB):
    response = capture_definition(api)
    object_id = response["created"][0]["object_id"]
    bundle = api.get([{"object_id": object_id}])["records"][0]
    assert bundle["occurred_at"] is None
    assert bundle["recorded_at"] is not None
