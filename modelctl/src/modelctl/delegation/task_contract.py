from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_task_hash(task: dict[str, Any]) -> str:
    j = json.dumps(task, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(j.encode()).hexdigest()


def load_task_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    text = p.read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid task JSON {p}: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"task file must be JSON object: {p}")
    return data

