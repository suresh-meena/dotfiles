from __future__ import annotations

from typing import Any

from ..inventory.registry import Registry


def check_budget(*, registry: Registry, config: dict[str, Any]) -> tuple[bool, str]:
    budget_cfg = config.get("budget", {})
    hard = budget_cfg.get("hard_daily_usd")
    soft = budget_cfg.get("soft_daily_usd")
    today = registry.budget_today()
    if hard is not None and today >= hard:
        return False, f"hard daily budget exceeded: {today:.4f} >= {hard}"
    if soft is not None and today >= soft:
        # warn but allow with cheaper model; for v1 we allow
        pass
    return True, f"today={today:.4f}"


def record_cost(registry: Registry, run_id: str, cost: float, model_ref: str) -> None:
    registry.add_budget(run_id, cost, model_ref)
