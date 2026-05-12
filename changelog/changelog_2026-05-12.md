# Changelog — 2026-05-12

## Summary
Built and deployed a complete daily NFL podcast pipeline — from feed ingestion to multi-speaker Gemini TTS audio generation to Spotify upload — and armed a cron job on the Hostinger VPS to fire each morning at 04:00 Berlin time. The first automated run is scheduled for 2026-05-13 04:00 CET.

## Changes

### Podcast Pipeline (`app/podcast/`)
- New module: `clustering.py` — re-clusters the raw league-wide feed for breadth (separate from the editorial cycle's story-depth clustering)
- New module: `personas.py` — Marcus (EN analyst) and Robin (DE reporter) host personas with voice sketches
- New module: `prompts.py` + `prompts.yml` — EN + DE dialogue prompts; includes a "CONTROVERSY IS THE SHOW" section mandating that hosts take different positions on most clusters, with concrete conflict patterns ("I disagree", "Hold on, that's not what I saw")
- New module: `agents.py` — `dialogue_writer_agent` and `episode_metadata_agent` (LLM-generated per-episode title + 1-2 sentence summary)
- New module: `script.py` — assembles interleaved Marcus/Robin turns into a `PodcastScript`
- New module: `render.py` — per-speaker Gemini TTS rendering with body chunking at speaker boundaries and per-call 5xx/429 retries
- New module: `audio_compose.py` — single-song music pipeline: brand vocal head, volume-envelope ducking under voice, sting return, fade; EBU R128 loudnorm + alimiter + HPF post-processing via ffmpeg
- New module: `schemas.py` — `PodcastScript`, `PodcastEpisode`, `EpisodeMetadata`
- New module: `workflow.py` — state machine: cluster → script → render → compose → deliver; persists episode row to `podcast_episodes` table

### Delivery Module (`app/delivery/`)
- New module: `spotify.py` — wraps Spotify's `save-to-spotify` CLI; corrected to real CLI shape (positional file arg, `--summary` not `--description`, `--json` for parseable output, no `--token-path`)
- New module: `dispatcher.py` — language-aware dispatcher; maps BCP-47 `de-DE` → CLI `de`, passes episode metadata (title + summary) to Spotify
- New module: `schemas.py` — `DeliveryResult`

### Gemini TTS Client (`app/clients/gemini_tts.py`)
- Direct multi-speaker TTS via `google-genai` SDK — no Cloud Run worker dependency
- Per-call retries on 5xx/429; body chunking at speaker boundaries for long-form consistency

### CLI (`app/cli.py`)
- New subgroup `editorial-cycle podcast` with three commands: `produce`, `deliver`, `latest-id`

### Config (`app/config.py`)
- New podcast-related settings: `GEMINI_TTS_MODEL`, `GEMINI_API_KEY`, `PODCAST_MUSIC_PATH`, `PODCAST_OUTPUT_DIR`, `SPOTIFY_CLI_PATH`

### Supabase Migrations
- `010_podcast_episodes.sql` — new `public.podcast_episodes` audit-log table (id, run_date, language, status, audio_path, duration, error, episode_title, episode_summary)
- `011_podcast_episode_metadata.sql` — adds `episode_title` + `episode_summary` columns (applied after initial schema)

### DB Adapter (`app/adapters.py`)
- New `PodcastEpisodeWriter` adapter — INSERT/PATCH podcast episode rows with metadata

### VPS Deployment (`scripts/podcast_daily.sh`, `docs/podcast_runbook.md`)
- `podcast_daily.sh` — production cron wrapper: activates venv, runs `produce` + `deliver` for both EN and DE, logs to `/opt/t4l/logs/`
- `docs/podcast_runbook.md` — full deployment guide (deadsnakes PPA for Python, `unzip` for save-to-spotify installer, OAuth bootstrap, timezone set to Europe/Berlin, cron armed at `0 4 * * *`)

### Tests
- 19 new delivery tests (`test_delivery_spotify.py`)
- Full coverage for clustering, script composition, Gemini TTS client, audio compose pipeline, podcast adapter, workflow state machine
- Total: 385 tests passing (up from ~330)

## Files Modified
- `app/podcast/` — new module (10 files)
- `app/delivery/` — new module (3 files)
- `app/clients/gemini_tts.py` — new file
- `app/adapters.py` — added `PodcastEpisodeWriter`
- `app/cli.py` — added `podcast` subgroup
- `app/config.py` — added podcast + Gemini TTS settings
- `supabase/migrations/010_podcast_episodes.sql` — new migration
- `supabase/migrations/011_podcast_episode_metadata.sql` — new migration
- `scripts/podcast_daily.sh` — new VPS cron wrapper
- `docs/podcast_runbook.md` — new runbook
- `pyproject.toml` — added `google-genai` dependency
- `.gitignore` — added podcast output dirs

## Code Quality Notes
- Tests: **385 PASSED**, 0 failed, 0 skipped (2.29s)
- Linting: not run (no lint command configured in pyproject.toml for CI)
- No debug print statements, TODO/FIXME/HACK leftovers, or commented-out code blocks observed in changed files
- Three untracked files present in working tree (`scripts/architecture_graph_manual.yml`, `scripts/build_architecture_graph.py`, `tests/test_architecture_graph.py`) — these are not part of today's podcast work and are left uncommitted pending review

## Open Items / Carry-over
- First automated VPS cron run fires 2026-05-13 at 04:00 CET — verify success in `/opt/t4l/logs/`
- Supabase migrations `010` and `011` need to be applied manually via SQL Editor on production
- Migration `007_add_story_fingerprint.sql` remains pending (carried from prior session)
- Three untracked architecture-graph files (`scripts/build_architecture_graph.py`, `scripts/architecture_graph_manual.yml`, `tests/test_architecture_graph.py`) need review and a decision on whether to commit
- PR #28 (`feat/team-beat-async-produce-harvest-v2`) is still open against main — podcast work is stacked on top of it
