from __future__ import annotations

import uuid
from typing import Any

from .inventory.registry import Registry


class EventLog:
    def __init__(self, registry: Registry):
        self.registry = registry

    def emit(self, event_type: str, *, target_id: str | None = None, deployment_id: str | None = None, machine_id: str | None = None, result: str | None = None, details: dict[str, Any] | None = None, trace_id: str | None = None) -> str:
        # never store prompt/completion/secrets; caller must redact
        if details:
            # strip sensitive keys if present
            redacted = {}
            for k, v in details.items():
                lk = k.lower()
                if any(s in lk for s in ("prompt", "completion", "api_key", "token", "secret", "authorization")):
                    continue
                redacted[k] = v
            details = redacted
        if trace_id:
            if details is None:
                details = {}
            details = dict(details)
            details["trace_id"] = trace_id
        self.registry.add_event(event_type, target_id=target_id, deployment_id=deployment_id, machine_id=machine_id, result=result, details=details)
        return trace_id or uuid.uuid4().hex[:12]
