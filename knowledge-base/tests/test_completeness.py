from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from research_kb.domain.blocking_rules import evaluate_rule, validate_rule
from research_kb.errors import KBError
from research_kb.service.api import ResearchKB
from research_kb.service.context import open_service
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


def refresh(api: ResearchKB, project_root: Path) -> None:
    from research_kb.config import load_policy, policy_hash

    api.ctx.policy = load_policy(api.ctx.state_dir)
    api.ctx.policy_revision = policy_hash(api.ctx.policy)


def capture_issue(api: ResearchKB, title="Rule blocker", severity="critical") -> str:
    response = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "issue",
                    "title": title,
                    "record_state": "active",
                    "state_json": {
                        "subkind": "issue",
                        "affected_scope": {"regime": "test"},
                        "severity": severity,
                        "effect": "blocks the assessment",
                        "blocking_operations": ["paper-ready assessment"],
                        "resolution_criterion": "rule resolved",
                        "status": "open",
                    },
                },
            }
        ],
    )
    return response["created"][0]["object_id"]


def test_blocking_rule_language():
    context = {"regime": "test", "L": 32, "dataset": "d1"}
    assert evaluate_rule({"field": "regime", "op": "eq", "value": "test"}, context) == "applies"
    assert evaluate_rule({"field": "regime", "op": "in", "value": ["a", "b"]}, context) == "not_applies"
    assert evaluate_rule({"field": "L", "op": "range", "value": {"min": 16, "max": 64}}, context) == "applies"
    assert evaluate_rule({"field": "L", "op": "range", "value": {"min": 64}}, context) == "not_applies"
    unknown = evaluate_rule({"field": "missing", "op": "eq", "value": 1}, context)
    assert unknown == "unknown"
    assert evaluate_rule({"all": [{"field": "regime", "op": "eq", "value": "test"}]}, context) == "applies"
    assert evaluate_rule({"any": [{"field": "regime", "op": "eq", "value": "x"}]}, context) == "not_applies"
    with pytest.raises(KBError):
        validate_rule({"field": "x", "op": "shell", "value": "rm -rf /"})


def test_blocker_rule_scopes_assessment(api: ResearchKB, reviewer: ResearchKB):
    claim = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "claim",
                    "subkind": "claim",
                    "title": "Rule-scoped claim",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "claim",
                        "statement": "X holds.",
                        "domain_applicability": {"regime": "test", "L": 32},
                        "evidence_criteria": [{"criterion": "derivation"}],
                    },
                },
            }
        ],
    )
    claim_id = claim["created"][0]["object_id"]
    issue_id = capture_issue(api, "Scoped blocker")
    api.propose(
        operations=[
            {
                "op": "link",
                "payload": {
                    "predicate": "blocks",
                    "src_ref": {"object_id": issue_id, "revision": 1},
                    "dst_ref": {"object_id": claim_id, "revision": 1},
                    "qualifiers": {
                        "applies_when": {"field": "regime", "op": "eq", "value": "other"}
                    },
                },
            }
        ],
        auto_apply=True,
    )
    allowed = reviewer.propose(
        operations=[
            {
                "op": "assess_evidence",
                "payload": {
                    "ref": {"object_id": claim_id, "revision": 1},
                    "evidence_state": "supported",
                    "review_state": "reviewed",
                    "rationale": "Rule does not apply in this regime.",
                    "assessed_revision": 1,
                },
            }
        ],
        persist=False,
    )
    assert allowed["status"] == "validated"
    api.propose(
        operations=[
            {
                "op": "link",
                "payload": {
                    "predicate": "blocks",
                    "src_ref": {"object_id": issue_id, "revision": 1},
                    "dst_ref": {"object_id": claim_id, "revision": 1},
                    "qualifiers": {
                        "applies_when": {"field": "missing_dimension", "op": "eq", "value": "x"}
                    },
                },
            }
        ],
        auto_apply=True,
    )
    with pytest.raises(KBError) as error:
        reviewer.propose(
            operations=[
                {
                    "op": "assess_evidence",
                    "payload": {
                        "ref": {"object_id": claim_id, "revision": 1},
                        "evidence_state": "supported",
                        "review_state": "reviewed",
                        "rationale": "Unknown applicability must block.",
                        "assessed_revision": 1,
                    },
                }
            ],
            persist=False,
        )
    assert error.value.code == "BLOCKED"


