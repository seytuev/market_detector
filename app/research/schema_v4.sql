CREATE TABLE IF NOT EXISTS research_object (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, current_revision_id TEXT NOT NULL,
 status TEXT NOT NULL, asset_uid TEXT, title TEXT NOT NULL,
 created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, due_at BIGINT, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_object_list ON research_object(kind,status,updated_at,id);
CREATE INDEX IF NOT EXISTS research_object_asset ON research_object(kind,asset_uid,updated_at,id);
CREATE INDEX IF NOT EXISTS research_object_due ON research_object(kind,due_at,status);
CREATE TABLE IF NOT EXISTS research_revision (
 id TEXT PRIMARY KEY, object_id TEXT NOT NULL REFERENCES research_object(id) DEFERRABLE INITIALLY DEFERRED,
 parent_id TEXT, available_at BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_revision_time ON research_revision(object_id,available_at,id);
CREATE TABLE IF NOT EXISTS research_note (
 id TEXT PRIMARY KEY, object_id TEXT NOT NULL, kind TEXT NOT NULL,
 revision_id TEXT, available_at BIGINT NOT NULL, dedup_key TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(object_id,kind,dedup_key)
);
CREATE INDEX IF NOT EXISTS research_note_list ON research_note(object_id,kind,available_at,id);
CREATE TABLE IF NOT EXISTS research_dataset (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at BIGINT NOT NULL, cutoff_at BIGINT NOT NULL,
 content_hash TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(kind,content_hash)
);
CREATE TABLE IF NOT EXISTS research_result (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, object_id TEXT, dataset_id TEXT,
 status TEXT NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
 content_hash TEXT, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_result_list ON research_result(kind,object_id,created_at,id);
CREATE TABLE IF NOT EXISTS research_series (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, object_id TEXT NOT NULL, at_ms BIGINT NOT NULL,
 available_at BIGINT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(kind,object_id,at_ms,content_hash)
);
CREATE INDEX IF NOT EXISTS research_series_time ON research_series(kind,object_id,at_ms,id);
CREATE TABLE IF NOT EXISTS research_model_usage (
 id TEXT PRIMARY KEY, at_ms BIGINT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
 reserved_usd TEXT NOT NULL, actual_usd TEXT, request_id TEXT, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_model_usage_day ON research_model_usage(at_ms,status);
