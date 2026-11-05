-- 004_ui_features.sql
-- Adds the data needed by the web UI:
--   * a short, shareable public id for every user (e.g. 7F3A9C21)
--   * optional display names
--   * delivery and read timestamps
--   * a READ state that comes after DELIVERED
--
-- This migration is written to be safe to run more than once, because the
-- compose "migrate" service applies every file on every start.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- users: shareable public id + display name
-- ---------------------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS public_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT;

-- Backfill existing users with a deterministic id derived from their username.
UPDATE users
SET public_id = upper(substr(encode(digest(username, 'sha256'), 'hex'), 1, 8))
WHERE public_id IS NULL;

-- New users get a random id automatically, even when the worker creates them
-- with a plain INSERT INTO users (username) ...
ALTER TABLE users
    ALTER COLUMN public_id
    SET DEFAULT upper(substr(encode(gen_random_bytes(4), 'hex'), 1, 8));

ALTER TABLE users ALTER COLUMN public_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_public_id ON users (public_id);
CREATE INDEX IF NOT EXISTS idx_users_username_lower ON users (lower(username));

-- ---------------------------------------------------------------------------
-- messages: delivery + read tracking and the READ state
-- ---------------------------------------------------------------------------
ALTER TABLE messages ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS read_at TIMESTAMPTZ;

ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_status_check;
ALTER TABLE messages
    ADD CONSTRAINT messages_status_check
    CHECK (status IN ('QUEUED', 'PROCESSING', 'DELIVERED', 'READ', 'FAILED'));

CREATE INDEX IF NOT EXISTS idx_messages_pair_created
    ON messages (sender_id, receiver_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_receiver_status
    ON messages (receiver_id, status);