def test_conflict_reports_small_diff(api: ResearchKB):
    definition = capture_definition(api)
    object_id = definition["created"][0]["object_id"]
    api.propose(
        operations=[
            {
                "op": "revise",
                "payload": {"ref": {"object_id": object_id, "revision": 1}, "title": "Updated spacing"},
            }
        ],
        auto_apply=True,
    )
    with pytest.raises(KBError) as error:
        api.propose(
            operations=[
                {
                    "op": "revise",
                    "payload": {"ref": {"object_id": object_id, "revision": 1}, "title": "Stale"},
                }
            ],
            persist=False,
        )
    assert error.value.code == "REVISION_CONFLICT"
    diff = error.value.details["diff"]
    assert "title" in diff
    assert diff["title"]["to"] == "Updated spacing"


def test_critical_backup_after_high_risk_apply(api: ResearchKB):
    definition = capture_definition(api)
    object_id = definition["created"][0]["object_id"]
    stored = api.propose(
        operations=[
            {
                "op": "tombstone",
                "payload": {"ref": {"object_id": object_id, "revision": 1}, "reason": "withdrawn"},
            }
        ],
        persist=True,
    )
    approval = api.approve(proposal_id=stored["proposal"]["proposal_id"])
    applied = api.apply(
        proposal_id=stored["proposal"]["proposal_id"], approval_token=approval["approval_token"]
    )
    assert applied["committed"] is True
    backup_dir = applied["result"]["critical_backup"]
    assert backup_dir and Path(backup_dir).is_dir()
    assert (Path(backup_dir) / "manifest.json").is_file()


def test_stale_lease_generation_is_fenced(project_root, api: ResearchKB):
    from research_kb.execution import leases
    from research_kb.storage.db import write_tx

    resource = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "resource",
                    "subkind": "cpu",
                    "title": "Fencing host",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "cpu",
                        "machine_id": "fence-host",
                        "capabilities": {},
                        "capacity": 1,
                        "admin_state": "enabled",
                    },
                },
            }
        ],
    )
    resource_id = resource["created"][0]["object_id"]
    with write_tx(api.ctx.conn):
        lease = leases.acquire_lease(
            api.ctx.conn,
            project_id=api.ctx.project_id,
            resource_id=resource_id,
            execution_id="exec-fence",
            seq=api.ctx.latest_seq(),
        )
    with pytest.raises(KBError) as error:
        with write_tx(api.ctx.conn):
            leases.assert_generation_current(api.ctx.conn, lease["lease_id"], lease["generation"] + 1)
    assert error.value.code == "BLOCKED"


def test_reconciliation_external_ownership_is_observe_only(project_root, api: ResearchKB):
    enable_policy(project_root, lambda policy: policy["execution"].update(enabled=True, adapters=["import_only"]))
    refresh(api, project_root)
    study_id = _study(api)
    prepared = _direct(api, "execute_prepare", _prepare_payload(study_id, {"a": 0.9}))
    execution_id = prepared["execution_id"]
    _direct(api, "execute_launch", {"execution_id": execution_id, "adapter": "import_only"})
    result = _direct(
        api,
        "execute_reconcile",
        {
            "execution_id": execution_id,
            "phase": "terminal",
            "payload": {"status": "completed", "ownership": "external", "termination_verified": True},
        },
    )
    assert result["observed_only"] is True
    lease_state = api.ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM resource_leases WHERE owner_execution_id = ?", (execution_id,)
    ).fetchone()["c"]
    assert lease_state == 0
    run_id = prepared["run"]["object_id"]
    run = api.get([{"object_id": run_id}])["records"][0]
    assert run["state_json"]["status"] == "queued"


