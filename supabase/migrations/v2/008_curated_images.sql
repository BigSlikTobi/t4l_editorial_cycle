-- Curated image pool, tier 3 of the image_selector cascade.
-- Copied verbatim from legacy migration 005_curated_images.sql.
--
-- Pre-generated + human-reviewed PNGs uploaded via
-- scripts/upload_curated_pool.py (in the legacy repo). Re-upload the pool
-- into the new project's storage bucket and reinsert the rows.

CREATE TABLE IF NOT EXISTS public.curated_images (
    id           bigserial PRIMARY KEY,
    slug         text NOT NULL UNIQUE,
    team_code    text,                      -- null for generic scenes
    scene        text NOT NULL,
    description  text NOT NULL,
    image_url    text NOT NULL,
    generated_by text NOT NULL DEFAULT 'gemini',
    prompt       text,
    active       boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS curated_images_team_scene_idx
    ON public.curated_images (team_code, scene)
    WHERE active;

CREATE INDEX IF NOT EXISTS curated_images_scene_idx
    ON public.curated_images (scene)
    WHERE active AND team_code IS NULL;

ALTER TABLE public.curated_images ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
    ON public.curated_images
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

