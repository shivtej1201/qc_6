-- Operations (CrawlPulse job orchestration §2, §4.3-4.5): a per-check event timeline, per-minute crawl figures,
-- richer worker registry, and the CANCELLING state (cancel asked, worker still stopping).
CREATE TABLE IF NOT EXISTS run_events (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL,
    data        JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events (run_id, id);

CREATE TABLE IF NOT EXISTS run_minutes (
    run_id          BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    minute          TIMESTAMPTZ NOT NULL,
    pages           INT NOT NULL DEFAULT 0,
    errors          INT NOT NULL DEFAULT 0,
    latency_ms_sum  DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, minute)
);

ALTER TABLE workers ADD COLUMN IF NOT EXISTS hostname TEXT;
ALTER TABLE workers ADD COLUMN IF NOT EXISTS pid INT;
ALTER TABLE workers ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'idle';
ALTER TABLE workers ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check
    CHECK (status IN ('queued', 'running', 'pausing', 'paused', 'cancelling', 'completed', 'failed', 'cancelled'));
DROP INDEX IF EXISTS runs_live;
CREATE INDEX IF NOT EXISTS runs_live ON runs (status, heartbeat_at) WHERE status IN ('running', 'pausing', 'cancelling');
