from __future__ import annotations

import sqlite3

import pytest

from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from research_kb.storage import migrations
from research_kb.storage.db import connect, probe_features, write_tx
from tests.conftest import apply_auto


def test_foreign_keys_are_enforced_per_connection(api: ResearchKB):
    with pytest.raises(sqlite3.IntegrityError):
        api.ctx.conn.execute(
            "INSERT INTO citations (citation_id, project_id, citing_object_id, citing_revision, anchor_id, role, created_seq, created_at) "
            "VALUES ('x', ?, 'missing', 1, 'missing', 'quote', 0, 'now')",
            (api.ctx.project_id,),
        )


def test_revisions_are_immutable(api: ResearchKB):
    response = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "idea",
                    "title": "Immutable",
                    "state_json": {"subkind": "idea", "proposal": "x"},
                },
            }
        ],
    )
    object_id = response["created"][0]["object_id"]
    with pytest.raises(sqlite3.IntegrityError):
        api.ctx.conn.execute("UPDATE revisions SET title='hacked' WHERE object_id = ?", (object_id,))
    with pytest.raises(sqlite3.IntegrityError):
        api.ctx.conn.execute("DELETE FROM revisions WHERE object_id = ?", (object_id,))


def test_commit_event_is_written_with_mutation(api: ResearchKB):
    before = api.ctx.latest_seq()
    apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "idea",
                    "title": "Audited",
                    "state_json": {"subkind": "idea", "proposal": "x"},
                },
            }
        ],
    )
    row = api.ctx.conn.execute(
        "SELECT * FROM commit_events WHERE seq = ?", (before + 1,)
    ).fetchone()
    assert row["action"] == "proposal_apply"
    assert row["changed_json"] != "[]"


def test_migration_checksum_mismatch_detected(project_root, tmp_path):
    conn = connect(project_root / ".research" / "state" / load_project_id(project_root) / "research.db")
    try:
        migrations.ensure_registry(conn)
        conn.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
        with pytest.raises(KBError):
            migrations.verify_checksums(conn)
    finally:
        conn.close()


def load_project_id(root):
    from research_kb.config import load_routing

    routing = load_routing(root)
    assert routing is not None
    return routing.project_id


def test_pragma_features_reported(api: ResearchKB):
    features = probe_features(api.ctx.conn)
    assert features["foreign_keys"] == 1
    assert features["strict_tables"] is True
    assert features["fts5"] is True
    assert features["json"] is True


def test_pinned_link_requires_revisions(api: ResearchKB):
    response = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "observation",
                    "title": "Evidence without pins",
                    "state_json": {"subkind": "observation", "observation": "x", "conditions": {}},
                },
            },
            {
                "op": "capture",
                "payload": {
                    "kind": "claim",
                    "subkind": "claim",
                    "title": "Claim target",
                    "state_json": {
                        "subkind": "claim",
                        "statement": "y",
                        "domain_applicability": {},
                        "evidence_criteria": [{"criterion": "z"}],
                    },
                },
            },
        ],
    )
    observation_id = response["created"][0]["object_id"]
    claim_id = response["created"][1]["object_id"]
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
    link = api.ctx.conn.execute("SELECT * FROM link_revisions WHERE predicate = 'supports'").fetchone()
    assert link["pin_mode"] == "pinned"
    assert link["src_revision"] == 1
    assert link["dst_revision"] == 1


def test_two_connections_serialize_writes(project_root, api: ResearchKB):
    from research_kb.service.context import open_service

    response = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "idea",
                    "title": "Contended",
                    "state_json": {"subkind": "idea", "proposal": "x"},
                },
            }
        ],
    )
    object_id = response["created"][0]["object_id"]
    first = open_service(project_root=project_root, actor_id="admin")
    second = open_service(project_root=project_root, actor_id="admin")
    try:
        from research_kb.service.objects import Mutation, revise_object

        with write_tx(first.conn):
            revise_object(
                Mutation(ctx=first, commit_seq=first.latest_seq()),
                object_id=object_id,
                expected_revision=1,
                title="winner",
            )
        with pytest.raises(KBError) as error:
            with write_tx(second.conn):
                revise_object(
                    Mutation(ctx=second, commit_seq=second.latest_seq()),
                    object_id=object_id,
                    expected_revision=1,
                    title="loser",
                )
        assert error.value.code == "REVISION_CONFLICT"
    finally:
        first.close()
        second.close()
