from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def run_validation(*, workdir: Path, commands: list[list[str]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    all_ok = True
    for argv in commands:
        # argv is list of strings, never shell
        try:
            cp = subprocess.run(argv, cwd=str(workdir), capture_output=True, timeout=300, text=True)
            results.append({"argv": argv, "exit_code": cp.returncode, "ok": cp.returncode == 0})
            if cp.returncode != 0:
                all_ok = False
        except Exception as e:
            results.append({"argv": argv, "exit_code": 127, "ok": False, "error": str(e)[:500]})
            all_ok = False
    return {"status": "passed" if all_ok else "failed", "commands": results, "scope": "passed" if all_ok else "failed"}


def check_patch_size(diff_text: str, max_lines: int) -> bool:
    lines = diff_text.count("\n")
    return lines <= max_lines
