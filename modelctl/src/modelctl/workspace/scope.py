from __future__ import annotations

import posixpath
from pathlib import Path


def enforce_scope(changed: list[str], allowed: list[str] | None) -> tuple[bool, list[str]]:
    if allowed is None:
        return False, changed
    allowed_canon = [posixpath.normpath(a.rstrip("/")) for a in allowed]
    out_of_scope = []
    for c in changed:
        canon = posixpath.normpath(c)
        # prevent .. traversal
        if canon.startswith("../") or canon == "..":
            out_of_scope.append(c)
            continue
        if not any(canon == a or canon.startswith(a + "/") for a in allowed_canon):
            out_of_scope.append(c)
    return (len(out_of_scope) == 0), out_of_scope
