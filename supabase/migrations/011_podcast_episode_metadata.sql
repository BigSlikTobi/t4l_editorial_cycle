-- T4L Daily Briefing — per-episode title + summary.
--
-- Adds two nullable columns to public.podcast_episodes so each episode
-- carries its own LLM-generated title + 1-2 sentence summary derived
-- from that day's story clusters. The dispatcher reads these at deliver
-- time and sends them as `--title` and `--summary` flags to the
-- save-to-spotify CLI, giving every episode unique metadata in the
-- listener's Spotify library.
--
-- Both columns are nullable so the row's lifecycle still works pre-LLM
-- (produce can fail before metadata is generated; deliver falls back
-- to a template if the columns are NULL).

ALTER TABLE public.podcast_episodes
    ADD COLUMN IF NOT EXISTS episode_title   text,
    ADD COLUMN IF NOT EXISTS episode_summary text;
