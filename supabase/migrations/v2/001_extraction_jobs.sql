-- Async job state for the three Google Cloud Functions:
--   news_extraction_service, url_content_extraction_service,
--   article_knowledge_extraction
--
-- Each function writes one row per submitted job, a background worker
-- updates it, and the client polls. Terminal polls atomically delete
-- via consume_extraction_job(uuid). A 5-min cleanup job re-queues
-- stale rows and expires abandoned ones.
--
-- Shared table, distinguished by `service` tag.

CREATE TABLE IF NOT EXISTS public.extraction_jobs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service        text NOT NULL,        -- 'news_extraction' | 'url_content_extraction' | 'article_knowledge_extraction'
    status         text NOT NULL DEFAULT 'queued',  -- queued | running | succeeded | failed | expired
    payload        jsonb NOT NULL,
    result         jsonb,
    error          jsonb,
    worker_token   text,
    started_at     timestamptz,
    expires_at     timestamptz NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS extraction_jobs_status_idx
    ON public.extraction_jobs (service, status, created_at);
CREATE INDEX IF NOT EXISTS extraction_jobs_expires_idx
    ON public.extraction_jobs (expires_at);

-- Atomic consume: return the row and delete it in one statement.
-- Used by the cloud functions' terminal-poll code path.
CREATE OR REPLACE FUNCTION public.consume_extraction_job(job_id uuid)
RETURNS SETOF public.extraction_jobs
LANGUAGE sql
AS $$
    DELETE FROM public.extraction_jobs
    WHERE id = job_id
      AND status IN ('succeeded', 'failed', 'expired')
    RETURNING *;
$$;

ALTER TABLE public.extraction_jobs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.extraction_jobs
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
