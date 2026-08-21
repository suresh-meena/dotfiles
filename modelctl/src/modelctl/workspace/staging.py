from __future__ import annotations

import shutil
from pathlib import Path


def create_staging(*, base: Path, files: list[str], dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        src = base / f
        if not src.exists():
            continue
        # prevent traversal
        rel = Path(f)
        if ".." in rel.parts or rel.is_absolute():
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    return dest
