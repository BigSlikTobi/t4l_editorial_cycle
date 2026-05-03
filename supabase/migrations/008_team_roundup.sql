-- T4L Team Beat — twice-daily team roundup tables.
--
-- See docs/team_beat_mvp.md for product context.
--
-- Two tables, both in the `public` schema (the new Supabase project does
-- not provision the `content` schema the legacy editorial pipeline uses):
--   * public.team_roundup        — one row per (team, cycle) when the beat
--                                    reporter files. Carries EN+DE written
--                                    bodies, DE radio script, and the public
--                                    audio URL produced by the gemini_tts_batch_service.
--   * public.team_beat_cycle_state — one row per (team, cycle) per attempt.
--                                    Records outcome ∈ {filed, no_news, error}
--                                    so cycle reliability is observable even
--                                    when no roundup row is written.
--
-- Storage for the audio MP3s lives in the Supabase Storage bucket
-- `team-beat-audio` (created out-of-band; not declared here).

-- ---------------------------------------------------------------- team_roundup

CREATE TABLE IF NOT EXISTS public.team_roundup (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_code       text NOT NULL,
    cycle_ts        timestamptz NOT NULL,
    cycle_slot      text NOT NULL,                         -- 'AM' | 'PM' (Berlin-time slot)
    persona_name    text NOT NULL,                         -- the fantasy-named beat reporter byline
    en_body         text NOT NULL,
    de_body         text NOT NULL,
    radio_script    text NOT NULL,                         -- DE only
    audio_url       text,                                  -- nullable: TTS may fail per item
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- One filed roundup per (team, cycle_ts). Reruns of the same cycle
    -- upsert in place rather than accumulate orphans.
    CONSTRAINT uq_team_roundup_team_cycle UNIQUE (team_code, cycle_ts)
);

CREATE INDEX IF NOT EXISTS idx_team_roundup_team_created
    ON public.team_roundup (team_code, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_team_roundup_cycle_ts
    ON public.team_roundup (cycle_ts DESC);

ALTER TABLE public.team_roundup ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.team_roundup
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- ----------------------------------------------------- team_beat_cycle_state

CREATE TABLE IF NOT EXISTS public.team_beat_cycle_state (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_code       text NOT NULL,
    cycle_ts        timestamptz NOT NULL,
    cycle_slot      text NOT NULL,                         -- 'AM' | 'PM'
    outcome         text NOT NULL,                         -- 'filed' | 'no_news' | 'error'
    reason          text,                                  -- short editorial / error note
    article_count   integer,                               -- raw articles seen in window
    roundup_id      bigint REFERENCES public.team_roundup(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT chk_beat_outcome CHECK (outcome IN ('filed', 'no_news', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_beat_state_team_created
    ON public.team_beat_cycle_state (team_code, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_beat_state_outcome_created
    ON public.team_beat_cycle_state (outcome, created_at DESC);

ALTER TABLE public.team_beat_cycle_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.team_beat_cycle_state
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
