# Prose style contract

The default is clear scientific prose with the author's voice intact. The
editorial practices below are local heuristics (D), not universal laws of good
writing. Explicit user preferences take precedence over these defaults.

## Meaning comes first

Preserve numbers, units, mathematical meaning, citation keys, cross-references,
comparison targets, and conditions under which a claim holds. Keep qualifications
that specify uncertainty, a population, an assumption, or a limitation. “Not
established” must not become “false,” a sufficient condition must not become a
necessary condition, and an observed association must not become an explanation.
Terminological consistency takes precedence over lexical variety.

Do not supply a plausible mechanism, baseline, or measurement to make a rewrite
sound specific. When evidence is missing, narrow the claim or identify the gap
outside the requested prose. Preserve evidence-bearing clauses during compression.

## Make the prose belong to this paper

- Start at the concrete problem, observation, or decision the reader needs now.
  Replace broad importance claims with the actual setting or constraint when it
  is known. Delete generic context when no useful detail supports it.
- Explain actions. “We use an adaptive module” leaves the method hidden; say
  what changes, what information drives the change, and where it occurs.
- Connect facts through their scientific relationship. A baseline can expose a
  tradeoff; an ablation can test an attribution; a failure can bound a result.
  A sequence of accurate sentences still needs a reason for that ordering.
- Give the reader enough interpretation to understand the comparison. Keep an
  ending that adds a consequence or boundary; remove a restatement that merely
  calls the method effective, promising, or important.
- Attach qualifications to the claim they limit. State established observations
  directly, with uncertainty where the evidence requires it. Repeating “may,”
  “potentially,” or “in this setting” does not improve calibration by itself.

## Preserve a natural technical voice

Keep a good sentence when it already does its job. Follow the surrounding text's
terminology, level of formality, and use of first person. A sample the author
likes is more useful than a generic instruction to sound academic.

Use grammatical relationships the argument needs. A contrast can state a real
method difference; negation can preserve a theorem condition; a three-item list
can enumerate exactly three measurements. Sentence length follows the complexity
of the idea. Do not split connected clauses or change a technical verb merely
to avoid a surface pattern. Do not add conversational flourishes, dramatic
questions, fragments, or manufactured irregularity to make prose seem human.

When a draft feels generic, first change its information and organization.
Replacing “leverage” with “use” leaves an unexplained method unexplained.
Read the revised paragraph as a whole, including the paragraphs around it when
available. Check what each sentence adds and what its pronouns refer to.

## Linter profiles

Use the linter as an optional review aid, after drafting. It cannot assess
scientific validity, author voice, or whether a paragraph develops an argument.
Its output is never evidence of authorship.

- **`editorial` (default):** advisory checks for generic, promotional, or vague
  prose. Findings use `review` severity and local-heuristic provenance `D`.
  Inspect the passage before changing it; even a flagged phrase can be justified.
- **`strict-house` (explicit opt-in):** the existing restrictive house profile,
  including contrast templates, semicolons, em dashes, triads, repeated openings,
  and similar sentence lengths. Apply it only when the user requests that style.
  Its `error`/`U` labels denote profile conventions, not scientific defects.
  Preserve necessary meaning even when a convention cannot be satisfied.
- **`synthetic-prose`:** inspect clusters of repeated patterns in longer drafts.
  A repeated form can express a real parallel; a single connector or contrast is
  weak evidence. Never rewrite merely to obtain a particular pattern count.

From the skill directory:

```bash
python scripts/prose_lint.py --profile editorial paper.tex
python scripts/prose_lint.py --profile strict-house paper.tex
python scripts/prose_lint.py --profile synthetic-prose paper.tex
```

A clean lint result is not a quality score. A useful final pass checks meaning,
paragraph flow, specificity, and the requested format and length.
