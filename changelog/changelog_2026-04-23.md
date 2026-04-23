# Changelog — 2026-04-23

## Summary
Replaced the legacy Supabase-edge-function feed with a first-party ingestion pipeline that chains three Google Cloud Functions (news extraction → URL content extraction → knowledge extraction), backed by a clean new Supabase project (aiknjzinyxzhoseyxqev) with per-source watermarks and DB-backed editorial adapters. 150 tests passing; PR #3 open on `backend-rebuild-ingestion-watermarks`.

## Changes

### Ingestion Pipeline (new)
- `app/ingestion/worker.py` — `run_ingestion_cycle()` drives the three-stage pipeline; idempotent and crash-safe (reruns pick up at current DB state)
- `app/ingestion/store.py` — `RawArticleStore`: PostgREST wrapper for `public.raw_articles`, `public.article_entities`, `public.article_topics`; 4-state machine (`discovered → content_ok → knowledge_ok / failed`)
- `app/ingestion/cli.py` — `ingestion-worker` binary entry point; writes JSON summary to stdout
- `app/clients/` (new package):
  - `base.py` — `AsyncCloudFunctionClient` base: submit→poll loop with configurable timeout/interval, `SupabaseJobsConfig`, `JobFailedError`, `JobTimeoutError`
  - `news_extraction.py` — `NewsExtractionClient` + `NewsItem`
  - `url_content.py` — `UrlContentClient` + `ContentResult`
  - `knowledge_extraction.py` — `KnowledgeExtractionClient` + `KnowledgeResult`

### Supabase Schema (new clean project)
11 migrations in `supabase/migrations/v2/` (all applied to project aiknjzinyxzhoseyxqev):
- `001` extraction_jobs job queue
- `002` reference data placeholder (public.players, teams, games via separate data_loading CF)
- `003` raw_articles
- `004` article_entities
- `005` article_topics
- `006` editorial_state
- `007` team_article (replaces `content.team_article` — all tables now in `public`)
- `008` curated_images (migrated 214 images from legacy project)
- `009` article_images
- `010` ingestion_watermarks (per-source, 15-min rewind on `since` per run)
- `011` raw_articles knowledge_extracted_at index

### GitHub Actions
- `.github/workflows/ingestion-worker.yml` — new workflow on `*/30 * * * *` cron (every 30 min), `workflow_dispatch` with `max_articles` override, 20-min timeout; chains all three CF URLs
- `.github/workflows/editorial-cycle.yml` — removed legacy `SUPABASE_NEWS_FEED_URL` / `SUPABASE_ARTICLE_LOOKUP_URL` / `SUPABASE_FUNCTION_AUTH_TOKEN` secrets (now unused)

### Adapters and Config
- `app/adapters.py` — `RawFeedReader` and `ArticleLookupAdapter` now read from `public.raw_articles` / `public.article_entities` via PostgREST instead of Supabase edge functions; `EditorialStateStore` and `ArticleWriter` updated to new schema (`public` only, no `content.*` prefix)
- `app/config.py` — 6 new optional URL fields for the three CF submit/poll pairs; `field_validator` treats empty strings as `None` so unset GitHub secrets (`""`) don't fail URL parsing
- `.env.example` updated with all new fields

### Tests (new)
- `tests/test_clients_base.py` — 132 lines covering `AsyncCloudFunctionClient` submit/poll logic
- `tests/test_clients_news.py` — `NewsExtractionClient` unit tests
- `tests/test_ingestion_store.py` — `RawArticleStore` status-machine tests
- `tests/test_ingestion_worker.py` — 390 lines covering the full three-stage pipeline, including partial failures and watermark persistence
- `tests/test_db_adapters.py` — 158 lines covering updated `RawFeedReader` and `ArticleLookupAdapter`

### Migration Script
- `scripts/migrate_curated_images.py` — one-shot script to copy 214 curated images from legacy project storage + DB to new project

### Codex Review Fixes (commits 2 and 3)
- Hardened client poll loop edge cases
- Added missing test coverage for timeout/failure paths
- Config empty-string fix (`field_validator` guards on 6 optional URL fields + 2 secret fields)

## Files Modified
- `app/config.py` — 6 new CF URL settings + empty-string field validators
- `app/adapters.py` — DB-backed feed/lookup adapters replacing edge-function calls
- `app/orchestration.py` — wired to updated adapters
- `app/editorial/tools.py` — minor adapter interface update
- `app/editorial/workflow.py` — minor adapter interface update
- `app/writer/image_selector.py` — minor cleanup
- `pyproject.toml` — `ingestion-worker` CLI entry point registered
- `.env.example` — new CF URL + secret fields documented
- `.github/workflows/editorial-cycle.yml` — removed 3 legacy secrets
- `.github/workflows/ingestion-worker.yml` — new file

## Code Quality Notes
- **Tests: 150 passed, 0 failed, 0 errors** (0.81s)
- **Linting: not run** (no lint command configured; UI not changed)
- No TODO/FIXME/HACK markers in any modified file
- The `print()` in `app/ingestion/cli.py:25` is intentional — it is the structured JSON output of the `ingestion-worker` CLI binary, not a debug statement

## Open Items / Carry-over
- **PR #3** (`backend-rebuild-ingestion-watermarks`) is open against `main` and awaiting review/merge
- **Two GitHub secrets still need to be set** before the ingestion-worker workflow will run successfully:
  - `NEWS_EXTRACTION_SUBMIT_URL`
  - `NEWS_EXTRACTION_POLL_URL`
  (The other four CF URL secrets and the three core secrets are already set)
- Migration `supabase/migrations/v1/007_add_story_fingerprint.sql` was listed as **pending** in CLAUDE.md — this is obsolete now that the v2 schema is the live target; can be cleaned up post-merge
- Reference data loader for new project (`public.players`, `public.teams`, `public.games`) needs to be run once from `tackle_4_loss_intelligence/src/functions/data_loading` before image tier 2 (player headshots) will work
- 214 curated images migrated via `scripts/migrate_curated_images.py`; verify storage bucket permissions in new project after merge
