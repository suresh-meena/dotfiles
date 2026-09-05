# Claim ledger example

Filled example of the template in `../skills/how-to-ml-paper/references/claim-ledger.md`.
Statuses: `Supported`, `Partially supported`, `Missing evidence`, `Overstated`,
`Citation needed`, `Outside scope`.

| ID | Claim | Type | Contribution | Evidence | Baseline | Limitation | Citation | First stated | Verified at | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| C1 | Routing objective improves exact match by 5.6 points | Empirical | New method | Table 2, three seeds, mean and interval | Matched-tuning dense baseline | Natural Questions and TriviaQA only | — | Abstract | Results | Supported |
| C2 | Gains come from routing rather than extra parameters | Analysis/interpretation | Ablation | Parameter-matched ablation, Table 3 | Same-size dense model | Single seed for one ablation cell | — | Introduction | Results | Partially supported |
| C3 | Representations become invariant to context length | Analysis/interpretation | Mechanism claim | Missing: no invariance measurement | — | — | — | Introduction | — | Missing evidence |

## Review-output snippet

Shape follows `../skills/how-to-ml-paper/references/review-protocol.md`.
Scientific findings come before prose findings.

```markdown
## Scientific findings

### Major: Ablation changes two factors
Location: Results, Table 3
Claim affected: C2
Evidence currently provided: routing on/off with different batch sizes
Missing control or comparison: rerun with matched batch size
Why the conclusion can change: batch size alone can shift exact match
Required action: control batch size or narrow C2 to an observed difference

## Prose findings

### Minor: Vague result language
Location: Abstract
Current text: Our method has better performance.
Suggested revision: Routing raises exact match from 41.2 to 46.8.
Rule and provenance: A15, C/D
```
