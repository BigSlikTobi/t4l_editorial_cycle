-- Published article output. Consolidates legacy migrations 003–007 so the
-- v2 project has the final column set from day one.
--
-- Written by app/writer via PostgREST (public schema — no profile header).
-- Uniqueness: (story_fingerprint, language) is the natural upsert key for
-- the multi-language write path (en-US + de-DE per story).

-- NOTE: The foreign key on `team` references the reference-data table
-- `public.teams` which is created by the data_loading cloud function
-- (see 002_reference_data.sql). If you apply this migration before
-- running the loader, leave the FK commented out and add it later.
-- We define the column as text (nullable) so bad values can be retried
-- with team=NULL — see ArticleWriter.write_article.

CREATE TABLE IF NOT EXISTS public.team_article (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team               text,
    language           text NOT NULL DEFAULT 'en-US',
    headline           text NOT NULL,
    sub_headline       text,
    introduction       text,
    content            text NOT NULL,
    x_post             text,
    bullet_points      text,
    image              text,
    tts_file           text,
    author             text,
    mentioned_players  text[] NOT NULL DEFAULT '{}',
    sources            jsonb NOT NULL DEFAULT '[]'::jsonb,
    story_fingerprint  text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS team_article_fingerprint_language_idx
    ON public.team_article (story_fingerprint, language)
    WHERE story_fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS team_article_team_idx
    ON public.team_article (team);
CREATE INDEX IF NOT EXISTS team_article_language_idx
    ON public.team_article (language);

-- Optional FK to public.teams(team_code). Uncomment after loader runs.
-- ALTER TABLE public.team_article
--     ADD CONSTRAINT team_article_team_fkey
--     FOREIGN KEY (team) REFERENCES public.teams(team_code)
--     ON DELETE SET NULL;

ALTER TABLE public.team_article ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.team_article
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
