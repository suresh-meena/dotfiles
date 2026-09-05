# Scientific review protocol

Run the scientific pass before the prose pass.

## Argument and contribution

Build the X/Y/Z/V map. Check whether prior work and inherited components remain distinct from the contribution. Organize related work around technical trajectories and state each relationship to the present method.

## Results skeleton

Before final experiments, list each empirical claim and the experiment that tests it. Name required baselines and ablations. Reserve the table or figure that will carry the evidence. State the analysis needed to interpret the result. Create empty tables with row and column headings and empty plots with axes and draft captions. A central claim without a planned result indicates a missing experiment.

## Baselines and fairness

For each superiority claim, identify the simplest credible baseline. Check implementation quality, tuning, data, preprocessing, compute, stopping rules, and model selection. Record method and baseline budgets. Unequal budgets become Critical when they can reverse a central ordering.

## Stochasticity and uncertainty

Identify randomness from initialization, sampling, environments, data splits, and nondeterministic kernels. Record independent runs, reported statistic, dispersion or interval definition, pairing, and validation selection. Require repeated runs when stochastic variation can change the claim. Ask for a justification when the report uses one run.

## Ablations and attribution

Identify every factor changed by an ablation. One controlled change can support component attribution under tested conditions. Retraining variance, interactions, and implementation coupling can preserve other explanations. Request a controlled comparison or narrow the attribution claim.

## Causal and explanatory language

Separate observation, component attribution, mechanism evidence, and speculation. A benchmark association does not establish an internal mechanism. Link phrases such as `arises from`, `is driven by`, `enables`, and `leads to` to measured mechanism evidence or rewrite them as observations.

## Scope and limitations

Match the claim to tasks, datasets, populations, scales, compute regimes, and assumptions tested. State failure regimes. A limitation should change the claim's scope where appropriate. Flag language that neutralizes a limitation through rhetoric.

## Severity

Set severity from the centrality of the claim, the plausible effect on the conclusion, and the absence of compensating evidence.

Use this report shape:

```markdown
## Scientific findings

### Critical: Unequal tuning budget
Location:
Claim affected:
Evidence currently provided:
Missing control or comparison:
Why the conclusion can change:
Required action:

## Prose findings

### Major: Repeated contrast template
Location:
Current text:
Suggested revision:
Rule and provenance:
```

