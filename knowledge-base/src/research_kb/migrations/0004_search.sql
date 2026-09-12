PRAGMA foreign_keys = ON;

CREATE TABLE search_documents (
  doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  object_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  doc_role TEXT NOT NULL CHECK (doc_role IN ('record', 'section')),
  section_key TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  alias_text TEXT NOT NULL DEFAULT '',
  anchor_id TEXT,
  source_object_id TEXT,
  recorded_seq INTEGER NOT NULL,
  projection_version INTEGER NOT NULL,
  projection_hash TEXT NOT NULL,
  UNIQUE (project_id, object_id, revision, doc_role, section_key),
  FOREIGN KEY (project_id, object_id) REFERENCES objects(project_id, object_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX search_documents_object ON search_documents(project_id, object_id, revision);
CREATE INDEX search_documents_anchor ON search_documents(anchor_id);

CREATE VIRTUAL TABLE search_fts USING fts5(
  title,
  body,
  alias_text,
  content='search_documents',
  content_rowid='doc_id',
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER search_documents_ai
AFTER INSERT ON search_documents
BEGIN
  INSERT INTO search_fts(rowid, title, body, alias_text)
  VALUES (new.doc_id, new.title, new.body, new.alias_text);
END;

CREATE TRIGGER search_documents_ad
AFTER DELETE ON search_documents
BEGIN
  INSERT INTO search_fts(search_fts, rowid, title, body, alias_text)
  VALUES ('delete', old.doc_id, old.title, old.body, old.alias_text);
END;

CREATE TRIGGER search_documents_au
AFTER UPDATE ON search_documents
BEGIN
  INSERT INTO search_fts(search_fts, rowid, title, body, alias_text)
  VALUES ('delete', old.doc_id, old.title, old.body, old.alias_text);
  INSERT INTO search_fts(rowid, title, body, alias_text)
  VALUES (new.doc_id, new.title, new.body, new.alias_text);
END;
