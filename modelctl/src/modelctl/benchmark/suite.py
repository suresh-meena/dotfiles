from __future__ import annotations



def delegation_efficiency(baseline: float, describe: float, review: float) -> float:
    if baseline == 0:
        return 0.0
    return (baseline - (describe + review)) / baseline
