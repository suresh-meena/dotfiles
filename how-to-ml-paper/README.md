# how-to-ml-paper — ML Paper Skill

Agent skill for planning, drafting, restructuring, and reviewing machine-learning
papers as clear, evidence-backed arguments. Adapted from Jakob N. Foerster's
*How to ML Paper* plus cited ML writing and reproducibility sources.

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
      style-contract.md             # strict-house + synthetic-prose contracts
      style-examples.md             # flag/rewrite pairs with provenance
      review-protocol.md            # scientific review pass + report shape
      claim-ledger.md               # claim ledger template
      venue-guides.md               # venue routing table
      pdf-review.md                 # rendered-PDF checks
      sources.md                    # provenance levels A/B/C/D/U + links
  tests/test_prose_lint.py          # pytest coverage for the linter
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
python skills/how-to-ml-paper/scripts/prose_lint.py --profile strict-house paper.tex
python skills/how-to-ml-paper/scripts/prose_lint.py --profile synthetic-prose paper.tex
python skills/how-to-ml-paper/scripts/prose_lint.py --profile strict-house --format json draft.md
echo "text" | python skills/how-to-ml-paper/scripts/prose_lint.py
```

Profiles:

- `strict-house` (default): explicit house-voice violations. Use for the prose pass.
- `synthetic-prose`: document-level repetition/density revision signals. Single
  occurrences are weak evidence; the linter only reports clusters.

Exit codes: `0` clean, `1` findings reported, `2` input could not be linted
(unreadable path, decode error).

The linter masks fenced code, LaTeX comments, display math, Markdown headings,
inline code, URLs, and inline math before matching. It catches surface patterns
only; discourse roles, semantic restatement, noun stacking, and causal support
still need human judgment (see `style-contract.md`).

## Tests

```bash
python -m pytest how-to-ml-paper/tests/ -q
```

Covers: clean strict-house pass, representative strict rules, non-prose masking,
synthetic thresholds, JSON finding shape, and CLI exit codes `0`/`1`/`2`.

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
