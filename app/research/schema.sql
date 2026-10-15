CREATE TABLE IF NOT EXISTS research_schema_version(version BIGINT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS research_asset (
    asset_uid TEXT PRIMARY KEY, asset_class TEXT NOT NULL, display_symbol TEXT NOT NULL,
    name TEXT NOT NULL, status TEXT NOT NULL, created_at BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_asset_symbol ON research_asset(display_symbol);
CREATE TABLE IF NOT EXISTS research_asset_external_id (
    namespace TEXT NOT NULL, external_id TEXT NOT NULL, asset_uid TEXT NOT NULL REFERENCES research_asset(asset_uid) DEFERRABLE INITIALLY DEFERRED,
    available_at BIGINT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,external_id)
);
CREATE TABLE IF NOT EXISTS research_instrument_asset (
    revision_id TEXT PRIMARY KEY, instrument_id BIGINT NOT NULL REFERENCES instrument(id) DEFERRABLE INITIALLY DEFERRED,
    asset_uid TEXT REFERENCES research_asset(asset_uid) DEFERRABLE INITIALLY DEFERRED,
    available_at BIGINT NOT NULL, valid_from BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_link_time ON research_instrument_asset(instrument_id,available_at,revision_id);
CREATE TABLE IF NOT EXISTS research_identity_candidate (
    id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, status TEXT NOT NULL, available_at BIGINT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_material (
    id TEXT PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL,
    current_revision_id TEXT NOT NULL, lifecycle TEXT NOT NULL, published_at BIGINT,
    first_available_at BIGINT NOT NULL, UNIQUE(provider,external_id)
);
CREATE TABLE IF NOT EXISTS research_material_revision (
    revision_id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES research_material(id) DEFERRABLE INITIALLY DEFERRED,
    available_at BIGINT NOT NULL, change_seq BIGINT NOT NULL, published_at BIGINT,
    source_kind TEXT NOT NULL, lifecycle TEXT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_material_revision_time ON research_material_revision(material_id,available_at,revision_id);
CREATE INDEX IF NOT EXISTS research_material_published ON research_material(published_at,id);
CREATE TABLE IF NOT EXISTS research_material_asset (
    revision_id TEXT NOT NULL REFERENCES research_material_revision(revision_id) DEFERRABLE INITIALLY DEFERRED,
    asset_uid TEXT NOT NULL REFERENCES research_asset(asset_uid) DEFERRABLE INITIALLY DEFERRED,
    role TEXT NOT NULL, resolution TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(revision_id,asset_uid,role)
);
CREATE INDEX IF NOT EXISTS research_material_asset_lookup ON research_material_asset(asset_uid,revision_id);
CREATE TABLE IF NOT EXISTS research_analysis (
    id TEXT PRIMARY KEY, material_revision_id TEXT NOT NULL REFERENCES research_material_revision(revision_id) DEFERRABLE INITIALLY DEFERRED,
    available_at BIGINT NOT NULL, change_seq BIGINT NOT NULL DEFAULT 0, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_analysis_material ON research_analysis(material_revision_id,available_at,id);
CREATE TABLE IF NOT EXISTS research_story (
    id TEXT PRIMARY KEY, asset_uid TEXT NOT NULL REFERENCES research_asset(asset_uid) DEFERRABLE INITIALLY DEFERRED,
    topic_key TEXT NOT NULL, stage TEXT, available_at BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_story_member (
    story_id TEXT NOT NULL REFERENCES research_story(id) DEFERRABLE INITIALLY DEFERRED,
    revision_id TEXT NOT NULL REFERENCES research_material_revision(revision_id) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(story_id,revision_id)
);
CREATE TABLE IF NOT EXISTS research_context_snapshot (
    id TEXT PRIMARY KEY, asset_uid TEXT NOT NULL REFERENCES research_asset(asset_uid) DEFERRABLE INITIALLY DEFERRED,
    as_of BIGINT NOT NULL, generated_at BIGINT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_context_asset ON research_context_snapshot(asset_uid,as_of,id);
CREATE TABLE IF NOT EXISTS research_summary (
    id TEXT PRIMARY KEY, asset_uid TEXT NOT NULL REFERENCES research_asset(asset_uid) DEFERRABLE INITIALLY DEFERRED,
    available_at BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_summary_asset ON research_summary(asset_uid,available_at,id);
CREATE TABLE IF NOT EXISTS research_source_state (
    id TEXT PRIMARY KEY, updated_at BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_import_receipt (
    provider TEXT NOT NULL, dataset_id TEXT NOT NULL, entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL, revision_id TEXT NOT NULL, change_seq BIGINT NOT NULL,
    available_at BIGINT NOT NULL, PRIMARY KEY(provider,dataset_id,entity_kind,entity_id,revision_id)
);
CREATE INDEX IF NOT EXISTS research_receipt_sequence ON research_import_receipt(provider,dataset_id,entity_kind,entity_id,change_seq);
CREATE TABLE IF NOT EXISTS research_quarantine (
    id TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset_id TEXT NOT NULL, entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL, revision_id TEXT NOT NULL, reason TEXT NOT NULL,
    available_at BIGINT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_job (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, dedup_key TEXT NOT NULL, status TEXT NOT NULL,
    attempts BIGINT NOT NULL DEFAULT 0, next_attempt_at BIGINT NOT NULL, lease_owner TEXT,
    lease_until BIGINT NOT NULL DEFAULT 0, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
    progress BIGINT NOT NULL DEFAULT 0, error_code TEXT, payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS research_job_active ON research_job(kind,dedup_key) WHERE status IN ('pending','running');
CREATE INDEX IF NOT EXISTS research_job_due ON research_job(status,next_attempt_at,lease_until);
CREATE TABLE IF NOT EXISTS research_idempotency (
    operation TEXT NOT NULL, key TEXT NOT NULL, body_hash TEXT NOT NULL,
    result TEXT NOT NULL, created_at BIGINT NOT NULL, PRIMARY KEY(operation,key)
);
CREATE TABLE IF NOT EXISTS research_audit (
    id TEXT PRIMARY KEY, object_kind TEXT NOT NULL, object_id TEXT NOT NULL,
    actor TEXT NOT NULL, at_ms BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_audit_object ON research_audit(object_kind,object_id,at_ms,id);
CREATE TABLE IF NOT EXISTS research_catalyst (
    id TEXT PRIMARY KEY, current_revision_id TEXT NOT NULL, created_at BIGINT NOT NULL,
    review_status TEXT NOT NULL, archived_at BIGINT
);
CREATE TABLE IF NOT EXISTS research_catalyst_revision (
    revision_id TEXT PRIMARY KEY, catalyst_id TEXT NOT NULL REFERENCES research_catalyst(id) DEFERRABLE INITIALLY DEFERRED,
    available_at BIGINT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
    stage TEXT NOT NULL, precision TEXT NOT NULL, starts_at BIGINT, ends_at BIGINT,
    review_status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_catalyst_time ON research_catalyst_revision(catalyst_id,available_at,revision_id);
CREATE INDEX IF NOT EXISTS research_catalyst_date ON research_catalyst_revision(starts_at,ends_at);
CREATE TABLE IF NOT EXISTS research_catalyst_asset (
    revision_id TEXT NOT NULL REFERENCES research_catalyst_revision(revision_id) DEFERRABLE INITIALLY DEFERRED,
    asset_uid TEXT NOT NULL REFERENCES research_asset(asset_uid) DEFERRABLE INITIALLY DEFERRED,
    relation TEXT NOT NULL, PRIMARY KEY(revision_id,asset_uid)
);
CREATE INDEX IF NOT EXISTS research_catalyst_asset_lookup ON research_catalyst_asset(asset_uid,revision_id);
CREATE TABLE IF NOT EXISTS research_catalyst_source (
    id TEXT PRIMARY KEY, catalyst_id TEXT NOT NULL REFERENCES research_catalyst(id) DEFERRABLE INITIALLY DEFERRED,
    available_at BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_catalyst_candidate (
    id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, review_status TEXT NOT NULL,
    accepted_catalyst_id TEXT REFERENCES research_catalyst(id) DEFERRABLE INITIALLY DEFERRED,
    available_at BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_candidate_status ON research_catalyst_candidate(review_status,available_at,id);
CREATE TABLE IF NOT EXISTS research_catalyst_transition (
    id TEXT PRIMARY KEY, catalyst_id TEXT NOT NULL REFERENCES research_catalyst(id) DEFERRABLE INITIALLY DEFERRED,
    at_ms BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_catalyst_alias (
    alias_id TEXT PRIMARY KEY REFERENCES research_catalyst(id) DEFERRABLE INITIALLY DEFERRED,
    canonical_id TEXT NOT NULL REFERENCES research_catalyst(id) DEFERRABLE INITIALLY DEFERRED,
    available_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_subscription (
    id TEXT PRIMARY KEY, active BIGINT NOT NULL, created_at BIGINT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_reminder (
    id TEXT PRIMARY KEY, catalyst_id TEXT NOT NULL REFERENCES research_catalyst(id) DEFERRABLE INITIALLY DEFERRED,
    event_number BIGINT NOT NULL UNIQUE,
    revision_id TEXT NOT NULL, subscription_id TEXT NOT NULL REFERENCES research_subscription(id) DEFERRABLE INITIALLY DEFERRED,
    due_at BIGINT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_reminder_due ON research_reminder(status,due_at);
