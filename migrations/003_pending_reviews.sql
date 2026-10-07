CREATE TABLE IF NOT EXISTS pending_reviews (
    review_id VARCHAR(64) PRIMARY KEY,
    gmail_message_id VARCHAR(255) NOT NULL,
    state_payload JSONB NOT NULL,
    telegram_chat_id TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    decision_by TEXT NULL,
    decided_at TIMESTAMP NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pending_reviews_status ON pending_reviews(status);