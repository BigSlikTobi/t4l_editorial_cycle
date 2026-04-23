-- Provenance store for images used on published articles. Written by
-- app/adapters.py::ImageUploader.record_metadata when an image is sourced
-- from the web (Google CC / Wikimedia) or uploaded fresh.
--
-- Keyed on original_url so reruns on the same story do not duplicate rows
-- (merge-duplicates upsert in the uploader).

CREATE TABLE IF NOT EXISTS public.article_images (
    id           bigserial PRIMARY KEY,
    image_url    text NOT NULL,
    original_url text NOT NULL UNIQUE,
    source       text NOT NULL,        -- 'google_cc' | 'wikimedia' | 'player_headshot' | 'curated' | ...
    author       text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS article_images_source_idx
    ON public.article_images (source);

ALTER TABLE public.article_images ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.article_images
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

