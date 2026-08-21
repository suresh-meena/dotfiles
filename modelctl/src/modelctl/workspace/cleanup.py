from __future__ import annotations

import shutil
from pathlib import Path


def cleanup_path(p: Path) -> None:
    if p.exists():
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


def verify_clean(p: Path) -> bool:
    return not p.exists()
