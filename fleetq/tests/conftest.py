"""Shared test setup.

Every test runs against temporary state, and the default runner
(`scripts/test`) executes pytest inside `unshare -rn`, a network namespace with
no route anywhere (§13.1). PATH fakes are a convenience, not the security
boundary; the empty network namespace is.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Never let a test see the developer's real fleet, tokens or SSH agent.
for _var in ("SSH_AUTH_SOCK", "FLEETCTL_PERMIT", "FQ_TOKEN_FILE", "FQ_URL"):
    os.environ.pop(_var, None)
