PRAGMA foreign_keys = ON;

CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  proposal_hash TEXT NOT NULL,
  operations_json TEXT NOT NULL,
  bound_versions_json TEXT NOT NULL DEFAULT '{}',
  policy_revision TEXT,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'stored' CHECK (status IN ('stored', 'applied', 'rejected', 'expired', 'superseded')),
  required_capabilities_json TEXT NOT NULL DEFAULT '[]',
  requires_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0, 1)),
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  applied_seq INTEGER,
  actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
  request_id TEXT,
  UNIQUE (project_id, proposal_hash)
) STRICT;

CREATE INDEX proposals_project_status ON proposals(project_id, status);
CREATE INDEX proposals_hash ON proposals(project_id, proposal_hash);

CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  proposal_hash TEXT NOT NULL,
  expected_versions_json TEXT NOT NULL DEFAULT '{}',
  policy_revision TEXT NOT NULL,
  approver_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
  capability TEXT NOT NULL,
  epoch TEXT NOT NULL,
  issued_seq INTEGER NOT NULL,
  issued_at TEXT NOT NULL,
  expires_at TEXT,
  consumed_seq INTEGER,
  token_hash TEXT NOT NULL UNIQUE
) STRICT;

CREATE INDEX approvals_proposal ON approvals(project_id, proposal_hash);

CREATE TABLE review_flags (
  flag_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  target_project_id TEXT NOT NULL,
  target_object_id TEXT NOT NULL,
  target_revision INTEGER,
  cause_project_id TEXT NOT NULL,
  cause_object_id TEXT NOT NULL,
  cause_revision INTEGER,
  predicate TEXT,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved', 'dismissed')),
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  acknowledged_by TEXT,
  acknowledged_seq INTEGER,
  rationale TEXT,
  review_ref_object_id TEXT,
  review_ref_revision INTEGER,
  FOREIGN KEY (target_project_id, target_object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT,
  FOREIGN KEY (cause_project_id, cause_object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX review_flags_target ON review_flags(project_id, target_object_id, status);
CREATE INDEX review_flags_status ON review_flags(project_id, status);

CREATE TABLE session_cursors (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  principal TEXT NOT NULL,
  session_id TEXT NOT NULL,
  acknowledged_seq INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged_seq >= 0),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (project_id, principal, session_id)
) STRICT;

CREATE TABLE idempotency_records (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
  request_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  commit_seq INTEGER,
  created_at TEXT NOT NULL,
  PRIMARY KEY (project_id, actor_id, request_id)
) STRICT;

CREATE TRIGGER idempotency_records_immutable_update
BEFORE UPDATE ON idempotency_records
BEGIN
  SELECT RAISE(ABORT, 'idempotency records are immutable');
END;

CREATE TABLE policy_revisions (
  policy_revision TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  profile_json TEXT NOT NULL,
  accepted_seq INTEGER NOT NULL,
  accepted_by TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
  accepted_at TEXT NOT NULL,
  note TEXT
) STRICT;

CREATE TRIGGER policy_revisions_immutable_update
BEFORE UPDATE ON policy_revisions
BEGIN
  SELECT RAISE(ABORT, 'policy revisions are immutable');
END;

CREATE TRIGGER policy_revisions_immutable_delete
BEFORE DELETE ON policy_revisions
BEGIN
  SELECT RAISE(ABORT, 'policy revisions are retained');
END;
