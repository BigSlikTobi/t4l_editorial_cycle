# T4L Editorial Cycle Agent — Product Proposal

## The Problem

The current n8n pipeline treats every inbound article as an independent publishing event. ESPN covers a trade at 9am, CBS covers the same trade at 9:20am — two articles get written and published as if they are separate stories. There is no memory of what has already been published, so the platform re-covers stale stories every cycle. The result is a feed full of redundant, low-signal content.

## Proposed System: T4L Editorial Cycle Agent

A stateful, hourly agentic system that: gathers all inbound raw stories, clusters them by underlying event, ranks clusters by news value, compares against what was already published, and writes only what is genuinely new. Every hour, the top-N stories are either published fresh or updated with new material — nothing else goes out.

**Standalone repo** — follows the architectural patterns of t4l_radio_agency (YAML prompts, agent factories, Pydantic schemas, nested agent-tools, clean module separation) but is fully decoupled with its own codebase, config, adapters, and deployment.

## Agent Hierarchy

```
Editorial Cycle Orchestrator
   └── story_cluster_tool
         └── Story Cluster Agent
               └── article_digest_tool
                     └── Article Data Agent
                           └── lookup_article_content
```

- **Article Data Agent** — digests a single raw article into structured `ArticleDigest` (summary, key facts, confidence score)
- **Story Cluster Agent** — receives digests covering the same event, compares against published state, returns `StoryClusterResult` (is this new? what is the synthesis? news value score?)
- **Editorial Cycle Orchestrator** — receives full feed + published state snapshot, calls cluster tool in parallel, ranks results, returns `CyclePublishPlan` (top-N stories to write or update, with reasoning)
- **Article Writer Agent** — runs per story in the plan, writes full article (headline, body) into a `PublishableArticle` Pydantic model that maps to the Supabase schema the Flutter app reads

## Module Layout

```
app/
   editorial/
      prompts.yml        # all prompt text, keyed by agent name
      prompts.py         # prompt loading only
      model.py           # model settings per agent
      agents.py          # agent factory functions
      tools.py           # nested agent-tool construction
      helpers.py         # deterministic: feed dedup, cluster grouping heuristics, fingerprinting
      context.py         # CycleRunContext dataclass
      workflow.py        # cycle assembly and execution
   schemas.py            # all Pydantic models
   orchestration.py      # thin entrypoint: load state, run cycle, persist state, write to Supabase
   adapters.py           # Supabase feed adapter, editorial state adapter
   config.py             # settings and environment config
   cli.py                # CLI entrypoint
   main.py               # API entrypoint
```

## Deterministic vs Agentic

| Step | How |
|---|---|
| Feed fetch | Deterministic — Supabase adapter |
| Source dedup (exact URL/ID) | Deterministic — Python set |
| Initial cluster grouping | Deterministic — title similarity, entity overlap |
| Cluster synthesis + scoring | Agentic — Story Cluster Agent |
| Editorial ranking + cut | Agentic — Editorial Cycle Orchestrator |
| Article writing | Agentic — Article Writer Agent |
| State persistence | Deterministic — Supabase adapter |

## State Model

### Tier 1 — Cycle Working Memory (in-process Python)

A `CycleRunContext` dataclass holding the current feed, the current published state snapshot, and the output plan. Lives only for the duration of one cycle run. No persistence needed.

### Tier 2 — Cross-Cycle Published State (Supabase)

A new `editorial_state` table storing:

- `story_fingerprint` — hash of the canonical story's key facts (not the URL, since URLs differ per source)
- `published_at` — timestamp of first publish
- `last_updated_at` — timestamp of most recent update
- `supabase_article_id` — foreign key to the existing articles table
- `cycle_id` — which hourly cycle published this

The Orchestrator Agent receives a snapshot of this table (filtered to the last 48 hours) as part of its context. After a cycle completes, the state is updated deterministically based on the `CyclePublishPlan`.

## Key Decisions

1. **Story fingerprinting strategy** — semantic embedding (robust, more expensive) vs deterministic hash of key facts (fast, cheap)?
2. **Cycle trigger** — cron job calling CLI (simplest) vs in-app background scheduler?
3. **Top-N threshold** — fixed number per cycle or dynamic based on news volume?

## V1 Scope

### In

- Hourly cycle runner triggered by cron or CLI command
- Feed fetch via Supabase adapter
- Deterministic source deduplication and initial cluster grouping
- Story Cluster Agent — synthesizes a cluster, scores news value, flags if new vs update
- Editorial Cycle Orchestrator — ranks clusters, produces `CyclePublishPlan` of top-N stories
- Article Writer Agent — writes full article per story in plan (headline, body)
- `editorial_state` Supabase table — stores fingerprints and published article references
- Write published articles to Supabase in the schema the Flutter app already reads

### Out (explicitly)

- Image generation (deferred to next iteration)
- Push notifications or breaking-news fast path
- Multi-language article generation
- Reader engagement signals feeding back into editorial ranking
- Human editorial review step or approval queue
- Any changes to the Flutter app or Supabase article schema

## Success Criteria

**Primary:** Duplicate story rate in the feed drops below 5% within the first week of operation, measured as the percentage of published articles sharing a story fingerprint with an article published within the prior 6 hours.

**Supporting signals:**

- Stories published per cycle stabilizes to a predictable top-N rather than spiking on busy news days
- Editorial Cycle Orchestrator produces a `CyclePublishPlan` with reasoning that reflects actual news judgment (qualitative review)
- Supabase `editorial_state` table grows at a sustainable rate with no orphaned fingerprints after 48h TTL cleanup

## What This Is Not

- Not a real-time breaking news system — the hourly cycle cadence is intentional
- Not a content moderation or fact-checking layer
- Not a replacement for the Flutter app or the Supabase article schema — output contract stays identical
- Not a general-purpose news aggregator — scoped entirely to NFL content from the existing feed
