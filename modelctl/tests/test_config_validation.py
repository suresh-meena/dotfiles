"""Config validation contract: exact error codes for each violation."""

from __future__ import annotations

import copy

import pytest

from modelctl.config.schema import validate_config
from modelctl.errors import ModelctlError


def base_config() -> dict:
    return {
        "version": 1,
        "machines": {"gpu-a": {"ssh": {"host": "gpu-a.example.internal"}}},
        "models": {"qwen-72b": {"served_model_name": "qwen-72b"}},
        "targets": {
            "qwen-72b@gpu-a": {
                "model": "qwen-72b",
                "machine": "gpu-a",
                "artifact": {"path": "/models/qwen"},
            }
        },
    }


def expect_error(cfg: dict, code: str):
    with pytest.raises(ModelctlError) as e:
        validate_config(cfg)
    assert e.value.code == code


def test_valid_base():
    validate_config(base_config())


def test_unknown_top_field():
    cfg = base_config()
    cfg["wat"] = 1
    expect_error(cfg, "E_CONFIG_UNKNOWN_FIELD")


def test_bad_version():
    cfg = base_config()
    cfg["version"] = 2
    expect_error(cfg, "E_CONFIG_INVALID")


def test_ssh_host_required():
    cfg = base_config()
    cfg["machines"]["gpu-a"]["ssh"] = {"user": "x"}
    expect_error(cfg, "E_CONFIG_INVALID")


def test_relative_inventory_root():
    cfg = base_config()
    cfg["machines"]["gpu-a"]["inventory"] = {"roots": ["models"]}
    expect_error(cfg, "E_CONFIG_INVALID")


def test_duplicate_gpus():
    cfg = base_config()
    cfg["targets"]["qwen-72b@gpu-a"]["gpus"] = [0, 0]
    expect_error(cfg, "E_CONFIG_INVALID")


def test_unknown_model_reference():
    cfg = base_config()
    cfg["targets"]["qwen-72b@gpu-a"]["model"] = "ghost"
    expect_error(cfg, "E_MODEL_NOT_FOUND")


def test_unknown_machine_reference():
    cfg = base_config()
    cfg["targets"]["qwen-72b@gpu-a"]["machine"] = "gpu-z"
    expect_error(cfg, "E_MACHINE_NOT_FOUND")


def test_exposure_requires_network_policy():
    cfg = base_config()
    cfg["targets"]["qwen-72b@gpu-a"]["security"] = {"allow_remote_exposure": True}
    expect_error(cfg, "E_CONFIG_INVALID")


def test_tensor_parallel_exceeds_gpus():
    cfg = base_config()
    t = cfg["targets"]["qwen-72b@gpu-a"]
    t["gpus"] = [0]
    t["vllm"] = {"tensor_parallel_size": 2}
    expect_error(cfg, "E_CONFIG_INVALID")


def test_secret_literal_rejected():
    cfg = base_config()
    cfg["machines"]["gpu-a"]["api_key"] = "sk-live"
    # unknown-field gate fires first: api_key is not a known machine key
    expect_error(cfg, "E_CONFIG_UNKNOWN_FIELD")


def test_unknown_target_field():
    cfg = base_config()
    cfg["targets"]["qwen-72b@gpu-a"]["vllm_extra"] = True
    expect_error(cfg, "E_CONFIG_UNKNOWN_FIELD")


def test_target_tunnel_local_port():
    cfg = base_config()
    cfg["targets"]["qwen-72b@gpu-a"]["tunnel"] = {"local_port": 18000}
    validate_config(cfg)


def test_target_tunnel_local_port_range():
    cfg = base_config()
    cfg["targets"]["qwen-72b@gpu-a"]["tunnel"] = {"local_port": 0}
    expect_error(cfg, "E_CONFIG_INVALID")
