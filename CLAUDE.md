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

# Run one editorial cycle (writes articles to Supabase)
./venv/bin/editorial-cycle run --output-json var/output.json

# Build HTML cycle report (run analysis + full articles, reads var/output.json)
./venv/bin/python scripts/build_cycle_report.py

# Build pre-publish overview (all team_articles paired EN/DE, with trace + state, writes var/overview_report.html)
./venv/bin/python scripts/build_overview_report.py [--limit N]

# Multi-hour integration test (12 cycles, 1/hour, logs to var/test_runs/)
nohup ./run_12h_test.sh &
```

## Architecture

Hourly editorial cycle that fetches NFL news, clusters by story, ranks by news value, and writes top-N articles to Supabase. Two modules with a clean phase boundary:

```
orchestration.py  (4 lines of logic — glues phases together)
  │
  ├── editorial/workflow.py  →  CyclePublishPlan
  │     Feed fetch → URL dedup → entity clustering → Orchestrator Agent
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

Two schemas, two auth patterns:

| Table | Schema | Auth | Adapter |
|-------|--------|------|---------|
| `editorial_state` | `public` | Service role key (PostgREST) | `EditorialStateStore` |
| `team_article` | `content` | Service role key + `Content-Profile: content` header | `ArticleWriter` |

> **PostgREST routing note:** `team_article` is no longer exposed under the `content` schema via PostgREST direct queries. Read scripts (e.g. `build_overview_report.py`, `build_cycle_report.py`) reach it as `/rest/v1/team_article` with **no** `Accept-Profile` header — the public schema routing works. `ArticleWriter` (the write path) still uses the `content` schema profile header for INSERTs/PATCHes via the Supabase client library. Do not add a profile header to read-only PostgREST calls or you will get a 406.
| Edge functions (feed, lookup) | n/a | Anon key (`SUPABASE_FUNCTION_AUTH_TOKEN`) | `RawFeedReader`, `ArticleLookupAdapter` |

`SUPABASE_FUNCTION_AUTH_TOKEN` is optional — falls back to `SUPABASE_SERVICE_ROLE_KEY` via `Settings.resolved_function_auth_token()`. But edge functions typically need the anon key (JWT with `role=anon`), not the service role key.

### Frontend Edge Functions (`supabase/functions/`)

Two Deno edge functions expose `content.team_article` to the Flutter frontend. Both require `verify_jwt = true` — callers must pass a valid Supabase anon JWT as `Authorization: Bearer <token>`.

| Function | Method | Purpose |
|----------|--------|---------|
| `get-articles` | POST | Cursor-paginated list of articles. Body: `{ language?, limit?, cursor? }`. Returns `{ items, next_cursor }`. Cursor on `(created_at DESC, id DESC)`. List columns only (no body/bullets). |
| `get-article-detail` | POST | Full article + enriched player data. Body: `{ id }`. Replaces raw `mentioned_players` ID array with `{ player_id, display_name, headshot }` objects via `public.players` join. |

Shared helpers live in `supabase/functions/_shared/`:
- `cors.ts` — CORS headers, `jsonResponse()`, `preflight()` (handles OPTIONS).
- `supabase.ts` — `clientFromRequest(req)` factory: reads `SUPABASE_URL` + `SUPABASE_ANON_KEY` from env, forwards caller's `Authorization` header, `persistSession: false`.

> **RLS note:** `content.team_article` currently has no SELECT grant for the `anon` role. Both functions return 200 with empty results until `GRANT SELECT ON content.team_article TO anon;` (or an equivalent RLS policy) is applied in Supabase SQL Editor.

All migrations applied manually via SQL Editor (not `supabase db push`):
- `001_editorial_state.sql` — applied
- `002_add_source_urls.sql` — applied
- `003_add_author.sql` — applied
- `004_add_mentioned_players.sql` — applied
- `005_curated_images.sql` — applied (`content.curated_images` table + storage GRANTs)
- `006_add_sources.sql` — applied (`sources jsonb` on `content.team_article`)
- `007_add_story_fingerprint.sql` — **pending** (`story_fingerprint text` on `content.team_article`)

