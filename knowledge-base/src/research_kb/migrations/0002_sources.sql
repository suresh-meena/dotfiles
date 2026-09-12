PRAGMA foreign_keys = ON;

CREATE TABLE blob_registry (
  blob_hash TEXT PRIMARY KEY CHECK (length(blob_hash) = 64),
  byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
  media_type TEXT,
  assurance TEXT NOT NULL CHECK (assurance IN ('content_sha256', 'manifest', 'metadata_only', 'unverified')),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE blob_locations (
  location_id INTEGER PRIMARY KEY AUTOINCREMENT,
  blob_hash TEXT NOT NULL REFERENCES blob_registry(blob_hash) ON DELETE RESTRICT,
  location TEXT NOT NULL,
  location_kind TEXT NOT NULL,
  availability TEXT NOT NULL CHECK (availability IN ('available', 'missing', 'unverified', 'restricted')),
  last_verified_at TEXT,
  UNIQUE (blob_hash, location)
) STRICT;

CREATE TABLE project_blobs (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  blob_hash TEXT NOT NULL REFERENCES blob_registry(blob_hash) ON DELETE RESTRICT,
  role TEXT NOT NULL,
  registered_seq INTEGER NOT NULL,
  PRIMARY KEY (project_id, blob_hash, role)
) STRICT;

CREATE TABLE source_extractions (
  extraction_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  source_object_id TEXT NOT NULL,
  source_revision INTEGER NOT NULL CHECK (source_revision > 0),
  original_blob_hash TEXT,
  extraction_blob_hash TEXT,
  parser_name TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  pipeline_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('extracted', 'metadata_only', 'unsupported', 'unavailable', 'needs_visual_check', 'ocr_uncertain')),
  omissions_json TEXT NOT NULL DEFAULT '[]',
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (project_id, source_object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX source_extractions_source ON source_extractions(project_id, source_object_id, source_revision);

CREATE TRIGGER source_extractions_immutable_update
BEFORE UPDATE ON source_extractions
BEGIN
  SELECT RAISE(ABORT, 'source extractions are immutable');
END;

CREATE TRIGGER source_extractions_immutable_delete
BEFORE DELETE ON source_extractions
BEGIN
  SELECT RAISE(ABORT, 'source extractions are immutable');
END;

CREATE TABLE source_anchors (
  anchor_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  source_object_id TEXT NOT NULL,
  source_revision INTEGER NOT NULL CHECK (source_revision > 0),
  extraction_id TEXT REFERENCES source_extractions(extraction_id) ON DELETE RESTRICT,
  anchor_kind TEXT NOT NULL,
  locator_json TEXT NOT NULL,
  coordinate_system TEXT NOT NULL,
  excerpt TEXT,
  excerpt_hash TEXT,
  status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'needs_visual_check', 'ocr_uncertain', 'unavailable')),
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (project_id, source_object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX source_anchors_source ON source_anchors(project_id, source_object_id, source_revision);

CREATE TRIGGER source_anchors_immutable_update
BEFORE UPDATE ON source_anchors
BEGIN
  SELECT RAISE(ABORT, 'source anchors are immutable');
END;

CREATE TRIGGER source_anchors_immutable_delete
BEFORE DELETE ON source_anchors
BEGIN
  SELECT RAISE(ABORT, 'source anchors are immutable');
END;

CREATE TABLE citations (
  citation_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  citing_object_id TEXT NOT NULL,
  citing_revision INTEGER NOT NULL CHECK (citing_revision > 0),
  anchor_id TEXT NOT NULL REFERENCES source_anchors(anchor_id) ON DELETE RESTRICT,
  role TEXT NOT NULL CHECK (role IN ('quote', 'paraphrase', 'evidence', 'formula', 'table_value', 'figure')),
  note TEXT,
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (project_id, citing_object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT,
  FOREIGN KEY (project_id, citing_object_id, citing_revision) REFERENCES revisions(project_id, object_id, revision) ON DELETE RESTRICT
) STRICT;

CREATE INDEX citations_citing ON citations(project_id, citing_object_id, citing_revision);
CREATE INDEX citations_anchor ON citations(anchor_id);

CREATE TRIGGER citations_immutable_update
BEFORE UPDATE ON citations
BEGIN
  SELECT RAISE(ABORT, 'citations are immutable');
END;

CREATE TRIGGER citations_immutable_delete
BEFORE DELETE ON citations
BEGIN
  SELECT RAISE(ABORT, 'citations are immutable');
END;

CREATE TABLE import_receipts (
  receipt_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
  connector_namespace TEXT NOT NULL,
  external_id TEXT NOT NULL,
  external_version TEXT NOT NULL,
  pipeline_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed', 'in_progress')),
  cursor_json TEXT NOT NULL DEFAULT '{}',
  outcomes_json TEXT NOT NULL DEFAULT '[]',
  failures_json TEXT NOT NULL DEFAULT '[]',
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (project_id, connector_namespace, external_id, external_version, pipeline_hash)
) STRICT;

CREATE INDEX import_receipts_lookup ON import_receipts(project_id, connector_namespace);

CREATE TRIGGER import_receipts_immutable_delete
BEFORE DELETE ON import_receipts
BEGIN
  SELECT RAISE(ABORT, 'import receipts are retained');
END;
