-- Which sitemap each listed address came from (the report's Sitemap Progress), and where the sitemap's hreflang
-- alternates were saved.
ALTER TABLE url_lists ADD COLUMN IF NOT EXISTS sitemaps TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE url_lists ADD COLUMN IF NOT EXISTS sitemap_idx INT[] NOT NULL DEFAULT '{}';
ALTER TABLE url_lists ADD COLUMN IF NOT EXISTS alternates_file TEXT;
