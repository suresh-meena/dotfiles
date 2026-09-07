"""Bounded, dependency-aware orchestration for delegated tasks.

The scheduler is intentionally code-driven.  Models may do the work inside a
node, but they do not get to choose graph edges, bypass concurrency limits, or
turn a failed prerequisite into a successful downstream result.
"""

from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from ..errors import ModelctlError
from ..inventory.registry import Registry
from .config import DelegationCfg
from .runner import run_delegate_task


_RETRYABLE_CODES = {
    "E_DELEGATE_TIMEOUT",
    "E_DELEGATE_PROVIDER_FAILURE",
    "E_DELEGATE_RATE_LIMITED",
}


def _invalid(message: str) -> ModelctlError:
    return ModelctlError(code="E_CONFIG_INVALID", message=message)


def _task_for_node(node: dict[str, Any]) -> dict[str, Any]:
    nested = node.get("task")
    if nested is not None:
        if not isinstance(nested, dict):
            raise _invalid(f"node {node.get('task_id', '<unknown>')} task must be an object")
        task = dict(nested)
    else:
        task = {
            k: v for k, v in node.items() if k not in {"task_id", "depends_on", "task_file", "role"}
        }
    task["role"] = node["role"]
    task["task_id"] = node["task_id"]
    if node.get("model_ref") and not task.get("model_ref"):
        task["model_ref"] = node["model_ref"]
    return task


def _normalize_node(raw: Any, normalized: dict[str, dict[str, Any]]) -> None:
    if not isinstance(raw, dict):
        raise _invalid("every graph node must be an object")
    task_id = raw.get("task_id")
    role = raw.get("role")
    if not isinstance(task_id, str) or not task_id.strip():
        raise _invalid("every graph node requires a non-empty task_id")
    if task_id in normalized:
        raise _invalid(f"duplicate graph task_id: {task_id}")
    if role not in ("worker", "driver"):
        raise _invalid(f"node {task_id} role must be worker or driver")
    depends_on = raw.get("depends_on", [])
    if not isinstance(depends_on, list) or any(not isinstance(dep, str) for dep in depends_on):
        raise _invalid(f"node {task_id}.depends_on must be a list of task ids")
    if task_id in depends_on:
        raise _invalid(f"node {task_id} cannot depend on itself")
    node = dict(raw)
    node["task_id"] = task_id
    node["depends_on"] = list(dict.fromkeys(depends_on))
    node["task"] = _task_for_node(node)
    normalized[task_id] = node


