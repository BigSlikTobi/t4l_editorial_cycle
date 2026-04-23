-- Resolved entity mentions per article. Replaces the embedded `entities[]`
-- that the legacy feed returned inline. Filled by the knowledge-extraction
-- step of the ingestion worker.
--
-- entity_id conventions (matched to shape legacy code expects):
--   entity_type='player' → GSIS id, e.g. '00-0026158'
--   entity_type='team'   → 3-letter team code, e.g. 'KC'
--   entity_type='game'   → nflverse game_id, e.g. '2024_01_KC_BAL'
--   entity_type='coach'  → stable coach slug

CREATE TABLE IF NOT EXISTS public.article_entities (
    article_id    uuid NOT NULL
                  REFERENCES public.raw_articles(id) ON DELETE CASCADE,
    entity_type   text NOT NULL,
    entity_id     text NOT NULL,
    mention_text  text,
    matched_name  text,
    confidence    real,
    team_abbr     text,
    position      text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS article_entities_by_entity_idx
    ON public.article_entities (entity_type, entity_id);

ALTER TABLE public.article_entities ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.article_entities
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
