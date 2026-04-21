# Changelog — 2026-04-21

## Summary
Replaced the on-demand Gemini image-generation tier with a curated pool of pre-generated, manually reviewed images stored in Supabase (`content.curated_images`). Added multilingual (EN + DE) article writing, source attribution, persona-based authoring, and a suite of supporting scripts for batch generation, review, and upload.

## Changes

### Image Cascade Rewrite (`app/writer/image_selector.py`)
- Removed Gemini AI generation tier entirely; deleted `GeminiImageClient` class from `image_clients.py`
- Added `curated_pool` tier: queries `content.curated_images` by `(team_code, scene)` with scene classification via `_scene_candidates()` and `_SCENE_RULES`
- New cascade order: web search (Google CC + Wikimedia) → player headshot (40% ceil cap) → curated pool (uncapped) → team logo
- `HeadshotBudget` generalised to support both curated and headshot tiers with configurable `round_up` flag
- Fixed `primary_team` resolution to prefer `article.team` for multi-team clusters (was falling back to entity list order)
- Scene selection is deterministic per story fingerprint (MD5 modulo rotation) so re-runs pick the same scene

### Multi-Language Article Writing (`app/writer/workflow.py`)
- Each story now produces two `PublishableArticle` records: `en-US` and `de-DE`
- Added `build_article_writer_agent_de` and `_write_in_language()` — image cascade runs once for EN then is shared with DE
- Added `_dedupe_sources()` — collapses `story.source_digests` into reader-visible `ArticleSource` list (deduped by URL)
- `LANGUAGES = ("en-US", "de-DE")` tuple drives the parallel write loop

### Persistence / Upsert Fix (`app/orchestration.py`, `app/adapters.py`)
- `CycleOrchestrator` now routes each language independently via `find_article_id(fingerprint, language)` — INSERT vs PATCH decision is per-(fingerprint, language), not per-story
- `editorial_state` continues to track only the EN row as the canonical reference; DE row is reachable by direct lookup
- Added `ArticleWriter.fetch_article_by_fingerprint()` and `ArticleWriter.find_article_id()` to `adapters.py`
- `adapters.py` payload now serialises `sources` (list of `ArticleSource`) and `story_fingerprint` on write

### Curated Pool Scripts (`scripts/`)
- `scripts/submit_curated_batch.py` — submit Gemini batch generation jobs; fixed response path, download URL, and 302-redirect handling
- `scripts/review_curated_pool.py` — local stdlib HTTP server for manual image review and one-click Supabase upload
- `scripts/upload_curated_pool.py` — bulk upload of reviewed PNGs from disk to `content.curated_images`
- `scripts/build_curated_pool_plan.py` — builds the generation plan JSON and taxonomy markdown

### Migrations (`supabase/migrations/`)
- `005_curated_images.sql` — `content.curated_images` table with RLS, storage bucket GRANTs, sequence GRANTs; applied
- `006_add_sources.sql` — adds `sources jsonb` column to `content.team_article`; applied
- `007_add_story_fingerprint.sql` — adds `story_fingerprint text` column to `content.team_article`; **pending**

### Schema / Config (`app/schemas.py`, `app/config.py`, `app/writer/prompts.yml`)
- Added `ArticleSource` schema (name + url)
- `PublishableArticle` gains `language`, `sources`, and `story_fingerprint` fields
- Writer prompts YAML extended with DE writer prompt and persona-selection prompt
- `app/writer/personas.py` extended with full persona definitions (byline, writing style, tone)

### Test Coverage (`tests/`)
- Rewrote `tests/test_image_selector.py` — 117 tests all pass; covers curated tier, budget enforcement, scene classification, cascade ordering
- Added `tests/test_personas.py`, `tests/test_player_enrichment.py`, `tests/test_source_attribution.py`, `tests/test_german_flow.py`, `tests/test_team_codes.py`

### Live Validation
- One full cycle ran successfully: 8 articles written (4 EN + 4 DE), cascade produced 2 curated pool hits, 1 headshot, 1 generic_nfl fallback

## Files Modified
- `app/adapters.py` — added `fetch_article_by_fingerprint`, `find_article_id`; serialise `sources` + `story_fingerprint` on write
- `app/orchestration.py` — per-language upsert routing; removed `GeminiImageClient`
- `app/schemas.py` — added `ArticleSource`, `language`/`sources`/`story_fingerprint` on `PublishableArticle`
- `app/writer/agents.py` — added `build_article_writer_agent_de`
- `app/writer/image_clients.py` — deleted `GeminiImageClient`; `ImageSelectionClient` + `WikimediaCommonsClient` unchanged
- `app/writer/image_selector.py` — full rewrite of cascade; added `_scene_candidates`, `_SCENE_RULES`, curated pool tier
- `app/writer/image_validator.py` — minor cleanups
- `app/writer/personas.py` — full persona definitions; `byline_to_persona_id`, `get_persona`
- `app/writer/prompts.py` — prompt loader extended for DE writer + persona selection
- `app/writer/prompts.yml` — DE writer prompt and persona-selection prompt added
- `app/writer/workflow.py` — multi-language write loop; `_dedupe_sources`; image shared across languages
- `run_12h_test.sh` — minor fix
- `tests/conftest.py`, `tests/test_config.py`, `tests/test_helpers.py` — updated for new schema fields
- `tests/test_image_selector.py` — full rewrite for new cascade
- `tests/test_german_flow.py`, `tests/test_personas.py`, `tests/test_player_enrichment.py`, `tests/test_source_attribution.py`, `tests/test_team_codes.py` — new test modules
- `supabase/migrations/005_curated_images.sql`, `006_add_sources.sql`, `007_add_story_fingerprint.sql` — new migrations
- `scripts/submit_curated_batch.py`, `review_curated_pool.py`, `upload_curated_pool.py`, `build_curated_pool_plan.py`, `build_6h_test_report.py` — new/updated scripts

## Code Quality Notes
- Tests: **117 passed, 0 failed, 0 skipped** (`./venv/bin/pytest tests/ -v`)
- No linting step configured for Python side (no `ruff`/`flake8` in dev deps); no debug artifacts (`print`, `pdb`, `breakpoint`) found in `app/` code
- `print()` calls in `scripts/` are intentional CLI feedback — not flagged
- CLAUDE.md image cascade table was stale (still referenced Gemini tier 3 and old tier ordering) — updated in this commit

## Open Items / Carry-over
- `supabase/migrations/007_add_story_fingerprint.sql` — **not yet applied** to production; apply via SQL Editor before next cycle run that needs `story_fingerprint` column
- 9 curated pool images rejected by Gemini safety filter during batch — can be retried in sync mode via `scripts/submit_curated_batch.py --mode sync`
- Orphaned German rows 2592/2594/2596 in `content.team_article` (from earlier test cycles before DE flow was stable) — cleanup deferred; safe to `DELETE WHERE id IN (2592, 2594, 2596)`
- Fresh Supabase project migration planning (brainstormed, not executed) — intelligence repo audit confirmed minimal dependencies: `news_extraction`, `url_content_extraction`, `knowledge_extraction`, `data_loading` (players/teams); `story_embeddings`/`story_grouping` NOT consumed by editorial cycle
- `app/writer/curated_pool_spec.py` is untracked — review whether it should be committed or added to `.gitignore`
