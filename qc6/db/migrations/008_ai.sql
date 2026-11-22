-- AI drafts (local model via Ollama). Every draft waits for a person: approved, edited or rejected. Nothing is applied.
CREATE TABLE IF NOT EXISTS ai_suggestions (
    id            BIGSERIAL PRIMARY KEY,
    site_id       BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    fix_id        BIGINT REFERENCES fixes(id) ON DELETE CASCADE,
    page_url      TEXT,
    kind          TEXT NOT NULL CHECK (kind IN ('fix_steps', 'title_description')),
    model         TEXT NOT NULL,
    evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,   -- what the model was given (shown next to the draft)
    draft         JSONB NOT NULL,                       -- what it wrote
    final         JSONB,                                -- what a person approved (possibly edited)
    status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected')),
    created_by    BIGINT REFERENCES members(id) ON DELETE SET NULL,
    decided_by    BIGINT REFERENCES members(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ai_suggestions_fix ON ai_suggestions (fix_id, id);
