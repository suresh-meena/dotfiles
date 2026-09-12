from __future__ import annotations

import json
from pathlib import Path

from research_kb.service.api import ResearchKB
from research_kb.service.backup import restore_backup
from tests.conftest import apply_auto, capture_definition


def test_markdown_export_is_marked_noncanonical(api: ResearchKB, tmp_path: Path):
    capture_definition(api)
    output = tmp_path / "exports" / "status.md"
    result = api.export(format="markdown", output=str(output))
    content = output.read_text(encoding="utf-8")
    assert "GENERATED — NOT CANONICAL" in content
    assert api.ctx.project_id in content
    assert result["export"]["bytes"] > 0


def test_jsonl_export_contains_revisions_and_manifest(api: ResearchKB, tmp_path: Path):
    capture_definition(api)
    output = tmp_path / "export.jsonl"
    api.export(format="jsonl", output=str(output))
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    types = {line["type"] for line in lines}
    assert "export_manifest" in types
    assert "revision" in types
    assert "export_omissions" in types


def test_verify_reports_findings(api: ResearchKB):
    capture_definition(api)
    result = api.verify(checks=["integrity", "references", "citations", "index"])
    finding_names = {finding["check"] for finding in result["result"]["findings"]}
    assert "integrity" in finding_names
    assert "search_index" in finding_names
    assert result["result"]["summary"]["errors"] == 0


def test_backup_and_restore_read_only_reconciliation(api: ResearchKB, tmp_path: Path):
    capture_definition(api)
    backup = api.backup(tag="test")
    backup_dir = Path(backup["backup"]["backup_dir"])
    assert (backup_dir / "research.db").exists()
    assert (backup_dir / "manifest.json").exists()
    restored = restore_backup(source=backup_dir, destination=tmp_path / "restored")
    assert restored["flags"]["mode"] == "read_only_reconciliation"
    assert restored["flags"]["dispatch_enabled"] is False
    assert restored["verification"]["integrity"] == "ok"
    assert (Path(restored["restored_to"]) / "restore.json").exists()
    from research_kb.storage.db import connect

    conn = connect(Path(restored["restored_to"]) / "research.db", read_only=True)
    try:
        row = conn.execute("SELECT value FROM controller_meta WHERE key = 'controller_epoch'").fetchone()
        assert row["value"] != api.ctx.epoch
        mode = conn.execute("SELECT value FROM controller_meta WHERE key = 'restore_mode'").fetchone()
        assert mode["value"] == "read_only_reconciliation"
    finally:
        conn.close()


def test_blob_gc_requires_grace_and_dry_run(api: ResearchKB):
    from research_kb.storage import blobs

    plan = blobs.gc_plan(api.ctx.conn, root=api.ctx.routing.sources_dir.parent, grace_seconds=0, dry_run=True)
    assert isinstance(plan, list)
