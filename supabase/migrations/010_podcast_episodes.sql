-- T4L Daily Briefing — personal NFL podcast audit log.
--
-- One row per (run_date, language) per day. Tracks episode lifecycle:
-- pending → rendering → rendered → delivered (or failed at any step).
--
-- Audio is intentionally NOT stored in Supabase. Episodes are rendered
-- to a local temp file on the VPS, uploaded to the user's personal
-- Spotify library via the Save-to-Spotify CLI, then deleted. This row
-- is the canonical audit trail; Spotify is the canonical media store.
--
-- v2 migration path: add `user_id uuid NULL` and a partial unique index
-- (run_date, language, user_id) once the multi-user subscription model
-- ships. Existing personal-only rows get a NULL user_id (the project
-- owner) and the partial unique tolerates the NULL.

CREATE TABLE IF NOT EXISTS public.podcast_episodes (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_date            date        NOT NULL,
    language            text        NOT NULL,
    story_count         integer     NOT NULL DEFAULT 0,
    word_count          integer     NOT NULL DEFAULT 0,
    duration_seconds    integer,
    audio_local_path    text,
    status              text        NOT NULL DEFAULT 'pending',
    delivered_at        timestamptz,
    spotify_episode_id  text,
    error_message       text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT chk_podcast_language
        CHECK (language IN ('en-US', 'de-DE')),
    CONSTRAINT chk_podcast_status
        CHECK (status IN ('pending', 'rendering', 'rendered', 'delivered', 'failed')),
    CONSTRAINT uq_podcast_run_lang
        UNIQUE (run_date, language)
);

CREATE INDEX IF NOT EXISTS idx_podcast_episodes_run_date
    ON public.podcast_episodes (run_date DESC, language);

CREATE INDEX IF NOT EXISTS idx_podcast_episodes_status_created
    ON public.podcast_episodes (status, created_at DESC);

ALTER TABLE public.podcast_episodes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.podcast_episodes
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
