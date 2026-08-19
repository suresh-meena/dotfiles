from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ..inventory.registry import Registry


DEFAULT_MODEL_REF = "opencode-go/muse-spark-1.2-contributor"
DEFAULT_PROVIDER = "opencode-go"
DEFAULT_MODEL_ID = "muse-spark-1.2-contributor"


class Catalog:
    def __init__(self, registry: Registry):
        self.registry = registry

    def sync(self, refresh: bool = True) -> dict[str, Any]:
        """Sync from `opencode models opencode-go --refresh --verbose` if available, otherwise ensure default exists."""
        # Try to run opencode models
        raw_models: list[dict[str, Any]] = []
        error: str | None = None
        try:
            cp = subprocess.run(
                ["opencode", "models", DEFAULT_PROVIDER, "--refresh", "--verbose"],
                capture_output=True,
                timeout=20,
                text=True,
            )
            if cp.returncode == 0:
                # Try parse json if --format json, else try to extract
                try:
                    data = json.loads(cp.stdout)
                    if isinstance(data, list):
                        raw_models = data
                    elif isinstance(data, dict) and "models" in data:
                        raw_models = data["models"]
                except Exception:
                    # fallback: treat as not json, just ensure default
                    raw_models = []
            else:
                error = cp.stderr[:500] or cp.stdout[:500]
        except FileNotFoundError:
            error = "opencode not found"
        except Exception as e:
            error = str(e)[:500]

        # Ensure default Muse model is present as AVAILABLE (per spec, driver/worker both use this single model).
        # Single entry per model_ref; the router aliases it to both bins (PK on model_ref).
        self.registry.upsert_delegate_model(DEFAULT_MODEL_REF, DEFAULT_PROVIDER, DEFAULT_MODEL_ID, "worker", True, "AVAILABLE", {"source": "builtin-default"})
        # For catalog sync, if we found raw_models, upsert them as UNCLASSIFIED disabled
        for m in raw_models:
            mid = m.get("id") or m.get("model_id") or m.get("name")
            if not mid:
                continue
            ref = f"{DEFAULT_PROVIDER}/{mid}"
            if ref == DEFAULT_MODEL_REF:
                continue
            self.registry.upsert_delegate_model(ref, DEFAULT_PROVIDER, mid, "unclassified", False, "AVAILABLE", {"source": "opencode-sync", "raw": m})

        # Mark disappeared as UNAVAILABLE (not deleting history)
        existing = self.registry.list_delegate_models()
        seen_refs = {DEFAULT_MODEL_REF} | {f"{DEFAULT_PROVIDER}/{m.get('id')}" for m in raw_models if m.get("id")}
        for e in existing:
            if e["model_ref"] not in seen_refs and e["availability_status"] == "AVAILABLE":
                self.registry.upsert_delegate_model(e["model_ref"], e["provider_id"], e["model_id"], e["bin"], bool(e["enabled"]), "UNAVAILABLE", json.loads(e["metadata_json"]) if e["metadata_json"] else None)

        return {"ok": True, "synced": True, "default_model": DEFAULT_MODEL_REF, "discovered": len(raw_models), "error": error}
