"""Test harness: a real Store + Controller driven against the FakeExecutor."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from fleetq import auth
from fleetq.db.store import Store
from fleetq.engine import admission, fence, state
from fleetq.engine.controller import Controller, ControllerConfig
from fleetq.executors.fake import FakeExecutor, FakeNode
from fleetq.util import utcnow

DEFAULT_CAPACITY = {"ram_budget_mb": 64000, "cpus": 32, "scratch_budget_mb": 100000, "job_slots": 4,
                    "gpu_count": 2, "max_vram_mb": 24000}
DEFAULT_DEFAULTS = {"mem_mb": 4000, "cpus": 2, "time_s": 3600}


class Harness:
    def __init__(self, tmp: Path, nodes: list[FakeNode] | None = None, *, mode: str = "exclusive",
                 config: ControllerConfig | None = None) -> None:
        self.tmp = tmp
        self.store = Store(tmp / "fq.db")
        self.store.open()
        self.fake = FakeExecutor(nodes or [FakeNode("n1", gpus=["GPU-a", "GPU-b"])])
        self.mode = mode
        self.config = config or ControllerConfig(tick_s=0.01, observe_interval_s=0.0, refusal_backoff_s=0,
                                                 stage_backoff_s=0, retry_backoff_s=0, post_job_cooldown_s=0)
        self.token_id, self.token = self.store.run_sync(
            lambda c: auth.create_token(c, owner="suresh", kind="human", label="laptop"))
        self.agent_id, self.agent_token = self.store.run_sync(
            lambda c: auth.create_token(c, owner="suresh", kind="agent", label="laptop/claude"))
        for node in self.fake.nodes.values():
            self.add_node(node.name, gpus=node.gpus)
        self.controller: Controller | None = None
        self.alerts: list[tuple[str, dict]] = []

    def add_node(self, name: str, *, gpus: list[str], capacity: dict | None = None,
                 defaults: dict | None = None, mode: str | None = None, enabled: bool = True) -> None:
        cfg = {"capacity": {**DEFAULT_CAPACITY, "gpu_count": len(gpus), **(capacity or {})},
               "defaults": {**DEFAULT_DEFAULTS, **(defaults or {})},
               "in_place_roots": ["/data"]}

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO nodes (id, backend, mode, enabled, config_json, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json, enabled=excluded.enabled,"
                " mode=excluded.mode",
                (name, "fake", mode or self.mode, int(enabled), json.dumps(cfg), utcnow()))
            for i, uuid in enumerate(gpus):
                conn.execute("INSERT OR IGNORE INTO node_gpus (node_id, uuid, model, vram_total, idx) VALUES (?,?,?,?,?)",
                             (name, uuid, "RTX4090", 24000, i))
        self.store.run_sync(run)

    def principal(self, token: str | None = None) -> auth.Principal:
        return self.store.run_sync(lambda c: auth.verify(c, token or self.token, clock_ok=True))

    def submit(self, *, key: str, token: str | None = None, **spec: Any) -> dict[str, Any]:
        raw = {"name": spec.pop("name", "job"), "command": spec.pop("command", {"argv": ["python", "train.py"]}),
               "workdir": spec.pop("workdir", {"in_place": "/data/proj"}),
               "placement": spec.pop("placement", {"on": ["n1"]}),
               "resources": {"gpus": 1, **spec.pop("resources", {})}, **spec}
        principal = self.principal(token)
        return self.store.run_sync(lambda c: admission.admit(c, principal, raw, idempotency_key=key))

    def start(self, *, restored: bool = False) -> Controller:
        ident = self.store.run_sync(lambda c: fence.start_controller(c, restored_from_backup=restored))
        self.controller = Controller(self.store, {"fake": self.fake}, ident, config=self.config,
                                     notifier=lambda conn, kind, detail: self.alerts.append((kind, detail)),
                                     clock_ok=lambda: True)
        return self.controller

    async def ticks(self, n: int = 1) -> None:
        assert self.controller is not None
        for _ in range(n):
            await self.controller.tick()
            await self.controller.drain()

    def job(self, job_id: int) -> sqlite3.Row:
        return self.store.run_sync(lambda c: state.get_job(c, job_id))

    def attempts(self, job_id: int) -> list[sqlite3.Row]:
        return self.store.run_sync(lambda c: c.execute(
            "SELECT * FROM attempts WHERE job_id = ? ORDER BY n", (job_id,)).fetchall())

    def invariants(self) -> list[str]:
        return self.store.run_sync(state.invariant_violations)

    def open_gpu_reservations(self) -> list[str]:
        return self.store.run_sync(lambda c: [r[0] for r in c.execute(
            "SELECT gpu_uuid FROM resource_reservations WHERE kind='gpu' AND released_at IS NULL")])

    def close(self) -> None:
        self.store.close()


def run(coro):
    return asyncio.run(coro)
