-- Index for the editorial cycle's feed query:
--   SELECT … FROM raw_articles
--   WHERE status = 'knowledge_ok'
--     AND knowledge_extracted_at >= now() - interval 'N hours'
--   ORDER BY knowledge_extracted_at DESC;
--
-- We filter on knowledge_extracted_at (not fetched_at) so rows that took a
-- while to traverse the ingestion pipeline don't age out of the editorial
-- window before they're ever eligible. See app/adapters.py
-- RawArticleDbReader.fetch_raw_articles.

CREATE INDEX IF NOT EXISTS raw_articles_knowledge_extracted_at_idx
    ON public.raw_articles (knowledge_extracted_at DESC)
    WHERE status = 'knowledge_ok';
