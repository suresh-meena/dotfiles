#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_validation import load_schema

SKILL_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = [
    "SKILL.md",
    "README.md",
    "agents/openai.yaml",
    "references/01-design-and-audit.md",
    "references/02-data-model.md",
    "references/03-capture-and-sources.md",
    "references/04-retrieval.md",
    "references/05-progress-and-publication.md",
    "references/06-execution.md",
    "references/07-safety-and-operations.md",
    "references/08-tool-contracts.md",
    "references/09-skill-and-delivery.md",
    "references/10-sources.md",
    "assets/project.example.toml",
    "assets/schemas/capture.schema.json",
    "assets/schemas/proposal.schema.json",
    "assets/schemas/context.schema.json",
    "assets/schemas/handoff.schema.json",
    "scripts/kb_validation.py",
    "scripts/doctor.py",
    "scripts/validate_payload.py",
    "scripts/lint_skill.py",
    "evals/scenarios.json",
    "evals/rubric.md",
    "tests/test_bundle.py",
    "requirements-validation.txt",
]
REQUIRED_FRONTMATTER = ["name", "description", "compatibility", "metadata"]
SKILL_SIZE_BUDGET = 12288
REFERENCE_SIZE_BUDGET = 32768
LINK_PATTERN = re.compile(r"\[[^\]]+\]\((?!https?://|#)([^)\s]+)\)")


def parse_frontmatter(text: str) -> tuple[dict[str, str], list[str]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, ["SKILL.md must start with YAML frontmatter delimited by ---."]
    values: dict[str, str] = {}
    errors: list[str] = []
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"')
    if not closed:
        errors.append("Frontmatter is missing a closing --- delimiter.")
    return values, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lint_skill.py", description="Validate the skill package layout.")
    parser.parse_args(argv)
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []
    for relative in REQUIRED_FILES:
        if not (SKILL_ROOT / relative).is_file():
            errors.append(f"Missing required file: {relative}")
    checks.append(f"required_files_checked={len(REQUIRED_FILES)}")
    skill_path = SKILL_ROOT / "SKILL.md"
    if skill_path.is_file():
        text = skill_path.read_text(encoding="utf-8")
        frontmatter, frontmatter_errors = parse_frontmatter(text)
        errors.extend(frontmatter_errors)
        for field in REQUIRED_FRONTMATTER:
            if field not in frontmatter:
                errors.append(f"Frontmatter is missing '{field}'.")
        if frontmatter.get("name") != SKILL_ROOT.name:
            errors.append(
                f"Frontmatter name '{frontmatter.get('name')}' does not match directory '{SKILL_ROOT.name}'."
            )
        if len(text.encode("utf-8")) > SKILL_SIZE_BUDGET:
            errors.append(f"SKILL.md exceeds the {SKILL_SIZE_BUDGET}-byte budget.")
        for match in LINK_PATTERN.finditer(text):
            target = match.group(1).split("#", 1)[0]
            if target and not (SKILL_ROOT / target).exists():
                errors.append(f"SKILL.md links to a missing resource: {target}")
        checks.append("frontmatter_and_links_checked")
    for reference in sorted((SKILL_ROOT / "references").glob("*.md")):
        if len(reference.read_bytes()) > REFERENCE_SIZE_BUDGET:
            errors.append(f"{reference.name} exceeds the {REFERENCE_SIZE_BUDGET}-byte budget.")
        text = reference.read_text(encoding="utf-8")
        if not text.lstrip().startswith("#"):
            errors.append(f"{reference.name} does not start with an H1 title.")
        for match in LINK_PATTERN.finditer(text):
            target = match.group(1).split("#", 1)[0]
            if target and not (reference.parent / target).exists():
                errors.append(f"{reference.name} links to a missing file: {target}")
    checks.append("references_checked")
    for schema_path in sorted((SKILL_ROOT / "assets" / "schemas").glob("*.schema.json")):
        name = schema_path.name.split(".")[0]
        try:
            schema = load_schema(name)
        except Exception as exc:
            errors.append(f"Bundled schema {schema_path.name} is invalid: {exc}")
            continue
        if schema.get("type") != "object":
            warnings.append(f"Bundled schema {schema_path.name} does not declare an object type.")
    checks.append("schemas_checked")
    if not (SKILL_ROOT / "evals" / "scenarios.json").is_file():
        errors.append("evals/scenarios.json is required.")
    result = {
        "ok": not errors,
        "skill_root": str(SKILL_ROOT),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "note": (
            "A clean lint proves package structure and link/JSON parsing only. It does not prove "
            "host compatibility, runtime availability, or agent task success."
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
