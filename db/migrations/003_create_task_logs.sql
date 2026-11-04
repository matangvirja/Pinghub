-- 003_create_task_logs.sql
-- One row per asynchronous task. task_id is UNIQUE so a RabbitMQ redelivery is
-- idempotent (the worker UPSERTs instead of inserting a duplicate).

CREATE TABLE IF NOT EXISTS task_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id     UUID NOT NULL UNIQUE,
    message_id  UUID REFERENCES messages(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'QUEUED'
                CHECK (status IN ('QUEUED', 'PROCESSING', 'DELIVERED', 'FAILED')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    request_id  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs (task_id);
CREATE INDEX IF NOT EXISTS idx_task_logs_status ON task_logs (status);
