CREATE TABLE IF NOT EXISTS editorial_state (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_fingerprint   text NOT NULL,
    published_at        timestamptz NOT NULL DEFAULT now(),
    last_updated_at     timestamptz NOT NULL DEFAULT now(),
    supabase_article_id bigint NOT NULL,
    cycle_id            text NOT NULL,
    cluster_headline    text NOT NULL DEFAULT '',

    CONSTRAINT uq_editorial_state_fingerprint UNIQUE (story_fingerprint)
);

CREATE INDEX idx_editorial_state_published_at ON editorial_state (published_at);
CREATE INDEX idx_editorial_state_cycle_id ON editorial_state (cycle_id);

ALTER TABLE editorial_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON editorial_state
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
