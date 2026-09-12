from __future__ import annotations

import os
from pathlib import Path

import pytest

from research_kb.service.api import ResearchKB
from research_kb.service.context import initialize_project, open_service


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    os.environ.pop("RESEARCH_KB_STATE_ROOT", None)
    os.environ["RESEARCH_KB_ALLOW_UNPATCHED_SQLITE"] = "1"
    initialize_project(root, "Test Project", actor_id="admin")
    return root


@pytest.fixture()
def api(project_root: Path):
    kb = ResearchKB(open_service(project_root=project_root, actor_id="admin"))
    yield kb
    kb.close()


def add_actor(root: Path, actor_id: str, *, roles: list[str] | None = None, capabilities: list[str] | None = None):
    from research_kb.storage.db import write_tx
    from research_kb.storage.repo import ensure_actor

    ctx = open_service(project_root=root, actor_id="admin")
    try:
        with write_tx(ctx.conn):
            ensure_actor(
                ctx.conn,
                actor_id,
                kind="agent",
                display_name=actor_id,
                roles=roles or ["reader"],
                capabilities=capabilities or [],
            )
    finally:
        ctx.close()


@pytest.fixture()
def reader(project_root: Path):
    add_actor(project_root, "reader-bot", roles=["reader"])
    kb = ResearchKB(open_service(project_root=project_root, actor_id="reader-bot"))
    yield kb
    kb.close()


@pytest.fixture()
def contributor(project_root: Path):
    add_actor(project_root, "contrib-bot", roles=["contributor"])
    kb = ResearchKB(open_service(project_root=project_root, actor_id="contrib-bot"))
    yield kb
    kb.close()


@pytest.fixture()
def reviewer(project_root: Path):
    add_actor(project_root, "reviewer-bot", roles=["reviewer"])
    kb = ResearchKB(open_service(project_root=project_root, actor_id="reviewer-bot"))
    yield kb
    kb.close()


def apply_auto(kb: ResearchKB, operations, request_id=None):
    response = kb.propose(
        operations=operations,
        reason="test",
        request_id=request_id,
        persist=True,
        auto_apply=True,
    )
    assert response["status"] == "applied", response
    return response


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
