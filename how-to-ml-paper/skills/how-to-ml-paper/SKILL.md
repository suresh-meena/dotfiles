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

Write the requested paper material, with enough technical specificity that it
could not be pasted into an unrelated paper. Help the reader understand the
research decision, the method, and what the evidence establishes. Preserve the
author's useful phrasing and scientific commitments.

## Start with the requested deliverable

- For drafting or rewriting, return usable prose first. Keep editorial notes
  separate and brief unless the user asks for a review. If they request LaTeX
  only, return LaTeX only.
- Match the requested scope. A paragraph edit does not require a claim ledger,
  venue lookup, full-paper review, or PDF build.
- Read supplied notes, nearby paragraphs, tables, and definitions before asking
  for information already available. Ask a focused question when its answer
  changes the argument; otherwise draft the supported portion and identify the
  specific gap. Do not turn sparse notes into a long intake questionnaire.
- Follow supplied voice examples for directness, detail, and rhythm, while
  keeping this paper's facts. Without examples, use clear technical prose;
  do not invent an author persona or imitate a supposedly typical ML paper.

## Paper spine

Use this as an internal reasoning aid, not a fixed sequence of sentences:

- **X, problem:** What is being solved, and why does it matter?
- **Y, obstacle:** What makes the problem hard? Which assumption or limitation blocks prior methods?
- **Z, contribution:** What changed in this work?
- **V, evidence:** Which experiment, analysis, or proof supports each claim?

Draw scientific facts from supplied material or verified sources. Separate
observations, proposed explanations, and established mechanisms. Do not invent
novelty, a literature gap, measurements, citations, or release plans to complete
the story. A qualification must survive a rewrite when it changes what is true.

For unfinished sections, use specific placeholders only where needed, such as
`[paired interval needed]`. For prose ready to paste into a paper, narrow the
claim to the available evidence and put unresolved questions outside the draft.

## Route by task

- For planning, restructuring, or ordinary drafting, read [references/guide.md](references/guide.md).
- Before writing or revising prose, read [references/style-contract.md](references/style-contract.md). For generic or unnatural writing, also read [references/style-examples.md](references/style-examples.md); use its worked revisions to reason about the paragraph, not as templates to copy.
- For a full paper review or experiment plan, read [references/review-protocol.md](references/review-protocol.md) and [references/claim-ledger.md](references/claim-ledger.md).
- When submission requirements affect the task, read [references/venue-guides.md](references/venue-guides.md) and retrieve current official instructions for the venue, track, and year. Their absence does not block an ordinary draft or excerpt review.
- For submission preparation, read [references/pdf-review.md](references/pdf-review.md) and inspect the rendered PDF.
- For rationale and provenance, consult [references/sources.md](references/sources.md).

## Draft and revise

1. Identify what the reader should understand after this passage. Select the
   evidence and method details that earn that understanding.
2. Draft the argument in connected prose. Explain the operation behind a method
   label and the relationship between consecutive facts. Use the strongest
   defensible comparison, including a tradeoff when the evidence shows one.
3. Read for generic writing: sentences that could describe any paper, labels
   standing in for explanations, facts with no stated relationship, and endings
   that repeat a result without interpreting it. Repair the missing substance
   or delete the sentence; synonym replacement does not solve these problems.
4. Check meaning against the source: numbers and units, baselines, uncertainty,
   quantifiers, assumptions, citation keys, labels, and scope. Shorter or smoother
   prose is not an improvement if it changes any of these.

Use the default `editorial` linter as an optional second opinion on a prose
pass: `python scripts/prose_lint.py --profile editorial <draft-files>` from
the skill directory. Findings are suggestions to examine in context. Use
`strict-house` only when the user requests that profile, and `synthetic-prose`
for repeated patterns in longer drafts. Never optimize writing for zero findings
or manufacture irregular sentence lengths. `--format json` gives structured
findings; status `1` means findings and `2` means an input error.

## Full review

Use this sequence for a full review, scaled to the supplied material:

1. Establish the supplied scope and any relevant venue requirements.
2. Build the X/Y/Z/V map and claim ledger.
3. Review contribution boundaries and related work.
4. Review experiments, including baselines, fairness, tuning budgets, stochasticity, uncertainty, ablations, attribution, robustness, and scope.
5. Review assumptions, limitations, and claim strength.
6. Review prose after central scientific issues are visible.
7. When a rendered-paper review is requested and build inputs are available,
   compile the paper and inspect every rendered page. Report any unavailable check.
8. Recheck abstract and introduction claims against final evidence.

## Review output

Put scientific findings before prose findings. Assign `Critical` only when an issue can change a central claim or conclusion. Use `Major` for material weaknesses. Use `Minor` for local cleanup. Give the location, affected claim, present evidence, consequence, and repair.

Prioritize the findings that change the author's next action. Give an exact
rewrite when the evidence permits one. In an excerpt review, distinguish
evidence missing from the excerpt from evidence absent from the whole paper.
Do not label a theorem invalid because its proof was not supplied.

When explaining the basis of a review rule, classify its provenance. These labels
belong in an audit or requested review, not in replacement paper prose:

- **A:** current official venue requirement
- **B:** peer-reviewed evidence for a measurable failure or text pattern
- **C:** explicit guidance from an established ML researcher
- **D:** local revision heuristic
- **U:** explicit user style policy

Describe synthetic-prose findings as revision signals. Never infer authorship from them.

