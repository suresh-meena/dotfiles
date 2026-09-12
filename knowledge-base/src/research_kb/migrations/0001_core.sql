PRAGMA foreign_keys = ON;

CREATE TABLE controller_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE TABLE projects (
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  namespace TEXT NOT NULL UNIQUE,
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  project_object_id TEXT
) STRICT;

CREATE TABLE actors (
  actor_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('human', 'agent', 'service', 'imported_source_author')),
  display_name TEXT NOT NULL,
  roles_json TEXT NOT NULL DEFAULT '[]',
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE commit_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
  request_id TEXT,
  action TEXT NOT NULL,
  reason TEXT,
  recorded_at TEXT NOT NULL,
  policy_revision TEXT,
  epoch TEXT NOT NULL,
  changed_json TEXT NOT NULL DEFAULT '[]'
) STRICT;

CREATE INDEX commit_events_project_seq ON commit_events(project_id, seq);

CREATE TABLE objects (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  object_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  current_revision INTEGER NOT NULL CHECK (current_revision > 0),
  record_state TEXT NOT NULL CHECK (record_state IN ('draft', 'active', 'retired', 'tombstoned')),
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  recorded_seq INTEGER NOT NULL,
  recorded_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY (project_id, object_id)
) STRICT;

CREATE INDEX objects_kind_state ON objects(project_id, kind, record_state);
CREATE INDEX objects_recorded_seq ON objects(project_id, recorded_seq);

CREATE TABLE revisions (
  project_id TEXT NOT NULL,
  object_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  kind TEXT NOT NULL,
  subkind TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  body_md TEXT NOT NULL DEFAULT '',
  record_state TEXT NOT NULL CHECK (record_state IN ('draft', 'active', 'retired', 'tombstoned')),
  state_json TEXT NOT NULL DEFAULT '{}',
  recorded_seq INTEGER NOT NULL,
  recorded_at TEXT NOT NULL,
  occurred_at TEXT,
  effective_from TEXT,
  effective_to TEXT,
  content_hash TEXT NOT NULL,
  commit_seq INTEGER NOT NULL REFERENCES commit_events(seq) ON DELETE RESTRICT,
  actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
  attribution_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (project_id, object_id, revision),
  FOREIGN KEY (project_id, object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX revisions_recorded_seq ON revisions(project_id, recorded_seq);
CREATE INDEX revisions_kind_subkind ON revisions(project_id, kind, subkind);

CREATE TRIGGER revisions_immutable_update
BEFORE UPDATE ON revisions
BEGIN
  SELECT RAISE(ABORT, 'revisions are immutable');
END;

CREATE TRIGGER revisions_immutable_delete
BEFORE DELETE ON revisions
BEGIN
  SELECT RAISE(ABORT, 'revisions are immutable');
END;

CREATE TABLE aliases (
  alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  namespace TEXT NOT NULL,
  alias_text TEXT NOT NULL,
  alias_norm TEXT NOT NULL,
  object_id TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  retired_seq INTEGER,
  UNIQUE (project_id, namespace, alias_norm, object_id),
  FOREIGN KEY (project_id, object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX aliases_lookup ON aliases(project_id, alias_norm);
CREATE INDEX aliases_namespace ON aliases(project_id, namespace, alias_norm);

CREATE TABLE link_revisions (
  project_id TEXT NOT NULL,
  object_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  predicate TEXT NOT NULL,
  src_project_id TEXT NOT NULL,
  src_object_id TEXT NOT NULL,
  src_revision INTEGER CHECK (src_revision IS NULL OR src_revision > 0),
  dst_project_id TEXT NOT NULL,
  dst_object_id TEXT NOT NULL,
  dst_revision INTEGER CHECK (dst_revision IS NULL OR dst_revision > 0),
  pin_mode TEXT NOT NULL CHECK (pin_mode IN ('tracking', 'pinned')),
  qualifiers_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (project_id, object_id, revision),
  FOREIGN KEY (project_id, object_id, revision) REFERENCES revisions(project_id, object_id, revision) ON DELETE RESTRICT,
  FOREIGN KEY (src_project_id, src_object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT,
  FOREIGN KEY (dst_project_id, dst_object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT,
  CHECK (src_project_id = project_id),
  CHECK (dst_project_id = project_id),
  CHECK (pin_mode = 'tracking' OR (src_revision IS NOT NULL AND dst_revision IS NOT NULL))
) STRICT;

CREATE INDEX link_src ON link_revisions(project_id, src_object_id, predicate);
CREATE INDEX link_dst ON link_revisions(project_id, dst_object_id, predicate);

CREATE TRIGGER link_revisions_immutable_update
BEFORE UPDATE ON link_revisions
BEGIN
  SELECT RAISE(ABORT, 'link revisions are immutable');
END;

CREATE TRIGGER link_revisions_immutable_delete
BEFORE DELETE ON link_revisions
BEGIN
  SELECT RAISE(ABORT, 'link revisions are immutable');
END;
