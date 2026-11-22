-- Built-in sign-in (QC6_AUTH=local): passwords (scrypt), sessions (only a hash of the token), two-factor (TOTP).
ALTER TABLE members ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE members ADD COLUMN IF NOT EXISTS totp_secret TEXT;              -- encrypted with QC6_SECRET_KEY
ALTER TABLE members ADD COLUMN IF NOT EXISTS totp_last_step BIGINT;         -- a code can't be used twice
ALTER TABLE members ADD COLUMN IF NOT EXISTS recovery_codes JSONB;          -- hashes of unused recovery codes
ALTER TABLE members ADD COLUMN IF NOT EXISTS failed_logins INT NOT NULL DEFAULT 0;
ALTER TABLE members ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS sessions (
    token_hash    TEXT PRIMARY KEY,
    member_id     BIGINT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    two_factor_ok BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    ip            TEXT,
    user_agent    TEXT
);
CREATE INDEX IF NOT EXISTS sessions_member ON sessions (member_id);
