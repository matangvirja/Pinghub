-- 005_auth_and_chat_delete.sql
-- Adds employee authentication and per-user chat deletion.
--
--   * users.password_hash  PBKDF2-SHA256 hash (NULL = legacy profile that has
--                          not been activated with the company code yet)
--   * users.is_active      lets an operator disable an account
--   * auth_sessions        opaque bearer tokens, stored as SHA-256 hashes
--   * conversation_hides   per-user soft delete of a whole conversation
--
-- A chat hidden at time T stays hidden for that user, but a message created
-- after T makes the conversation appear again. The other person is unaffected.
--
-- Safe to run more than once: the compose "migrate" service applies every file
-- on every start.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- users: password + enabled flag
-- ---------------------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

-- ---------------------------------------------------------------------------
-- auth_sessions: one row per signed-in device/tab
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth_sessions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id
    ON auth_sessions (user_id);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at
    ON auth_sessions (expires_at);

-- ---------------------------------------------------------------------------
-- conversation_hides: a user deleted their view of a conversation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_hides (
    user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    peer_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    hidden_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, peer_id)
);

CREATE INDEX IF NOT EXISTS idx_conversation_hides_user
    ON conversation_hides (user_id);
