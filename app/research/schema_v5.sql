CREATE TABLE IF NOT EXISTS research_telegram_analysis (
 revision_id TEXT PRIMARY KEY REFERENCES research_material_revision(revision_id) DEFERRABLE INITIALLY DEFERRED,
 status TEXT NOT NULL, updated_at BIGINT NOT NULL, error_code TEXT, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_telegram_analysis_status ON research_telegram_analysis(status,updated_at);
CREATE TABLE IF NOT EXISTS research_telegram_digest (
 id TEXT PRIMARY KEY, input_hash TEXT NOT NULL UNIQUE, created_at BIGINT NOT NULL,
 from_at BIGINT NOT NULL, to_at BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_telegram_digest_time ON research_telegram_digest(created_at,id);
