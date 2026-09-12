# Synthetic evaluation: narrow LaTeX revision

The excerpt is an invented fixture, not a real research claim.

User request: Make this paragraph clearer without changing the science or expanding the discussion. Return replacement LaTeX only. Preserve the citation keys, equation label, notation, numbers, and the distinction between a sufficient condition and an observed result. Do not review the whole paper.

```latex
Under Assumption~\ref{ass:bounded}, the update in Eq.~\eqref{eq:update} is stable when $0 < \eta < 2/L$; this is a sufficient condition, not a necessary one. Table~\ref{tab:main} reports a 0.6 percentage-point gain over \citet{lee2024baseline} on Dataset A, averaged across five seeds. The 95\% confidence interval for the paired difference is $[-0.2, 1.4]$ percentage points, so these experiments do not establish a positive population-level effect. Prior work analyzes a convex objective, whereas our guarantee assumes local smoothness along the optimization trajectory.
```
