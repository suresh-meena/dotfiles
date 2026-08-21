"""Smoke-test fixtures.

Isolation: state root + db are redirected via env vars set here, before any
modelctl import (state.py reads MODELCTL_STATE_ROOT at import time).
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_STATE = Path(tempfile.mkdtemp(prefix="modelctl-smoke-"))
os.environ["MODELCTL_STATE_ROOT"] = str(_STATE)
os.environ["MODELCTL_DB"] = str(_STATE / "modelctl.db")

from click.testing import CliRunner  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def fake_model_dir(tmp_path_factory):
    """A model dir that satisfies the scanner markers."""
    d = tmp_path_factory.mktemp("models") / "fake-72b"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"model_type": "qwen2"}))
    (d / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {}}))
    (d / "tokenizer.json").write_text("{}")
    return d


@pytest.fixture(scope="session")
def config_file(fake_model_dir, tmp_path_factory):
    """Example config repointed at the fake artifact and a free port."""
    text = (PROJECT_ROOT / "examples" / "modelctl.yaml").read_text()
    text = text.replace("roots: [/models, /mnt/models]", f"roots: [{fake_model_dir.parent}]")
    text = text.replace("path: /models/Qwen2.5-72B-Instruct", f"path: {fake_model_dir}")
    text = text.replace("defaults: { port: 8000,", f"defaults: {{ port: {_free_port()},")
    p = tmp_path_factory.mktemp("cfg") / "modelctl.yaml"
    p.write_text(text)
    return p


@pytest.fixture(scope="session")
def cfg_path(config_file):
    return ["--config", str(config_file)]


@pytest.fixture
def run(cfg_path):
    runner = CliRunner()

    def _run(*args: str, expect_success: bool = True):
        res = runner.invoke(_load_cli(), ["--json", *cfg_path, *args])
        if expect_success:
            assert res.exit_code == 0, f"{args} failed:\n{res.output}"
        return res

    return _run


def _load_cli():
    from modelctl.cli import cli

    return cli


def parse_json(res):
    """Parse the first JSON object printed on stdout."""
    out = res.output.strip()
    start = out.find("{")
    assert start != -1, f"no JSON in output:\n{out}"
    obj, _end = json.JSONDecoder().raw_decode(out[start:])
    return obj
