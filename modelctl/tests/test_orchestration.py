from __future__ import annotations

import threading
import time

import pytest

from modelctl.config.schema import validate_config
from modelctl.delegation.config import DelegationCfg
from modelctl.delegation.orchestrator import execute_graph, validate_graph
from modelctl.errors import ModelctlError


def _config(**orchestration):
    return {"delegation": {"orchestration": orchestration}}


def _envelope(task_id: str):
    return {
        "ok": True,
        "run_id": f"run-{task_id}",
        "role": "worker",
        "selected_model": "test/provider",
        "workspace": {"changed_paths": []},
        "validation": {"status": "passed"},
        "usage": {"latency_ms": 1},
    }


def test_graph_rejects_cycles_and_depth():
    cfg = DelegationCfg.from_config(_config(max_task_depth=1))
    with pytest.raises(ModelctlError) as exc:
        validate_graph(
            {
                "nodes": [
                    {"task_id": "a", "role": "worker", "depends_on": ["b"]},
                    {"task_id": "b", "role": "worker", "depends_on": ["a"]},
                ]
            },
            cfg,
        )
    assert "dependency cycle" in exc.value.message
    with pytest.raises(ModelctlError) as exc:
        validate_graph(
            {
                "nodes": [
                    {"task_id": "a", "role": "worker"},
                    {"task_id": "b", "role": "worker", "depends_on": ["a"]},
                    {"task_id": "c", "role": "worker", "depends_on": ["b"]},
                ]
            },
            cfg,
        )
    assert "max_task_depth" in exc.value.message


def test_graph_rejects_excessive_fanout():
    cfg = DelegationCfg.from_config(_config(max_task_fanout=1))
    with pytest.raises(ModelctlError) as exc:
        validate_graph(
            {
                "nodes": [
                    {"task_id": "root", "role": "worker"},
                    {"task_id": "one", "role": "worker", "depends_on": ["root"]},
                    {"task_id": "two", "role": "worker", "depends_on": ["root"]},
                ]
            },
            cfg,
        )
    assert "fan-out" in exc.value.message


def test_graph_runs_independent_workers_in_parallel_and_returns_sorted_results():
    lock = threading.Lock()
    active = 0
    maximum = 0

    def fake_run(**kwargs):
        nonlocal active, maximum
        task_id = kwargs["task"]["task_id"]
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return _envelope(task_id)

    graph = {
        "graph_id": "parallel-test",
        "nodes": [
            {"task_id": "c", "role": "worker", "task": {"task_id": "c"}},
            {"task_id": "a", "role": "worker", "task": {"task_id": "a"}},
            {"task_id": "b", "role": "worker", "task": {"task_id": "b"}},
        ],
    }
    result = execute_graph(
        registry=object(),  # type: ignore[arg-type]
        config=_config(max_total_parallel=2, worker_batch_size=2, fail_fast=True),
        graph=graph,
        trace_id="test",
        run_fn=fake_run,
    )
    assert result["ok"] is True
    assert result["max_parallel"] == 2
    assert maximum == 2
    assert [item["task_id"] for item in result["results"]] == ["a", "b", "c"]


def test_graph_skips_dependents_after_failure():
    calls: list[str] = []

    def fake_run(**kwargs):
        task_id = kwargs["task"]["task_id"]
        calls.append(task_id)
        if task_id == "bad":
            raise ModelctlError(code="E_DELEGATE_PROVIDER_FAILURE", message="temporary")
        return _envelope(task_id)

    result = execute_graph(
        registry=object(),  # type: ignore[arg-type]
        config=_config(max_total_parallel=2, fail_fast=False),
        graph={
            "nodes": [
                {"task_id": "bad", "role": "worker", "task": {"task_id": "bad"}},
                {"task_id": "child", "role": "worker", "depends_on": ["bad"]},
            ]
        },
        trace_id="test",
        run_fn=fake_run,
    )
    assert calls == ["bad"]
    assert result["failed"] == 1
    assert result["skipped"] == 1
    assert result["results"][1]["reason"] == "dependency_failed"


def test_shared_project_writes_are_serialized():
    lock = threading.Lock()
    active = 0
    maximum = 0

    def fake_run(**kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return _envelope(kwargs["task"]["task_id"])

    result = execute_graph(
        registry=object(),  # type: ignore[arg-type]
        config=_config(max_total_parallel=2, overlap_policy="serialize"),
        graph={
            "nodes": [
                {
                    "task_id": "one",
                    "role": "driver",
                    "allowed_write_paths": ["src/one.py"],
                },
                {
                    "task_id": "two",
                    "role": "driver",
                    "allowed_write_paths": ["src/two.py"],
                },
            ]
        },
        trace_id="test",
        run_fn=fake_run,
    )
    assert result["ok"] is True
    assert maximum == 1


def test_transient_retry_is_bounded():
    attempts = 0

    def fake_run(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelctlError(code="E_DELEGATE_PROVIDER_FAILURE", message="temporary")
        return _envelope(kwargs["task"]["task_id"])

    result = execute_graph(
        registry=object(),  # type: ignore[arg-type]
        config={
            **_config(max_total_parallel=1),
            "budget": {"max_retry_per_candidate": 1},
        },
        graph={"nodes": [{"task_id": "retry", "role": "worker"}]},
        trace_id="test",
        run_fn=fake_run,
    )
    assert result["ok"] is True
    assert attempts == 2
    assert result["results"][0]["attempts"] == 2


def test_delegation_policy_is_strict_and_retry_is_bounded():
    base = {"version": 1}
    invalid = {**base, "delegation": {"orchestration": {"unknown": True}}}
    with pytest.raises(ModelctlError) as exc:
        validate_config(invalid)
    assert exc.value.code == "E_CONFIG_UNKNOWN_FIELD"

    invalid = {**base, "budget": {"max_retry_per_candidate": -1}}
    with pytest.raises(ModelctlError) as exc:
        validate_config(invalid)
    assert exc.value.code == "E_CONFIG_INVALID"
