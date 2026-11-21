-- Fast "find a page" on big sites: trigram indexes on page address and title (pg_trgm ships with PostgreSQL).
-- Where the extension can't be created (no permission), the indexes are skipped and search still works, just slower.
DO $$
BEGIN
  BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pg_trgm not available (%); page search works without the index', SQLERRM;
  END;
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS pages_url_trgm ON pages USING gin (url gin_trgm_ops)';
    EXECUTE 'CREATE INDEX IF NOT EXISTS pages_title_trgm ON pages USING gin (title gin_trgm_ops)';
  END IF;
END $$;
