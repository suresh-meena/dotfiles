from __future__ import annotations

from typing import Any


def report(data: dict[str, Any]) -> str:
    lines = [f"Benchmark: {len(data.get('results', []))} tasks", f"avg eta: {data.get('avg_eta', 0):.3f}", f"elapsed: {data.get('elapsed_s', 0):.2f}s"]
    for r in data.get("results", []):
        lines.append(f"  {r['task']}: eta={r['eta']:.3f}")
    return "\n".join(lines)
