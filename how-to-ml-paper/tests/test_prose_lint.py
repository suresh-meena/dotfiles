"""Coverage for the how-to-ml-paper prose linter."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "how-to-ml-paper"
SCRIPT = SKILL_DIR / "scripts" / "prose_lint.py"

sys.path.insert(0, str(SCRIPT.parent))

import prose_lint  # noqa: E402


CLEAN_ABSTRACT = (
    "Retrieval augments a frozen language model with passages from Wikipedia. "
    "We train the retriever with contrastive supervision on Natural Questions. "
    "Retrieval raises exact match from 41.2 to 46.8 on the test split across three random seeds, "
    "with less than 2 percent added latency. "
    "Accuracy gains persist under distribution shift to TriviaQA, although the margin narrows to 1.9 points. "
    "We release code and preprocessing scripts to support reproduction."
)


def rule_names(text: str, profile: str = "strict-house") -> set[str]:
    return {f.rule for f in prose_lint.lint_text(text, "<test>", profile)}


def test_clean_abstract_passes_strict_house():
    assert prose_lint.lint_text(CLEAN_ABSTRACT, "<test>", "strict-house") == []


def test_strict_flags_throat_opener_summary_and_enthusiasm():
    text = "This section discusses the method. Our method has better performance! In summary, it is exciting."
    rules = rule_names(text)
    assert "throat-clearing-opener" in rules
    assert "summary-beat" in rules
    assert "performed-enthusiasm" in rules


def test_strict_flags_in_this_section_announcement():
    text = "In this section, we describe our optimization procedure."
    assert "throat-clearing-opener" in rule_names(text)


def test_strict_flags_house_register():
    text = "We leverage the baseline to underscore the gain, which is arguably quite significant."
    rules = rule_names(text)
    assert "corporate-register" in rules
    assert "empty-hedge" in rules


def test_masking_ignores_fenced_code_headings_and_latex_comments():
    text = (
        "# In summary, an exciting heading\n"
        "```\n"
        "In summary, it is exciting!\n"
        "```\n"
        "% In summary, it is exciting!\n"
        "The method improves top-1 accuracy by 2.4 points across three random seeds, "
        "and the margin holds on a held-out shift where tuning budgets match exactly."
    )
    assert prose_lint.lint_text(text, "<test>", "strict-house") == []


def test_synthetic_flags_connector_and_vague_result_clusters():
    lines = [
        "Moreover, the method shows better performance.",
        "Moreover, the method shows strong performance.",
        "Moreover, the method shows promising results.",
        "Moreover, the method shows significant improvement.",
        "Moreover, the method shows robust generalization.",
    ]
    findings = prose_lint.lint_text("\n".join(lines), "<test>", "synthetic-prose")
    rules = {f.rule for f in findings}
    assert "A03" in rules
    assert "A15" in rules
    assert all(f.severity == "review" for f in findings)


def test_synthetic_single_connector_is_weak_evidence():
    text = "Moreover, the method improves top-1 accuracy by 2.4 points on the test split."
    assert prose_lint.lint_text(text, "<test>", "synthetic-prose") == []


def test_json_findings_carry_expected_shape():
    findings = prose_lint.lint_text("In summary, it is exciting!", "<test>", "strict-house")
    assert findings, "expected at least one finding"
    payload = json.loads(json.dumps([f.__dict__ for f in findings]))
    required = {"path", "line", "column", "rule", "severity", "basis", "message", "excerpt"}
    for item in payload:
        assert required <= set(item)


def run_cli(*args: str, stdin_text: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin_text,
        text=True,
        capture_output=True,
        cwd=str(cwd or SKILL_DIR),
        check=False,
    )


def test_cli_exit_zero_on_clean_file(tmp_path: Path):
    target = tmp_path / "clean.md"
    target.write_text(CLEAN_ABSTRACT, encoding="utf-8")
    proc = run_cli(str(target))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_exit_one_and_json_on_flagged_stdin():
    proc = run_cli("--format", "json", stdin_text="In summary, it is exciting!")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload and payload[0]["rule"] in {"summary-beat", "performed-enthusiasm"}


def test_cli_exit_two_on_missing_file(tmp_path: Path):
    proc = run_cli(str(tmp_path / "no-such-file.md"))
    assert proc.returncode == 2
