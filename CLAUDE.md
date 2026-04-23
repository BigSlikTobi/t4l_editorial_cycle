# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install
./venv/bin/pip install -e '.[dev]'

# Run all tests
./venv/bin/pytest tests/ -v

# Run single test class or test
./venv/bin/pytest tests/test_helpers.py::TestGroupByEntity
./venv/bin/pytest tests/test_helpers.py::TestGroupByEntity::test_player_entity_clusters_across_teams

# Run the ingestion worker (populates raw_articles / entities / topics)
./venv/bin/ingestion-worker   # prints JSON summary to stdout

# Run one editorial cycle (writes articles to Supabase)
./venv/bin/editorial-cycle run --output-json var/output.json

# Build HTML cycle report (run analysis + full articles, reads var/output.json)
./venv/bin/python scripts/build_cycle_report.py

# Multi-hour integration test (12 cycles, 1/hour, logs to var/test_runs/)
nohup ./run_12h_test.sh &
```

## Architecture

Two independent processes run on separate crons and share a Supabase DB:

**Ingestion worker** (`*/30 * * * *`) chains three Google Cloud Functions to fill `public.raw_articles`:
```
ingestion/worker.py  →  IngestionSummary
  │
  ├── NewsExtractionClient     →  insert raw_articles (status: discovered)
  ├── UrlContentClient         →  fill content        (status: content_ok)
  └── KnowledgeExtractionClient → fill entities/topics (status: knowledge_ok)

public.ingestion_watermarks tracks per-source `last_fetched_at` (15-min rewind on `since`)
```

**Editorial cycle** (`0 */2 * * *`) reads from those tables and publishes articles:
```
orchestration.py  (glues phases together)
  │
  ├── editorial/workflow.py  →  CyclePublishPlan
  │     Feed fetch (from raw_articles) → URL dedup → entity clustering → Orchestrator Agent
  │
  └── writer/workflow.py     →  list[PublishableArticle]
        Parallel article generation from plan, then Supabase write + state persist
```

Modules share only `schemas.py` and `adapters.py`. No cross-module imports of internals.

Each story produces **two** `PublishableArticle` records (`en-US` + `de-DE`). The image cascade runs once for EN and the result is shared with the DE article. `orchestration.py` routes each language to INSERT or PATCH independently via `ArticleWriter.find_article_id(fingerprint, language)`. `editorial_state` tracks only the EN article id as the canonical reference.

### Image Cascade (`writer/image_selector.py`)

Four-tier fallback, evaluated in order for each article:

| Tier | Source | Notes |
|------|--------|-------|
| 1a | Google CC Search (`image_clients.GoogleCCSearchClient`) | Skipped when a dominant player is present (headshot preferred) |
| 1b | Wikimedia Commons (`image_clients.WikimediaCommonsClient`) | Public MediaWiki API, no key. Query: player name or team code + "NFL football". `upload.wikimedia.org` requires `User-Agent` on downloads or returns 403 |
| 2 | Player headshot from `public.players` | Dominant single-player stories only; subject to 40%-ceil per-cycle budget cap |
| 3 | Curated pool (`content.curated_images`) | Pre-generated + manually reviewed PNGs; scene chosen via `_scene_candidates()` + `_SCENE_RULES` (keyword-matched, then fingerprint-deterministic rotation); uncapped |
| 4 | Team logo reference string | Always succeeds; downstream renders team logo |

Gemini AI generation was removed. Tiers 1–3 produce real image URLs; tier 4 is a Flutter asset reference (`asset://team_logo/{TEAM_CODE}`). A `generic_nfl` fallback (`asset://generic/nfl`) fires when there is no `team_code`.

Image is selected once (for EN) and reused by the DE article for the same story.

`image_validator.does_image_match` accepts `expected_team_code` + `expected_team_name` and rejects: different-team-wordmark contradictions, portraits/mugshots, dated archival photos, low-quality. Ambiguity = accept; only positive contradiction = reject. OCR check (`image_contains_text`) accepts jersey/yard-line numbers but rejects words/wordmarks/scoreboards.