def test_reconciliation_pid_reuse_is_ambiguous(project_root, api: ResearchKB):
    from research_kb.storage.db import new_id, write_tx

    enable_policy(project_root, lambda policy: policy["execution"].update(enabled=True, adapters=["import_only"]))
    refresh(api, project_root)
    study_id = _study(api)
    prepared = _direct(api, "execute_prepare", _prepare_payload(study_id, {"a": 0.91}))
    execution_id = prepared["execution_id"]
    _direct(api, "execute_launch", {"execution_id": execution_id, "adapter": "import_only"})
    with write_tx(api.ctx.conn):
        api.ctx.conn.execute(
            """
            INSERT INTO executor_receipts
              (receipt_id, project_id, execution_id, phase, payload_json, external_ref, generation,
               receipt_time, dedup_key, recorded_seq)
            VALUES (?, ?, ?, 'accepted', ?, 'pid:111', NULL, 'now', ?, ?)
            """,
            (
                new_id(),
                api.ctx.project_id,
                execution_id,
                json.dumps({"pid": 111, "pid_start_time": "1000"}),
                f"{execution_id}:accepted:pid-seed",
                api.ctx.latest_seq(),
            ),
        )
    result = _direct(
        api,
        "execute_reconcile",
        {
            "execution_id": execution_id,
            "phase": "terminal",
            "payload": {
                "status": "completed",
                "pid": 111,
                "pid_start_time": "2000",
                "termination_verified": True,
            },
        },
    )
    assert result["identity_mismatch"] is True
    assert result["code"] == "EXECUTION_AMBIGUOUS"


def test_reconciliation_boot_change_quarantines(project_root, api: ResearchKB):
    from research_kb.execution import leases
    from research_kb.storage.db import write_tx

    enable_policy(project_root, lambda policy: policy["execution"].update(enabled=True, adapters=["import_only"]))
    refresh(api, project_root)
    study_id = _study(api)
    resource = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "resource",
                    "subkind": "cpu",
                    "title": "Boot host",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "cpu",
                        "machine_id": "boot-host",
                        "capabilities": {},
                        "capacity": 1,
                        "admin_state": "enabled",
                    },
                },
            }
        ],
    )
    resource_id = resource["created"][0]["object_id"]
    payload = _prepare_payload(study_id, {"a": 0.92})
    payload["trial"]["resources"] = [{"resource_id": resource_id}]
    prepared = _direct(api, "execute_prepare", payload)
    execution_id = prepared["execution_id"]
    _direct(api, "execute_launch", {"execution_id": execution_id, "adapter": "import_only"})
    from research_kb.execution import reconciler

    with write_tx(api.ctx.conn):
        reconciler.observe(
            api.ctx.conn,
            project_id=api.ctx.project_id,
            subject_kind="run_attempt",
            subject_id=execution_id,
            status="running",
            detail={},
            worker_boot_id="boot-A",
        )
    result = _direct(
        api,
        "execute_reconcile",
        {
            "execution_id": execution_id,
            "phase": "terminal",
            "payload": {
                "status": "completed",
                "worker_boot_id": "boot-B",
                "termination_verified": True,
            },
        },
    )
    state = api.ctx.conn.execute(
        "SELECT state, quarantine_reason FROM resource_leases WHERE owner_execution_id = ?",
        (execution_id,),
    ).fetchone()
    assert state["state"] == "quarantined"
    assert "rebooted" in (state["quarantine_reason"] or "")


def test_search_pagination_covers_all_candidates(api: ResearchKB):
    for index in range(9):
        apply_auto(
            api,
            [
                {
                    "op": "capture",
                    "payload": {
                        "kind": "knowledge",
                        "subkind": "idea",
                        "title": f"Pagination idea {index}",
                        "record_state": "active",
                        "state_json": {"subkind": "idea", "proposal": f"Pagination topic {index}."},
                    },
                }
            ],
        )
    collected: list[str] = []
    cursor = None
    pages = 0
    while True:
        result = api.search(query="pagination", limit=4, page_cursor=cursor)
        payload = result["result"]
        collected.extend(candidate["object_id"] for candidate in payload["candidates"])
        pages += 1
        if payload["next_page_cursor"] is None or pages > 10:
            break
        cursor = payload["next_page_cursor"]
    assert len(collected) == len(set(collected)) == 9
    assert pages >= 3


