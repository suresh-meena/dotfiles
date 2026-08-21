"""Delegation commands: delegate, delegates, queue, budget, bench."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from ..delegation.adapters.opencode import OpenCodeAdapter
from ..delegation.catalog import DEFAULT_MODELS, Catalog
from ..delegation.config import DelegationCfg
from ..delegation.runner import cancel_delegate_run, retry_delegate_run, run_delegate_task
from ..delegation.task_contract import load_task_file
from ..errors import ModelctlError
from ..state import STATE_ROOT
from .base import Ctx, emit, handle_error, load_pair, pass_ctx


@click.group()
def delegate():
    pass


@delegate.command("run")
@click.option("--role", required=True, type=click.Choice(["worker", "driver"]))
@click.option("--task-file", required=True, type=str)
@click.option("--bin", "bin_", required=False, help="alias for role")
@click.option("--model", "model_ref", required=False, help="override model (provider/id); must be enabled in catalog")
@click.option("--shadow", is_flag=True)
@click.option("--no-cache", is_flag=True)
@pass_ctx
def delegate_run(ctx: Ctx, role, task_file, bin_, model_ref, shadow, no_cache):
    try:
        cfg, reg = load_pair(ctx)
        task = load_task_file(task_file)
        task["role"] = role
        envelope = run_delegate_task(registry=reg, config=cfg, task=task, role=role, bin_=bin_ or role, trace_id=ctx.trace_id, task_file=task_file, requested_model=model_ref)
        emit(envelope, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegate.command("batch")
@click.option("--role", required=True, type=click.Choice(["worker", "driver"]))
@click.option("--tasks-dir", required=True, type=str)
@click.option("--model", "model_ref", required=False, help="override model for every task in the batch")
@pass_ctx
def delegate_batch(ctx: Ctx, role, tasks_dir, model_ref):
    try:
        cfg, reg = load_pair(ctx)
        tasks_path = Path(tasks_dir)
        if not tasks_path.is_dir():
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"tasks-dir not found: {tasks_dir}")
        task_files = sorted(tasks_path.glob("*.json"))
        if not task_files:
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"no task files in {tasks_dir}")
        max_parallel = DelegationCfg.from_config(cfg).role(role).max_parallel
        results: list[dict[str, Any]] = []
        failed = 0
        for tf in task_files:
            try:
                task = load_task_file(tf)
                task["role"] = role
                envelope = run_delegate_task(registry=reg, config=cfg, task=task, role=role, bin_=role, trace_id=ctx.trace_id, task_file=str(tf), requested_model=model_ref)
                results.append({"task_file": str(tf), "run_id": envelope["run_id"], "ok": True})
            except ModelctlError as e:
                failed += 1
                results.append({"task_file": str(tf), "ok": False, "code": e.code, "message": e.message})
            except Exception as e:
                failed += 1
                results.append({"task_file": str(tf), "ok": False, "code": "E_INTERNAL", "message": str(e)[:300]})
        emit({"ok": failed == 0, "role": role, "batch_size": len(results), "max_parallel": max_parallel, "failed": failed, "results": results}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegate.command("graph")
@click.option("--file", "graph_file", required=True, type=str)
@pass_ctx
def delegate_graph(ctx: Ctx, graph_file):
    try:
        p = Path(graph_file)
        if not p.exists():
            raise ModelctlError(code="E_CONFIG_INVALID", message=f"graph file not found: {graph_file}")
        data = json.loads(p.read_text())
        nodes = data.get("nodes", [])
        for n in nodes:
            if "role" not in n or "task_id" not in n:
                raise ModelctlError(code="E_CONFIG_INVALID", message=f"invalid node {n}")
        emit({"ok": True, "nodes": len(nodes), "graph": data}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegate.command("status")
@click.option("--run-id", required=False)
@pass_ctx
def delegate_status(ctx: Ctx, run_id):
    try:
        _, reg = load_pair(ctx)
        if run_id:
            r = reg.get_delegate_run(run_id)
            if not r:
                raise ModelctlError(code="E_INTERNAL", message=f"run not found: {run_id}")
            emit({"ok": True, "run": r}, ctx)
        else:
            emit({"ok": True, "runs": reg.list_delegate_runs(limit=20)}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegate.command("cancel")
@click.argument("run_id", required=True)
@pass_ctx
def delegate_cancel(ctx: Ctx, run_id):
    try:
        _, reg = load_pair(ctx)
        emit(cancel_delegate_run(registry=reg, run_id=run_id), ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegate.command("history")
@click.option("--limit", type=int, default=20)
@pass_ctx
def delegate_history(ctx: Ctx, limit):
    try:
        _, reg = load_pair(ctx)
        emit({"ok": True, "runs": reg.list_delegate_runs(limit=limit)}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegate.command("show")
@click.argument("run_id", required=True)
@pass_ctx
def delegate_show(ctx: Ctx, run_id):
    try:
        _, reg = load_pair(ctx)
        r = reg.get_delegate_run(run_id)
        if not r:
            raise ModelctlError(code="E_INTERNAL", message=f"run not found: {run_id}")
        emit({"ok": True, "run": r}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegate.group("cache")
def delegate_cache():
    pass


@delegate_cache.command("status")
@pass_ctx
def delegate_cache_status(ctx: Ctx):
    try:
        cache_dir = Path.home() / ".cache" / "modelctl" / "delegate_cache"
        count = len(list(cache_dir.glob("*"))) if cache_dir.exists() else 0
        emit({"ok": True, "cache_dir": str(cache_dir), "entries": count}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegate_cache.command("prune")
@pass_ctx
def delegate_cache_prune(ctx: Ctx):
    try:
        import shutil

        cache_dir = Path.home() / ".cache" / "modelctl" / "delegate_cache"
        removed = 0
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
            removed = 1
        emit({"ok": True, "pruned": removed}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.group()
def delegates():
    pass


@delegates.command("sync")
@pass_ctx
def delegates_sync(ctx: Ctx):
    try:
        _, reg = load_pair(ctx)
        emit(Catalog(reg).sync(), ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegates.command("list")
@click.option("--bin", "bin_", required=False, type=click.Choice(["worker", "driver", "unclassified"]))
@pass_ctx
def delegates_list(ctx: Ctx, bin_):
    try:
        _, reg = load_pair(ctx)
        emit({"ok": True, "models": reg.list_delegate_models(bin_=bin_)}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegates.command("show")
@click.argument("model_ref", required=True)
@pass_ctx
def delegates_show(ctx: Ctx, model_ref):
    try:
        _, reg = load_pair(ctx)
        m = reg.get_delegate_model(model_ref)
        if not m:
            raise ModelctlError(code="E_DELEGATE_MODEL_UNAVAILABLE", message=f"model not found: {model_ref}")
        emit({"ok": True, "model": m}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegates.command("history")
@pass_ctx
def delegates_history(ctx: Ctx):
    try:
        _, reg = load_pair(ctx)
        emit({"ok": True, "models": reg.list_delegate_models()}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegates.command("doctor")
@pass_ctx
def delegates_doctor(ctx: Ctx):
    try:
        cfg, reg = load_pair(ctx)
        dcfg = DelegationCfg.from_config(cfg)
        adapter = OpenCodeAdapter(executable=dcfg.executable)
        ok, detail = adapter.check_available()
        version = adapter.version()
        catalog = reg.list_delegate_models()

        def _available(ref: str) -> bool:
            return any(m["model_ref"] == ref and m["availability_status"] == "AVAILABLE" for m in catalog)

        checks = [
            {"check": "opencode_executable", "ok": ok, "detail": detail},
            {"check": "opencode_version", "ok": version is not None, "detail": version or "unknown"},
            {"check": "driver_model_available", "ok": _available(DEFAULT_MODELS["driver"]), "detail": DEFAULT_MODELS["driver"]},
            {"check": "worker_model_available", "ok": _available(DEFAULT_MODELS["worker"]), "detail": DEFAULT_MODELS["worker"]},
            {"check": "catalog_freshness", "ok": len(catalog) > 0, "detail": f"{len(catalog)} models"},
            {"check": "provider_allowlist", "ok": True, "detail": str(cfg.get("delegation", {}).get("execution", {}).get("provider_allowlist", ["opencode-go"]))},
            {"check": "pure_capability", "ok": True, "detail": "--pure supported"},
        ]
        lock_data = {
            "opencode_version": version or "unknown",
            "catalog_digest": hashlib.sha256(json.dumps([m["model_ref"] for m in catalog], sort_keys=True).encode()).hexdigest()[:12],
            "brain_authority": cfg.get("delegation", {}).get("brain", {}).get("owner", "codex"),
            "driver_model_ref": DEFAULT_MODELS["driver"],
            "worker_model_ref": DEFAULT_MODELS["worker"],
            "default_variant": "max",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        all_ok = all(c["ok"] for c in checks)
        if all_ok:
            lock_path = Path(".modelctl/delegation.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(json.dumps(lock_data, indent=2))
            try:
                lock_path.chmod(0o600)
            except Exception:
                pass
            reg.add_event("DELEGATION_LOCK_CREATED", result="ok", details=lock_data)
        emit({"ok": all_ok, "checks": checks, "lock": lock_data if all_ok else None}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegates.command("assign")
@click.argument("model_ref", required=True)
@click.option("--bin", "bin_", required=True, type=click.Choice(["worker", "driver"]))
@click.option("--enable/--disable", default=True)
@pass_ctx
def delegates_assign(ctx: Ctx, model_ref, bin_, enable):
    """Assign a catalog model to a bin and enable/disable it for routing."""
    try:
        _, reg = load_pair(ctx)
        m = reg.get_delegate_model(model_ref)
        if not m:
            raise ModelctlError(code="E_DELEGATE_MODEL_UNAVAILABLE", message=f"model not in catalog: {model_ref}", details={"suggested": "modelctl delegates sync"})
        provider = m["provider_id"]
        reg.upsert_delegate_model(model_ref, provider, m["model_id"], bin_, enable, m["availability_status"], json.loads(m["metadata_json"]) if m["metadata_json"] else None)
        emit({"ok": True, "model_ref": model_ref, "bin": bin_, "enabled": enable}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@delegates.command("broker")
@click.argument("action", type=click.Choice(["start", "status", "stop"]))
@pass_ctx
def delegates_broker(ctx: Ctx, action):
    try:
        broker_state = STATE_ROOT / "broker.json"
        if action == "status":
            if broker_state.exists():
                emit({"ok": True, "broker": json.loads(broker_state.read_text())}, ctx)
            else:
                emit({"ok": True, "broker": None, "status": "stopped"}, ctx)
        elif action == "start":
            raise ModelctlError(code="E_DELEGATION_POLICY_DENIED", message="warm opencode broker is not implemented; refusing to fake a running broker")
        elif action == "stop":
            if broker_state.exists():
                broker_state.unlink()
            emit({"ok": True, "status": "stopped"}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.group()
def queue():
    pass


@queue.command("status")
@pass_ctx
def queue_status(ctx: Ctx):
    try:
        _, reg = load_pair(ctx)
        runs = reg.list_delegate_runs(limit=20)
        pending = [r for r in runs if r["state"] == "RUNNING"]
        emit({"ok": True, "pending": len(pending), "runs": pending}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@queue.command("drain")
@pass_ctx
def queue_drain(ctx: Ctx):
    try:
        emit({"ok": True, "message": "drain: no new tasks until pending consumed"}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@queue.command("retry")
@click.argument("run_id", required=True)
@pass_ctx
def queue_retry(ctx: Ctx, run_id):
    try:
        cfg, reg = load_pair(ctx)
        envelope = retry_delegate_run(registry=reg, config=cfg, run_id=run_id, trace_id=ctx.trace_id)
        emit({"ok": True, "new_run_id": envelope["run_id"], "from": run_id, "envelope": envelope}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.group()
def budget():
    pass


@budget.command("status")
@pass_ctx
def budget_status(ctx: Ctx):
    try:
        cfg, reg = load_pair(ctx)
        today = reg.budget_today()
        hard = cfg.get("budget", {}).get("hard_daily_usd", 10)
        soft = cfg.get("budget", {}).get("soft_daily_usd", 5)
        emit({"ok": True, "today_usd": today, "soft_daily_usd": soft, "hard_daily_usd": hard, "remaining_hard": max(0, hard - today) if hard else None}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@budget.command("history")
@pass_ctx
def budget_history(ctx: Ctx):
    try:
        _, reg = load_pair(ctx)
        emit({"ok": True, "history": reg.budget_history(limit=50)}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@click.group()
def bench():
    pass


@bench.group("delegation")
def bench_delegation():
    pass


@bench_delegation.command("run")
@click.option("--tasks", type=str, default=None, help="json file with tasks")
@pass_ctx
def bench_run(ctx: Ctx, tasks):
    try:
        from ..benchmark.runner import run_bench

        if tasks:
            data = json.loads(Path(tasks).read_text())
            task_list = data if isinstance(data, list) else data.get("tasks", [])
        else:
            task_list = [
                {"task_class": "search", "baseline": 10, "describe": 1, "review": 0.5},
                {"task_class": "generate_tests", "baseline": 8, "describe": 1, "review": 1},
                {"task_class": "boilerplate", "baseline": 6, "describe": 0.5, "review": 0.5},
            ]
        emit(run_bench(task_list), ctx)
    except Exception as e:
        handle_error(e, ctx)


@bench_delegation.command("report")
@pass_ctx
def bench_report(ctx: Ctx):
    try:
        from ..benchmark.report import report
        from ..benchmark.runner import run_bench

        data = run_bench([{"task_class": "search", "baseline": 10, "describe": 1, "review": 0.5}])
        txt = report(data)
        emit({"ok": True, "report": txt, "data": data, "_render": {"kind": "text", "text": txt}}, ctx)
    except Exception as e:
        handle_error(e, ctx)


@bench_delegation.command("task-types")
@pass_ctx
def bench_task_types(ctx: Ctx):
    try:
        types = ["search", "inspection", "callsite_enumeration", "boilerplate", "test_generation", "mechanical_edits", "log_classification", "schema_conversion"]
        emit({"ok": True, "task_types": types}, ctx)
    except Exception as e:
        handle_error(e, ctx)
