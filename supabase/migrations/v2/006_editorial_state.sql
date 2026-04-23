-- Cross-cycle dedup memory: one row per published story (by fingerprint).
-- Consolidates legacy migrations 001–002 from supabase/migrations/ into a
-- single table definition for the fresh v2 project.

CREATE TABLE IF NOT EXISTS public.editorial_state (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_fingerprint    text NOT NULL,
    published_at         timestamptz NOT NULL DEFAULT now(),
    last_updated_at      timestamptz NOT NULL DEFAULT now(),
    supabase_article_id  bigint NOT NULL,
    cycle_id             text NOT NULL,
    cluster_headline     text NOT NULL DEFAULT '',
    source_urls          text[] NOT NULL DEFAULT '{}',
    CONSTRAINT uq_editorial_state_fingerprint UNIQUE (story_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_editorial_state_published_at
    ON public.editorial_state (published_at);
CREATE INDEX IF NOT EXISTS idx_editorial_state_cycle_id
    ON public.editorial_state (cycle_id);

ALTER TABLE public.editorial_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.editorial_state
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
