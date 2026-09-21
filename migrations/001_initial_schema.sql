CREATE TABLE IF NOT EXISTS processed_emails (
    id SERIAL PRIMARY KEY,
    gmail_message_id VARCHAR(255) UNIQUE NOT NULL,
    sender TEXT,
    subject TEXT,
    received_at TIMESTAMP,
    processed_at TIMESTAMP,
    category VARCHAR(100),
    confidence NUMERIC(4,3),
    summary TEXT,
    status VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS workflow_log (
    id SERIAL PRIMARY KEY,
    gmail_message_id VARCHAR(255),
    action TEXT,
    timestamp TIMESTAMP,
    details TEXT
);

CREATE TABLE IF NOT EXISTS sender_profiles (
    sender_email TEXT PRIMARY KEY,
    sender_type VARCHAR(50),
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    email_count INTEGER DEFAULT 0
);