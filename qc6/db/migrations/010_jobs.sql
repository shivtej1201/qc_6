-- Simple QC flow: URL lists (from a sitemap, one address or a sheet) that a check then crawls; worker liveness.
CREATE TABLE IF NOT EXISTS url_lists (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,                       -- sitemap | single | sheet
    label         TEXT NOT NULL,                       -- the sitemap address, the URL, or the file name
    status        TEXT NOT NULL DEFAULT 'ready',       -- extracting | ready | failed | cancelled
    urls          TEXT[] NOT NULL DEFAULT '{}',
    url_count     INT NOT NULL DEFAULT 0,
    progress      JSONB NOT NULL DEFAULT '{}',
    note          JSONB NOT NULL DEFAULT '{}',         -- other hosts dropped, duplicates
    error         TEXT,
    run_id        BIGINT REFERENCES runs(id) ON DELETE SET NULL,
    created_by    BIGINT REFERENCES members(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS url_lists_ws ON url_lists (workspace_id, id DESC);

CREATE TABLE IF NOT EXISTS workers (
    id         TEXT PRIMARY KEY,
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id     BIGINT
);
