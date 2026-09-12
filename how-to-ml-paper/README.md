# how-to-ml-paper — ML Paper Skill

Agent skill for planning, drafting, restructuring, and reviewing machine-learning
papers as clear, evidence-backed arguments. Adapted from Jakob N. Foerster's
*How to ML Paper* plus cited ML writing and reproducibility sources.

The default workflow returns usable prose first, explains the method through
its actual operations, and develops the comparison that matters to the paper.
It preserves the author's effective phrasing, scientific uncertainty, and LaTeX
references. A paragraph edit stays a paragraph edit; full reviews use the claim
ledger and scientific protocol.

For writing that feels generic or unnatural, the skill includes worked examples
with supplied facts and reasons for each revision. Strict house-style rules are
opt-in. The default linter offers editorial suggestions without penalizing
ordinary scientific contrasts, technical lists, or sentence lengths.

## Layout

```text
how-to-ml-paper/
  install.sh                        # symlink skill into Codex + OpenCode
  README.md                         # this file
  skills/how-to-ml-paper/
    SKILL.md                        # router: paper spine, task routes, review order
    scripts/prose_lint.py           # deterministic prose linter (stdlib only)
    references/
      guide.md                      # section contracts, paragraph-first workflow
      style-contract.md             # editorial default + optional strict profiles
      style-examples.md             # worked prose revisions with supplied facts
      review-protocol.md            # scientific review pass + report shape
      claim-ledger.md               # claim ledger template
      venue-guides.md               # venue routing table
      pdf-review.md                 # rendered-PDF checks
      sources.md                    # provenance levels A/B/C/D/U + links
  tests/test_prose_lint.py          # pytest coverage for the linter
  evals/                            # writing tasks, before/after outputs, assessment
  examples/
    clean-abstract.md               # passes strict-house (expected: 0 findings)
    flagged-draft.md                # deliberately flagged prose + expected rules
    synthetic-sample.md             # triggers synthetic-prose density signals
    claim-ledger-example.md         # filled ledger + review-output snippet
```

## Install

```bash
./how-to-ml-paper/install.sh
# Codex:   ~/.codex/skills/how-to-ml-paper -> <repo>/how-to-ml-paper/skills/how-to-ml-paper
# OpenCode: ~/.config/opencode/skills/how-to-ml-paper -> <repo>/how-to-ml-paper/skills/how-to-ml-paper
```

Custom homes:

```bash
CODEX_HOME=/path/to/codex ./how-to-ml-paper/install.sh
./how-to-ml-paper/install.sh /path/to/codex /path/to/opencode-config
```

Restart the agent host after (re)install so the skill is reloaded.

## Linter

Stdlib-only Python 3. No dependencies.

```bash
python skills/how-to-ml-paper/scripts/prose_lint.py --profile editorial paper.tex
python skills/how-to-ml-paper/scripts/prose_lint.py --profile strict-house paper.tex
python skills/how-to-ml-paper/scripts/prose_lint.py --profile synthetic-prose paper.tex
python skills/how-to-ml-paper/scripts/prose_lint.py --format json draft.md
echo "text" | python skills/how-to-ml-paper/scripts/prose_lint.py
```

Profiles:

- `editorial` (default): advisory checks for generic, promotional, and vague prose.
  Findings carry `review` severity and local-heuristic provenance `D`. Interpret
  them in context; a zero count is not a writing-quality score.
- `strict-house`: explicit house-voice conventions, retained as an opt-in profile.
- `synthetic-prose`: document-level repetition/density revision signals. Single
  occurrences are weak evidence; the linter only reports clusters.

Exit codes: `0` clean, `1` findings reported, `2` input could not be linted
(unreadable path, decode error).

The default changed from `strict-house` to `editorial`. Existing automation that
requires the restrictive checks should pass `--profile strict-house` explicitly.
Finding fields and exit-code meanings are unchanged; advisory findings also
return `1`.

The linter masks fenced code, LaTeX comments, display math, Markdown headings,
inline code, URLs, and inline math before matching, preserving source columns.
Inline `%` comments are recognized for `.tex` and `.ltx` files; percentages in
ordinary prose remain visible. Stdin is treated as ordinary prose, so pass a
`.tex` file when inline LaTeX-comment masking is needed. It catches surface patterns
only; discourse roles, semantic restatement, noun stacking, and causal support
still need human judgment (see `style-contract.md`).

## Tests

```bash
python -m pytest how-to-ml-paper/tests/ -q
```

Covers: advisory defaults, ordinary scientific constructions, explicit strict
rules, masking and source coordinates, percentage handling, synthetic thresholds,
JSON finding shape, and CLI exit codes `0`/`1`/`2`.

Writing quality is evaluated separately with realistic synthetic tasks in
[evals/README.md](evals/README.md). Those trials assess the actual prose and its
fidelity to the supplied facts; linter results cannot establish either.

## Examples

```bash
# expected: 0 findings
python skills/how-to-ml-paper/scripts/prose_lint.py --profile strict-house examples/clean-abstract.md; echo $?
# expected: throat-clearing-opener, summary-beat, performed-enthusiasm, ...
python skills/how-to-ml-paper/scripts/prose_lint.py --profile strict-house examples/flagged-draft.md; echo $?
# expected: A03 connector saturation, A15 vague result language
python skills/how-to-ml-paper/scripts/prose_lint.py --profile synthetic-prose examples/synthetic-sample.md; echo $?
```

`claim-ledger-example.md` shows a filled claim ledger plus the scientific/prose
finding shape from `review-protocol.md`.

## License

Skill content: CC-BY-NC (see `skills/how-to-ml-paper/SKILL.md` and
`references/sources.md` for the upstream source and access date).
