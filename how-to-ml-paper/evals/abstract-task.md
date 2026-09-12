# Synthetic evaluation: draft an abstract

These notes are invented for a skill evaluation, not real research results.

User request: Write a 150–180 word abstract from the notes below. Lead with the actual research problem, make the method understandable, and state the strongest result the evidence supports. Return the abstract followed by at most three brief notes about unresolved claims. Draft with the available facts rather than asking questions first. No external literature search is needed.

Research notes:
- Decoder-only transformer inference often executes every block for every token. We investigate reducing inference latency through token-dependent block skipping.
- Method: a small learned gate uses the current hidden state to decide whether to execute or bypass each transformer block via its residual path. Fine-tuning optimizes the task loss with a penalty on the number of executed blocks. The method is called SkipGate.
- Evaluated on one 7B model, one GPU type, batch size one, and four held-out tasks: MMLU, GSM8K, ARC-C, HellaSwag. No other model scale, device, or serving load was tested.
- Equal fine-tuning data and 12 tuning trials per method. Five independent fine-tuning seeds. Accuracy summaries below are means across seeds; uncertainty intervals were not computed.
- Dense baseline: mean task accuracy 63.8%; median end-to-end latency 42 ms/token.
- SkipGate: mean task accuracy 63.5%; median end-to-end latency 29 ms/token. The latency includes gating overhead and uses the same prompts, generation length, and device. This is a 31% latency reduction, not a 31% throughput measurement.
- Static-depth baseline, matched on the average number of executed blocks: 61.9% mean task accuracy, 27 ms/token.
- Per-task differences for SkipGate relative to dense: +0.1, -0.2, -0.3, -0.8 percentage points respectively. Paired confidence intervals are not available, so significance/equivalence is not established.
- No gate ablation, serving throughput, energy measurement, or out-of-distribution experiment exists.
- Lab brainstorming notes claim: first adaptive-depth method; no accuracy loss; faster because redundant reasoning is removed; generalizes to any model. These are hypotheses or desired claims, not established findings.
