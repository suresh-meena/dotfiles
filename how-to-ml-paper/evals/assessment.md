# Assessment: 2026-09-08

The user's complaint was that writing felt generic or unnatural. The revision
adds positive drafting guidance, supplied-fact examples, author-voice preservation,
and an advisory default linter. These trials examine the resulting prose.

## Setup

The baseline skill was copied from the working tree before this revision; its
content matched the root repository's `69e7ff0` version after line-ending
normalization. The revised snapshot was taken after the skill and drafting
references were edited. Writer agents used inherited session settings without
model overrides. They saw the task, skill, and required references, but no other
candidates or assessment criteria. Each candidate is one generation, without
sampling-seed control.

An independent reader compared anonymous candidates without access to the skill
revisions or other outputs. The abstract task was used in the initial diagnosis;
the calibration-method task was introduced after the first revision. The reader
was allowed to prefer either candidate or report a tie.

| Trial | Result |
| --- | --- |
| Abstract, 150–180 words | Baseline narrowly preferred overall for comparison controls; revision preferred for opening, closing, and flow. Both preserved the supplied facts and respected the length. |
| Method, two paragraphs, 130–180 words | Revision preferred for a precise threshold rule, connected procedure, and closer use of the supplied author voice. Both preserved the method and citation key. |
| Direct LaTeX revision | Citation/reference tokens, inline mathematics, and numbers preserved. The revised passage retains the sufficient/necessary distinction and uncertainty. This was a direct primary-agent trial, not a blind independent comparison. |
| Initial excerpt review | Baseline identified unequal tuning, confounded attribution, and unsupported robustness. This characterized existing scientific-review behavior; no before/after preference was measured for this task. |

## Actual outputs

- Abstract: [before](outputs/baseline-abstract.md), [after](outputs/revised-abstract.md).
- Calibration method: [before](outputs/baseline-holdout.md), [after](outputs/revised-holdout.md).
- LaTeX: [before](outputs/baseline-revision.tex), [direct revised trial](outputs/revised-revision.tex).
- [Initial excerpt review](outputs/baseline-review.md).
- [Independent reader's full assessment](outputs/blind-assessment.md).

Anonymous mapping: abstract A = revised, B = baseline; method A = baseline,
B = revised. The assessment preserves the reader's original labels.

The abstract was 171 words before and 154 after; the method was 142 before and
140 after. All four passages had zero default editorial findings. That equal
lint result did not capture the differences the reader identified.

## Deterministic validation

All 43 linter tests passed. Coverage includes the default advisory profile,
explicit strict/synthetic profiles, natural scientific constructions, source
coordinates, percentages versus LaTeX comments, JSON fields, and exit codes.
The skill validator passed, local Markdown links were checked, and the content
diff passed whitespace checks with the checkout's existing CRLF convention.

## Limits

This is a small qualitative trial using synthetic notes, one session's inherited
model settings, and one independent reader. It does not establish improvement
across authors, research fields, or complete manuscripts. No real manuscript or
rendered-paper submission review was performed. The best next evidence is the
user's response to a revision of their own paragraph, including which original
phrasing they want to retain. The abstract comparison also cautions against
discarding useful experimental controls solely to improve flow.