def test_embeddings_disabled_by_default(api: ResearchKB):
    from research_kb.retrieval import embeddings

    result = api.search(query="spacing")
    assert result["result"]["embedding"]["enabled"] is False
    with pytest.raises(KBError):
        embeddings.drain(api.ctx)


def test_embeddings_drain_and_semantic_search(project_root, api: ResearchKB):
    from research_kb.retrieval import embeddings
    from research_kb.storage.db import write_tx

    enable_policy(project_root, lambda policy: policy["retrieval"].update(embeddings=True))
    refresh(api, project_root)
    response = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "definition",
                    "title": "Semantic zeta definition",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "A semantic zeta quantity.",
                        "symbol": "zeta",
                        "namespace": "test",
                    },
                },
            }
        ],
    )
    object_id = response["created"][0]["object_id"]
    pending = api.ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM embedding_outbox WHERE project_id = ? AND state = 'pending'",
        (api.ctx.project_id,),
    ).fetchone()["c"]
    assert pending >= 1
    with write_tx(api.ctx.conn):
        summary = embeddings.drain(api.ctx)
    assert summary["embedded"] >= 1
    result = api.search(query="zeta")
    assert result["result"]["embedding"]["enabled"] is True
    assert result["result"]["embedding"]["watermark"]["provider"] == "local_hashing"
    assert any(candidate["object_id"] == object_id for candidate in result["result"]["candidates"])


def test_rrf_fusion_math():
    from research_kb.retrieval.embeddings import reciprocal_rank_fusion

    lexical = [{"object_id": "a", "revision": 1}, {"object_id": "b", "revision": 1}]
    semantic = [{"object_id": "b", "revision": 1}, {"object_id": "c", "revision": 1}]
    fused = reciprocal_rank_fusion([lexical, semantic], k=60)
    ids = [item["object_id"] for item in fused]
    assert ids[0] == "b"
    assert set(ids) == {"a", "b", "c"}
    best = fused[0]
    assert abs(best["fusion_score"] - (1 / 61 + 1 / 62)) < 1e-9


def test_draft_export_edit_and_apply(api: ResearchKB, tmp_path: Path):
    definition = capture_definition(api)
    object_id = definition["created"][0]["object_id"]
    draft_path = tmp_path / "draft.json"
    result = api.export(format="draft", output=str(draft_path))
    assert result["export"]["records"] >= 1
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft["notice"].startswith("GENERATED")
    record = next(item for item in draft["records"] if item["ref"]["object_id"] == object_id)
    record["title"] = "Edited spacing title"
    record["state_json"]["meaning"] = "Edited meaning."
    dry = api.propose_draft(draft=draft, persist=False)
    assert dry["status"] == "validated_dry_run"
    assert any(diff["object_id"] == object_id for diff in dry["proposal"]["draft_diffs"])
    applied = api.propose_draft(draft=draft, persist=True, auto_apply=True)
    assert applied["status"] == "applied"
    bundle = api.get([{"object_id": object_id}])["records"][0]
    assert bundle["title"] == "Edited spacing title"
    assert bundle["state_json"]["meaning"] == "Edited meaning."


def test_draft_stale_revision_conflicts(api: ResearchKB, tmp_path: Path):
    definition = capture_definition(api)
    object_id = definition["created"][0]["object_id"]
    draft_path = tmp_path / "draft.json"
    api.export(format="draft", output=str(draft_path))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    api.propose(
        operations=[
            {
                "op": "revise",
                "payload": {"ref": {"object_id": object_id, "revision": 1}, "title": "Concurrent edit"},
            }
        ],
        auto_apply=True,
    )
    record = next(item for item in draft["records"] if item["ref"]["object_id"] == object_id)
    record["title"] = "Stale draft edit"
    with pytest.raises(KBError) as error:
        api.propose_draft(draft=draft, persist=False)
    assert error.value.code == "REVISION_CONFLICT"


