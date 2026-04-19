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

### Image Cascade (`writer/image_selector.py`)

Four-tier fallback, evaluated in order for each article:

| Tier | Source | Notes |
|------|--------|-------|
| 1a | Google CC Search (`image_clients.GoogleCCSearchClient`) | Skipped when a dominant player is present (headshot preferred) |
| 1b | Wikimedia Commons (`image_clients.WikimediaCommonsClient`) | Public MediaWiki API, no key. Query: player name or team code + "NFL football". `upload.wikimedia.org` requires `User-Agent` on downloads or returns 403 |
| 2 | Supabase headshot table | Single-player stories only; subject to per-cycle budget cap |
| 3 | OpenAI image generation | Prompt: wire-service photojournalism, in-game action, team full name + uniform colors injected |
| 4 | Logo reference string | Always succeeds; downstream renders team logo |

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
| Edge functions (feed, lookup) | n/a | Anon key (`SUPABASE_FUNCTION_AUTH_TOKEN`) | `RawFeedReader`, `ArticleLookupAdapter` |

`SUPABASE_FUNCTION_AUTH_TOKEN` is optional — falls back to `SUPABASE_SERVICE_ROLE_KEY` via `Settings.resolved_function_auth_token()`. But edge functions typically need the anon key (JWT with `role=anon`), not the service role key.

All migrations applied manually via SQL Editor (not `supabase db push`):
- `001_editorial_state.sql` — applied
- `002_add_source_urls.sql` — **pending**
- `003_add_author.sql` — **pending**
- `004_add_mentioned_players.sql` — **pending**

## Prompts

All prompt text lives in YAML files (`editorial/prompts.yml`, `writer/prompts.yml`), loaded once via `@lru_cache`. Each module validates required prompt keys at load time. Prompt iteration does not require changing Python code.

## Config

`app/config.py` uses pydantic-settings `BaseSettings` with `.env` file. Per-agent model names are configurable via env vars (`OPENAI_MODEL_ARTICLE_DATA_AGENT`, etc.). `agent_model(name)` resolves agent name to model string.

Tests must use `Settings(_env_file=None)` and explicitly `monkeypatch.delenv` model override vars to avoid real `.env` bleeding into test fixtures.

## Team Codes (`app/team_codes.py`)

Central map of all 32 NFL teams. Provides:
- `canonicalize_team_codes(raw: list[str]) -> list[str]` — normalizes abbreviations and common nicknames, drops unknowns, deduplicates preserving order
- `team_full_name(code: str) -> str` — e.g. `"KC"` → `"Kansas City Chiefs"` (used by image validator)
- `team_colors(code: str) -> str` — e.g. `"KC"` → `"red and gold"` (injected into AI image generation prompts)
