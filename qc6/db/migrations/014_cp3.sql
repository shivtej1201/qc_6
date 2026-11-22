-- CrawlPulse 3 gaps: day-by-day Search Console figures per page (fix impact), opportunity reviews, AI content briefs.
CREATE TABLE IF NOT EXISTS search_page_days (
    site_id      BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    page         TEXT NOT NULL,
    day          DATE NOT NULL,
    clicks       INT NOT NULL,
    impressions  INT NOT NULL,
    position     DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (site_id, page, day)
);
CREATE INDEX IF NOT EXISTS search_page_days_site_day ON search_page_days (site_id, day);

CREATE TABLE IF NOT EXISTS opportunity_reviews (
    id          BIGSERIAL PRIMARY KEY,
    site_id     BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('accepted', 'dismissed', 'done', 'open')),
    note        TEXT NOT NULL DEFAULT '',
    score       DOUBLE PRECISION,
    member_id   BIGINT REFERENCES members(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS opportunity_reviews_site_url ON opportunity_reviews (site_id, url, id DESC);

CREATE TABLE IF NOT EXISTS content_briefs (
    id          BIGSERIAL PRIMARY KEY,
    site_id     BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    sections    JSONB NOT NULL DEFAULT '{}',      -- name -> {text, edited_by_person, generated_at}
    evidence    JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'final')),
    created_by  BIGINT REFERENCES members(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS content_briefs_site ON content_briefs (site_id, id DESC);
