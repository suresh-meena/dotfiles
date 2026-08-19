from __future__ import annotations

from pathlib import Path
from typing import Any


def _canonical(p: str, base: Path | None) -> Path:
    """Canonicalize a path, resolving symlinks and ``..`` so symlink escapes
    and traversal are caught. Relative paths resolve against base."""
    target = Path(p)
    if base is not None and not target.is_absolute():
        target = base / target
    try:
        return target.resolve()
    except OSError:
        return target.absolute()


def enforce_scope(changed: list[str], allowed: list[str] | None, base: Path | None = None) -> tuple[bool, list[str]]:
    if allowed is None:
        return False, changed
    base_p = base.resolve() if base is not None else None
    allowed_canon = [_canonical(a, base_p) for a in allowed]
    out_of_scope = []
    for c in changed:
        canon = _canonical(c, base_p)
        if not any(canon == a or canon.is_relative_to(a) for a in allowed_canon):
            out_of_scope.append(c)
    return (len(out_of_scope) == 0), out_of_scope