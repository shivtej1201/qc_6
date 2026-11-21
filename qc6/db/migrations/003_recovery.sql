-- Stuck-check recovery (FEAT-19-02): which worker holds a check, when it last said it was alive, how often it started.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS worker_id TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS runs_live ON runs (status, heartbeat_at) WHERE status IN ('running', 'pausing');
