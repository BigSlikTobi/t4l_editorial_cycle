# T4L Editorial Cycle

Hourly NFL editorial pipeline for Tackle4Loss. Fetches news from a Supabase
edge-function feed, clusters by story, ranks by news value, runs each draft
through an LLM editorial quality gate, and writes the top‑N stories to
Supabase as paired EN + DE articles.

Built on the OpenAI Agents SDK with nested agent‑tools, deterministic
dedup layers, a four‑tier image cascade, and a markdown‑based editorial
memory wiki that learns from the quality gate's feedback.

For Claude Code's working notes (commands, conventions, gotchas), see
[`CLAUDE.md`](./CLAUDE.md). This README is the canonical project overview.

---

## Table of contents

1. [Quick start](#quick-start)
2. [Pipeline overview](#pipeline-overview)
3. [Module layout](#module-layout)
4. [Agent chain](#agent-chain)
5. [Entity clustering & dedup](#entity-clustering--dedup)
6. [Editorial quality gate](#editorial-quality-gate)
7. [Editorial memory wiki](#editorial-memory-wiki)
8. [Image cascade](#image-cascade)
9. [Supabase](#supabase)
10. [Frontend edge functions](#frontend-edge-functions)
11. [Configuration](#configuration)
12. [Prompts](#prompts)
13. [CI / GitHub Actions](#ci--github-actions)
14. [Reports & tooling](#reports--tooling)
15. [Testing](#testing)

---

## Quick start

```bash
# Install (editable + dev extras)
./venv/bin/pip install -e '.[dev]'

# Run a single editorial cycle (writes articles to Supabase)
./venv/bin/editorial-cycle run --output-json var/output.json

# Run all tests
./venv/bin/pytest tests/ -v

# Build the post-cycle HTML report (reads var/output.json)
./venv/bin/python scripts/build_cycle_report.py

# Build the pre-publish overview report (paired EN/DE, with state + trace)
./venv/bin/python scripts/build_overview_report.py [--limit N]

# 12-hour soak test (1 cycle/hour, logs to var/test_runs/)
nohup ./run_12h_test.sh &
```

Required env (see [Configuration](#configuration) for the full list):
`OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_NEWS_FEED_URL`, `SUPABASE_ARTICLE_LOOKUP_URL`,
`EXTRACTION_FUNCTION_AUTH_TOKEN`.

---

## Pipeline overview

```
feed (Supabase edge fn)
  → URL dedup
  → entity clustering (player > game > team)
  → orchestrator agent: cluster scoring + ranking + dedup vs. published_fingerprints
  → for each top-N story:
       EN writer ──► quality gate ──► (approve | rewrite once | dismiss)
                          │
                          ├─► image cascade (once per story; result reused for DE)
                          └─► DE writer
                          → write to Supabase content.team_article (INSERT or PATCH)
                          → persist editorial_state (cross-cycle memory)
```

Cycle runs every 2 hours via GitHub Actions. Each story produces **two**
`PublishableArticle` records (`en-US` + `de-DE`); the image cascade runs
once for EN and the resulting URL/asset is reused by the DE article.

The pipeline is split into two modules with a clean phase boundary —
`editorial/` produces a `CyclePublishPlan`, `writer/` consumes it and
emits articles. Modules share only `app/schemas.py` and `app/adapters.py`.

---

## Module layout

```
app/
├── orchestration.py        # 4 lines of glue between editorial → writer
├── editorial/
│   ├── workflow.py         # feed → dedup → cluster → orchestrator
│   ├── helpers.py          # entity clustering, post-orchestrator dedup
│   ├── tools.py            # nested agent-tools (analyze_story_cluster, digest_article)
│   ├── tracing.py          # OpenAI Agents run_config + trace metadata
│   └── prompts.yml         # all editorial-side prompts
├── writer/
│   ├── workflow.py         # parallel article generation, quality gate, memory
│   ├── agents.py           # writer, quality gate, editorial memory agent
│   ├── editorial_memory.py # markdown read/write helpers
│   ├── image_selector.py   # 4-tier image cascade
│   ├── image_clients.py    # Google CC, Wikimedia, headshot, curated pool clients
│   ├── image_validator.py  # vision-model contradiction check
│   ├── persona_selector.py # picks one of 3 bylines per story
│   ├── personas.py         # analyst / insider / columnist definitions
│   └── prompts.yml         # writer + gate + memory prompts
├── ingestion/              # ingestion worker (knowledge extraction jobs)
├── adapters.py             # Supabase clients (state, articles, feed, lookup)
├── schemas.py              # all Pydantic models
├── team_codes.py           # canonicalize_team_codes, team_full_name, team_colors
├── config.py               # pydantic-settings BaseSettings
└── cli.py                  # `editorial-cycle` typer entry point

editorial_memory/
├── wiki/                   # coaching markdown (LLM-maintained + hand-seeded)
└── raw_feedback/           # append-only daily event log

scripts/
├── build_cycle_report.py       # post-cycle HTML report
├── build_overview_report.py    # all team_articles, paired EN/DE
└── build_architecture_graph.py # generates the architecture graph

tests/                      # 191+ tests (unit + integration)
.github/workflows/          # editorial-cycle (every 2h) + ingestion-worker
supabase/functions/         # Deno edge functions for the Flutter frontend
```

---

## Agent chain

Five agents wired through the OpenAI Agents SDK. Nested agent‑tools share
trace context via `_run_nested_agent()` in `editorial/tools.py`, which
forwards `tool_context.context` and `tool_context.run_config` to
`Runner.run()`.

```
Editorial Cycle Orchestrator        (output: CyclePublishPlan)
  └── analyze_story_cluster tool
        └── Story Cluster Agent     (output: StoryClusterResult)
              └── digest_article tool
                    └── Article Data Agent  (output: ArticleDigestAgentResult)
                          └── lookup_article_content tool  (Supabase edge fn)

Article Writer Agent (EN)           (no tools, output: PublishableArticle)
Article Writer Agent (DE)           (no tools, output: PublishableArticle)
Article Quality Gate Agent          (no tools, output: ArticleQualityDecision)
Editorial Memory Agent              (no tools, output: EditorialMemoryRevision)
Persona Selector Agent              (no tools, output: PersonaSelection)
```

Per‑agent model IDs are configurable via `OPENAI_MODEL_*` env vars and
resolved by `Settings.agent_model(name)`.

---

## Entity clustering & dedup

Articles are assigned to their **most specific** entity:
`player > game > team`. Two‑pass grouping in
`editorial/helpers.py`:

1. Group by best entity → split into multi‑source (2+ articles) and
   pending singles.
2. Merge singles into any existing multi‑source cluster if they share
   ANY entity via a reverse index.

This prevents the same story (e.g., a player trade) from appearing as
both a cluster result and a standalone candidate. Only articles with zero
entity overlap with any cluster remain as single‑source candidates.

### Four deterministic dedup layers

| Layer | Where | What it does |
|---|---|---|
| URL dedup | `deduplicate()` pre‑clustering | Removes exact URL matches |
| Entity clustering | `helpers.py` | Groups related articles, merges singles |
| `deduplicate_plan()` | Post‑orchestrator | Removes stories with duplicate fingerprints (keeps highest‑ranked) |
| Cross‑cycle | `editorial_state` (Supabase) | Prior fingerprints passed to orchestrator as `published_fingerprints` |

Story fingerprints are deterministic hashes; the agent's emitted
fingerprint string is used only for logs. INSERT vs PATCH is decided per
language via `ArticleWriter.find_article_id(fingerprint, language)`.
`editorial_state` tracks only the EN article id as canonical reference.

---

## Editorial quality gate

An LLM editorial reviewer runs **after EN drafting and before image/DE
generation**. Implemented in `WriterWorkflow._run_quality_gate()`.

The gate scores each draft on five dimensions (each 0.0–1.0):
**impact**, **specificity**, **readworthiness**, **grounding**,
**execution**. It returns one of three decisions:

| Decision | Meaning | What happens |
|---|---|---|
| `approve` | Grounded, specific, clear reader payoff | Continue to image + DE |
| `rewrite` | Source has substance, draft wastes it (vague angle, weak headline, buried stakes) | Writer reruns once with a `rewrite_brief` |
| `dismiss` | Source itself can't support a worthwhile article | Story drops out for this cycle (no EN, no DE, no image) |

After a rewrite, the gate runs again. A second `rewrite` or `dismiss`
drops the story — we don't loop.

### Fail‑soft on outage

If the gate raises (OpenAI hiccup, timeout, etc.):

- **First attempt** → fall back to **approve** the original draft. Logged
  at `ERROR` with the prefix `QUALITY_GATE_OUTAGE` for log filtering. The
  writer prompt's own discipline rules already apply.
- **Rewrite attempt** → fall back to **dismiss**. We won't publish an
  unreviewed second draft.

This is a deliberate trade‑off: a sustained gate outage publishes
unreviewed drafts rather than producing a silent zero‑publish window.

### Prompt precedence on rewrites of `patch` actions

When a "patch" action (updating an existing article) gets rewritten, the
writer payload contains both `existing_article` and
`quality_gate_feedback`. The writer prompt explicitly states
`quality_gate_feedback` wins — don't preserve weak phrasing from the
existing article just because it was published before.

---

## Editorial memory wiki

A markdown coaching wiki that feeds the EN writer and learns from
quality‑gate feedback. **Coaching, not facts** — `source_digests` remain
the only authority for football claims.

### Layout

```
editorial_memory/
├── wiki/
│   ├── what_makes_a_story_readworthy.md   ← seed, hand‑edited
│   ├── headline_patterns_that_work.md     ← seed, hand‑edited
│   ├── thin_story_rejection_rules.md      ← seed, hand‑edited
│   └── rewrite_lessons.md                 ← LLM‑maintained
└── raw_feedback/
    └── YYYY-MM-DD.md                      ← append‑only event log
```

### Read path (every EN draft)

`load_editorial_memory()` concatenates the wiki pages (capped at ~7000
chars total) and injects them into the writer payload as
`editorial_memory`. The writer prompt is explicit: this is coaching, not
facts; `source_digests` win on conflict.

### Write path (after each gate decision)

When the gate returns `rewrite` or `dismiss` (clean first‑attempt
approves are skipped):

1. The event is appended to `raw_feedback/YYYY-MM-DD.md`.
2. The **editorial memory agent** reads the existing
   `wiki/rewrite_lessons.md` plus the new event and rewrites the page in
   place. Instructed to merge duplicate lessons and keep ~8–14 high‑signal
   bullets, under 1200 words.

Only `rewrite_lessons.md` is auto‑updated. The other three wiki pages are
hand‑edited seeds.

### Why memory lives on its own branch

To allow branch protection on `main` and to keep main's history clean,
the workflow stores memory on a dedicated `editorial-memory` data
branch:

1. **Cycle start**: fetches `editorial_memory/` from `editorial-memory`
   and overlays it on the working tree.
2. **Cycle end**: switches to `editorial-memory` (creating it as an orphan
   branch on first run), applies the updated `editorial_memory/`, and
   pushes there — never to main.

`main` holds **code** (including the wiki seed files); `editorial-memory`
holds **runtime data**. The seeds on main are only used until the data
branch exists; after that, the data branch is the source of truth.

To inspect live memory:
```bash
git fetch
git checkout origin/editorial-memory -- editorial_memory/
```

To pause learning, comment out the "Persist editorial memory" step in
`.github/workflows/editorial-cycle.yml`.

### Comparison to Karpathy's LLM wiki pattern

Same family of idea (LLM‑maintained markdown, append‑only log + curated
summary), much narrower scope. Karpathy's wiki encodes **knowledge** with
cross‑references and lint passes; ours encodes editorial **taste** in a
single curated page. Natural growth path: let the agent update any wiki
page, add an `index.md`, run a periodic lint cycle.

---

## Image cascade

Four‑tier fallback in `writer/image_selector.py`, evaluated in order per
article:

| Tier | Source | Notes |
|---|---|---|
| 1a | Google CC Search (`GoogleCCSearchClient`) | Skipped when a dominant player is present (headshot preferred) |
| 1b | Wikimedia Commons (`WikimediaCommonsClient`) | Public MediaWiki API, no key. Query: player name or `team_code + "NFL football"`. `upload.wikimedia.org` requires `User-Agent` on downloads. |
| 2 | Player headshot from `public.players` | Dominant single‑player stories only. Subject to a 40%‑ceil per‑cycle budget cap. |
| 3 | Curated pool (`content.curated_images`) | Pre‑generated + manually reviewed PNGs. Scene chosen via `_scene_candidates()` + `_SCENE_RULES` (keyword‑matched, then fingerprint‑deterministic rotation). Uncapped. |
| 4 | Team logo reference string | Always succeeds; downstream renders team logo. |

Tiers 1–3 produce real image URLs; tier 4 is a Flutter asset reference
(`asset://team_logo/{TEAM_CODE}`). A `generic_nfl` fallback
(`asset://generic/nfl`) fires when there's no `team_code`. Gemini AI
generation was removed in an earlier iteration.

### Image validator

`image_validator.does_image_match` accepts `expected_team_code` +
`expected_team_name` and **rejects only on positive contradiction**:
different‑team wordmarks, portraits/mugshots, dated archival photos, low
quality. Ambiguity = accept. The OCR check (`image_contains_text`)
accepts jersey/yard‑line numbers but rejects words/wordmarks/scoreboards.

---

## Supabase

Two schemas, two auth patterns:

| Table | Schema | Auth | Adapter |
|---|---|---|---|
| `editorial_state` | `public` | Service role key (PostgREST) | `EditorialStateStore` |
| `team_article` | `content` | Service role key + `Content-Profile: content` | `ArticleWriter` |
| Edge fns (feed, lookup) | n/a | Anon key (`SUPABASE_FUNCTION_AUTH_TOKEN`) | `RawFeedReader`, `ArticleLookupAdapter` |

> **PostgREST routing note**: `team_article` is no longer exposed under
> the `content` schema via direct queries. Read scripts hit
> `/rest/v1/team_article` with **no** `Accept-Profile` header (public
> schema routing). `ArticleWriter` (the write path) still uses the
> `content` profile header for INSERT/PATCH via the Supabase client.
> Don't add a profile header to read‑only PostgREST calls or you'll get a
> 406.

`SUPABASE_FUNCTION_AUTH_TOKEN` is optional — falls back to
`SUPABASE_SERVICE_ROLE_KEY` via
`Settings.resolved_function_auth_token()`. Edge functions typically need
the anon key (JWT with `role=anon`), not the service role key.

### Migrations

All migrations applied **manually via Supabase SQL Editor** (not
`supabase db push`):

- `001_editorial_state.sql` — applied
- `002_add_source_urls.sql` — applied
- `003_add_author.sql` — applied
- `004_add_mentioned_players.sql` — applied
- `005_curated_images.sql` — applied (`content.curated_images` + storage GRANTs)
- `006_add_sources.sql` — applied (`sources jsonb` on `content.team_article`)
- `007_add_story_fingerprint.sql` — **pending** (`story_fingerprint text` on `content.team_article`)

---

## Frontend edge functions

Two Deno edge functions in `supabase/functions/` expose
`content.team_article` to the Flutter frontend. Both require
`verify_jwt = true` — callers must pass a Supabase anon JWT as
`Authorization: Bearer <token>`.

| Function | Method | Purpose |
|---|---|---|
| `get-articles` | POST | Cursor‑paginated list. Body: `{ language?, limit?, cursor? }`. Returns `{ items, next_cursor }`. Cursor on `(created_at DESC, id DESC)`. List columns only. |
| `get-article-detail` | POST | Full article + enriched players. Body: `{ id }`. Replaces raw `mentioned_players` ID array with `{ player_id, display_name, headshot }` via `public.players` join. |

Shared helpers in `supabase/functions/_shared/`:

- `cors.ts` — CORS headers, `jsonResponse()`, `preflight()` (OPTIONS).
- `supabase.ts` — `clientFromRequest(req)` factory: reads `SUPABASE_URL`
  + `SUPABASE_ANON_KEY` from env, forwards caller's `Authorization`,
  `persistSession: false`.

> **RLS note**: `content.team_article` currently has no SELECT grant for
> the `anon` role. Both functions return 200 with empty results until
> `GRANT SELECT ON content.team_article TO anon;` (or an equivalent RLS
> policy) is applied.

---

## Configuration

`app/config.py` uses `pydantic-settings` with a `.env` file. Per‑agent
model IDs are configurable via env vars:

| Env var | Default | Agent |
|---|---|---|
| `OPENAI_MODEL_ARTICLE_DATA_AGENT` | `gpt-5.4-mini` | Article digest |
| `OPENAI_MODEL_STORY_CLUSTER_AGENT` | `gpt-5.4` | Cluster synthesis |
| `OPENAI_MODEL_EDITORIAL_ORCHESTRATOR_AGENT` | `gpt-5.4` | Top‑N selection |
| `OPENAI_MODEL_ARTICLE_WRITER_AGENT` | `gpt-5.4-mini` | EN + DE drafting |
| `OPENAI_MODEL_PERSONA_SELECTOR_AGENT` | `gpt-5.4-mini` | Persona pick |
| `OPENAI_MODEL_ARTICLE_QUALITY_GATE_AGENT` | `gpt-5.4-mini` | Quality gate |
| `OPENAI_MODEL_EDITORIAL_MEMORY_AGENT` | `gpt-5.4-mini` | Memory revision |
| `OPENAI_MODEL_VISION_VALIDATOR` | (provider default) | Image contradiction check |

`agent_model(name)` resolves an agent name to its model string.
`editorial_memory_dir` defaults to `Path("editorial_memory")` (relative).

> **Tests must use** `Settings(_env_file=None)` and explicitly
> `monkeypatch.delenv` model override vars to avoid real `.env` bleeding
> into fixtures.

---

## Prompts

All prompt text lives in YAML (`app/editorial/prompts.yml`,
`app/writer/prompts.yml`), loaded once via `@lru_cache`. Each module
validates required prompt keys at load time. Prompt iteration does not
require Python changes.

Notable prompt blocks worth knowing:

- **Beat‑reporter texture** (writer EN + DE): cap ~15% of prose.
  Active/specific verbs, scene/stakes language tied to source facts,
  varied sentence length, concrete nouns. Texture rides on facts, never
  replaces them.
- **Source‑as‑story anti‑pattern** (writer EN + DE): the publication of
  a source article is never the story. For roundups/rankings/grades the
  writer must focus on *this* team's row, and is forbidden from sentences
  whose subject is the source publication or the act of publishing. A
  per‑paragraph self‑check: "what new football fact did this give the
  reader?"
- **Roundup / ranking / grade extraction rule** (article data agent):
  for multi‑team source pieces, extract row‑level verdicts as
  `key_facts`, not column‑level meta. Column‑level only →
  `content_status="thin"` + `confidence <= 0.3`, routing to the writer's
  "thin → write shorter" path.
- **Reader contract / angle discipline / specificity checks** (writer):
  added in PR #22 to push drafts toward concrete reader payoff over
  generic team chatter.
- **Editorial taste test / single‑source scoring guide** (orchestrator):
  added in PR #22 to suppress vague single‑source items unless their
  title carries concrete news.

---

## CI / GitHub Actions

`.github/workflows/editorial-cycle.yml`:

- **Schedule**: `0 */2 * * *` (every 2 hours).
- **Manual**: `workflow_dispatch` with `top_n` / `lookback_hours`
  overrides.
- **Concurrency**: group `editorial-cycle`, `cancel-in-progress: false` —
  no overlapping runs.
- **Timeout**: 30 minutes.
- **Steps**:
  1. Checkout main.
  2. Sync `editorial_memory/` from the `editorial-memory` data branch
     (creates‑on‑first‑run via the persist step).
  3. Install + run cycle.
  4. Persist memory: snapshot `editorial_memory/`, switch to the
     `editorial-memory` branch (orphan if missing), apply snapshot, push.
  5. Upload `var/output.json` as an artifact (14d retention).

Required secrets: `OPENAI_API_KEY`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_NEWS_FEED_URL`,
`SUPABASE_ARTICLE_LOOKUP_URL`, `SUPABASE_FUNCTION_AUTH_TOKEN`,
`EXTRACTION_FUNCTION_AUTH_TOKEN`. Optional: `IMAGE_SELECTION_URL`,
`GOOGLE_CUSTOM_SEARCH_KEY`, `GOOGLE_CUSTOM_SEARCH_ENGINE_ID`. Model
overrides via repo vars (`OPENAI_MODEL_*`).

`EXTRACTION_FUNCTION_AUTH_TOKEN` authenticates calls to the
knowledge‑extraction Cloud Run functions (`AsyncJobClient` sends it as
`Authorization: Bearer <token>`). The ingestion worker fails fast at
startup if this secret is unset. Rotation procedure:
[`docs/rotating-extraction-function-auth-token.md`](./docs/rotating-extraction-function-auth-token.md).

---

## Reports & tooling

### Cycle report (`scripts/build_cycle_report.py`)

Reads `var/output.json` + fetches matching `content.team_article` rows by
fingerprint. Writes `var/cycle_report.html` and prints a `file://` URL.
Per‑story card:

- **Left**: rank, score, action, reasoning, team codes, player mentions,
  source digests.
- **Right**: EN + DE articles with cover image, tier badge, language
  badge, full content, collapsible extras (bullets, X post, mentioned
  players with headshots).
- **Header KPIs**: stories / written / updated / prevented, language
  split, image tier mix.

### Overview report (`scripts/build_overview_report.py`)

All `team_article` rows paired EN/DE, with trace + state. Writes
`var/overview_report.html`. Optional `--limit N`.

### Architecture graph (`scripts/build_architecture_graph.py`)

Generates a manual architecture YAML/graph snapshot in `scripts/`. See
the script for usage.

---

## Testing

```bash
# All tests (191+ at last count)
./venv/bin/pytest tests/ -v

# Single test class or test
./venv/bin/pytest tests/test_helpers.py::TestGroupByEntity
./venv/bin/pytest tests/test_helpers.py::TestGroupByEntity::test_player_entity_clusters_across_teams
```

Test conventions:

- `Settings(_env_file=None)` to avoid `.env` bleed.
- Explicit `monkeypatch.delenv` for any `OPENAI_MODEL_*` override that
  could affect the test.
- Async tests use `pytest-asyncio` in auto mode (set in
  `pyproject.toml`).

Suites worth knowing:

- `test_article_quality_gate.py` — approve / dismiss / rewrite‑then‑approve
  / rewrite‑then‑dismiss / fail‑soft / fail‑closed‑on‑rewrite.
- `test_editorial_memory.py` — wiki read path, raw feedback append, memory
  injection into writer payload, memory agent revision.
- `test_helpers.py` — entity clustering edge cases.
- `test_schemas.py` — Pydantic model `extra="forbid"` and bounds.

---

## Pointers for future work

| Area | File |
|---|---|
| Add a wiki page the memory agent can update | `app/writer/editorial_memory.py`, `app/writer/prompts.yml` |
| Tune the quality‑gate scoring rubric | `app/writer/prompts.yml` (`article_quality_gate_agent`) |
| Add a new image source tier | `app/writer/image_selector.py`, `app/writer/image_clients.py` |
| Add a new agent | `app/writer/agents.py` or `app/editorial/` + register in `Settings.agent_models` |
| Change cycle cadence | `.github/workflows/editorial-cycle.yml` (`schedule.cron`) and `LOOKBACK_HOURS` |
