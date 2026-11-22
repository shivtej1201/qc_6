-- "Seen before" remarks: earlier checks that found the same problem on the same page.
CREATE INDEX IF NOT EXISTS findings_fix_page ON findings (fix_id, page_url, run_id);