def test_ownership_leases(project_root: Path, api: ResearchKB):
    from tests.conftest import add_actor

    from tests.test_work_evidence import capture_work

    work = capture_work(api, title="Lease work")
    object_id = work["created"][0]["object_id"]
    claimed = api.propose(
        operations=[
            {
                "op": "claim_work",
                "payload": {
                    "ref": {"object_id": object_id, "revision": 1},
                    "expected_revision": 1,
                    "ttl_seconds": 3600,
                },
            }
        ],
        auto_apply=True,
    )
    assert claimed["status"] == "applied"
    lease_id = claimed["details"]["claim_work"][0]["lease_id"]
    add_actor(project_root, "other-agent", roles=["contributor"])
    other = ResearchKB(open_service(project_root=project_root, actor_id="other-agent"))
    try:
        with pytest.raises(KBError) as error:
            other.propose(
                operations=[
                    {
                        "op": "claim_work",
                        "payload": {"ref": {"object_id": object_id, "revision": 1}, "expected_revision": 1},
                    }
                ],
                auto_apply=True,
            )
        assert error.value.code == "BLOCKED"
    finally:
        other.close()
    released = api.propose(
        operations=[{"op": "release_work", "payload": {"lease_id": lease_id, "reason": "checkpoint"}}],
        auto_apply=True,
    )
    assert released["status"] == "applied"


def test_supersede_redirects_aliases(api: ResearchKB):
    old = capture_definition(api)
    old_id = old["created"][0]["object_id"]
    replacement = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "definition",
                    "title": "Replacement spacing",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "definition",
                        "meaning": "Replacement meaning.",
                        "symbol": "a2",
                        "namespace": "lattice",
                    },
                },
            }
        ],
    )
    replacement_id = replacement["created"][0]["object_id"]
    stored = api.propose(
        operations=[
            {
                "op": "retire",
                "payload": {
                    "ref": {"object_id": old_id, "revision": 1},
                    "superseded_by_ref": {"object_id": replacement_id, "revision": 1},
                    "reason": "Definition corrected by the replacement.",
                },
            }
        ],
        persist=True,
    )
    approval = api.approve(proposal_id=stored["proposal"]["proposal_id"])
    applied = api.apply(
        proposal_id=stored["proposal"]["proposal_id"], approval_token=approval["approval_token"]
    )
    assert applied["committed"] is True
    resolved = api.get([{"object_id": "lattice spacing"}])["records"][0]
    assert resolved["object_id"] == replacement_id


def test_jsonl_export_lists_missing_blobs(api: ResearchKB, tmp_path: Path):
    from research_kb.storage.db import utc_now, write_tx

    with write_tx(api.ctx.conn):
        api.ctx.conn.execute(
            "INSERT INTO blob_registry (blob_hash, byte_size, media_type, assurance, created_at) "
            "VALUES (?, 10, 'text/plain', 'content_sha256', ?)",
            ("f" * 64, utc_now()),
        )
    output = tmp_path / "export.jsonl"
    api.export(format="jsonl", output=str(output))
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    omissions = next(line for line in lines if line["type"] == "export_omissions")
    assert any(item["blob"] == "f" * 64 for item in omissions["missing_registered_blobs"])


