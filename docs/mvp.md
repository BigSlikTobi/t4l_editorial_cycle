# MVP Definition: T4L Editorial Cycle Agent

**Generated**: 2026-04-15
**Version**: 1.0

## Product Vision

An hourly agentic editorial cycle that replaces a stateless n8n pipeline with a stateful system that clusters NFL news by underlying event, compares against published history, and writes only genuinely new content.

## Target User

Solo technical founder operating a T4L NFL content platform — the immediate consumer of this system is the Flutter web app and its readers; the operator is the founder running a cron job.

## Problem Being Solved

The current n8n pipeline treats every inbound article as an independent publishing event with no memory of what was already published. ESPN and CBS cover the same trade — two articles go out. The feed fills with redundant, low-signal content.

## Success Metrics

**Primary:** Duplicate story rate drops below 5% within the first week, measured as published articles sharing a fingerprint with an article published in the prior 6 hours.

**Supporting:**
- Stories per cycle stabilizes to predictable top-N
- `CyclePublishPlan.reasoning` reflects actual editorial judgment on qualitative review
- "Prevented duplicates" counter grows meaningfully each cycle (the Delighter)

---

## KANO Feature Analysis

### Must-Haves (Non-Negotiable)

| Feature | File(s) | Justification |
|---------|---------|---------------|
| Supabase feed adapter — `RawFeedReader` | `adapters.py` | No feed, no pipeline. Everything is gated on this. |
| Deterministic dedup + cluster grouping | `editorial/helpers.py` — `deduplicate()`, `group_by_entity()`, `fingerprint()` | This is the core problem fix. Skipping it means building the same broken n8n system again. |
| Story fingerprint schema + `editorial_state` Supabase table | `schemas.py`, `adapters.py` — `EditorialStateStore` | Cross-cycle memory is the entire reason this system exists. Without persisted fingerprints, the Orchestrator re-publishes every cycle. |
| Article Data Agent | `editorial/agents.py` — `make_article_data_agent()` | Converts raw prose into structured `ArticleDigest`. The Cluster Agent cannot reason reliably over raw text. |
| Story Cluster Agent | `editorial/agents.py` — `make_story_cluster_agent()` | Core intelligence: synthesizes multi-source coverage into a single scored cluster with `is_new` flag. |
| Editorial Cycle Orchestrator | `editorial/agents.py` — `make_editorial_orchestrator_agent()` | Produces `CyclePublishPlan`. Without it there is no ranked output and the writer phase has nothing to consume. |
| Article Writer Agent | `writer/agents.py` — `make_article_writer_agent()` | Writes `PublishableArticle` (headline, body) in the exact schema the Flutter app reads. Nothing appears in the app without this. |
| All Pydantic schemas | `schemas.py` | Every module-to-module contract. Must exist before anything else is built. |
| `run_cycle()` entrypoint + `cli.py` | `orchestration.py`, `cli.py` | The harness that threads everything together. CLI is how cron triggers a cycle. |

### Performance Indicators (Included in MVP)

| Feature | File(s) | Dev Effort | User Impact | Why Included |
|---------|---------|------------|-------------|--------------|
| Parallel article writing | `writer/workflow.py` — `asyncio.gather()` | Low | High | Sequential writing of N stories compresses latency badly. Low effort to do right first time, painful to retrofit. |
| `article_digest_tool` nested in Cluster Agent | `editorial/tools.py` — `make_article_digest_tool()` | Medium | High | Cluster Agent reasoning over structured `ArticleDigest` vs raw article text is not a marginal quality difference. Load-bearing for cluster accuracy. |
| Externalized prompts in YAML | `editorial/prompts.yml`, `writer/prompts.yml` | Low | Medium | Prompts are the fastest-changing artifact in V1. YAML externalization means prompt iteration without touching Python. Proven pattern from radio agency. |
| Fixed top-N threshold | `config.py` — env var | Low | High | Without a threshold the Orchestrator output is unpredictable volume-wise. Fixed-N makes the primary success metric measurable. |
| Run tracing | `editorial/` — tracing config passed to Runner | Low | High (for operator) | No tracing means debugging a failed cycle by reading logs and guessing where the agentic chain broke. |
| `CycleResult` structured output | `orchestration.py` — `CycleResult` Pydantic model | Low | Medium | Makes cycle output inspectable and testable without parsing prose logs. |

### Delighter (One Strategic Surprise)

| Feature | File(s) | Dev Effort | Delight Factor |
|---------|---------|------------|----------------|
| "Prevented duplicates" counter per cycle | `editorial/helpers.py` + `orchestration.py` log line | Very Low | High — makes the system's core value proposition visible as a concrete number every cycle. Turns an invisible correctness guarantee into an observable editorial catch. |

### Deferred Features (Post-MVP)

