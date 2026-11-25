-- Generated report packages (ZIP) of each check: stored on disk under runs/<id>/package/, metadata here.
CREATE TABLE IF NOT EXISTS reports (
    id           BIGSERIAL PRIMARY KEY,
    run_id       BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    site_id      BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL DEFAULT 'package',           -- package (ZIP of every file)
    file_name    TEXT NOT NULL,
    file_type    TEXT NOT NULL DEFAULT 'application/zip',
    path         TEXT NOT NULL,                             -- local file (never the bytes in the database)
    status       TEXT NOT NULL DEFAULT 'building' CHECK (status IN ('building', 'ready', 'failed', 'replaced', 'deleted')),
    size_bytes   BIGINT,
    files        INT,
    compressed   BOOLEAN NOT NULL DEFAULT true,
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,        -- rows per file, seconds taken, folder
    error        TEXT,
    downloads    INT NOT NULL DEFAULT 0,
    last_download_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS reports_run ON reports (run_id, id DESC);
