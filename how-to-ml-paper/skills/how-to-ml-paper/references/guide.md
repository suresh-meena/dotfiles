# Guide: the reasoning behind a clear ML paper

This reference adapts Jakob N. Foerster's *How to ML Paper: A brief Guide* into operational guidance for an agent. Source: <https://www.jakobfoerster.com/how-to-ml-paper> (accessed 2026-09-04). The source is marked CC BY-NC.

## What the author is implying

The guide treats an ML paper as a layered argument designed around the reader's limited attention. Formatting conventions serve that argument.

- The abstract is the whole argument at minimum resolution.
- The introduction is the same argument with motivation, contributions, and headline evidence.
- The body earns that argument by supplying prerequisites, precise definitions, methods, and verification.
- Conventional sections reduce search cost because readers already know what kind of answer belongs where.
- Clear writing exposes research weaknesses early. Drafting the abstract and paragraph outline before results are final therefore tests the research story while it shapes the prose.
- Rules are defaults justified by reader comprehension. Break one when the local reason is stronger than the default, and be able to articulate that reason.

The recurring standard is: say exactly what is true, distinguish new work from inherited work, support claims, and make the path through complicated content obvious.

## Section contracts

### Abstract

Give a compact X–Y–Z–V account: problem and relevance, difficulty, contribution, then empirical and/or theoretical verification. Avoid background that does not help the reader evaluate those four items.

### Introduction

Expand X–Y–Z–V. State contributions distinctly, commonly as bullets. Include headline results and relevant prior-state comparison. Use spare room for limitations or future directions only after the core story is complete.

### Related work: academic siblings

Discuss alternative attempts to solve the same problem. Explain how their assumptions and methods relate to this work. Follow each description with that relationship.

When a related method applies to the paper's setting, include it in the experiments. When its assumptions exclude the setting, state the exact incompatibility.

### Background: academic ancestors

Introduce the concepts and prior work required to understand the method. Define the problem setting, notation, and unusual assumptions here unless defining a novel problem setting is itself a contribution; in that case, give it a distinct section.

The sibling/ancestor distinction prevents related work from becoming a generic literature list and background from becoming an unstructured history lesson.

### Method

Explain what the work does and why. Use the already introduced formalism and build explicitly on the already introduced foundations. Make the boundary between inherited components and new contributions unambiguous.

### Experimental setup

Explain how the claims are tested: the concrete problem instance, datasets or environments, metrics, baselines, implementation choices, and the method's instantiation. Include enough detail to judge validity and fairness.

### Results and discussion

Connect every result to a claim. Compare applicable baselines, report statistics and uncertainty, disclose consequential hyperparameters and fairness considerations, and use ablations to test whether claimed components matter. Discuss limitations rather than hiding them.

Explain the conclusion supported by each table. Keep its stated scope within the evidence.

### Conclusion

Briefly restate what was established and how. Future work may identify plausible next questions, but must not inflate what the current paper proved.

## Paragraph-first workflow

Start with one sentence per intended paragraph. Each sentence states one idea, not merely a topic. Reorder, merge, or delete these messages until the logical flow works. This is the inexpensive moment for collaborator feedback.

Then expand each message into prose. In LaTeX, retaining it as a comment can make structure visible during review:

```latex
% Message: Existing methods require centralized training, which is unavailable in our setting.
Existing methods assume ...
```

The paragraph should express its message as simply and clearly as the technical content allows. Comments are scaffolding: retain them for collaboration if helpful, and remove them if the venue or authors prefer clean source.

## Evidence and claim checks

For each claimed contribution, record:

| Claim | Evidence needed | Where defined | Where verified | Caveat |
|---|---|---|---|---|
| New problem setting | Formal distinction and relevance | Problem setting | Appropriate analysis or study | Scope and assumptions |
| New method | Clear delta from prior work | Method | Baselines and ablations | Failure modes |
| Performance improvement | Fair metric and comparison | Experimental setup | Results with uncertainty | Dataset/task limits |
| Theoretical property | Precise assumptions and statement | Background/method | Proof or derivation | Assumption sensitivity |

Use the table as a reasoning aid. Include it in the paper when it helps the reader.

## High-value editing checks

- Prefer direct statements and named agents when agency matters. Passive voice is acceptable when the object is the focus or continuity improves reader load.
- Keep tense consistent. Prefer present tense for paper organization and established content instead of promises such as “we will show.”
- Delete filler and modal hedging that changes no meaning. After the first complete draft, attempt a substantial compression pass; the source suggests roughly one third as a useful challenge, not a quota.
- Use one term for each work-specific concept. Define confusing terminology near first use and contrast it with likely alternatives.
- Introduce acronyms and symbols before use, and introduce only those used later.
- Cite externally supported claims. Use calibrated language; adjectives and broad superiority statements deserve special scrutiny.
- Select the correct published version of references where appropriate. Check the rendered paper for broken citations and cross-references.
- Treat equations as grammatical parts of sentences, including commas and periods. Use a colon when the surrounding grammar calls for one.
- Apply capitalization, emphasis, quotation style, spelling variant, citation commands, and cross-reference conventions consistently.
- Avoid anthropomorphic descriptions of algorithms unless technically defined.
- Split cumbersome sentences. Avoid stranded one-word lines, unexplained whitespace, and other layout artifacts in the final PDF.
- Quote copied prose and provide attribution. Write all other passages from understanding.

## Collaboration and authorship

Encourage early outlines, a complete draft well before the deadline, visible change tracking, and frequent coordination near submission.

Discuss authorship when the request calls for it. The source favors generous inclusion for substantial time or material improvement and early expectation-setting. Ordering conventions vary by field, institution, and collaboration. Present the source's ordering advice as a personal rule of thumb. Encourage collaborators to agree explicitly and follow applicable venue and institutional guidance.

## How to apply the defaults

Judge each deviation through these questions:

1. What reader or scientific failure is the default meant to prevent?
2. Does that failure occur here?
3. Does the author's alternative have a stronger local justification?

This preserves the guide's main caveat: efficient communication is the goal, and conventions are tools for achieving it.
