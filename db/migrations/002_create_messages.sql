-- 002_create_messages.sql
-- status is constrained to the four states from the assignment.

CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY,
    sender_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    receiver_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'QUEUED'
                CHECK (status IN ('QUEUED', 'PROCESSING', 'DELIVERED', 'FAILED')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_sender_created
    ON messages (sender_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_receiver_created
    ON messages (receiver_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_created
    ON messages (created_at DESC);
