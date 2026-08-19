from __future__ import annotations

import time
from typing import Any

from .suite import delegation_efficiency


def run_bench(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    start = time.time()
    results = []
    for t in tasks:
        baseline = t.get("baseline", 1.0)
        describe = t.get("describe", 0.2)
        review = t.get("review", 0.1)
        eta = delegation_efficiency(baseline, describe, review)
        results.append({"task": t.get("task_class", "unknown"), "eta": eta, "baseline": baseline})
    elapsed = time.time() - start
    avg_eta = sum(r["eta"] for r in results) / len(results) if results else 0
    return {"ok": True, "elapsed_s": elapsed, "avg_eta": avg_eta, "results": results}