def test_crash_before_commit_rolls_back(project_root: Path):
    routing = load_routing(project_root)
    code = (
        "import os, sys\n"
        "from research_kb.storage.db import connect\n"
        "conn = connect(sys.argv[1])\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "conn.execute(\"INSERT INTO controller_meta (key, value) VALUES ('crash-before', 'x')\")\n"
        "os._exit(1)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    subprocess.run([sys.executable, "-c", code, str(routing.db_path)], env=env, check=False)
    conn = open_service(project_root=project_root, actor_id="admin").conn
    try:
        assert conn.execute("SELECT 1 FROM controller_meta WHERE key = 'crash-before'").fetchone() is None
    finally:
        conn.close()


def test_crash_after_commit_persists(project_root: Path):
    routing = load_routing(project_root)
    code = (
        "import os, sys\n"
        "from research_kb.storage.db import connect\n"
        "conn = connect(sys.argv[1])\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "conn.execute(\"INSERT INTO controller_meta (key, value) VALUES ('crash-after', 'y')\")\n"
        "conn.execute('COMMIT')\n"
        "os._exit(1)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    subprocess.run([sys.executable, "-c", code, str(routing.db_path)], env=env, check=False)
    ctx = open_service(project_root=project_root, actor_id="admin")
    try:
        row = ctx.conn.execute("SELECT value FROM controller_meta WHERE key = 'crash-after'").fetchone()
        assert row["value"] == "y"
    finally:
        ctx.close()


def test_fts_rebuild_reprojects_canonical_revisions(project_root: Path, api: ResearchKB, tmp_path: Path):
    from research_kb.ingestion import extract as extraction
    from research_kb.storage import blobs
    from research_kb.storage.search import check_integrity, rebuild_projections

    source_file = tmp_path / "source.md"
    source_file.write_text(
        "# Section One\n\nThe rebuildable marker appears here.\n", encoding="utf-8"
    )
    apply_auto(
        api,
        [
            {
                "op": "register_source",
                "payload": {
                    "subkind": "note",
                    "title": "Rebuild source",
                    "version": "v1",
                    "identity_assurance": "content_sha256",
                    "preservation": "local_copy_allowed",
                    "captured_path": str(source_file),
                },
            }
        ],
    )
    capture_definition(api)
    with api.ctx.conn:
        pass
    api.ctx.conn.execute("DELETE FROM search_documents")
    routing = load_routing(project_root)

    def resolver(project_id: str, object_id: str, revision: int):
        row = api.ctx.conn.execute(
            """
            SELECT extraction_blob_hash FROM source_extractions
            WHERE project_id = ? AND source_object_id = ? AND source_revision = ?
            ORDER BY created_seq DESC LIMIT 1
            """,
            (project_id, object_id, revision),
        ).fetchone()
        if row is None or not row["extraction_blob_hash"]:
            return []
        text = blobs.read_blob(routing.extractions_dir.parent, row["extraction_blob_hash"]).decode(
            "utf-8", errors="replace"
        )
        return extraction.attach_line_ranges(text, extraction.chunk_markdown(text))

    result = rebuild_projections(api.ctx.conn, project_id=api.ctx.project_id, section_resolver=resolver)
    assert result["revisions_projected"] >= 2
    assert result["sections_projected"] >= 1
    assert check_integrity(api.ctx.conn)["healthy"] is True
    hits = api.search(query="rebuildable")["result"]["candidates"]
    assert hits


def test_coverage_counts_only_eligible_evidence(project_root, api: ResearchKB):
    from research_kb.execution.slots import coverage

    enable_policy(project_root, lambda policy: policy["execution"].update(enabled=True, adapters=["import_only"]))
    refresh(api, project_root)
    study_id = _study(api)
    prepared = _direct(api, "execute_prepare", _prepare_payload(study_id, {"a": 0.77}))
    execution_id = prepared["execution_id"]
    _direct(api, "execute_launch", {"execution_id": execution_id, "adapter": "import_only"})
    study_row = api.ctx.conn.execute(
        "SELECT current_revision FROM objects WHERE object_id = ?", (study_id,)
    ).fetchone()
    pending = coverage(api.ctx.conn, api.ctx.project_id, study_id, study_row["current_revision"])
    assert pending["denominator"] == 1
    assert pending["covered"] == 0
    assert pending["attempted_but_ineligible"]
    _direct(
        api,
        "execute_reconcile",
        {
            "execution_id": execution_id,
            "phase": "terminal",
            "payload": {"status": "completed", "validity": "valid", "termination_verified": True},
        },
    )
    covered = coverage(api.ctx.conn, api.ctx.project_id, study_id, study_row["current_revision"])
    assert covered["covered"] == 1
    assert covered["missing_slots"] == []


def _capture_study_run(api: ResearchKB) -> tuple[str, str]:
    study_id = _study(api)
    run = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "run",
                    "subkind": "attempt",
                    "title": "Selection run attempt",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "attempt",
                        "study_ref": {"object_id": study_id, "revision": 1},
                        "attempt_no": 1,
                        "manifest": {"code_identity": "git:selection"},
                        "status": "completed",
                        "validity": "valid",
                    },
                },
            }
        ],
    )
    return study_id, run["created"][0]["object_id"]


