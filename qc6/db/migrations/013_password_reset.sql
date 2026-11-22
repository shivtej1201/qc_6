-- "Forgot password" by e-mail (QC6_AUTH=local with QC6_SMTP_HOST set): one-time links, only their hash is kept.
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash  TEXT PRIMARY KEY,
    member_id   BIGINT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
