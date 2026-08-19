from __future__ import annotations

import re

SECRET_PATTERN = re.compile(r"(api[_-]?key|hf[_-]?token|secret|password|authorization).{0,20}[:=]\s*\S+", re.IGNORECASE)


def redact(text: str) -> str:
    return SECRET_PATTERN.sub(r"\1=[REDACTED]", text)


def redact_dict(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        lk = k.lower()
        if any(s in lk for s in ("api_key", "hf_token", "secret", "token", "password", "authorization")):
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, str):
            out[k] = redact(v)
        else:
            out[k] = v
    return out
