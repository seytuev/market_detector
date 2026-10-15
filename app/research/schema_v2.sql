CREATE TABLE IF NOT EXISTS research_channel (
    handle TEXT PRIMARY KEY, enabled BIGINT NOT NULL, last_id BIGINT,
    updated_at BIGINT NOT NULL, payload TEXT NOT NULL
);
