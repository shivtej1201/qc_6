-- Search Console (query x page, last N days) and GA4 landing-page sessions, one snapshot per site (FEAT-12/13/14).
CREATE TABLE IF NOT EXISTS search_syncs (
    site_id       BIGINT PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    start_date    DATE,
    end_date      DATE,
    gsc_rows      INT NOT NULL DEFAULT 0,
    ga4_pages     INT NOT NULL DEFAULT 0,
    truncated     BOOLEAN NOT NULL DEFAULT false,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS search_rows (
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    query         TEXT NOT NULL,
    page          TEXT NOT NULL,
    clicks        INT NOT NULL,
    impressions   INT NOT NULL,
    ctr           DOUBLE PRECISION NOT NULL,
    position      DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS search_rows_site_query ON search_rows (site_id, query);
CREATE INDEX IF NOT EXISTS search_rows_site_page ON search_rows (site_id, page);
CREATE TABLE IF NOT EXISTS ga4_pages (
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    path          TEXT NOT NULL,
    sessions      INT NOT NULL,
    PRIMARY KEY (site_id, path)
);