### Agent Chain (nested agent-tools via OpenAI Agents SDK)

```
Editorial Cycle Orchestrator  (output: CyclePublishPlan)
  └── analyze_story_cluster tool
        └── Story Cluster Agent  (output: StoryClusterResult)
              └── digest_article tool
                    └── Article Data Agent  (output: ArticleDigestAgentResult)
                          └── lookup_article_content tool  (Supabase edge function)

Article Writer Agent  (separate phase, no tools, output: PublishableArticle)
```

Nested calls use `_run_nested_agent()` in `editorial/tools.py` which passes `tool_context.context` and `tool_context.run_config` through to `Runner.run()` for trace continuity.

Note: the `lookup_article_content` tool now queries `public.raw_articles` + `public.article_entities` directly via PostgREST instead of calling a Supabase edge function.

### Ingestion Clients (`app/clients/`)

Each of the three cloud functions uses a submit→poll async pattern implemented by `AsyncCloudFunctionClient` in `base.py`:
1. POST to `submit_url` with a job payload → returns `job_id`
2. Poll `poll_url?job_id=...` at `extraction_poll_interval_seconds` (default 2s) until `status` is `completed` or `failed`, or `extraction_timeout_seconds` (default 300s) elapses

`SupabaseJobsConfig` carries the base URL + service role key and is constructed from `Settings` in the worker. `JobFailedError` and `JobTimeoutError` are the two terminal error types.

The three concrete clients (`NewsExtractionClient`, `UrlContentClient`, `KnowledgeExtractionClient`) each define their own payload and result dataclasses (`NewsItem`, `ContentResult`, `KnowledgeResult`).

The worker (`ingestion/worker.py`) drives the pipeline stage-by-stage and persists results to `public.raw_articles` via `RawArticleStore`. It is fully idempotent: re-runs pick up rows at their current status. `public.ingestion_watermarks` records `last_fetched_at` per source so the `since` parameter advances correctly across runs (with a 15-minute rewind for safety).

### Entity Clustering (`editorial/helpers.py`)

Articles are assigned to their **most specific** entity with priority: `player > game > team`. Two-pass grouping:
1. Group by best entity → split into multi-source (2+ articles) and pending singles
2. Merge singles into any existing multi-source cluster if they share ANY entity via a reverse index

This prevents the same story (e.g., a player trade) from appearing as both a cluster result and a standalone candidate. Only articles with zero entity overlap with any cluster remain as single-source candidates.

### Deterministic Dedup Layers

1. **URL dedup** — `deduplicate()` removes exact URL matches before clustering
2. **Entity clustering** — groups related articles, merges singles into existing clusters
3. **`deduplicate_plan()`** — post-orchestrator, removes stories with duplicate fingerprints (keeps highest-ranked)
4. **Cross-cycle `editorial_state`** — fingerprints from prior cycles are passed to the orchestrator as `published_fingerprints`

## Supabase

**Active project: `aiknjzinyxzhoseyxqev`** (clean v2 project; legacy project retired)

Single schema (`public`), single auth pattern:

| Table | Auth | Adapter |
|-------|------|---------|
| `raw_articles`, `article_entities`, `article_topics` | Service role key (PostgREST) | `RawArticleStore` (ingestion), `RawFeedReader` / `ArticleLookupAdapter` (editorial) |
| `ingestion_watermarks` | Service role key (PostgREST) | `RawArticleStore` |
| `editorial_state` | Service role key (PostgREST) | `EditorialStateStore` |
| `team_article` | Service role key (PostgREST) | `ArticleWriter` |
| `curated_images` | Service role key (PostgREST) | `CuratedImageStore` |

Edge functions are no longer used. `SUPABASE_FUNCTION_AUTH_TOKEN` is removed from CI.

All migrations applied manually via SQL Editor (not `supabase db push`). v2 migrations in `supabase/migrations/v2/`:
- `001_extraction_jobs.sql` — applied
- `002_reference_data.sql` — applied (placeholder; reference data loaded separately)
- `003_raw_articles.sql` — applied
- `004_article_entities.sql` — applied
- `005_article_topics.sql` — applied
- `006_editorial_state.sql` — applied
- `007_team_article.sql` — applied
- `008_curated_images.sql` — applied (214 images migrated)
- `009_article_images.sql` — applied
- `010_ingestion_watermarks.sql` — applied
- `011_raw_articles_knowledge_extracted_at_idx.sql` — applied

