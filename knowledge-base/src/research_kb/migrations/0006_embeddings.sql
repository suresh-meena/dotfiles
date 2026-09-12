PRAGMA foreign_keys = ON;

CREATE TABLE embedding_cache (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  object_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  projection_hash TEXT NOT NULL,
  model_identifier TEXT NOT NULL,
  model_revision TEXT NOT NULL,
  dimensions INTEGER NOT NULL CHECK (dimensions > 0),
  normalization TEXT NOT NULL,
  chunker_version TEXT NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (
    project_id,
    object_id,
    revision,
    model_identifier,
    model_revision,
    dimensions,
    normalization,
    chunker_version
  ),
  FOREIGN KEY (project_id, object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE embedding_outbox (
  job_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  object_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  projection_hash TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'done', 'failed', 'dead_letter')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  provider TEXT NOT NULL,
  model_identifier TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_error TEXT,
  UNIQUE (project_id, object_id, revision, projection_hash)
) STRICT;

CREATE TABLE embedding_watermark (
  project_id TEXT PRIMARY KEY REFERENCES projects(project_id) ON DELETE RESTRICT,
  embedded_seq INTEGER NOT NULL DEFAULT 0 CHECK (embedded_seq >= 0),
  provider TEXT NOT NULL DEFAULT 'disabled',
  model_identifier TEXT NOT NULL DEFAULT 'none',
  updated_at TEXT NOT NULL
) STRICT;
