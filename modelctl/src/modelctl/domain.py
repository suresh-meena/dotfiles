from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def target_id(model: str, machine: str) -> str:
    return f"{model}@{machine}"


def config_digest(canonical_json: str | bytes) -> str:
    if isinstance(canonical_json, str):
        canonical_json = canonical_json.encode()
    return hashlib.sha256(canonical_json).hexdigest()


def deployment_id(target: str, digest: str, nonce: str) -> str:
    return f"{target}:{digest[:12]}:{nonce}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sanitize_unit_name(target_id_str: str, digest: str) -> str:
    # modelctl-qwen-72b-gpu-a-58b3c7f1.service
    base = target_id_str.replace("@", "-").replace("_", "-").replace(".", "-")
    # keep alnum and dash
    base = re.sub(r"[^A-Za-z0-9-]", "-", base)
    base = re.sub(r"-+", "-", base).strip("-").lower()
    return f"modelctl-{base}-{digest[:8]}.service"
