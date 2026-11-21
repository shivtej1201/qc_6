-- Pages explorer: open fixes per page of a check (findings by run + page) without scanning every finding.
CREATE INDEX IF NOT EXISTS findings_run_page ON findings (run_id, page_url);
