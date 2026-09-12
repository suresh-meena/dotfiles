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


def test_editorial_is_default_and_findings_are_advisory():
    text = (
        "It is worth noting that we conduct an analysis. "
        "In summary, the method is really exciting and yields better performance."
    )
    findings = prose_lint.lint_text(text, "draft.md")
    assert findings == prose_lint.lint_text(text, "draft.md", "editorial")
    assert {finding.rule for finding in findings} == {
        "rhetorical-crutch", "nominalization", "summary-beat",
        "filler-intensifier", "performed-enthusiasm", "vague-result",
    }
    assert all(finding.severity == "review" and finding.basis == "D" for finding in findings)


@pytest.mark.parametrize("text", [
    r"Under Assumption~\ref{ass:bounded}, $0 < \eta < 2/L$ is sufficient, not necessary, for stability.",
    "Prior work analyzes a convex objective, whereas our guarantee assumes local smoothness.",
    "We estimate paired differences rather than compare independent means.",
    "The loss decreases; the constraint remains active.",
    "We report accuracy, latency, and memory.",
    "We reflect the vector across the constraint plane.",
    "The confidence interval reflects uncertainty across five seeds.",
    "No seed reaches the target. No run satisfies the constraint.",
    "We fit the model. We test the model.",
    "Overall accuracy increases by 2.4 points on the held-out split.",
    r"The state space contains $n!$ permutations.",
])
def test_editorial_accepts_natural_scientific_constructions(text: str):
    assert prose_lint.lint_text(text, "draft.tex", "editorial") == []


@pytest.mark.parametrize(("text", "rule"), [
    ("The bound is not global but local.", "corrective-negation"),
    ("The baseline converges, whereas the variant diverges.", "contrast-pair"),
    ("The loss decreases; the constraint remains active.", "parataxis-candidate"),
    ("We report accuracy, latency, and memory.", "rule-of-three"),
    ("We reflect the vector across the constraint plane.", "corporate-register"),
    ("We fit the model. We test the model.", "uniform-sentence-length"),
])
def test_explicit_strict_keeps_existing_house_rules(text: str, rule: str):
    findings = prose_lint.lint_text(text, "draft.md", "strict-house")
    assert rule in {finding.rule for finding in findings}
    assert all(finding.severity == "error" and finding.basis == "U" for finding in findings)
    assert prose_lint.lint_text(text, "draft.md", "editorial") == []


@pytest.mark.parametrize("profile", ["editorial", "strict-house"])
def test_masking_preserves_source_columns(profile: str):
    text = "  $x + y$ `code` https://example.org In summary, the estimate rises."
    findings = prose_lint.lint_text(text, "draft.md", profile)
    summary = next(finding for finding in findings if finding.rule == "summary-beat")
    assert (summary.line, summary.column) == (1, text.index("In summary") + 1)


def test_synthetic_masking_preserves_source_columns():
    line = "$x$ `code` https://example.org The model has better performance."
    findings = prose_lint.lint_text("\n".join([line] * 4), "draft.md", "synthetic-prose")
    vague = next(finding for finding in findings if finding.rule == "A15")
    assert (vague.line, vague.column) == (4, line.index("better performance") + 1)


@pytest.mark.parametrize("path", ["draft.md", "draft.txt", "<stdin>"])
def test_inline_percent_in_prose_does_not_hide_findings(path: str):
    text = "Accuracy reaches 95%, which is remarkable."
    findings = prose_lint.lint_text(text, path)
    assert [finding.rule for finding in findings] == ["performed-enthusiasm"]
    assert findings[0].column == text.index("remarkable") + 1


@pytest.mark.parametrize("path", ["draft.tex", "draft.ltx", "draft.TEX"])
def test_tex_comments_are_masked_but_escaped_percent_is_prose(path: str):
    assert prose_lint.lint_text("Accuracy rises. % In summary, it is exciting!", path) == []
    text = r"Accuracy reaches 95\%, which is remarkable."
    findings = prose_lint.lint_text(text, path)
    assert [finding.rule for finding in findings] == ["performed-enthusiasm"]
    assert findings[0].column == text.index("remarkable") + 1


def test_masked_lines_keep_original_lengths():
    lines = [
        "# Exciting heading", "```", "exciting code", "```",
        r"\[", "exciting math", r"\]", "$x$ `code` https://example.org",
        "% exciting comment", "The estimate rises. % exciting comment",
    ]
    assert [len(line) for line in prose_lint.mask_nonprose(lines, "draft.tex")] == [
        len(line) for line in lines
    ]


def test_cli_default_json_uses_advisory_findings():
    proc = run_cli("--format", "json", stdin_text="The method yields better performance.")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert len(payload) == 1
    assert payload[0] == {
        "path": "<stdin>", "line": 1, "column": 19, "rule": "vague-result",
        "severity": "review", "basis": "D",
        "message": "Check that nearby text supplies the metric, comparison, and setting for this result.",
        "excerpt": "The method yields better performance.",
    }
    assert not proc.stderr


def test_cli_default_and_explicit_strict_remain_distinct():
    text = "We report accuracy, latency, and memory."
    advisory = run_cli("--format", "json", stdin_text=text)
    strict = run_cli("--format", "json", "--profile", "strict-house", stdin_text=text)
    assert advisory.returncode == 0, advisory.stdout + advisory.stderr
    assert json.loads(advisory.stdout) == []
    assert strict.returncode == 1, strict.stdout + strict.stderr
    findings = json.loads(strict.stdout)
    assert {finding["rule"] for finding in findings} == {"rule-of-three"}
    assert all(finding["severity"] == "error" and finding["basis"] == "U" for finding in findings)


def test_cli_explicit_editorial_and_synthetic_profiles():
    text = "Moreover, the method shows better performance.\n" * 5
    editorial = run_cli("--format", "json", "--profile", "editorial", stdin_text=text)
    synthetic = run_cli("--format", "json", "--profile", "synthetic-prose", stdin_text=text)
    assert editorial.returncode == synthetic.returncode == 1
    assert {item["rule"] for item in json.loads(editorial.stdout)} == {"vague-result"}
    assert {item["rule"] for item in json.loads(synthetic.stdout)} == {"A03", "A15"}


def test_cli_exit_two_on_invalid_utf8(tmp_path: Path):
    target = tmp_path / "invalid.md"
    target.write_bytes(b"\xff")
    proc = run_cli("--format", "json", str(target))
    assert proc.returncode == 2
    assert str(target) in proc.stderr
    assert not proc.stdout
