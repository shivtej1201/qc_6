-- Internal tickets: every fix is a ticket (QC-<id>); when someone raises it in the Ops team's tool they record the
-- number they got there. Separate from ticket_key, which the GitHub / Jira sync owns.
ALTER TABLE fixes ADD COLUMN IF NOT EXISTS raised_ref TEXT;
ALTER TABLE fixes ADD COLUMN IF NOT EXISTS raised_at TIMESTAMPTZ;
ALTER TABLE fixes ADD COLUMN IF NOT EXISTS raised_by BIGINT REFERENCES members(id) ON DELETE SET NULL;
