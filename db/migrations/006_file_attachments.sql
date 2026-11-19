-- 006_file_attachments.sql
-- Chat file attachments.
--
-- Files are stored as BYTEA in PostgreSQL so no extra volume or object store is
-- needed. The gateway remains the only public entry point; message-api owns the
-- bytes and checks who is allowed to read them.

CREATE TABLE IF NOT EXISTS attachments (
    id            UUID PRIMARY KEY,
    uploader_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    size_bytes    BIGINT NOT NULL CHECK (size_bytes > 0),
    content       BYTEA NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_attachments_uploader_created
    ON attachments (uploader_id, created_at DESC);

-- A message may carry one attachment. Deleting an attachment never deletes a
-- message; an unsent (orphan) upload can be removed safely by its owner.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS attachment_id UUID REFERENCES attachments(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_messages_attachment_id
    ON messages (attachment_id);
