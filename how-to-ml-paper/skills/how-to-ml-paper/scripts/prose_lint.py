#!/usr/bin/env python3
"""Flag house-style violations and repeated synthetic-prose patterns."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    column: int
    rule: str
    severity: str
    basis: str
    message: str
    excerpt: str


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    message: str


def compile_rule(name: str, pattern: str, message: str, flags: int = re.IGNORECASE) -> Rule:
    return Rule(name, re.compile(pattern, flags), message)


LINE_RULES = (
    compile_rule("em-dash", r"—", "Replace the em dash with punctuation that fits the grammar.", 0),
    compile_rule(
        "corrective-negation",
        r"\b(?:not\s+(?:only\s+)?[^.!?;]{1,100}\s+but(?:\s+also)?|not\s+[^.!?;]{1,80}\s+rather\s+than)\b",
        "State the intended claim directly without corrective negation.",
    ),
    compile_rule(
        "contrast-pair",
        r"\b(?:on the one hand|on the other hand|by contrast|in contrast|conversely|whereas|rather than)\b",
        "Rewrite the contrast as direct claims with distinct sentence shapes.",
    ),
    compile_rule(
        "rhetorical-crutch",
        r"\b(?:it is worth noting|it should be noted|importantly|interestingly|notably|needless to say)\b",
        "Begin with the substantive claim.",
    ),
    compile_rule(
        "summary-beat",
        r"\b(?:in summary|to summarize|in conclusion|overall|all in all|taken together|to sum up)\b",
        "Remove the recap and end on the final piece of evidence or analysis.",
    ),
    compile_rule(
        "setup-payoff",
        r"\b(?:as we shall see|as we will see|the key (?:point|idea) is|what matters is|the important thing is|as follows)\b",
        "State the substantive content at its point of use.",
    ),
    compile_rule(
        "parataxis-candidate",
        r";",
        "Check for parataxis and connect the clauses through explicit syntax.",
        0,
    ),
    compile_rule(
        "filler-intensifier",
        r"\b(?:genuinely|really|truly|actually)\b",
        "Remove the filler intensifier or replace it with a measurable statement.",
    ),
    compile_rule(
        "corporate-register",
        r"\b(?:leverage|leverages|leveraged|leveraging|underscore|underscores|underscored|underscoring|reflect|reflects|reflected|reflecting)\b",
        "Use a concrete verb that names the action.",
    ),
    compile_rule(
        "empty-hedge",
        r"\b(?:arguably|perhaps|somewhat|rather|fairly|quite|possibly|potentially|seemingly|apparently|in some sense|to some extent|kind of|sort of)\b",
        "Replace the hedge with the exact scope or source of uncertainty.",
    ),
    compile_rule(
        "performed-enthusiasm",
        r"\b(?:exciting|excitingly|remarkable|remarkably|groundbreaking|revolutionary|game-changing|amazing)\b|!",
        "Replace performed enthusiasm with evidence.",
    ),
    compile_rule(
        "nominalization",
        r"\b(?:conduct(?:ed|s|ing)? an? (?:analysis|evaluation|investigation)|(?:make|makes|made|making) an? (?:assessment|determination|comparison)|provide(?:s|d|ing)? an? (?:explanation|description)|perform(?:s|ed|ing)? an? (?:analysis|evaluation|measurement))\b",
        "Use the concrete verb carried by the noun.",
    ),
    compile_rule(
        "rule-of-three",
        r"\b[^,.;:!?—]{1,45},\s+[^,.;:!?—]{1,45},\s+(?:and|or)\s+[^,.;:!?—]{1,45}(?=[.;:!?—]|$)",
        "Recast the three-part sequence without a matched triad.",
    ),
)

THROAT_OPENERS = re.compile(
    r"^\s*(?:this (?:section|paper|paragraph) (?:discusses|presents|describes|shows|explores)|"
    r"in this (?:section|paper|paragraph),|there (?:are|is) several|when it comes to|"
    r"it is (?:important|useful|necessary) to)(?!\w)",
    re.IGNORECASE,
)
NEGATIVE_OPENERS = re.compile(r"^\s*(?:no|not|never|neither|nor|without)\b", re.IGNORECASE)
SENTENCE_RE = re.compile(r"(?<=[.!?])(?:[\"'”’)}\]]*)\s+")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def mask_nonprose(lines: list[str]) -> list[str]:
    """Mask fenced code, LaTeX comments, display math, and Markdown headings."""
    masked: list[str] = []
    in_fence = False
    in_display_math = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            masked.append("")
            continue
        if in_fence:
            masked.append("")
            continue
        if stripped.startswith("\\[") or stripped.startswith("$$"):
            in_display_math = True
            masked.append("")
            if stripped.count("$$") >= 2 or "\\]" in stripped:
                in_display_math = False
            continue
        if in_display_math:
            masked.append("")
            if "\\]" in stripped or "$$" in stripped:
                in_display_math = False
            continue
        if stripped.startswith(("#", "%")):
            masked.append("")
            continue
        text = re.sub(r"(?<!\\)%.*$", "", line)
        text = re.sub(r"`[^`]*`", "", text)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"\$[^$]+\$", "", text)
        masked.append(text)
    return masked


def excerpt_for(line: str, column: int, width: int = 120) -> str:
    clean = line.strip()
    if len(clean) <= width:
        return clean
    start = max(0, column - 25)
    return clean[start : start + width].strip()


def add_finding(
    findings: list[Finding], path: str, lines: list[str], line_index: int,
    column: int, rule: str, message: str, severity: str = "error", basis: str = "U",
) -> None:
    findings.append(
        Finding(path, line_index + 1, column + 1, rule, severity, basis, message, excerpt_for(lines[line_index], column))
    )


def paragraph_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines + [""]):
        if line.strip() and start is None:
            start = index
        elif not line.strip() and start is not None:
            ranges.append((start, index))
            start = None
    return ranges


def sentence_records(lines: list[str], start: int, end: int) -> list[tuple[str, int, int]]:
    records: list[tuple[str, int, int]] = []
    for line_index in range(start, end):
        line = lines[line_index]
        cursor = 0
        for sentence in SENTENCE_RE.split(line):
            sentence = sentence.strip()
            if not sentence:
                continue
            column = line.find(sentence, cursor)
            column = max(column, 0)
            records.append((sentence, line_index, column))
            cursor = column + len(sentence)
    return records


def lint_strict(text: str, path: str) -> list[Finding]:
    original = text.splitlines()
    lines = mask_nonprose(original)
    findings: list[Finding] = []

    for line_index, line in enumerate(lines):
        for rule in LINE_RULES:
            for match in rule.pattern.finditer(line):
                add_finding(findings, path, original, line_index, match.start(), rule.name, rule.message)

    for start, end in paragraph_ranges(lines):
        records = sentence_records(lines, start, end)
        if not records:
            continue
        first_sentence, first_line, first_column = records[0]
        if THROAT_OPENERS.search(first_sentence):
            add_finding(
                findings, path, original, first_line, first_column,
                "throat-clearing-opener", "Open with the first substantive claim.",
            )

        negative_streak = 0
        seen_openings: dict[str, tuple[int, int]] = {}
        for sentence, line_index, column in records:
            if NEGATIVE_OPENERS.search(sentence):
                negative_streak += 1
                if negative_streak >= 2:
                    add_finding(
                        findings, path, original, line_index, column,
                        "negative-anaphora", "Vary the opening and state the claim in positive form.",
                    )
            else:
                negative_streak = 0

            words = [word.lower() for word in WORD_RE.findall(sentence)]
            if len(words) >= 3:
                opening = " ".join(words[:3])
                if opening in seen_openings:
                    add_finding(
                        findings, path, original, line_index, column,
                        "repeated-sentence-frame", "Change the sentence opening and grammar within this paragraph.",
                    )
                else:
                    seen_openings[opening] = (line_index, column)

        if len(records) >= 2:
            lengths = [len(WORD_RE.findall(sentence)) for sentence, _, _ in records]
            if max(lengths) - min(lengths) <= 2:
                _, line_index, column = records[-1]
                add_finding(
                    findings, path, original, line_index, column,
                    "uniform-sentence-length", "Vary sentence length within the paragraph.",
                )

    return sorted(findings, key=lambda item: (item.path, item.line, item.column, item.rule))


SYNTHETIC_RULES = (
    ("A03", re.compile(r"(?im)^\s*(?:moreover|furthermore|however|importantly|in contrast|consequently|more broadly|notably|crucially)\b"), 5, "connector saturation", "B/D"),
    ("A05", re.compile(r"(?i)\b(?:while|although)\b[^.!?]{1,100},|\brather than\b|\bnot\s+[^.!?;]{1,80}\s+but\b"), 4, "repeated contrast templates", "B/D"),
    ("A06", re.compile(r"(?im)^\s*(?:no|not|never|neither|without)\b"), 4, "repeated rhetorical negative openings", "D"),
    ("A07", re.compile(r"(?i)\b(?:the reason is simple|the intuition is straightforward|the answer lies in|this raises a natural question|at first glance|the picture changes when|here is the key observation)\b"), 3, "setup/payoff boilerplate", "C/D"),
    ("A09", re.compile(r"(?i)\b(?:we now turn to|the remainder of this section|having established|to better understand|to further investigate|we next conduct)\b"), 4, "document-navigation prose", "C/D"),
    ("A10", re.compile(r"[^?\n]{8,}\?"), 3, "rhetorical-question density", "D"),
    ("A14", re.compile(r"\b[^,.;:!?—]{1,45},\s+[^,.;:!?—]{1,45},\s+(?:and|or)\s+[^,.;:!?—]{1,45}(?=[.;:!?—]|$)"), 6, "three-part enumeration density", "D"),
    ("A15", re.compile(r"(?i)\b(?:better performance|strong performance|significant improvement|meaningful improvement|robust generalization|promising results)\b"), 4, "vague result language", "C/D"),
)


def offset_location(text: str, offset: int) -> tuple[int, int]:
    prefix = text[:offset]
    line = prefix.count("\n")
    last_break = prefix.rfind("\n")
    column = offset if last_break < 0 else offset - last_break - 1
    return line, column


def lint_synthetic(text: str, path: str) -> list[Finding]:
    original = text.splitlines()
    masked_lines = mask_nonprose(original)
    masked = "\n".join(masked_lines)
    findings: list[Finding] = []
    word_count = len(WORD_RE.findall(masked))

    for rule, pattern, threshold, label, basis in SYNTHETIC_RULES:
        matches = list(pattern.finditer(masked))
        if len(matches) < threshold:
            continue
        last = matches[-1]
        line, column = offset_location(masked, last.start())
        density = len(matches) * 1000 / max(word_count, 1)
        add_finding(
            findings, path, original, min(line, len(original) - 1), column, rule,
            f"Review {label}: {len(matches)} occurrence(s), {density:.1f} per 1,000 words.",
            severity="review", basis=basis,
        )

    paragraph_shapes: dict[tuple[str, ...], list[int]] = {}
    for start, end in paragraph_ranges(masked_lines):
        records = sentence_records(masked_lines, start, end)
        shape: list[str] = []
        for sentence, _, _ in records:
            lower = sentence.lower()
            if re.search(r"\b(?:however|although|while|depends|limitation|except)\b", lower):
                shape.append("qualification")
            elif re.search(r"\b(?:therefore|thus|suggests|demonstrates|indicates|implies)\b", lower):
                shape.append("implication")
            elif re.search(r"\b(?:because|arises from|driven by|due to)\b", lower):
                shape.append("explanation")
            elif re.search(r"\b(?:table|figure|fig\.|accuracy|loss|score|%)\b", lower):
                shape.append("evidence")
            else:
                shape.append("claim")
        if len(shape) >= 3:
            paragraph_shapes.setdefault(tuple(shape), []).append(start)
    for shape, starts in paragraph_shapes.items():
        if len(starts) >= 3:
            start = starts[-1]
            add_finding(
                findings, path, original, start, 0, "A01",
                f"Review repeated paragraph architecture: {' > '.join(shape)} appears {len(starts)} times.",
                severity="review", basis="B/D",
            )

    return sorted(findings, key=lambda item: (item.path, item.line, item.column, item.rule))


def lint_text(text: str, path: str, profile: str) -> list[Finding]:
    if profile == "strict-house":
        return lint_strict(text, path)
    return lint_synthetic(text, path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flag deterministic violations of the how-to-ml-paper prose contract."
    )
    parser.add_argument("paths", nargs="*", help="UTF-8 text, Markdown, or LaTeX files. Reads stdin when omitted.")
    parser.add_argument("--format", choices=("text", "json"), default="text", dest="output_format")
    parser.add_argument("--profile", choices=("strict-house", "synthetic-prose"), default="strict-house")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    findings: list[Finding] = []
    if args.paths:
        for raw_path in args.paths:
            path = Path(raw_path)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                print(f"prose_lint: {path}: {error}", file=sys.stderr)
                return 2
            findings.extend(lint_text(text, str(path), args.profile))
    else:
        findings.extend(lint_text(sys.stdin.read(), "<stdin>", args.profile))

    if args.output_format == "json":
        print(json.dumps([asdict(finding) for finding in findings], indent=2, ensure_ascii=False))
    else:
        for finding in findings:
            print(
                f"{finding.path}:{finding.line}:{finding.column}: "
                f"{finding.rule} [{finding.severity}, {finding.basis}]: {finding.message}\n  {finding.excerpt}"
            )
        print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
