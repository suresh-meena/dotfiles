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
    variant: str | None = "max"


@dataclass(frozen=True)
class DelegationCfg:
    executable: str = "opencode"
    driver: RoleCfg = RoleCfg()
    worker: RoleCfg = RoleCfg()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "DelegationCfg":
        d = config.get("delegation", {})
        roles = d.get("roles", {})

        def role(name: str) -> RoleCfg:
            r = roles.get(name, {})
            model = r.get("model")
            variant = r.get("variant", "max")
            return RoleCfg(
                default_timeout_s=int(r.get("default_timeout_s", 600)),
                max_patch_lines=int(r.get("max_patch_lines", 1500)),
                max_parallel=int(r.get("max_parallel", 4)),
                model=str(model) if model else None,
                variant=str(variant) if variant else None,
            )

        return cls(
            executable=d.get("backend", {}).get("executable", "opencode"),
            driver=role("driver"),
            worker=role("worker"),
        )

    def role(self, name: str) -> RoleCfg:
        return self.driver if name == "driver" else self.worker
