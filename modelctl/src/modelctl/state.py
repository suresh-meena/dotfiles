from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_ROOT = Path.home() / ".local" / "state" / "modelctl"


def ensure_state_dirs(mode: int = 0o700) -> None:
    for sub in ["", "locks", "deployments", "generated", "tunnels"]:
        p = STATE_ROOT / sub if sub else STATE_ROOT
        p.mkdir(parents=True, exist_ok=True)
        try:
            p.chmod(mode)
        except Exception:
            pass


def deployment_dir(deployment_id: str) -> Path:
    # sanitize
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in deployment_id)
    return STATE_ROOT / "deployments" / safe


def lock_path(kind: str, name: str) -> Path:
    return STATE_ROOT / "locks" / kind / f"{name}.lock"
