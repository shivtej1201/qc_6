-- Template x Component x Issue: each fix (problem) records the page component it is about (Breadcrumb, Header,
-- Product schema, Canonical...) and the template it belongs to for tickets ("Site-wide" when a link in the header /
-- footer / navigation is on pages of several templates).
ALTER TABLE fixes ADD COLUMN IF NOT EXISTS component TEXT NOT NULL DEFAULT '';
ALTER TABLE fixes ADD COLUMN IF NOT EXISTS ticket_template TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS fixes_site_group ON fixes (site_id, rule, ticket_template, component);
