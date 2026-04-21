-- Story-fingerprint on team_article.
--
-- Needed for the multi-language write path: each story produces both an
-- en-US and a de-DE row; (story_fingerprint, language) is the natural
-- upsert key for them. Without this column, find_article_id can't locate
-- the German row on subsequent cycles (editorial_state tracks only the
-- English row) and every update would INSERT a duplicate German article.
--
-- Backfill: English rows can be recovered via editorial_state, which
-- already stores (story_fingerprint, supabase_article_id). German rows
-- written during the initial German rollout (2026-04-20 onward) will
-- have NULL fingerprint and will simply INSERT fresh German versions
-- on the next update cycle — orphaned old rows can be cleaned manually.

ALTER TABLE content.team_article
    ADD COLUMN IF NOT EXISTS story_fingerprint text;

-- Backfill English rows from editorial_state.
UPDATE content.team_article ta
SET story_fingerprint = es.story_fingerprint
FROM public.editorial_state es
WHERE ta.id = es.supabase_article_id
  AND ta.story_fingerprint IS NULL;

-- Partial index for the per-language upsert lookup.
CREATE INDEX IF NOT EXISTS team_article_fingerprint_language_idx
    ON content.team_article (story_fingerprint, language)
    WHERE story_fingerprint IS NOT NULL;
