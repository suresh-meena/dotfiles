"""Enabled Slurm budgets must cover the controller's bounded result pull."""

from __future__ import annotations

import pytest

from fleetq.config import load_config
from fleetq.errors import FqError


def _config(tmp_path, *, collect_limit: str = ""):
    path = tmp_path / "fleetqd.toml"
    path.write_text(
        f'[daemon]\nstate_dir = "{tmp_path / "state"}"\ndev_mode = true\n'
        f'{collect_limit}'
        '\n[[node]]\nid = "site"\nbackend = "slurm"\nenabled = true\n'
        '[node.budget]\nmax_bytes_per_transfer = 1073741824\n'
        'bytes_burst = 1073741824\n'
    )
    return path


def test_enabled_site_rejects_collection_larger_than_transfer_ceiling(tmp_path):
    with pytest.raises(FqError, match="bounded result pull"):
        load_config(_config(tmp_path))


def test_enabled_site_accepts_compatible_bounded_collection(tmp_path):
    path = _config(tmp_path, collect_limit='\n[controller]\ncollect_max_bytes = 16777216\ncollect_max_files = 1000\n')
    assert load_config(path).controller["collect_max_bytes"] == 16777216


def test_bare_node_python_accepts_absolute_path_and_defaults_to_python3(tmp_path):
    path = tmp_path / "fleetqd.toml"
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n\n'
                    '[[node]]\nid = "ada1"\nbackend = "bare"\nmode = "exclusive"\n'
                    'node_python = "/data/home/ayand/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12"\n')
    assert load_config(path).nodes[0].node_python.endswith("/python3.12")

    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n\n'
                    '[[node]]\nid = "ada1"\nbackend = "bare"\nmode = "exclusive"\n')
    assert load_config(path).nodes[0].node_python == "python3"


@pytest.mark.parametrize("value", ['"python3.12"', '"/tmp/python\\nmalicious"', '""'])
def test_bare_node_python_rejects_non_absolute_or_unsafe_values(tmp_path, value):
    path = tmp_path / "fleetqd.toml"
    path.write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n\n'
                    '[[node]]\nid = "ada1"\nbackend = "bare"\nmode = "exclusive"\n'
                    f'node_python = {value}\n')
    with pytest.raises(FqError, match="node_python"):
        load_config(path)