Reference data (`public.players`, `public.teams`, `public.games`) must be loaded once via `tackle_4_loss_intelligence/src/functions/data_loading` scripts before image tier 2 (player headshots) will function.

## Prompts

All prompt text lives in YAML files (`editorial/prompts.yml`, `writer/prompts.yml`), loaded once via `@lru_cache`. Each module validates required prompt keys at load time. Prompt iteration does not require changing Python code.

Both EN and DE `article_writer_agent` prompts include a "Beat-reporter texture" block (capped at ~15% of prose): active/specific verbs, scene/stakes language tied to source facts, sentence-length variety, and concrete nouns. The guardrail is explicit: texture rides on facts, never replaces them.

## Config

`app/config.py` uses pydantic-settings `BaseSettings` with `.env` file. Per-agent model names are configurable via env vars (`OPENAI_MODEL_ARTICLE_DATA_AGENT`, etc.). `agent_model(name)` resolves agent name to model string.

Tests must use `Settings(_env_file=None)` and explicitly `monkeypatch.delenv` model override vars to avoid real `.env` bleeding into test fixtures.

## CI / GitHub Actions

Two workflows:

**`editorial-cycle.yml`** — runs the full cycle on `0 */2 * * *` (every 2 hours), `workflow_dispatch` with `top_n` / `lookback_hours`. Concurrency group `editorial-cycle`, `cancel-in-progress: false`. Timeout: 30 minutes. `var/output.json` artifact (14d).

**`ingestion-worker.yml`** — populates `raw_articles` on `*/30 * * * *` (every 30 min), `workflow_dispatch` with `max_articles`. Concurrency group `ingestion-worker`, `cancel-in-progress: false`. Timeout: 20 minutes.

Required secrets (both workflows share the first three):
- `OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- Ingestion only: `URL_CONTENT_EXTRACTION_SUBMIT_URL`, `URL_CONTENT_EXTRACTION_POLL_URL`, `KNOWLEDGE_EXTRACTION_SUBMIT_URL`, `KNOWLEDGE_EXTRACTION_POLL_URL`
- Ingestion only (**not yet set**): `NEWS_EXTRACTION_SUBMIT_URL`, `NEWS_EXTRACTION_POLL_URL`

Optional: `IMAGE_SELECTION_URL`, `GOOGLE_CUSTOM_SEARCH_KEY`, `GOOGLE_CUSTOM_SEARCH_ENGINE_ID`. Model overrides via repo vars (`OPENAI_MODEL_*`).

Removed (no longer used): `SUPABASE_NEWS_FEED_URL`, `SUPABASE_ARTICLE_LOOKUP_URL`, `SUPABASE_FUNCTION_AUTH_TOKEN`.

## Cycle Report Script (`scripts/build_cycle_report.py`)

Reads `var/output.json` + fetches matching `content.team_article` rows by fingerprint. Writes `var/cycle_report.html` and prints the `file://` URL. Per-story card: rank/score/action/reasoning/team codes/player mentions/source digests on the left; EN+DE articles with cover image, tier badge, language badge, full content, and collapsible extras (bullets/X post/mentioned players with headshots) on the right. Header KPIs: stories/written/updated/prevented/language split/image tier mix.

## Team Codes (`app/team_codes.py`)

Central map of all 32 NFL teams. Provides:
- `canonicalize_team_codes(raw: list[str]) -> list[str]` — normalizes abbreviations and common nicknames, drops unknowns, deduplicates preserving order
- `team_full_name(code: str) -> str` — e.g. `"KC"` → `"Kansas City Chiefs"` (used by image validator)
- `team_colors(code: str) -> str` — e.g. `"KC"` → `"red and gold"` (available for prompt injection; was used by the now-removed Gemini AI generation tier)
