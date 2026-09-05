# Prose style contract

Apply this contract to prose drafted or rewritten for the paper.

## Precedence

Preserve factual accuracy, mathematical meaning, direct quotations, required venue syntax, and precise scientific scope. Keep qualifications that specify a population, assumption, probability, confidence interval, effect size, or source of uncertainty. Preserve logically necessary negation, negative results, theorem conditions, limitations, and comparison targets.

Resolve a surface violation through a new sentence shape. Record any remaining exception in the review. Terminological consistency outranks lexical variety.

## Strict house profile

The explicit house voice excludes:

- antithesis and corrective-negation templates;
- paragraph announcements, paragraph recaps, and delayed payoff;
- paratactic strings whose logical relationship stays implicit;
- rhetorical crutches, negative anaphora, and balanced contrast pairs;
- three-part rhetorical enumeration and em dashes;
- setup/payoff mini-dramas and repeated sentence frames;
- stacked noun phrases and filler intensifiers;
- corporate-register verbs and avoidable nominalizations;
- empty hedges, performed enthusiasm, and patterned sentence lengths.

Write for the spoken voice. Use concrete verbs and familiar words. Let technical information determine the paragraph's shape. Vary sentence length and grammar without a visible cycle. Enter through substantive content. Stop after the final useful fact or analysis.

## Synthetic-prose profile

Inspect repetition, density, and document-level regularity. A single connector, contrast, adjective, triad, or summary sentence provides weak evidence. Report clusters and repeated templates.

High-value signals include repeated paragraph architecture, redundant interpretive closure, connector saturation, unsupported importance words, recurring contrast templates, rhetorical negative repetition, setup/payoff boilerplate, repeated syntactic skeletons, document-navigation prose, rhetorical questions, restated claims, unsupported causal explanations, symmetrical limitation neutralization, excessive triads, and vague result language.

Use these signals to improve prose. Never label a passage as AI-authored.

## Manual pass

Read the prose aloud. Find paragraphs that feel staged, balanced, slogan-like, or uniformly polished. Check whether each transition carries information. Replace importance labels with evidence. Trace explanatory claims to measurements or controlled tests. Preserve unevenness when the scientific content has uneven importance.

Run the linter from the skill directory:

```bash
python scripts/prose_lint.py --profile strict-house paper.tex
python scripts/prose_lint.py --profile synthetic-prose paper.tex
```

The linter catches surface patterns. Human judgment remains necessary for discourse roles, semantic restatement, noun stacking, and causal support.
