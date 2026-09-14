"""Isolate tests from the machine: no learning writes, no real cache dir."""

import os
import tempfile

os.environ.setdefault("KIAC_SLURM_LEARN", "off")
_scratch = tempfile.mkdtemp(prefix="kiac-slurm-test-")
os.environ.setdefault("KIAC_SLURM_CACHE_DIR", os.path.join(_scratch, "cache"))
os.environ.setdefault("KIAC_SLURM_OBSERVATIONS", os.path.join(_scratch, "obs.jsonl"))
