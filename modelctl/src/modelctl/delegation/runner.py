from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ModelctlError
from ..inventory.registry import Registry
from ..privacy.classification import allows_cloud
from ..workspace.cleanup import cleanup_path, verify_clean
from ..workspace.scope import enforce_scope
from . import policy as delegation_policy
from .adapters.opencode import OpenCodeAdapter
from .budget import check_budget
from .config import DelegationCfg
from .result_contract import make_result_envelope
from .router import deterministic_select
from .task_contract import canonical_task_hash
from .validation import check_patch_size, run_validation


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_run(
    registry: Registry,
    run_id: str,
    state: str,
    *,
    finished_at: str | None = None,
    validation_status: str | None = None,
    observed_cost: float | None = None,
) -> None:
    registry.update_delegate_run(
        run_id,
        state=state,
        finished_at=finished_at,
        validation_status=validation_status,
        observed_cost=observed_cost,
    )


def _fail(
    registry: Registry, run_id: str, code: str, message: str, *, status: str, **kwargs
) -> ModelctlError:
    _mark_run(registry, run_id, "FAILED", finished_at=_utc_now(), validation_status=status)
    return ModelctlError(code=code, message=message, **kwargs)


def _git_snapshot(workdir: Path) -> dict[str, str] | None:
    """Capture fingerprints of the current git worktree changes.

    A status-only comparison cannot distinguish a delegate edit from a dirty
    file that already existed before the run.  Fingerprinting changed paths
    makes post-run scope checks safe in a user-owned, dirty worktree.
    """
    try:
        cp = subprocess.run(
            [
                "git",
                "-C",
                str(workdir),
                "status",
                "--porcelain",
                "--no-renames",
                "--untracked-files=all",
                "-z",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if cp.returncode != 0:
            return None
        out = cp.stdout
        snapshot: dict[str, str] = {}
        toks = out.split("\0")
        index = 0
        while index < len(toks):
            tok = toks[index]
            if not tok:
                index += 1
                continue
            # entries look like "XY path" or "XY orig -> new"
            if len(tok) < 4:
                index += 1
                continue
            status = tok[:2]
            rest = tok[3:]
            if rest:
                path = rest
                target = workdir / path
                try:
                    stat = target.lstat()
                    if (stat.st_mode & 0o170000) == 0o100000:
                        digest = hashlib.sha256(target.read_bytes()).hexdigest()
                    elif (stat.st_mode & 0o170000) == 0o120000:
                        digest = hashlib.sha256(os.readlink(target).encode()).hexdigest()
                    else:
                        digest = f"mode:{stat.st_mode:o}"
                    fingerprint = f"{status}:{stat.st_mode:o}:{digest}"
                except OSError:
                    fingerprint = f"{status}:missing"
                snapshot[path] = fingerprint
            index += 1
        return snapshot
    except Exception:
        return None


def _git_changed_paths(workdir: Path, baseline: dict[str, str] | None = None) -> list[str] | None:
    """Return net changed paths, optionally relative to a pre-run snapshot."""
    current = _git_snapshot(workdir)
    if current is None:
        return None
    if baseline is None:
        return sorted(current)
    return sorted(
        path for path, fingerprint in current.items() if baseline.get(path) != fingerprint
    )


def _is_git_workdir(workdir: Path) -> bool:
    try:
        cp = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return cp.returncode == 0 and cp.stdout.strip() == "true"
    except Exception:
        return False


_SCOPE_NOISE = (
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".coverage",
    "*.egg-info/",
    ".ipynb_checkpoints/",
    ".tox/",
    ".venv/",
)


def _is_scope_noise(path: str) -> bool:
    import fnmatch

    p = path.rstrip("/")
    return any(
        fnmatch.fnmatch(p, pat.rstrip("/")) or f"{p}/".startswith(pat)
        for pat in _SCOPE_NOISE
        if not pat.startswith("*")
    ) or any(fnmatch.fnmatch(p, pat) for pat in _SCOPE_NOISE if pat.startswith("*"))


def _validate_result(
    registry: Registry,
    run_id: str,
    task: dict[str, Any],
    result: dict[str, Any],
    workdir: Path,
    role: str,
    dcfg: DelegationCfg,
    baseline: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Scope enforcement + patch size + declared validations. Raises on violation."""
    changed_paths: list[str] = []
    parsed = result.get("parsed", {})
    if isinstance(parsed, dict) and isinstance(parsed.get("changed_paths"), list):
        changed_paths = [str(p) for p in parsed["changed_paths"]]
    # Trust the filesystem, not the model's self-report.  A non-git workdir
    # retains the parsed paths as a last resort for isolated adapters.
    if _is_git_workdir(workdir):
        actual_paths = _git_changed_paths(workdir, baseline)
        if actual_paths is None:
            raise _fail(
                registry,
                run_id,
                "E_DELEGATE_VALIDATION_FAILED",
                "unable to inspect git worktree after delegate run",
                status="git_inspection_failed",
            )
        changed_paths = actual_paths
    # validation byproducts (caches, coverage) are not real scope changes
    changed_paths = [p for p in changed_paths if not _is_scope_noise(p)]

    allowed = task.get("allowed_write_paths")
    validation: dict[str, Any] = {"scope": "passed", "status": "passed", "commands": []}
    if not changed_paths:
        return validation, changed_paths

    ok_scope, oos = enforce_scope(changed_paths, allowed, base=workdir)
    if not ok_scope:
        raise _fail(
            registry,
            run_id,
            "E_DELEGATE_OUT_OF_SCOPE_CHANGE",
            f"out of scope: {oos}",
            status="out_of_scope",
            details={"changed": changed_paths, "allowed": allowed},
        )

    diff_text = str(result.get("stdout", ""))[:10000]
    if not check_patch_size(diff_text, dcfg.role(role).max_patch_lines):
        raise _fail(
            registry,
            run_id,
            "E_DELEGATE_VALIDATION_FAILED",
            "patch too large",
            status="patch_too_large",
        )

    validation_cmds = task.get("validation") or []
    if validation_cmds:
        val = run_validation(
            workdir=workdir,
            commands=[c if isinstance(c, list) else str(c).split() for c in validation_cmds],
        )
        if val["status"] != "passed":
            raise _fail(
                registry,
                run_id,
                "E_DELEGATE_VALIDATION_FAILED",
                "validation failed",
                status="failed",
                details=val,
            )
        validation = val
    return validation, changed_paths


def _preflight(
    registry: Registry,
    config: dict[str, Any],
    task: dict[str, Any],
    role: str,
    bin_: str,
    requested_model: str | None,
) -> tuple[DelegationCfg, dict[str, Any], str]:
    """Budget + routing + privacy + agent-profile gates. Returns (dcfg, selected, agent_profile).

    Model precedence: task file model_ref > requested_model (CLI flag) >
    config delegation.roles.<role>.model > built-in bin default."""
    delegation_policy.validate_task_contract(task)

    dcfg = DelegationCfg.from_config(config)
    explicit = task.get("model_ref") or requested_model or dcfg.role(role).model

    ok_budget, reason = check_budget(registry=registry, config=config)
    if not ok_budget:
        raise ModelctlError(code="E_DELEGATION_BUDGET_EXCEEDED", message=reason)

    selected = deterministic_select(
        registry=registry,
        requested_bin=bin_,
        task_class=task.get("task_class"),
        requested_model=explicit,
        config=config,
    )

    data_class = str(task.get("data_class", "INTERNAL")).upper()
    max_allowed = str(task.get("max_data_class", "INTERNAL")).upper()
    if not allows_cloud(data_class, max_allowed):
        raise ModelctlError(
            code="E_DELEGATION_PRIVACY_DENIED",
            message=f"data class {data_class} not allowed for cloud delegation (max {max_allowed})",
        )

    agent_profile = task.get("agent_profile") or (
        "modelctl-driver" if role == "driver" else "modelctl-worker-read"
    )
    if agent_profile not in delegation_policy.ALLOWED_AGENTS:
        raise ModelctlError(
            code="E_DELEGATION_POLICY_DENIED", message=f"agent {agent_profile} not allowed"
        )
    return DelegationCfg.from_config(config), selected, agent_profile


def _resolve_delegate_workdir(
    task: dict[str, Any], dcfg: DelegationCfg, role: str
) -> tuple[Path, str, bool, dict[str, str] | None]:
    """Resolve the configured workspace and capture its git baseline."""
    role_workspace = dcfg.role(role).workspace
    if task.get("isolated") is True or role_workspace == "isolated_worktree":
        return Path(tempfile.mkdtemp(prefix="modelctl-wt-")), "isolated_worktree", True, None

    raw_wd = task.get("workdir") or os.getcwd()
    workdir = Path(raw_wd).expanduser().resolve()
    if not workdir.is_dir():
        raise ModelctlError(
            code="E_CONFIG_INVALID", message=f"task workdir does not exist: {workdir}"
        )
    if not _is_git_workdir(workdir):
        return workdir, "project_dir", False, None
    baseline = _git_snapshot(workdir)
    if baseline is None:
        raise ModelctlError(
            code="E_DELEGATE_VALIDATION_FAILED",
            message="unable to inspect git worktree before delegate run",
        )
    return workdir, "project_dir", False, baseline


def run_delegate_task(
    *,
    registry: Registry,
    config: dict[str, Any],
    task: dict[str, Any],
    role: str,
    bin_: str | None = None,
    trace_id: str | None = None,
    task_file: str | None = None,
    caller: str = "codex",
    requested_model: str | None = None,
) -> dict[str, Any]:
    """Honest delegation pipeline: every claim is backed by a real check.

    No mock success: if the opencode executable is absent the run fails with
    E_OPENCODE_NOT_FOUND and no RUNNING/SUCCEEDED record is fabricated.
    """
    trace_id = trace_id or uuid.uuid4().hex[:12]
    bin_ = bin_ or role
    task_class = str(task.get("task_class") or task.get("objective") or "unknown")[:40]

    dcfg, selected, agent_profile = _preflight(registry, config, task, role, bin_, requested_model)

    adapter = OpenCodeAdapter(executable=dcfg.executable)
    ok_avail, detail = adapter.check_available()
    if not ok_avail:
        raise ModelctlError(
            code="E_OPENCODE_NOT_FOUND",
            message=f"opencode executable unavailable: {detail}",
            details={"bin": bin_},
        )

    # Workdir: run in the real project by default; isolate only when requested
    # by the task or role policy.
    workdir, workspace_mode, created_temp, baseline = _resolve_delegate_workdir(task, dcfg, role)
    run_id = f"dlg_{uuid.uuid4().hex[:12]}"
    registry.insert_delegate_run(
        run_id,
        caller,
        bin_,
        selected["model_ref"],
        task_class,
        workspace_mode,
        "RUNNING",
        parent_trace_id=trace_id,
        task_json=json.dumps(task, sort_keys=True),
    )

    prompt = task.get("objective") or task.get("prompt") or json.dumps(task)[:4000]
    timeout_s = int(task.get("timeout_s") or dcfg.role(role).default_timeout_s)

    def _record_pid(proc: Any) -> None:
        try:
            registry.update_delegate_run(run_id, process_pid=proc.pid)
        except Exception:
            pass

    start = time.time()
    try:
        result = adapter.run(
            model_ref=selected["model_ref"],
            agent_profile=agent_profile,
            task_prompt=prompt,
            workdir=workdir,
            timeout_s=timeout_s,
            on_start=_record_pid,
            variant=task.get("variant") or dcfg.role(role).variant,
        )
    except ModelctlError as e:
        status = "timeout" if e.code == "E_DELEGATE_TIMEOUT" else "provider_failure"
        _mark_run(registry, run_id, "FAILED", finished_at=_utc_now(), validation_status=status)
        raise
    except Exception:
        _mark_run(
            registry, run_id, "FAILED", finished_at=_utc_now(), validation_status="internal_error"
        )
        raise

    latency_ms = result.get("latency_ms", int((time.time() - start) * 1000))
    if result.get("exit_code") != 0:
        stderr = str(result.get("stderr", ""))[:2000]
        raise _fail(
            registry,
            run_id,
            "E_DELEGATE_PROVIDER_FAILURE",
            f"opencode exited {result.get('exit_code')}",
            status="non_zero_exit",
            details={"stderr": stderr[:500]},
        )

    try:
        validation, changed_paths = _validate_result(
            registry, run_id, task, result, workdir, role, dcfg, baseline
        )
    finally:
        if created_temp:
            cleanup_path(workdir)
            if not verify_clean(workdir):
                raise ModelctlError(
                    code="E_WORKTREE_CLEANUP_FAILED", message="worktree cleanup failed"
                )

    # honest cost accounting: adapter does not report tokens, so record 0.0
    # and flag the estimate instead of fabricating a number
    _mark_run(
        registry,
        run_id,
        "SUCCEEDED",
        finished_at=_utc_now(),
        validation_status="passed",
        observed_cost=0.0,
    )
    registry.add_budget(run_id, 0.0, selected["model_ref"])

    envelope = make_result_envelope(
        run_id=run_id,
        caller=caller,
        role=role,
        selected_model=selected["model_ref"],
        task_class=task_class,
        workspace={
            "mode": workspace_mode,
            "base_commit": "unknown",
            "changed_paths": changed_paths,
        },
        validation=validation,
        usage={"cost_usd": 0.0, "cost_estimated": True, "latency_ms": latency_ms},
    )
    envelope["task_hash"] = canonical_task_hash(task)
    return envelope


def cancel_delegate_run(*, registry: Registry, run_id: str) -> dict[str, Any]:
    r = registry.get_delegate_run(run_id)
    if not r:
        raise ModelctlError(code="E_INTERNAL", message=f"run not found: {run_id}")
    if r["state"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
        return {"ok": True, "run_id": run_id, "state": r["state"], "idempotent": True}
    pid = r.get("process_pid")
    from ..runtime.vllm import kill_group, process_group_gone

    if pid:
        kill_group(pid)
        if not process_group_gone(pid):
            raise ModelctlError(
                code="E_DELEGATE_CANCEL_FAILED",
                message=f"could not terminate process group of run {run_id}",
                details={"pid": pid},
            )
    _mark_run(registry, run_id, "CANCELLED", finished_at=_utc_now())
    return {"ok": True, "run_id": run_id, "state": "CANCELLED", "pid": pid}


def retry_delegate_run(
    *, registry: Registry, config: dict[str, Any], run_id: str, trace_id: str | None = None
) -> dict[str, Any]:
    r = registry.get_delegate_run(run_id)
    if not r:
        raise ModelctlError(code="E_INTERNAL", message=f"run not found: {run_id}")
    if r["state"] != "FAILED":
        raise ModelctlError(
            code="E_DELEGATION_POLICY_DENIED", message="only failed runs can be retried"
        )
    if not r.get("task_json"):
        raise ModelctlError(
            code="E_INTERNAL", message=f"run {run_id} has no stored task payload; cannot retry"
        )
    task = json.loads(r["task_json"])
    role = task.get("role") or r["requested_bin"]
    return run_delegate_task(
        registry=registry,
        config=config,
        task=task,
        role=role,
        bin_=r["requested_bin"],
        trace_id=trace_id,
        requested_model=r["selected_model_ref"],
    )
