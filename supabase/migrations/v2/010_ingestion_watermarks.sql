-- Per-source watermark for news_extraction. Keyed on source_name (the
-- feed label the CF emits, e.g. "ESPN", "CBSSports"). Advanced on every
-- ingestion run from the max publication_date seen for each source in
-- the CF response, BEFORE content/knowledge extraction runs — so later
-- stage failures never block future ingestion.
--
-- Replaces the old `INGESTION_NEWS_LOOKBACK_HOURS` env knob.
--
-- Read path: app/ingestion/worker._discover()
--   since = (min(last_publication_at) if any else now()-6h) - 15min
-- The 15-min rewind buffer absorbs any back-dated articles the feed may
-- publish slightly out of order; URL dedup absorbs the duplicates that
-- produces.

CREATE TABLE IF NOT EXISTS public.ingestion_watermarks (
    source_name          text PRIMARY KEY,
    last_publication_at  timestamptz NOT NULL,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.ingestion_watermarks ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.ingestion_watermarks
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
