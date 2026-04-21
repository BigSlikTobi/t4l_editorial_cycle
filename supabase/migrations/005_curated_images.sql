-- Curated image pool.
--
-- Pre-generated images (initially via Gemini batch) reviewed by hand before
-- going live. The image_selector reads from this table at tier 3 (replacing
-- on-demand AI generation) to eliminate per-cycle Gemini cost.
--
-- Filled by scripts/upload_curated_pool.py after manual review of the
-- generated pool.

CREATE TABLE IF NOT EXISTS content.curated_images (
    id bigserial PRIMARY KEY,
    slug text NOT NULL UNIQUE,            -- e.g. "KC_offense_action", "generic_press_conference_01"
    team_code text,                       -- null for generic scenes
    scene text NOT NULL,                  -- e.g. "offense_action", "press_conference"
    description text NOT NULL,
    image_url text NOT NULL,              -- public Supabase Storage URL
    generated_by text NOT NULL DEFAULT 'gemini',  -- 'gemini' | 'human' | 'licensed' — surfaced in FE later
    prompt text,                          -- original generation prompt (nullable for non-AI sources)
    active boolean NOT NULL DEFAULT true, -- soft-delete flag; set false to exclude from selection
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Fast filter for the selector: "team-specific for team X" and
-- "generic (team IS NULL)", both optionally scoped to a scene.
CREATE INDEX IF NOT EXISTS curated_images_team_scene_idx
    ON content.curated_images (team_code, scene)
    WHERE active;

CREATE INDEX IF NOT EXISTS curated_images_scene_idx
    ON content.curated_images (scene)
    WHERE active AND team_code IS NULL;