| Feature | Category | Why Deferred |
|---------|----------|--------------|
| Dynamic top-N based on news volume | Performance Indicator | Adds Orchestrator reasoning complexity. Fixed-N is sufficient. Revisit after 2+ weeks of cycle data. |
| 48h TTL cleanup job for `editorial_state` | Performance Indicator | `load_published_state(hours=48)` filter already caps Orchestrator context. Table stays small initially. |
| Semantic embedding-based fingerprinting | Performance Indicator | Deterministic hash with swappable interface is the correct V1 default. Embeddings add cost and latency before simpler approach is validated. |
| `main.py` full API entrypoint | Indifferent | Cron calls `cli.py`. A real API adds FastAPI, auth, error handling — none of which cron needs. Stub only. |
| Source trust scores / confidence weighting | Indifferent | Premature optimization before a single cycle of real data. Article Data Agent `confidence_score` is sufficient signal. |
| Image generation + validation pipeline | Performance Indicator | Adds a separate generate-validate-retry loop with vision AI. Deferred to next iteration to keep MVP focused on the editorial intelligence core. |

### Excluded Features (Never Build in V1)

| Feature | Reason |
|---------|--------|
| Human editorial review / approval queue | In a solo hourly cron system, an approval queue fills up and halts the pipeline. The agentic judgment must be trusted or the system has no value. |
| Push notifications / breaking-news fast path | Contradicts the intentional hourly cadence. Adds infrastructure complexity before core cycle is validated. |
| Multi-language article generation | Scoped entirely to NFL English content. No user demand yet. |
| Reader engagement signals feeding into ranking | No engagement data exists yet. Requires feedback loop infrastructure that does not exist. |
| Any changes to Flutter app or Supabase article schema | The output contract to Flutter is fixed. The writer agent must produce to the existing schema. |

---

## MVP Scope Summary

### What We're Building

An hourly editorial cycle runner triggered by cron via `cli.py`. Each cycle: fetches the raw NFL article feed from Supabase, deduplicates exact URLs, groups articles by entity overlap into candidate clusters, runs the Editorial Cycle Orchestrator (which calls the Story Cluster Agent as a nested tool, which calls the Article Data Agent as a nested tool), produces a ranked `CyclePublishPlan` of top-N stories, writes one `PublishableArticle` (headline, body) per story in parallel via the Article Writer Agent, and persists fingerprints and article references to `editorial_state`. A per-cycle log line reports how many duplicates were prevented. Image generation is deferred to the next iteration.

### What We're NOT Building (Yet)

- Image generation (next iteration)
- A real API entrypoint (`main.py` is a stub)
- Dynamic top-N threshold
- Semantic fingerprinting
- 48h TTL cleanup job
- Any Flutter or Supabase schema changes
- Source trust scoring

---

## Development Phases

### Phase 1: Contracts and Foundation
- `schemas.py` — all Pydantic models defined
- `config.py` — env vars, top-N threshold, Supabase config
- `adapters.py` — `RawFeedReader`, `EditorialStateStore` (load + persist), `ArticleWriter`
- `editorial_state` Supabase table migration
- `editorial/context.py` — `CycleRunContext` dataclass

### Phase 2: Editorial Intelligence
- `editorial/helpers.py` — `fingerprint()`, `deduplicate()` with prevented-duplicate counter, `group_by_entity()`
- `editorial/prompts.yml` — all three agent prompts
- `editorial/model.py`, `editorial/prompts.py`
- `editorial/agents.py` — all three agent factories
- `editorial/tools.py` — `make_article_digest_tool()`, `make_story_cluster_tool()`
- `editorial/workflow.py` — `run_editorial_cycle()`

### Phase 3: Write Phase and Entrypoint
- `writer/prompts.yml`, `writer/prompts.py`, `writer/model.py`
- `writer/agents.py` — `make_article_writer_agent()`
- `writer/workflow.py` — `run_write_phase()` with parallel `asyncio.gather()`
- `orchestration.py` — `run_cycle()` with cycle summary log including prevented-duplicate count
- `cli.py` — cron-callable entrypoint

---

## Key Technical Risks

- **Nested agent-tool wiring** (`article_digest_tool` inside `story_cluster_tool`) is the most complex SDK surface. Mirror the radio agency two-level nesting pattern exactly.
- **Story fingerprint quality** determines dedup accuracy. The deterministic hash must be stable across equivalent stories from different sources. Test with real feed data before trusting cycle output.
- **Parallel writer calls** introduce concurrent Supabase writes. Test `ArticleWriter.write_article()` under concurrent load before shipping.
- **Articles ship without images in MVP.** Image generation pipeline (generate, validate via vision AI, retry) is deferred to the next iteration. Ensure the Supabase article schema allows nullable image fields.

---

## Validation Hypothesis

**We believe** NFL content readers will encounter a higher-signal feed **because** the Editorial Cycle Agent will reduce duplicate story coverage to under 5% by clustering and fingerprinting stories before any article is written.

**We'll know we're right when** the prevented-duplicate counter averages 2+ catches per cycle in the first week of operation, and the published duplicate rate drops below 5%.

---

## Resolved Decisions

1. **Tracing** — Full tracing, same pattern as radio agency (`tracing.py` with `RunConfig` builder). Low cost, high value.
2. **`main.py`** — Stub only for MVP. Cron + CLI is the sole trigger.
3. **Delighter placement** — Prevented-duplicate counter in `editorial/helpers.py` (counter) and `orchestration.py` (log line). No new files needed.
4. **Image generation** — Deferred to next iteration. Articles ship text-only in MVP.