def _capture_claim_object(api: ResearchKB, title: str) -> str:
    claim = apply_auto(
        api,
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
                        "statement": "X holds.",
                        "domain_applicability": {"regime": "test"},
                        "evidence_criteria": [{"criterion": "derivation"}],
                    },
                },
            }
        ],
    )
    return claim["created"][0]["object_id"]


def test_comparison_assessment_conflict_rejected(api: ResearchKB):
    _, run_id = _capture_study_run(api)
    target_id = _capture_claim_object(api, title="Comparison conflict target")
    with pytest.raises(KBError) as error:
        _direct(
            api,
            "assess_comparison",
            {
                "run_ref": {"object_id": run_id, "revision": 1},
                "target_ref": {"object_id": target_id, "revision": 1},
                "assessment": "eligible",
                "dimensions": [
                    {"name": "preprocessing", "run_value": "none", "target_value": "z-score"}
                ],
                "rationale": "Claimed eligible despite a mismatched dimension.",
            },
        )
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"


def test_selection_manifest_records_default_selection(api: ResearchKB):
    _, run_id = _capture_study_run(api)
    target = apply_auto(
        api,
        [
            {
                "op": "register_artifact",
                "payload": {
                    "subkind": "manuscript_target",
                    "role": "figure-2",
                    "role_note": "Figure 2",
                    "content_identity": {"kind": "manifest", "value": "figure-2-v0"},
                    "assurance": "manifest",
                    "availability": "available",
                },
            }
        ],
    )
    target_id = target["created"][0]["object_id"]
    result = _direct(
        api,
        "create_selection_manifest",
        {
            "target_ref": {"object_id": target_id, "revision": 1},
            "input_refs": [{"object_id": run_id, "revision": 1}],
            "inclusion_rule": "earliest attempt passing validity checks",
            "generated_artifact_hashes": ["sha256:def"],
        },
    )
    manifest = result["selection_manifest"]
    assert manifest["selected_input_ref"]["object_id"] == run_id
    assert manifest["selection_reason"]
    with pytest.raises(KBError):
        _direct(
            api,
            "create_selection_manifest",
            {
                "target_ref": {"object_id": target_id, "revision": 2},
                "input_refs": [{"object_id": run_id, "revision": 1}],
                "inclusion_rule": "aggregate independent replicates",
                "selection_mode": "aggregate",
                "generated_artifact_hashes": ["sha256:ghi"],
            },
        )


def _study(api: ResearchKB) -> str:
    response = apply_auto(
        api,
        [
            {
                "op": "capture",
                "payload": {
                    "kind": "study",
                    "subkind": "numerical",
                    "title": "Completeness study",
                    "record_state": "active",
                    "state_json": {
                        "subkind": "numerical",
                        "question": "Does it run?",
                        "protocol": {"method_profile": "numerical"},
                        "required_outputs": [{"name": "x"}],
                        "completion_criteria": ["done"],
                        "study_state": "active",
                        "executable": True,
                    },
                },
            }
        ],
    )
    return response["created"][0]["object_id"]


def _prepare_payload(study_id: str, conditions: dict) -> dict:
    return {
        "study_ref": {"object_id": study_id},
        "trial": {
            "conditions": conditions,
            "method_profile": "numerical",
            "adapter": "import_only",
            "code_identity": "git:test",
            "environment_identity": "env",
            "inputs_identity": "inputs",
        },
    }


def _direct(api: ResearchKB, operation: str, payload: dict) -> dict:
    from research_kb.service.operations import apply_operations

    batch = apply_operations(
        api.ctx,
        operations=[{"op": operation, "payload": payload}],
        reason="completeness test",
        action="proposal_apply",
    )
    return batch.details[operation][0]
