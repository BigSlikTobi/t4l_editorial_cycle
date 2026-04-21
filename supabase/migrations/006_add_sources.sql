-- Reader-visible source attribution on each published article.
--
-- Stored as a JSON array of {"name": str, "url": str} objects, deterministic
-- from the feed's source_digests (name = source_name, url = article URL).
-- Same story clustered from multiple outlets → multiple entries.
--
-- Exists separately from editorial_state.source_urls (which is internal
-- provenance, not shown to readers). This column is intended to be
-- displayed at the bottom of each article as "Sources: CBS Sports, ESPN, ..."

ALTER TABLE content.team_article
    ADD COLUMN IF NOT EXISTS sources jsonb NOT NULL DEFAULT '[]'::jsonb;
