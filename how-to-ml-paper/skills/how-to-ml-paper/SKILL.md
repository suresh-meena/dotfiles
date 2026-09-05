---
name: how-to-ml-paper
description: Plan, draft, restructure, or review machine-learning papers as clear, evidence-backed arguments. Use for outlines, sections, experiment plans, scientific reviews, prose revision, or submission-ready PDF checks.
license: CC-BY-NC
metadata:
  author: Adapted from Jakob N. Foerster's How to ML Paper and cited ML writing and reproducibility sources
  source: https://www.jakobfoerster.com/how-to-ml-paper
  compatibility: Codex and OpenCode
---

# How to ML Paper

Help researchers build a paper whose claims, evidence, and prose survive expert review. Use conventional sections to reduce reader effort. Treat writing as part of research design.

## Paper spine

Resolve these before polishing prose:

- **X, problem:** What is being solved, and why does it matter?
- **Y, obstacle:** What makes the problem hard? Which assumption or limitation blocks prior methods?
- **Z, contribution:** What changed in this work?
- **V, evidence:** Which experiment, analysis, or proof supports each claim?

Use X/Y/Z/V internally. Use problem, obstacle, contribution, and evidence in reader-facing reports. Mark missing content with visible placeholders. Draw every factual statement from supplied material or a cited source.

## Route by task

- For planning, restructuring, or ordinary drafting, read [references/guide.md](references/guide.md).
- Before writing or revising prose, read [references/style-contract.md](references/style-contract.md). Consult [references/style-examples.md](references/style-examples.md) when a rule needs interpretation.
- For a full paper review or experiment plan, read [references/review-protocol.md](references/review-protocol.md) and [references/claim-ledger.md](references/claim-ledger.md).
- When a venue or submission year is known, read [references/venue-guides.md](references/venue-guides.md) and retrieve the current official instructions.
- For submission preparation, read [references/pdf-review.md](references/pdf-review.md) and inspect the rendered PDF.
- For rationale and provenance, consult [references/sources.md](references/sources.md).

## Full review order

1. Resolve the venue, track, and year.
2. Build the X/Y/Z/V map and claim ledger.
3. Review contribution boundaries and related work.
4. Review experiments, including baselines, fairness, tuning budgets, stochasticity, uncertainty, ablations, attribution, robustness, and scope.
5. Review assumptions, limitations, and claim strength.
6. Review prose after central scientific issues are visible.
7. Compile the paper and inspect every rendered page.
8. Recheck abstract and introduction claims against final evidence.

Run `python scripts/prose_lint.py <draft-files>` during the prose pass. Use `--profile strict-house` for the requested house style and `--profile synthetic-prose` for document-level regularity checks. Use `--format json` for structured findings. Status `1` means findings were reported. Status `2` means the input could not be linted.

## Review output

Put scientific findings before prose findings. Assign `Critical` only when an issue can change a central claim or conclusion. Use `Major` for material weaknesses. Use `Minor` for local cleanup. Give the location, affected claim, present evidence, consequence, and repair.

Classify rule provenance:

- **A:** current official venue requirement
- **B:** peer-reviewed evidence for a measurable failure or text pattern
- **C:** explicit guidance from an established ML researcher
- **D:** local revision heuristic
- **U:** explicit user style policy

Describe synthetic-prose findings as revision signals. Never infer authorship from them.

