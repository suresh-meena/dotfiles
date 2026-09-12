PRAGMA foreign_keys = ON;

CREATE TABLE resources (
  resource_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  resource_kind TEXT NOT NULL,
  machine_id TEXT NOT NULL,
  display_name TEXT,
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  capacity INTEGER NOT NULL DEFAULT 1 CHECK (capacity >= 1),
  admin_state TEXT NOT NULL DEFAULT 'enabled' CHECK (admin_state IN ('enabled', 'draining', 'disabled', 'unknown')),
  limitations_json TEXT NOT NULL DEFAULT '[]',
  parent_resource_id TEXT REFERENCES resources(resource_id) ON DELETE RESTRICT,
  physical_identity TEXT,
  allocation_domain TEXT,
  created_at TEXT NOT NULL
) STRICT;

CREATE INDEX resources_machine ON resources(project_id, machine_id);

CREATE TABLE trial_slots (
  slot_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  study_object_id TEXT NOT NULL,
  study_revision INTEGER NOT NULL CHECK (study_revision > 0),
  trial_key TEXT NOT NULL,
  trial_key_format TEXT NOT NULL,
  replicate_identity TEXT,
  conditions_json TEXT NOT NULL,
  required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (project_id, study_object_id, study_revision, trial_key, replicate_identity),
  FOREIGN KEY (project_id, study_object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX trial_slots_study ON trial_slots(project_id, study_object_id, study_revision);

CREATE TABLE run_attempts (
  attempt_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  run_object_id TEXT NOT NULL,
  run_revision INTEGER NOT NULL CHECK (run_revision > 0),
  slot_id TEXT NOT NULL REFERENCES trial_slots(slot_id) ON DELETE RESTRICT,
  attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
  execution_id TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (slot_id, attempt_no),
  UNIQUE (project_id, execution_id),
  FOREIGN KEY (project_id, run_object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE TRIGGER run_attempts_immutable_update
BEFORE UPDATE ON run_attempts
BEGIN
  SELECT RAISE(ABORT, 'run attempts are immutable');
END;

CREATE TRIGGER run_attempts_immutable_delete
BEFORE DELETE ON run_attempts
BEGIN
  SELECT RAISE(ABORT, 'run attempts are immutable');
END;

CREATE TABLE runtime_observations (
  observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  observed_at TEXT NOT NULL,
  freshness_seq INTEGER,
  receipt_time TEXT NOT NULL,
  worker_clock TEXT,
  worker_boot_id TEXT
) STRICT;

CREATE INDEX runtime_observations_subject ON runtime_observations(project_id, subject_kind, subject_id);

CREATE TABLE resource_leases (
  lease_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  resource_id TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE RESTRICT,
  generation INTEGER NOT NULL CHECK (generation > 0),
  owner_execution_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'quarantined', 'released', 'expired')),
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_seq INTEGER,
  quarantine_reason TEXT,
  UNIQUE (resource_id, generation)
) STRICT;

CREATE INDEX resource_leases_active ON resource_leases(project_id, resource_id, state);
CREATE INDEX resource_leases_owner ON resource_leases(project_id, owner_execution_id);

CREATE TABLE dispatch_outbox (
  outbox_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  execution_id TEXT NOT NULL,
  intent_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'dispatched', 'acknowledged', 'failed', 'dead_letter')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  dispatched_at TEXT,
  acknowledged_at TEXT,
  last_error TEXT,
  UNIQUE (project_id, execution_id)
) STRICT;

CREATE TABLE executor_receipts (
  receipt_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  execution_id TEXT NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('accepted', 'rejected', 'running', 'terminal', 'ambiguous')),
  payload_json TEXT NOT NULL,
  external_ref TEXT,
  generation INTEGER,
  receipt_time TEXT NOT NULL,
  dedup_key TEXT NOT NULL,
  recorded_seq INTEGER NOT NULL,
  UNIQUE (project_id, dedup_key)
) STRICT;

CREATE INDEX executor_receipts_execution ON executor_receipts(project_id, execution_id);

CREATE TRIGGER executor_receipts_immutable_update
BEFORE UPDATE ON executor_receipts
BEGIN
  SELECT RAISE(ABORT, 'executor receipts are immutable');
END;