def _graph_edges(
    normalized: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    children: dict[str, list[str]] = {task_id: [] for task_id in normalized}
    indegree = {task_id: 0 for task_id in normalized}
    for task_id, node in normalized.items():
        for dependency in node["depends_on"]:
            if dependency not in normalized:
                raise _invalid(f"node {task_id} depends on unknown task: {dependency}")
            children[dependency].append(task_id)
            indegree[task_id] += 1
    return children, indegree


def _topological_depth(children: dict[str, list[str]], indegree: dict[str, int]) -> dict[str, int]:
    ready = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
    depth = {task_id: 0 for task_id in ready}
    visited: list[str] = []
    while ready:
        task_id = ready.pop(0)
        visited.append(task_id)
        for child in sorted(children[task_id]):
            depth[child] = max(depth.get(child, 0), depth[task_id] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(visited) != len(indegree):
        raise _invalid("graph contains a dependency cycle")
    return depth


def validate_graph(
    graph: dict[str, Any], orchestration: DelegationCfg, *, max_task_fanout: int | None = None
) -> dict[str, dict[str, Any]]:
    """Validate and normalize a graph before starting any delegate process."""
    fanout_limit = (
        max_task_fanout
        if max_task_fanout is not None
        else orchestration.orchestration.max_task_fanout
    )
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise _invalid("graph.nodes must be a non-empty list")

    normalized: dict[str, dict[str, Any]] = {}
    for raw in nodes:
        _normalize_node(raw, normalized)
    for node in normalized.values():
        role_workspace = orchestration.role(node["role"]).workspace
        if role_workspace == "isolated_worktree" and node["task"].get("isolated") is not True:
            node["task"]["isolated"] = True

    children, indegree = _graph_edges(normalized)

    for task_id, dependents in children.items():
        if len(dependents) > fanout_limit:
            raise _invalid(
                f"node {task_id} has fan-out {len(dependents)}, exceeding "
                f"max_task_fanout={fanout_limit}"
            )

    # Kahn's algorithm gives both cycle detection and deterministic depth.
    depth = _topological_depth(children, indegree)
    too_deep = [
        task_id
        for task_id, value in depth.items()
        if value > orchestration.orchestration.max_task_depth
    ]
    if too_deep:
        raise _invalid(
            f"graph depth exceeds max_task_depth={orchestration.orchestration.max_task_depth}: "
            f"{', '.join(sorted(too_deep))}"
        )
    for task_id, value in depth.items():
        normalized[task_id]["depth"] = value
    return normalized


def _path_overlap(left: set[str], right: set[str]) -> bool:
    for a in left:
        for b in right:
            if a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/"):
                return True
    return False


def _write_info(task: dict[str, Any]) -> tuple[bool, set[str]]:
    """Return whether a task can mutate its shared workspace and its paths."""
    if task.get("isolated") is True:
        return False, set()
    profile = task.get("agent_profile")
    writes = (
        bool(task.get("allowed_write_paths"))
        or task.get("role") == "driver"
        or profile
        in {
            "modelctl-worker-edit",
            "modelctl-driver",
        }
    )
    paths = {
        Path(str(path)).as_posix().rstrip("/")
        for path in (task.get("allowed_write_paths") or [])
        if str(path).strip()
    }
    return writes, paths


def _has_conflict(
    task: dict[str, Any],
    active: list[dict[str, Any]],
    policy: str,
) -> bool:
    writes, paths = _write_info(task)
    if not writes:
        if policy == "serialize":
            return any(item["writes"] for item in active)
        return False
    for item in active:
        if not item["writes"]:
            if policy == "serialize":
                return True
            continue
        if policy == "serialize":
            return True
        # An unknown write set is conservatively treated as overlapping.
        if not paths or not item["paths"] or _path_overlap(paths, item["paths"]):
            return True
    return False


def _failure(exc: Exception, attempts: int) -> dict[str, Any]:
    if isinstance(exc, ModelctlError):
        return {
            "status": "failed",
            "ok": False,
            "code": exc.code,
            "message": exc.message[:300],
            "retryable": exc.code in _RETRYABLE_CODES,
            "attempts": attempts,
        }
    return {
        "status": "failed",
        "ok": False,
        "code": "E_INTERNAL",
        "message": str(exc)[:300] or "internal error",
        "retryable": False,
        "attempts": attempts,
    }


def _compact_success(envelope: dict[str, Any], attempts: int) -> dict[str, Any]:
    workspace = envelope.get("workspace", {})
    validation = envelope.get("validation", {})
    usage = envelope.get("usage", {})
    return {
        "status": "succeeded",
        "ok": True,
        "run_id": envelope.get("run_id"),
        "role": envelope.get("role"),
        "selected_model": envelope.get("selected_model"),
        "changed_paths": list(workspace.get("changed_paths", [])),
        "validation": validation.get("status", "unknown"),
        "latency_ms": usage.get("latency_ms"),
        "attempts": attempts,
    }


def _run_node(
    *,
    registry: Registry,
    config: dict[str, Any],
    node: dict[str, Any],
    requested_model: str | None,
    trace_id: str,
    caller: str,
    run_fn: Callable[..., dict[str, Any]],
    retry_limit: int,
) -> dict[str, Any]:
    task = dict(node["task"])
    task_id = node["task_id"]
    for attempt in range(1, retry_limit + 2):
        try:
            envelope = run_fn(
                registry=registry,
                config=config,
                task=task,
                role=node["role"],
                bin_=node["role"],
                trace_id=trace_id,
                task_file=node.get("task_file"),
                caller=caller,
                requested_model=requested_model,
            )
            result = _compact_success(envelope, attempt)
            result["task_id"] = task_id
            result["role"] = node["role"]
            if node.get("task_file"):
                result["task_file"] = node["task_file"]
            return result
        except Exception as exc:
            if (
                isinstance(exc, ModelctlError)
                and exc.code in _RETRYABLE_CODES
                and attempt <= retry_limit
            ):
                continue
            result = _failure(exc, attempt)
            result["task_id"] = task_id
            result["role"] = node["role"]
            if node.get("task_file"):
                result["task_file"] = node["task_file"]
            return result
    raise AssertionError("retry loop must return")


def _skip_failed_dependencies(
    pending: set[str], nodes: dict[str, dict[str, Any]], completed: dict[str, dict[str, Any]]
) -> None:
    for task_id in sorted(tuple(pending)):
        dependencies = nodes[task_id]["depends_on"]
        if not any(
            dependency in completed and completed[dependency]["status"] != "succeeded"
            for dependency in dependencies
        ):
            continue
        completed[task_id] = {
            "task_id": task_id,
            "role": nodes[task_id]["role"],
            "status": "skipped",
            "ok": False,
            "reason": "dependency_failed",
        }
        pending.remove(task_id)


def _skip_pending(
    pending: set[str],
    nodes: dict[str, dict[str, Any]],
    completed: dict[str, dict[str, Any]],
    reason: str,
) -> None:
    for task_id in sorted(tuple(pending)):
        completed[task_id] = {
            "task_id": task_id,
            "role": nodes[task_id]["role"],
            "status": "skipped",
            "ok": False,
            "reason": reason,
        }
        pending.remove(task_id)


def _dispatch_ready(
    *,
    pending: set[str],
    nodes: dict[str, dict[str, Any]],
    completed: dict[str, dict[str, Any]],
    running: dict[Future[dict[str, Any]], str],
    active: dict[str, dict[str, Any]],
    executor: ThreadPoolExecutor,
    total_limit: int,
    worker_limit: int,
    driver_limit: int,
    worker_batch_size: int,
    overlap_policy: str,
    registry: Registry,
    config: dict[str, Any],
    requested_model: str | None,
    trace_id: str,
    caller: str,
    run_fn: Callable[..., dict[str, Any]],
    retry_limit: int,
) -> None:
    role_counts = {"worker": 0, "driver": 0}
    for item in active.values():
        role_counts[item["role"]] += 1
    worker_dispatches = 0
    for task_id in sorted(tuple(pending)):
        if len(running) >= total_limit:
            break
        node = nodes[task_id]
        if not all(
            dependency in completed and completed[dependency]["status"] == "succeeded"
            for dependency in node["depends_on"]
        ):
            continue
        role = node["role"]
        role_limit = worker_limit if role == "worker" else driver_limit
        if role_counts[role] >= role_limit:
            continue
        if role == "worker" and worker_dispatches >= worker_batch_size:
            continue
        task = node["task"]
        if _has_conflict(task, list(active.values()), overlap_policy):
            continue
        future = executor.submit(
            _run_node,
            registry=registry,
            config=config,
            node=node,
            requested_model=requested_model,
            trace_id=trace_id,
            caller=caller,
            run_fn=run_fn,
            retry_limit=retry_limit,
        )
        running[future] = task_id
        writes, paths = _write_info(task)
        active[task_id] = {"role": role, "writes": writes, "paths": paths}
        role_counts[role] += 1
        if role == "worker":
            worker_dispatches += 1
        pending.remove(task_id)


def _collect_done(
    running: dict[Future[dict[str, Any]], str],
    active: dict[str, dict[str, Any]],
    completed: dict[str, dict[str, Any]],
    fail_fast: bool,
) -> bool:
    done, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
    aborted = False
    for future in sorted(done, key=lambda item: running[item]):
        task_id = running.pop(future)
        active.pop(task_id, None)
        try:
            result = future.result()
        except Exception as exc:  # defensive: _run_node normally catches all task errors
            result = _failure(exc, 1)
            result["task_id"] = task_id
        completed[task_id] = result
        if fail_fast and result["status"] == "failed":
            aborted = True
    return aborted


def execute_graph(
    *,
    registry: Registry,
    config: dict[str, Any],
    graph: dict[str, Any],
    requested_model: str | None = None,
    trace_id: str = "",
    caller: str = "codex",
    run_fn: Callable[..., dict[str, Any]] = run_delegate_task,
) -> dict[str, Any]:
    """Execute a validated DAG and return compact, deterministic results."""
    dcfg = DelegationCfg.from_config(config)
    configured_fanout = dcfg.orchestration.max_task_fanout
    budget_fanout = config.get("budget", {}).get("max_task_fanout")
    fanout_limit = (
        min(configured_fanout, int(budget_fanout))
        if budget_fanout is not None
        else configured_fanout
    )
    nodes = validate_graph(graph, dcfg, max_task_fanout=fanout_limit)
    total_limit, _ = dcfg.limits(config, "worker")
    worker_limit = dcfg.limits(config, "worker")[1]
    driver_limit = dcfg.limits(config, "driver")[1]
    retry_limit = max(0, int(config.get("budget", {}).get("max_retry_per_candidate", 0)))
    fail_fast = dcfg.orchestration.fail_fast
    overlap_policy = dcfg.orchestration.overlap_policy

    pending = set(nodes)
    running: dict[Future[dict[str, Any]], str] = {}
    active: dict[str, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    executor = ThreadPoolExecutor(max_workers=total_limit, thread_name_prefix="modelctl-delegate")
    aborted = False
    try:
        while pending or running:
            # First, deterministically skip nodes whose prerequisites failed.
            _skip_failed_dependencies(pending, nodes, completed)

            if aborted:
                _skip_pending(pending, nodes, completed, "fail_fast")

            _dispatch_ready(
                pending=pending,
                nodes=nodes,
                completed=completed,
                running=running,
                active=active,
                executor=executor,
                total_limit=total_limit,
                worker_limit=worker_limit,
                driver_limit=driver_limit,
                worker_batch_size=dcfg.orchestration.worker_batch_size,
                overlap_policy=overlap_policy,
                registry=registry,
                config=config,
                requested_model=requested_model,
                trace_id=trace_id,
                caller=caller,
                run_fn=run_fn,
                retry_limit=retry_limit,
            )

            if not running:
                if pending:
                    raise _invalid(
                        "graph cannot make progress under the configured concurrency policy"
                    )
                break

            if _collect_done(running, active, completed, fail_fast):
                aborted = True
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    ordered = [completed[task_id] for task_id in sorted(completed)]
    failed = sum(result["status"] == "failed" for result in ordered)
    skipped = sum(result["status"] == "skipped" for result in ordered)
    return {
        "ok": failed == 0 and skipped == 0,
        "graph_id": graph.get("graph_id"),
        "nodes": len(nodes),
        "completed": len(nodes) - failed - skipped,
        "failed": failed,
        "skipped": skipped,
        "max_parallel": total_limit,
        "overlap_policy": overlap_policy,
        "results": ordered,
        "graph_hash": __import__("hashlib")
        .sha256(json.dumps(graph, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest(),
    }


def batch_graph(tasks: list[tuple[str, dict[str, Any]]], role: str) -> dict[str, Any]:
    """Convert a batch into a graph with deterministic task ids."""
    nodes = []
    for task_file, task in tasks:
        task_id = Path(task_file).stem
        nodes.append({"task_id": task_id, "role": role, "task_file": task_file, "task": task})
    return {"graph_id": f"batch-{role}", "nodes": nodes}
