from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DelegateModel:
    model_ref: str  # provider/model
    provider_id: str
    model_id: str
    bin: str  # driver|worker|frontier (frontier is brain, not delegate)
    enabled: bool
    availability_status: str  # AVAILABLE, UNAVAILABLE, UNCLASSIFIED
    metadata: dict[str, Any] | None = None
