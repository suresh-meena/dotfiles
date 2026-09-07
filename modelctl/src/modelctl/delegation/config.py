"""Typed access to the delegation config section with single-source defaults."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RoleCfg:
    default_timeout_s: int = 600
    max_patch_lines: int = 1500
    max_parallel: int = 4
    model: str | None = None
    variant: str | None = None
    workspace: str = "project_dir"


@dataclass(frozen=True)
class OrchestrationCfg:
    """Boundaries for the code-driven scheduler.

    These are deliberately policy values, not model-selection hints.  The
    brain remains responsible for deciding what belongs in a graph; this
    object only limits how much work the control plane may execute.
    """

    max_total_parallel: int = 12
    max_task_depth: int = 1
    max_task_fanout: int = 8
    worker_batch_size: int = 8
    worker_return_tokens: int = 800
    driver_return_tokens: int = 2000
    aggregate_return_tokens: int = 4000
    require_structured_results: bool = True
    recursive_delegation: bool = False
    fail_fast: bool = True
    overlap_policy: str = "serialize"


@dataclass(frozen=True)
class DelegationCfg:
    executable: str = "opencode"
    driver: RoleCfg = RoleCfg()
    worker: RoleCfg = RoleCfg()
    orchestration: OrchestrationCfg = OrchestrationCfg()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "DelegationCfg":
        d = config.get("delegation", {})
        roles = d.get("roles", {})

        def role(name: str) -> RoleCfg:
            r = roles.get(name, {})
            model = r.get("model")
            variant = r.get("variant")  # None → omit --variant, use model default
            workspace = str(r.get("workspace", "project_dir"))
            return RoleCfg(
                default_timeout_s=int(r.get("default_timeout_s", 600)),
                max_patch_lines=int(r.get("max_patch_lines", 1500)),
                max_parallel=int(r.get("max_parallel", 4)),
                model=str(model) if model else None,
                variant=str(variant) if variant else None,
                workspace=workspace,
            )

        o = d.get("orchestration", {})
        orchestration = OrchestrationCfg(
            max_total_parallel=int(o.get("max_total_parallel", 12)),
            max_task_depth=int(o.get("max_task_depth", 1)),
            max_task_fanout=int(o.get("max_task_fanout", 8)),
            worker_batch_size=int(o.get("worker_batch_size", 8)),
            worker_return_tokens=int(o.get("worker_return_tokens", 800)),
            driver_return_tokens=int(o.get("driver_return_tokens", 2000)),
            aggregate_return_tokens=int(o.get("aggregate_return_tokens", 4000)),
            require_structured_results=bool(o.get("require_structured_results", True)),
            recursive_delegation=bool(o.get("recursive_delegation", False)),
            fail_fast=bool(o.get("fail_fast", True)),
            overlap_policy=str(o.get("overlap_policy", "serialize")),
        )

        return cls(
            executable=d.get("backend", {}).get("executable", "opencode"),
            driver=role("driver"),
            worker=role("worker"),
            orchestration=orchestration,
        )

    def role(self, name: str) -> RoleCfg:
        return self.driver if name == "driver" else self.worker

    def limits(self, config: dict[str, Any], role: str) -> tuple[int, int]:
        """Return effective ``(total, role)`` concurrency caps.

        A budget cap can only reduce the delegation policy; it can never
        silently broaden it.
        """
        budget = config.get("budget", {})
        total = min(
            self.orchestration.max_total_parallel,
            int(budget.get("max_parallel_total", self.orchestration.max_total_parallel)),
        )
        role_key = f"max_parallel_{role}"
        role_limit = min(
            self.role(role).max_parallel, int(budget.get(role_key, self.role(role).max_parallel))
        )
        return max(1, total), max(1, role_limit)
