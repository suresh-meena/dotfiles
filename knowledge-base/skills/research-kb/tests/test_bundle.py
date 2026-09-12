from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import kb_validation  # noqa: E402


def run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *args],
        capture_output=True,
        text=True,
        cwd=str(SKILL_ROOT),
    )


def test_frontmatter_name_and_description():
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: research-kb" in text
    assert "description:" in text
    assert "compatibility:" in text


def test_required_references_exist_and_have_titles():
    references = sorted((SKILL_ROOT / "references").glob("*.md"))
    assert len(references) == 10
    for reference in references:
        content = reference.read_text(encoding="utf-8")
        assert content.lstrip().startswith("# "), reference.name


def test_lint_skill_passes():
    result = run_script("lint_skill.py")
    payload = json.loads(result.stdout)
    assert payload["ok"] is True, payload["errors"]
    assert result.returncode == 0


def test_bundled_schemas_parse():
    for name in kb_validation.list_schemas():
        schema = kb_validation.load_schema(name)
        assert schema["type"] == "object"
        assert "$schema" in schema


def test_valid_examples_pass_and_invalid_examples_fail():
    expectations = {
        "capture.valid.json": ("capture", True),
        "capture.invalid.json": ("capture", False),
        "proposal.valid.json": ("proposal", True),
        "proposal.invalid.json": ("proposal", False),
        "context.valid.json": ("context", True),
        "handoff.valid.json": ("handoff", True),
        "handoff.invalid.json": ("handoff", False),
    }
    for filename, (schema_name, should_pass) in expectations.items():
        path = SKILL_ROOT / "assets" / "examples" / filename
        result = kb_validation.validate_file(schema_name, path)
        assert result["valid"] is should_pass, (filename, result["errors"])


def test_validate_payload_cli_exit_codes():
    valid = run_script(
        "validate_payload.py",
        "--schema",
        "capture",
        "--input",
        str(SKILL_ROOT / "assets" / "examples" / "capture.valid.json"),
    )
    invalid = run_script(
        "validate_payload.py",
        "--schema",
        "capture",
        "--input",
        str(SKILL_ROOT / "assets" / "examples" / "capture.invalid.json"),
    )
    assert valid.returncode == 0
    assert json.loads(valid.stdout)["valid"] is True
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout)["valid"] is False


def test_validate_payload_rejects_duplicate_keys(tmp_path: Path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"op": "capture", "op": "capture"}', encoding="utf-8")
    result = run_script("validate_payload.py", "--schema", "capture", "--input", str(duplicate))
    assert result.returncode == 2
    assert any("Duplicate JSON key" in error for error in json.loads(result.stdout)["errors"])


def test_doctor_reports_environment(tmp_path: Path):
    result = run_script("doctor.py", "--project-root", str(tmp_path))
    payload = json.loads(result.stdout)
    assert payload["python_ok"] is True
    assert payload["sqlite"]["sqlite_version"]
    assert payload["routing_file"] is None
    assert payload["rkb_discoverable"] in (None, "rkb") or isinstance(payload["rkb_discoverable"], str)


def test_doctor_reads_routing_without_opening_database(tmp_path: Path):
    routing = tmp_path / ".research" / "project.toml"
    routing.parent.mkdir(parents=True)
    routing.write_text(
        "[project]\nid = \"00000000-0000-4000-8000-000000000000\"\nname = \"x\"\n\n[state]\nroot = \".research/state\"\n",
        encoding="utf-8",
    )
    result = run_script("doctor.py", "--project-root", str(tmp_path))
    payload = json.loads(result.stdout)
    assert payload["routing"]["project_id"] == "00000000-0000-4000-8000-000000000000"
    assert payload["routing"]["state_root"] == ".research/state"


def test_scenarios_are_behavior_specifications():
    payload = json.loads((SKILL_ROOT / "evals" / "scenarios.json").read_text(encoding="utf-8"))
    categories = {scenario["category"] for scenario in payload["scenarios"]}
    assert "positive_activation" in categories
    assert "negative_activation" in categories
    assert "failure_injection" in categories
    assert len(payload["scenarios"]) >= 12
    for scenario in payload["scenarios"]:
        assert scenario["expected_behaviors"]
        assert scenario["prohibited_behaviors"]


def test_skill_size_budget():
    assert (SKILL_ROOT / "SKILL.md").stat().st_size <= 12288


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
