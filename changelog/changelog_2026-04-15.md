# Changelog — 2026-04-15

## Summary
Built the T4L Editorial Cycle Agent from scratch — a standalone agentic NFL news system that replaces n8n workflows. The project went from idea discovery through architecture, MVP scoping, full implementation, and successful production testing in a single session.

## Changes

### Idea & Architecture
- Defined the core problem: n8n treating every article as an independent event, producing duplicate stateless content with no cross-cycle memory
- Designed the two-module architecture: `editorial/` (clustering + ranking) and `writer/` (article generation), with a clean phase boundary between them
- Decided to make the system standalone — decoupled repo, its own config, adapters, and deployment, following t4l_radio_agency patterns

### Product Scoping (KANO + MVP)
- KANO analysis produced three dev phases; image generation deferred to next iteration
- MVP defined as: fetch raw articles, cluster by shared entity, rank by news value, dedup against published state, write top-N articles to Supabase
- Planning docs committed to `t4l_automation/`: `idea.md`, `product-architecture.md`, `mvp.md`

### Implementation (33 files, 2218 lines)
- `app/editorial/` module:
  - `agents.py` — Article Data Agent and Story Cluster Agent factory functions (OpenAI Agents SDK, nested agent-tool pattern)
  - `tools.py` — `article_digest_tool` and `story_cluster_tool` wrappers
  - `workflow.py` — `run_editorial_cycle()`: fetch, group, deduplicate against prior state, produce ranked `CyclePublishPlan`
  - `helpers.py` — entity-agnostic clustering, single-source merge into existing clusters, URL dedup, SHA-256 fingerprinting, `deduplicate_plan()`
  - `prompts.yml` — YAML-sourced prompts for both agents
  - `context.py`, `model.py`, `tracing.py` — agent context, model config, OpenAI tracing setup
- `app/writer/` module:
  - `agents.py` — Article Writer Agent factory
  - `workflow.py` — `run_writer_cycle()`: parallel article writing using `asyncio.gather`
  - `prompts.yml` — writer agent YAML prompts
- `app/orchestration.py` — top-level orchestrator: compact input assembly, runs editorial then writer, persists `editorial_state` to Supabase
- `app/adapters.py` — all Supabase I/O (fetch raw articles, save articles, load/persist editorial state); deterministic plan serialization
- `app/schemas.py` — full Pydantic schema hierarchy: `RawArticle`, `ArticleDigest`, `StoryClusterResult`, `CyclePublishPlan`, `PublishableArticle`, `PublishedStoryRecord`, `CycleResult`
- `app/config.py` — `Settings` via pydantic-settings with `.env` support
- `app/constants.py` — `TOP_N_ARTICLES`, `LOOKBACK_HOURS`, entity priority weights
- `app/cli.py` — `editorial-cycle run` CLI entrypoint (Typer)
- `supabase/migrations/001_editorial_state.sql` — `editorial_state` table DDL

### Key Design Decisions
- Entity-agnostic clustering: most-specific shared entity determines cluster key (player > game > team)
- Single-source articles merge into existing clusters rather than forming new ones — reduces noise
- Compact orchestrator input: passes digests not full text to the orchestrator LLM, reducing token cost
- Deterministic plan deduplication: cross-cluster fingerprint dedup on orchestrator output prevents duplicates even if agent produces overlapping plans
- Cross-cycle state awareness: `editorial_state` table persisted to Supabase; lookback window loads prior fingerprints each cycle

### Testing
- 43 unit tests covering schemas, helpers, config, and fingerprinting — all passing in 0.05s
- Production test: fetched 25 articles, formed 3 multi-source clusters + 1 single-source candidate, wrote articles to Supabase
- 12-hour background test started (`run_12h_test.sh`): 12 hourly cycles, logs to `var/test_runs/`

### Optimizations Applied During Session
- Replaced per-article LLM calls with batch entity grouping in `helpers.py`
- Removed intermediate orchestrator round-trip; writer receives plan directly
- Fingerprint dedup added as final guard before writer to prevent near-duplicate articles within a single cycle

## Files Modified

### New repo: `t4l_editorial_cycle/` (github.com/BigSlikTobi/t4l_editorial_cycle)
- `app/__init__.py`, `app/main.py` — package root
- `app/adapters.py` — all Supabase I/O (fetch articles, save, editorial state)
- `app/cli.py` — CLI entrypoint
- `app/config.py` — settings from environment
- `app/constants.py` — editorial tuning constants
- `app/orchestration.py` — top-level cycle orchestration
- `app/schemas.py` — all Pydantic schemas
- `app/editorial/` — 9 files (agents, tools, workflow, helpers, prompts, context, model, tracing, init)
- `app/writer/` — 6 files (agents, workflow, prompts, model, prompts.yml, init)
- `tests/` — conftest + 3 test modules (43 tests)
- `supabase/migrations/001_editorial_state.sql`
- `pyproject.toml`, `.gitignore`, `.env.example`
- `run_12h_test.sh` — untracked (not committed; contains runtime paths)

### Planning docs: `t4l_automation/`
- `idea.md` — problem statement and proposed system design
- `product-architecture.md` — agent hierarchy, module layout, data flow, design decisions
- `mvp.md` — KANO analysis, 3-phase roadmap, MVP feature list

## Code Quality Notes

### Tests
- **43 passed, 0 failed, 0 errors** (`pytest -v`, 0.05s)
- Coverage: schemas round-trips, config validation, fingerprinting determinism and order-independence, deduplication logic, helper functions

### Linting
- No linter configured in `pyproject.toml` (no ruff/flake8/mypy). Not flagged as a new issue — project is brand new.
- No TODO, FIXME, or HACK comments found in `app/`
- No debug `print()` statements found in `app/`

### Other Observations
- `run_12h_test.sh` is untracked and intentionally excluded from git — it references runtime paths and live `.env`. This is correct behavior.
- `var/` directory is git-ignored — test logs and PID file will not be committed. Correct.
- No linting or type-checking toolchain configured yet. Recommend adding `ruff` + `mypy` before next major feature iteration.

## Open Items / Carry-over
- **12-hour test in progress** — PID in `var/test_runs/test_run.pid`, logs at `var/test_runs/summary.log`. Review quality after all 12 cycles complete.
- **Image generation** — deferred to next iteration (KANO Phase 2). Will require a separate image pipeline module.
- **Linting toolchain** — add `ruff` and optionally `mypy` to `pyproject.toml` dev deps. No baseline established yet.
- **`run_12h_test.sh`** — decide whether to commit this script to the repo (currently untracked). If committed, parameterize the venv path.
- **Supabase migration applied manually** — `supabase/migrations/001_editorial_state.sql` was run manually. Consider adding Supabase CLI setup to README for future contributors.
- **CLAUDE.md** — no project documentation file exists yet. Consider adding one to document test commands, architecture, and deployment notes once the 12-hour test validates the system.
- **Production deployment** — cron job not yet configured. Next step after 12-hour test passes.
