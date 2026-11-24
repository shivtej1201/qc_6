-- SMTP settings for the crawl summary e-mail (one per workspace). The password is sealed with QC6_SECRET_KEY
-- (AES-256-GCM, see site_access.seal) and never sent to the browser.
CREATE TABLE IF NOT EXISTS mail_settings (
    workspace_id    BIGINT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    enabled         BOOLEAN NOT NULL DEFAULT false,
    host            TEXT NOT NULL DEFAULT '',
    port            INT NOT NULL DEFAULT 587,
    security        TEXT NOT NULL DEFAULT 'starttls' CHECK (security IN ('starttls', 'ssl', 'none')),
    username        TEXT NOT NULL DEFAULT '',
    password_sealed TEXT,
    sender          TEXT NOT NULL DEFAULT '',
    recipients      TEXT[] NOT NULL DEFAULT '{}',
    attach_report   BOOLEAN NOT NULL DEFAULT true,
    last_result     JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by      BIGINT REFERENCES members(id) ON DELETE SET NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
