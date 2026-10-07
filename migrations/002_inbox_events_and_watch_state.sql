CREATE TABLE IF NOT EXISTS inbox_events (
    id SERIAL PRIMARY KEY,
    pubsub_message_id VARCHAR(255) UNIQUE NOT NULL,
    publish_time TIMESTAMP NULL,
    email_address TEXT NULL,
    history_id VARCHAR(255) NULL,
    payload JSONB NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'received',
    error TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inbox_events_status ON inbox_events(status);
CREATE INDEX IF NOT EXISTS idx_inbox_events_history_id ON inbox_events(history_id);

CREATE TABLE IF NOT EXISTS watch_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

ALTER TABLE workflow_log
    ALTER COLUMN details TYPE TEXT;