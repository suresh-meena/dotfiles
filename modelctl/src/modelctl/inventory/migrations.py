from __future__ import annotations

import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS machines(
  machine_id TEXT PRIMARY KEY,
  last_seen_at TEXT,
  last_probe_status TEXT,
  ssh_fingerprint TEXT,
  gpu_summary_json TEXT
);

CREATE TABLE IF NOT EXISTS artifacts(
  artifact_id TEXT PRIMARY KEY,
  machine_id TEXT NOT NULL,
  model_alias TEXT,
  canonical_path TEXT NOT NULL,
  format TEXT,
  size_bytes INTEGER,
  manifest_fingerprint TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  current_status TEXT
);

CREATE TABLE IF NOT EXISTS artifact_observations(
  observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  exists_flag INTEGER NOT NULL,
  size_bytes INTEGER,
  manifest_fingerprint TEXT,
  probe_version TEXT,
  error_code TEXT,
  FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id)
);

CREATE TABLE IF NOT EXISTS deployments(
  deployment_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL,
  machine_id TEXT NOT NULL,
  config_digest TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  artifact_fingerprint TEXT,
  state TEXT NOT NULL,
  supervisor_unit TEXT,
  started_at TEXT,
  ready_at TEXT,
  stopped_at TEXT,
  server_pid INTEGER,
  port INTEGER,
  lease_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  event_type TEXT NOT NULL,
  target_id TEXT,
  deployment_id TEXT,
  machine_id TEXT,
  result TEXT,
  details_json TEXT
);

-- delegation catalog per §32
CREATE TABLE IF NOT EXISTS delegate_models(
  model_ref TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  bin TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  availability_status TEXT NOT NULL,
  metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS delegate_model_policy(
  model_ref TEXT PRIMARY KEY,
  max_data_class TEXT NOT NULL,
  training_policy TEXT,
  retention_policy TEXT,
  tool_profile TEXT NOT NULL,
  max_parallel INTEGER,
  operator_notes TEXT
);

CREATE TABLE IF NOT EXISTS delegate_runs(
  run_id TEXT PRIMARY KEY,
  parent_trace_id TEXT,
  caller TEXT NOT NULL,
  requested_bin TEXT NOT NULL,
  selected_model_ref TEXT NOT NULL,
  task_class TEXT NOT NULL,
  workspace_mode TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  state TEXT NOT NULL,
  observed_cost REAL,
  validation_status TEXT,
  escalation_parent_run_id TEXT,
  task_json TEXT,
  process_pid INTEGER
);

CREATE TABLE IF NOT EXISTS budget_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  run_id TEXT,
  cost_usd REAL NOT NULL,
  model_ref TEXT
);

-- locks/reservations are file-based on remote; local opportunistic cache
CREATE TABLE IF NOT EXISTS gpu_reservations(
  gpu_uuid TEXT PRIMARY KEY,
  machine_id TEXT NOT NULL,
  deployment_id TEXT,
  reserved_at TEXT,
  released_at TEXT
);

CREATE TABLE IF NOT EXISTS tunnels(
  tunnel_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL,
  machine_id TEXT NOT NULL,
  local_port INTEGER NOT NULL,
  remote_port INTEGER NOT NULL,
  pid INTEGER,
  created_at TEXT NOT NULL,
  state TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delegation_locks(
  lock_id TEXT PRIMARY KEY,
  digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  details_json TEXT
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_column(conn, "deployments", "server_pid", "INTEGER")
    _ensure_column(conn, "deployments", "port", "INTEGER")
    _ensure_column(conn, "deployments", "lease_expires_at", "TEXT")
    _ensure_column(conn, "delegate_runs", "task_json", "TEXT")
    _ensure_column(conn, "delegate_runs", "process_pid", "INTEGER")
    conn.commit()