## Prompts

All prompt text lives in YAML files (`editorial/prompts.yml`, `writer/prompts.yml`), loaded once via `@lru_cache`. Each module validates required prompt keys at load time. Prompt iteration does not require changing Python code.

Both EN and DE `article_writer_agent` prompts include a "Beat-reporter texture" block (capped at ~15% of prose): active/specific verbs, scene/stakes language tied to source facts, sentence-length variety, and concrete nouns. The guardrail is explicit: texture rides on facts, never replaces them.

Both `article_writer_agent` prompts (EN + DE) also include a "Source-as-story anti-pattern" block: the publication of a source article is never the story. For roundups, rankings, and grade columns the writer must focus on THIS team's specific row (grade, rank, pick, named player), and is forbidden from sentences whose subject is the source publication or the act of publishing. A per-paragraph self-check is mandated: "what new football fact did this give the reader?"

The `article_data_agent` prompt includes a "Roundup / ranking / grade extraction rule": for multi-team source pieces it must extract row-level verdicts as `key_facts`, not column-level meta. If only column-level meta is available, `content_status="thin"` + `confidence <= 0.3` is required, routing downstream to the writer's "thin → write shorter" path. The two prompts interlock.

## Config

`app/config.py` uses pydantic-settings `BaseSettings` with `.env` file. Per-agent model names are configurable via env vars (`OPENAI_MODEL_ARTICLE_DATA_AGENT`, etc.). `agent_model(name)` resolves agent name to model string.

Tests must use `Settings(_env_file=None)` and explicitly `monkeypatch.delenv` model override vars to avoid real `.env` bleeding into test fixtures.

## CI / GitHub Actions

`.github/workflows/editorial-cycle.yml` runs the full cycle on a `0 */2 * * *` schedule (every 2 hours) and supports `workflow_dispatch` with `top_n` / `lookback_hours` overrides. Concurrency group `editorial-cycle` with `cancel-in-progress: false` prevents overlapping runs. Timeout: 30 minutes. `var/output.json` is uploaded as an artifact (14d retention).

Required secrets: `OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_NEWS_FEED_URL`, `SUPABASE_ARTICLE_LOOKUP_URL`, `SUPABASE_FUNCTION_AUTH_TOKEN`, `EXTRACTION_FUNCTION_AUTH_TOKEN`. Optional: `IMAGE_SELECTION_URL`, `GOOGLE_CUSTOM_SEARCH_KEY`, `GOOGLE_CUSTOM_SEARCH_ENGINE_ID`. Model overrides via repo vars (`OPENAI_MODEL_*`).

`EXTRACTION_FUNCTION_AUTH_TOKEN` authenticates calls to the knowledge-extraction Cloud Run functions (`AsyncJobClient` sends it as `Authorization: Bearer <token>`). The ingestion worker fails fast at startup if this secret is unset. Rotation procedure: `docs/rotating-extraction-function-auth-token.md`.

## Cycle Report Script (`scripts/build_cycle_report.py`)

Reads `var/output.json` + fetches matching `content.team_article` rows by fingerprint. Writes `var/cycle_report.html` and prints the `file://` URL. Per-story card: rank/score/action/reasoning/team codes/player mentions/source digests on the left; EN+DE articles with cover image, tier badge, language badge, full content, and collapsible extras (bullets/X post/mentioned players with headshots) on the right. Header KPIs: stories/written/updated/prevented/language split/image tier mix.

## Team Codes (`app/team_codes.py`)

Central map of all 32 NFL teams. Provides:
- `canonicalize_team_codes(raw: list[str]) -> list[str]` — normalizes abbreviations and common nicknames, drops unknowns, deduplicates preserving order
- `team_full_name(code: str) -> str` — e.g. `"KC"` → `"Kansas City Chiefs"` (used by image validator)
- `team_colors(code: str) -> str` — e.g. `"KC"` → `"red and gold"` (available for prompt injection; was used by the now-removed Gemini AI generation tier)
