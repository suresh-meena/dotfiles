# Researcher-backed style examples

Each example carries a provenance level from [sources.md](sources.md).

## Section announcement

**C, researcher guidance**

Flag: `In this section, we describe our optimization procedure.`

Rewrite: `We optimize the latent code with Adam for 500 steps.`

The heading supplies navigation. The sentence supplies technical information. A complex method may need a compact overview when that overview reduces reader load.

## Imprecise metric

**C, researcher guidance**

Flag: `Our method has better performance.`

Rewrite: `Our method improves top-1 accuracy by 2.4 points.`

Name the measured quantity and comparison target.

## Stacked noun phrase

**C, direct researcher example**

Flag: `incremental instance-based learning algorithms`

Rewrite: `incremental algorithms for instance-based learning`

Expose attachment through a preposition or clause.

## Repeated paragraph shape

**D, heuristic derived from discourse evidence**

Flag a manuscript that repeatedly uses `claim > explanation > qualification > implication`. Rewrite each affected paragraph around its technical role. Preserve a repeated form when it expresses a real logical parallel.

## Redundant interpretive closure

**D, local heuristic**

Flag: `Accuracy rises from 71.2 to 76.8 with less than 2% added latency. Taken together, these results demonstrate that retrieval improves accuracy without meaningfully increasing latency.`

Rewrite: `Retrieval raises accuracy from 71.2 to 76.8 with less than 2% added latency.`

A recap at another level of abstraction can help. Same-level restatement adds little.

## Rhetorical negative framing

**D, local heuristic; U under strict house style**

Flag: `Not more parameters. Not more data. The gain comes from the routing objective.`

Rewrite: `The routing objective produces the gain under fixed parameter and data budgets.`

Preserve negation that states a result, constraint, theorem condition, or precise comparison.

## Setup/payoff mini-drama

**C/D, researcher guidance and local pattern**

Flag: `This raises a natural question: why does context fail? The answer lies in attention dilution.`

Rewrite: `Performance saturates as attention mass becomes diffuse with longer context (Fig. 4).`

## Related-work laundry list

**C, researcher guidance**

Group papers by technical trajectory. Describe the trajectory and state how the current method relates to it. Avoid an author-by-author list that leaves the contribution boundary implicit.

## Unsupported causal explanation

**B/C, scientific review**

Observed: `Removing module X reduces accuracy by 2.1 points.`

Unsupported mechanism: `Module X improves accuracy by learning invariant representations.`

The mechanism claim needs a measurement or controlled test of representation invariance.

