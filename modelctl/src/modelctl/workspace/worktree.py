from __future__ import annotations

import subprocess
from pathlib import Path

from ..errors import ModelctlError


def create_worktree(*, repo_root: Path, dest: Path, base_commit: str | None = None) -> Path:
    # Ensure repo_root is git repo
    if not (repo_root / ".git").exists():
        raise ModelctlError(code="E_WORKTREE_CREATE_FAILED", message=f"not a git repo: {repo_root}")
    # Check dirty
    cp = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_root), capture_output=True, text=True, timeout=10)
    if cp.stdout.strip():
        raise ModelctlError(code="E_WORKTREE_DIRTY_BASE", message="base worktree is dirty; commit or stash before delegation")
    base = base_commit or "HEAD"
    # verify commit exists
    cp2 = subprocess.run(["git", "rev-parse", "--verify", base], cwd=str(repo_root), capture_output=True, timeout=5)
    if cp2.returncode != 0:
        raise ModelctlError(code="E_WORKTREE_CREATE_FAILED", message=f"base commit not found: {base}")
    # create worktree
    dest.parent.mkdir(parents=True, exist_ok=True)
    cp3 = subprocess.run(["git", "worktree", "add", "--detach", str(dest), base], cwd=str(repo_root), capture_output=True, text=True, timeout=15)
    if cp3.returncode != 0:
        raise ModelctlError(code="E_WORKTREE_CREATE_FAILED", message=f"git worktree add failed: {cp3.stderr[:500]}")
    return dest


def remove_worktree(*, repo_root: Path, dest: Path) -> None:
    try:
        subprocess.run(["git", "worktree", "remove", "--force", str(dest)], cwd=str(repo_root), capture_output=True, timeout=15)
    except Exception:
        pass
    # also try to remove dir
    import shutil

    try:
        if dest.exists():
            shutil.rmtree(dest)
    except Exception:
        raise ModelctlError(code="E_WORKTREE_CLEANUP_FAILED", message=f"failed to clean worktree {dest}")


def capture_diff(worktree: Path) -> tuple[str, list[str]]:
    cp = subprocess.run(["git", "diff", "--name-only"], cwd=str(worktree), capture_output=True, text=True, timeout=10)
    files = [x.strip() for x in cp.stdout.splitlines() if x.strip()]
    cp2 = subprocess.run(["git", "diff"], cwd=str(worktree), capture_output=True, text=True, timeout=10)
    # also untracked
    cp3 = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=str(worktree), capture_output=True, text=True, timeout=10)
    untracked = [x.strip() for x in cp3.stdout.splitlines() if x.strip()]
    all_files = files + untracked
    # get full diff including untracked via git diff
    diff = cp2.stdout
    if untracked:
        # add untracked content
        for f in untracked:
            try:
                content = (worktree / f).read_text()[:2000]
                diff += f"\n# untracked: {f}\n{content}\n"
            except Exception:
                pass
    return diff, all_files
