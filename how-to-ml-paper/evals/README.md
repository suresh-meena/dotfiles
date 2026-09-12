# Writing trials

These tasks use invented research material. Their measurements are fixtures,
not results from an actual paper. They test the skill's writing behavior
separately from the deterministic linter tests.

## Run a trial

Give an independent agent a task file and the skill directory to use. Have it
read the required references and save its actual response. Do not give it the
assessment criteria below, other candidates' outputs, or an expected rewrite.
Use the same model/settings when comparing skill revisions and record any
differences. No external literature search is needed for these supplied facts.

Tasks:

- [abstract-task.md](abstract-task.md): draft from notes containing real tradeoffs
  and unsupported brainstorming claims.
- [holdout-task.md](holdout-task.md): explain a calibration method from notes and
  an author's voice sample. This task was introduced after the initial revision.
- [revision-task.md](revision-task.md): revise LaTeX without changing scope,
  uncertainty, notation, or references.
- [review-task.md](review-task.md): identify consequential issues in an excerpt
  and supply a supported replacement claim.

## Assess the output

Read candidates without revision labels when practical. First check fidelity:
unsupported facts, altered numbers or units, invented citations, strengthened
causal/novelty claims, and lost qualifications disqualify a stylistic improvement.
Check requested format and length as well.

Then compare specificity, explanation of the method, how sentences connect,
selection and interpretation of evidence, retention of useful author voice,
and unnecessary boilerplate. Identify actual passages that support the judgment.
Allow ties and mixed outcomes; do not assume the new skill must win.

Keep critique separate from the candidate prose. A low lint count, artificial
sentence-length variation, or an authorship detector is not a writing-quality
measure. Report the number and nature of trials and any missing validation.

See [assessment.md](assessment.md) for the recorded run and its limits.
