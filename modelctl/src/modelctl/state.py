from __future__ import annotations

import os
from pathlib import Path

STATE_ROOT = Path(os.environ.get("MODELCTL_STATE_ROOT") or Path.home() / ".local" / "state" / "modelctl")


def ensure_state_dirs(mode: int = 0o700) -> None:
    for sub in ["", "locks", "deployments", "generated", "tunnels"]:
        p = STATE_ROOT / sub if sub else STATE_ROOT
        p.mkdir(parents=True, exist_ok=True)
        try:
            p.chmod(mode)
        except Exception:
            pass


def lock_path(kind: str, name: str) -> Path:
    return STATE_ROOT / "locks" / kind / f"{name}.lock"
