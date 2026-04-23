-- Topics extracted per article by the knowledge-extraction step.
-- Stored separately from article_entities so downstream queries can
-- filter/rank by topic independently. Not yet consumed by the editorial
-- cycle, but populated from day one so we can layer topic-based ranking
-- later without a backfill.

CREATE TABLE IF NOT EXISTS public.article_topics (
    article_id  uuid NOT NULL
                REFERENCES public.raw_articles(id) ON DELETE CASCADE,
    topic       text NOT NULL,
    confidence  real,
    rank        int,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, topic)
);

CREATE INDEX IF NOT EXISTS article_topics_by_topic_idx
    ON public.article_topics (topic);

ALTER TABLE public.article_topics ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.article_topics
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
