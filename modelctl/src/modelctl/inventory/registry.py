from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .migrations import migrate

DEFAULT_DB = Path.home() / ".local" / "state" / "modelctl" / "modelctl.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Registry:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # ensure mode 0700 for state dir, 0600 for file (best effort)
        try:
            self.db_path.parent.chmod(0o700)
        except Exception:
            pass
        self._conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        migrate(self._conn)
        # set file mode
        try:
            if self.db_path.exists():
                self.db_path.chmod(0o600)
        except Exception:
            pass

    def conn(self) -> sqlite3.Connection:
        return self._conn

    # machines
    def upsert_machine(self, machine_id: str, last_probe_status: str = "UNKNOWN", ssh_fingerprint: str | None = None, gpu_summary_json: str | None = None) -> None:
        now = utc_now()
        self._conn.execute(
            "INSERT INTO machines(machine_id, last_seen_at, last_probe_status, ssh_fingerprint, gpu_summary_json) VALUES(?,?,?,?,?) "
            "ON CONFLICT(machine_id) DO UPDATE SET last_seen_at=excluded.last_seen_at, last_probe_status=excluded.last_probe_status, ssh_fingerprint=COALESCE(excluded.ssh_fingerprint, machines.ssh_fingerprint), gpu_summary_json=COALESCE(excluded.gpu_summary_json, machines.gpu_summary_json)",
            (machine_id, now, last_probe_status, ssh_fingerprint, gpu_summary_json),
        )

    def list_machines(self) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM machines ORDER BY machine_id")
        return [dict(r) for r in cur.fetchall()]

    def get_machine(self, machine_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM machines WHERE machine_id=?", (machine_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    # artifacts
    def upsert_artifact(self, artifact_id: str, machine_id: str, model_alias: str | None, canonical_path: str, fmt: str | None, size_bytes: int | None, fingerprint: str | None, status: str) -> None:
        now = utc_now()
        # check existing
        cur = self._conn.execute("SELECT first_seen_at FROM artifacts WHERE artifact_id=?", (artifact_id,))
        row = cur.fetchone()
        first = row["first_seen_at"] if row else now
        self._conn.execute(
            "INSERT INTO artifacts(artifact_id, machine_id, model_alias, canonical_path, format, size_bytes, manifest_fingerprint, first_seen_at, last_seen_at, current_status) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(artifact_id) DO UPDATE SET model_alias=excluded.model_alias, canonical_path=excluded.canonical_path, format=excluded.format, size_bytes=excluded.size_bytes, manifest_fingerprint=excluded.manifest_fingerprint, last_seen_at=excluded.last_seen_at, current_status=excluded.current_status",
            (artifact_id, machine_id, model_alias, canonical_path, fmt, size_bytes, fingerprint, first, now, status),
        )

    def add_observation(self, artifact_id: str, exists_flag: int, size_bytes: int | None, fingerprint: str | None, probe_version: str | None = None, error_code: str | None = None) -> None:
        now = utc_now()
        self._conn.execute(
            "INSERT INTO artifact_observations(artifact_id, observed_at, exists_flag, size_bytes, manifest_fingerprint, probe_version, error_code) VALUES(?,?,?,?,?,?,?)",
            (artifact_id, now, exists_flag, size_bytes, fingerprint, probe_version, error_code),
        )

    def list_artifacts(self, machine: str | None = None, model: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM artifacts WHERE 1=1"
        params: list[Any] = []
        if machine:
            q += " AND machine_id=?"
            params.append(machine)
        if model:
            q += " AND model_alias=?"
            params.append(model)
        q += " ORDER BY machine_id, canonical_path"
        cur = self._conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def artifact_history(self, artifact_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM artifact_observations WHERE artifact_id=? ORDER BY observed_at DESC", (artifact_id,))
        return [dict(r) for r in cur.fetchall()]

    def stale_artifacts(self, max_age_s: int = 3600) -> list[dict[str, Any]]:
        # artifacts whose last_seen_at older than threshold or no observation
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_s
        cur = self._conn.execute("SELECT * FROM artifacts")
        out = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                ts = datetime.fromisoformat(d["last_seen_at"]).timestamp() if d["last_seen_at"] else 0
            except Exception:
                ts = 0
            if ts < cutoff:
                out.append(d)
        return out

    # deployments
    def upsert_deployment(self, deployment_id: str, target_id: str, machine_id: str, config_digest: str, artifact_id: str, fingerprint: str | None, state: str, unit: str, started_at: str | None = None, ready_at: str | None = None, stopped_at: str | None = None, server_pid: int | None = None, port: int | None = None, lease_expires_at: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO deployments(deployment_id, target_id, machine_id, config_digest, artifact_id, artifact_fingerprint, state, supervisor_unit, started_at, ready_at, stopped_at, server_pid, port, lease_expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(deployment_id) DO UPDATE SET state=excluded.state, ready_at=COALESCE(excluded.ready_at, deployments.ready_at), stopped_at=COALESCE(excluded.stopped_at, deployments.stopped_at), supervisor_unit=excluded.supervisor_unit, server_pid=excluded.server_pid, port=excluded.port, lease_expires_at=COALESCE(excluded.lease_expires_at, deployments.lease_expires_at)",
            (deployment_id, target_id, machine_id, config_digest, artifact_id, fingerprint, state, unit, started_at or utc_now(), ready_at, stopped_at, server_pid, port, lease_expires_at),
        )

    def get_deployment(self, deployment_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def deployment_for_target(self, target_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM deployments WHERE target_id=? ORDER BY started_at DESC LIMIT 1", (target_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_deployments(self, machine: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM deployments WHERE 1=1"
        params: list[Any] = []
        if machine:
            q += " AND machine_id=?"
            params.append(machine)
        q += " ORDER BY started_at DESC"
        cur = self._conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    # events
    def add_event(self, event_type: str, target_id: str | None = None, deployment_id: str | None = None, machine_id: str | None = None, result: str | None = None, details: dict[str, Any] | None = None) -> None:
        now = utc_now()
        details_json = json.dumps(details, sort_keys=True) if details else None
        self._conn.execute(
            "INSERT INTO events(timestamp, event_type, target_id, deployment_id, machine_id, result, details_json) VALUES(?,?,?,?,?,?,?)",
            (now, event_type, target_id, deployment_id, machine_id, result, details_json),
        )

    def list_events(self, machine: str | None = None, target: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if machine:
            q += " AND machine_id=?"
            params.append(machine)
        if target:
            q += " AND target_id=?"
            params.append(target)
        q += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        cur = self._conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    # delegate models
    def upsert_delegate_model(self, model_ref: str, provider_id: str, model_id: str, bin_: str, enabled: bool, status: str, metadata: dict[str, Any] | None = None) -> None:
        now = utc_now()
        cur = self._conn.execute("SELECT first_seen_at FROM delegate_models WHERE model_ref=?", (model_ref,))
        row = cur.fetchone()
        first = row["first_seen_at"] if row else now
        self._conn.execute(
            "INSERT INTO delegate_models(model_ref, provider_id, model_id, bin, enabled, first_seen_at, last_seen_at, availability_status, metadata_json) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(model_ref) DO UPDATE SET bin=excluded.bin, enabled=excluded.enabled, last_seen_at=excluded.last_seen_at, availability_status=excluded.availability_status, metadata_json=excluded.metadata_json",
            (model_ref, provider_id, model_id, bin_, 1 if enabled else 0, first, now, status, json.dumps(metadata) if metadata else None),
        )

    def list_delegate_models(self, bin_: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM delegate_models WHERE 1=1"
        params: list[Any] = []
        if bin_:
            q += " AND bin=?"
            params.append(bin_)
        q += " ORDER BY provider_id, model_id"
        cur = self._conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    def get_delegate_model(self, model_ref: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM delegate_models WHERE model_ref=?", (model_ref,))
        row = cur.fetchone()
        return dict(row) if row else None

    # delegate runs
    def insert_delegate_run(self, run_id: str, caller: str, requested_bin: str, selected_model_ref: str, task_class: str, workspace_mode: str, state: str, parent_trace_id: str | None = None, task_json: str | None = None, process_pid: int | None = None) -> None:
        now = utc_now()
        self._conn.execute(
            "INSERT INTO delegate_runs(run_id, parent_trace_id, caller, requested_bin, selected_model_ref, task_class, workspace_mode, started_at, state, task_json, process_pid) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, parent_trace_id, caller, requested_bin, selected_model_ref, task_class, workspace_mode, now, state, task_json, process_pid),
        )

    def update_delegate_run(self, run_id: str, state: str | None = None, finished_at: str | None = None, validation_status: str | None = None, observed_cost: float | None = None, process_pid: int | None = None) -> None:
        fields = []
        params: list[Any] = []
        if state:
            fields.append("state=?")
            params.append(state)
        if finished_at:
            fields.append("finished_at=?")
            params.append(finished_at)
        if validation_status:
            fields.append("validation_status=?")
            params.append(validation_status)
        if observed_cost is not None:
            fields.append("observed_cost=?")
            params.append(observed_cost)
        if process_pid is not None:
            fields.append("process_pid=?")
            params.append(process_pid)
        if not fields:
            return
        params.append(run_id)
        self._conn.execute(f"UPDATE delegate_runs SET {', '.join(fields)} WHERE run_id=?", params)

    def list_delegate_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM delegate_runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def get_delegate_run(self, run_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM delegate_runs WHERE run_id=?", (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    # budget
    def add_budget(self, run_id: str | None, cost_usd: float, model_ref: str | None = None) -> None:
        now = utc_now()
        self._conn.execute("INSERT INTO budget_ledger(timestamp, run_id, cost_usd, model_ref) VALUES(?,?,?,?)", (now, run_id, cost_usd, model_ref))

    def budget_today(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        cur = self._conn.execute("SELECT COALESCE(SUM(cost_usd),0) as s FROM budget_ledger WHERE timestamp >= ?", (today,))
        row = cur.fetchone()
        return float(row["s"]) if row else 0.0

    def budget_history(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM budget_ledger ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    # tunnels
    def upsert_tunnel(self, tunnel_id: str, target_id: str, machine_id: str, local_port: int, remote_port: int, pid: int | None, state: str) -> None:
        now = utc_now()
        self._conn.execute(
            "INSERT INTO tunnels(tunnel_id, target_id, machine_id, local_port, remote_port, pid, created_at, state) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tunnel_id) DO UPDATE SET pid=excluded.pid, state=excluded.state",
            (tunnel_id, target_id, machine_id, local_port, remote_port, pid, now, state),
        )

    def list_tunnels(self) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM tunnels ORDER BY created_at DESC")
        return [dict(r) for r in cur.fetchall()]

    def get_tunnel(self, tunnel_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM tunnels WHERE tunnel_id=?", (tunnel_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def delete_tunnel(self, tunnel_id: str) -> None:
        self._conn.execute("DELETE FROM tunnels WHERE tunnel_id=?", (tunnel_id,))
