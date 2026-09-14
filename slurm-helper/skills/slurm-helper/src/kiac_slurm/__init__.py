"""KIAC-aware Slurm batch script generator, validator, and diagnostics CLI."""

__version__ = "0.1.0"

from .diagnostics import (  # noqa: F401
    CONF_CONFLICT,
    CONF_DOCUMENTED,
    CONF_INFERRED,
    CONF_VERIFIED_LIVE,
    ERROR,
    INFO,
    PASS,
    WARN,
    Diagnostic,
    Report,
)
