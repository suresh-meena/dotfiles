PRAGMA foreign_keys = ON;

CREATE TABLE work_leases (
  lease_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  object_id TEXT NOT NULL,
  owner TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'released', 'expired')),
  acquired_seq INTEGER NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT,
  released_seq INTEGER,
  release_reason TEXT,
  FOREIGN KEY (project_id, object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE UNIQUE INDEX work_leases_active_unique
ON work_leases(project_id, object_id)
WHERE state = 'active';

CREATE INDEX work_leases_owner ON work_leases(project_id, owner, state);
