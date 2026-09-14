"""Shared helpers for the test suite."""

from _path import SRC  # noqa: F401  (must import first to set sys.path)

from kiac_slurm.cli import run_check
from kiac_slurm.config import load_site_config

FIXTURES = SRC.parent / "tests" / "fixtures"
SITE = load_site_config()


def check(path, **kwargs):
    kwargs.setdefault("shellcheck", "off")
    return run_check(str(path), site=SITE, **kwargs)


def fixture(name):
    return FIXTURES / name
