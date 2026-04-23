-- Owned ingestion store. Replaces the legacy news-feed edge function.
--
-- Populated by app/ingestion/worker.py, which chains:
--   1. news_extraction_service → inserts rows with status='discovered'
--   2. url_content_extraction_service → fills content, status='content_ok'
--   3. article_knowledge_extraction → fills article_entities + article_topics,
--      status='knowledge_ok'
--
-- editorial/workflow.py reads rows with status='knowledge_ok' within the
-- lookback window and hydrates RawArticle objects (see app/schemas.py).

CREATE TABLE IF NOT EXISTS public.raw_articles (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    url                     text NOT NULL UNIQUE,
    title                   text,
    source_name             text,
    publisher               text,
    category                text,
    publication_date        timestamptz,
    fetched_at              timestamptz NOT NULL DEFAULT now(),
    content                 text,
    content_extracted_at    timestamptz,
    knowledge_extracted_at  timestamptz,
    status                  text NOT NULL DEFAULT 'discovered',
                                -- discovered | content_ok | knowledge_ok | failed
    error                   jsonb,
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT raw_articles_status_check
        CHECK (status IN ('discovered','content_ok','knowledge_ok','failed'))
);

CREATE INDEX IF NOT EXISTS raw_articles_status_idx
    ON public.raw_articles (status, fetched_at DESC);
CREATE INDEX IF NOT EXISTS raw_articles_pub_date_idx
    ON public.raw_articles (publication_date DESC);

ALTER TABLE public.raw_articles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.raw_articles
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
