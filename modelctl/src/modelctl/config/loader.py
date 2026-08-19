from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ..errors import ModelctlError
from .schema import validate_config


DEFAULT_USER_CONFIG = Path.home() / ".config" / "modelctl" / "config.yaml"
PROJECT_CONFIG = Path("modelctl.yaml")

BUILTIN_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "defaults": {
        "startup_timeout_s": 1200,
        "graceful_stop_timeout_s": 30,
        "kill_timeout_s": 15,
        "cleanup_verify_timeout_s": 30,
        "bind_host": "127.0.0.1",
        "access": "tunnel",
        "lifecycle": {"mode": "persistent"},
        "security": {
            "request_logging": False,
            "output_logging": False,
            "allow_remote_exposure": False,
            "state_file_mode": "0600",
            "state_dir_mode": "0700",
        },
    },
}


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ModelctlError(code="E_CONFIG_INVALID", message=f"YAML parse error in {path}: {e}")
    if not isinstance(data, dict):
        raise ModelctlError(code="E_CONFIG_INVALID", message=f"config {path} must be a mapping")
    return data


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(cli_config: str | Path | None = None) -> tuple[dict[str, Any], list[Path]]:
    """Load with precedence: builtin < user < project < cli --config. Returns (merged, sources)."""
    merged: dict[str, Any] = dict(BUILTIN_DEFAULTS)
    sources: list[Path] = []
    # user
    if DEFAULT_USER_CONFIG.exists():
        data = _load_yaml(DEFAULT_USER_CONFIG)
        merged = _deep_merge(merged, data)
        sources.append(DEFAULT_USER_CONFIG)
    # project
    if PROJECT_CONFIG.exists():
        data = _load_yaml(PROJECT_CONFIG)
        merged = _deep_merge(merged, data)
        sources.append(PROJECT_CONFIG)
    # cli override
    if cli_config:
        p = Path(cli_config)
        if not p.exists():
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"config file not found: {p}")
        data = _load_yaml(p)
        merged = _deep_merge(merged, data)
        sources.append(p)
    validate_config(merged)
    return merged, sources


def config_paths() -> list[Path]:
    return [DEFAULT_USER_CONFIG, PROJECT_CONFIG]
