-- (016) Internal tickets, one per issue type of a site (all redirecting links = one ticket; every URL goes in its
-- attachment). The number QC-<id> stays the same for that site and issue; raised_ref is what the Ops team's tool
-- called it.
CREATE TABLE IF NOT EXISTS issue_tickets (
    id          BIGSERIAL PRIMARY KEY,
    site_id     BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    group_key   TEXT NOT NULL,
    raised_ref  TEXT,
    raised_at   TIMESTAMPTZ,
    raised_by   BIGINT REFERENCES members(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (site_id, group_key)
);
