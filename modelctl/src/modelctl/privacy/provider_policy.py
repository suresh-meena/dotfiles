from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def is_policy_stale(metadata: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not metadata:
        return False
    valid_until = metadata.get("valid_until") or metadata.get("expires_at")
    if not valid_until:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        return now > expiry
    except Exception:
        return False
