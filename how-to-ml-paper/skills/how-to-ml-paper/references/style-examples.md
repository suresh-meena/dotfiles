# Worked prose revisions

These examples are synthetic, not reported research results. Their numbers and
method details are supplied explicitly so the rewrite never has to invent them.
The editing decisions are local heuristics (D), consistent with the precision
and reader-oriented guidance listed in [sources.md](sources.md). Copy the
reasoning, not the sentences or a fixed paragraph structure.

## Explain the operation hidden by a method label

Available facts: a reranker processes candidate passages in batches of ten;
it stops when the highest-ranked passage is unchanged across three consecutive
batches. No optimality result or quality measurement is supplied.

Draft:

> We introduce a novel adaptive framework that leverages contextual information
> to intelligently streamline retrieval, delivering an efficient and effective
> solution to the limitations of existing approaches.

Revision:

> The reranker processes ten candidate passages at a time. It stops when the
> highest-ranked passage remains unchanged across three consecutive batches.

The revision tells the reader what the system does. The unsupported novelty,
quality, and prior-work claims disappear; specific operations replace the label.
If the paragraph also needs evidence of time savings, request that measurement
instead of inventing it.

## Develop a comparison instead of listing results

Available facts: CacheBlend reuses cached features and refreshes selected
features. Across three tasks on one GPU, mean accuracy is 74.0% at 56 ms per
request; recomputing everything gives 74.1% at 80 ms. Reusing all cached features
gives 72.8% at 54 ms. No uncertainty intervals or other serving loads were tested.

Draft:

> CacheBlend achieves strong performance with reduced computational cost. It
> obtains 74.0% accuracy at 56 ms. The recomputation baseline obtains 74.1% at
> 80 ms. The cache-only baseline obtains 72.8% at 54 ms. These promising results
> demonstrate the effectiveness of selective refresh.

Revision:

> CacheBlend reduces latency from 80 to 56 ms per request while mean accuracy
> changes from 74.1% to 74.0% across three tasks on one GPU. Reusing every cached
> feature is slightly faster at 54 ms, but accuracy falls to 72.8%. Selective
> refresh therefore trades 2 ms per request for 1.2 percentage points of observed
> accuracy compared with full cache reuse.

The comparison gives the numbers a purpose. It does not establish accuracy
equivalence to recomputation, statistical significance, throughput, or an
internal mechanism. Whether to include the missing interval in this paragraph
depends on the surrounding claim; it must be addressed if the text asserts
accuracy preservation. A full uncertainty discussion is not a stock ending.

## Keep useful voice and reduce only the obstruction

Available facts: the author expected additional labels to help; a supplied
learning curve is flat after 2,000 labels, and evaluation is on Dataset A.

Draft:

> We expected more labels to help. That did not happen on Dataset A. As can be
> seen in the learning curve, accuracy stayed flat after 2,000 labels, which
> suggests that simply adding labels did not help in this particular setting.

Revision:

> We expected more labels to help. On Dataset A, accuracy stayed flat after
> 2,000 labels.

The author's direct opening works. Keep it and remove the repeated explanation.
Do not replace it with “We investigate the effect of annotation scale” merely
because that sounds more academic.

## Preserve scientific contrast and uncertainty

Draft:

> The baseline uses one threshold for every group, whereas our method learns a
> threshold for each group. We measure accuracy, calibration, and coverage.

Leave this alone when those facts are correct. The contrast explains a method
difference, and the list names three actual measurements. Splitting the first
sentence or disguising the list would add no information.

Draft:

> The paired difference is 0.6 percentage points, with a 95% confidence interval
> of [-0.2, 1.4]. This interval does not establish a positive effect.

Keep the uncertainty. Replacing the second sentence with “The method has no
effect” changes the inference. Replacing it with “The method improves accuracy”
overstates it. In LaTeX, also preserve the notation, citation keys, and labels.

## Repair a mechanism claim at the supported level

Available evidence: removing module X reduces mean accuracy by 2.1 percentage
points. The supplied material contains no representation-invariance measurement.

Draft:

> Module X improves accuracy by learning invariant representations.

Revision:

> Removing module X reduces mean accuracy by 2.1 percentage points.

Put the missing mechanism evidence in an editorial note when relevant. Do not
insert a figure reference or a new measurement to make the explanation plausible.
A controlled ablation can inform attribution without establishing the proposed
representation mechanism.

