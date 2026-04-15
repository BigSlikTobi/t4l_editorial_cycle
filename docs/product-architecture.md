# T4L Editorial Cycle Agent — Product Architecture

## Overview

Two agent modules with a clean phase boundary, connected through shared schemas and a thin orchestration layer.

```
Supabase (raw feed)
   -> RawFeedReader
      -> CycleRunContext
         -> editorial/workflow.py
            -> [deterministic: dedup, group]
            -> Editorial Cycle Orchestrator
               -> story_cluster_tool
                  -> Story Cluster Agent
                     -> article_digest_tool
                        -> Article Data Agent
            -> CyclePublishPlan
               -> writer/workflow.py
                  -> Article Writer Agent (x N, parallel)
               -> list[PublishableArticle]
                  -> ArticleWriter (Supabase write)
                  -> EditorialStateStore.persist_cycle_results()
```

## Module 1 — `editorial` (the agentic core)

Runs the full editorial cycle: digest raw articles, cluster by story, rank, plan what to publish.

All agentic logic lives here. Nothing outside `editorial/` knows about SDK agents (except `writer/`).

### Files

```
app/editorial/
    prompts.yml      # all prompt text keyed by agent name
    prompts.py       # load_prompt(key) only
    model.py         # make_model_settings(agent_name) only
    agents.py        # agent factory functions — returns Runner-ready Agent objects
    tools.py         # nested agent-tool construction (story_cluster_tool, article_digest_tool)
    helpers.py       # deterministic: fingerprint(), similarity_score(), group_by_entity()
    context.py       # CycleRunContext dataclass
    workflow.py      # cycle assembly and execution
```

### Agents

| Factory function | Agent | Output schema |
|---|---|---|
| `make_article_data_agent()` | Article Data Agent | `ArticleDigest` |
| `make_story_cluster_agent()` | Story Cluster Agent | `StoryClusterResult` |
| `make_editorial_orchestrator_agent()` | Editorial Cycle Orchestrator | `CyclePublishPlan` |

### Tools

| Factory function | Wraps | Used by |
|---|---|---|
| `make_article_digest_tool()` | Article Data Agent | Story Cluster Agent |
| `make_story_cluster_tool()` | Story Cluster Agent | Editorial Cycle Orchestrator |

### Workflow execution

```
run_editorial_cycle(ctx: CycleRunContext) -> CyclePublishPlan
   1. fetch raw feed               # via adapters.RawFeedReader
   2. exact-URL dedup              # helpers.deduplicate()
   3. initial cluster grouping     # helpers.group_by_entity()
   4. run Editorial Cycle Orchestrator (with cluster tool available)
   5. return CyclePublishPlan
```

Steps 1-3 are deterministic Python. Step 4 is the agentic call.

## Module 2 — `writer` (the write phase)

Takes a `CyclePublishPlan`, writes one `PublishableArticle` per story entry.

Writing is a separate phase with a clean input contract (`CyclePublishPlan`) and a clean output contract (`PublishableArticle`). If writing fails for one story, you retry just the write phase without re-running the expensive cluster + ranking phase.

### Files

```
app/writer/
    prompts.yml      # writer agent prompt text
    prompts.py       # load_prompt() — same pattern as editorial
    model.py         # make_model_settings() for writer agent
    agents.py        # make_article_writer_agent() -> Agent[PublishableArticle]
    workflow.py      # run_write_phase(plan: CyclePublishPlan) -> list[PublishableArticle]
```

### Workflow execution

```
run_write_phase(plan: CyclePublishPlan) -> list[PublishableArticle]
   for each story in plan.stories (parallel):
      run Article Writer Agent -> PublishableArticle
   return results
```

## Shared Contract — `schemas.py`

No module imports from another module's internals. They import from `schemas.py` only.

```python
class RawArticle(BaseModel): ...
    # inbound from feed

class ArticleDigest(BaseModel): ...
    # output of Article Data Agent (summary, key facts, confidence score)

class StoryClusterResult(BaseModel): ...
    # output of Story Cluster Agent (is_new, synthesis, news_value_score)

class CyclePublishPlan(BaseModel): ...
    stories: list[StoryEntry]
    cycle_id: str
    reasoning: str

class StoryEntry(BaseModel): ...
    story_fingerprint: str
    action: Literal["publish", "update"]
    source_digests: list[ArticleDigest]
    existing_article_id: str | None

class PublishableArticle(BaseModel): ...
    headline: str
    body: str
    story_fingerprint: str
    supabase_article_id: str | None  # None = new, str = update
```

## Shared Contract — `adapters.py`

```python
class RawFeedReader:
    def fetch_raw_articles(self) -> list[RawArticle]: ...

class EditorialStateStore:
    def load_published_state(self, hours: int = 48) -> list[PublishedStoryRecord]: ...
    def persist_cycle_results(self, plan: CyclePublishPlan, articles: list[PublishableArticle]) -> None: ...

class ArticleWriter:
    def write_article(self, article: PublishableArticle) -> str: ...   # returns supabase_article_id
    def update_article(self, article: PublishableArticle) -> None: ...
```

`RawFeedReader` and `EditorialStateStore` are called in `editorial/workflow.py`. `ArticleWriter` is called in `writer/workflow.py`. Nothing else touches Supabase directly.

## Thin Entrypoint — `orchestration.py`

```python
async def run_cycle() -> CycleResult:
    ctx = CycleRunContext(
        raw_articles=adapters.RawFeedReader().fetch_raw_articles(),
        published_state=adapters.EditorialStateStore().load_published_state(hours=48),
    )
    plan: CyclePublishPlan = await editorial_workflow.run_editorial_cycle(ctx)
    articles: list[PublishableArticle] = await writer_workflow.run_write_phase(plan)
    adapters.EditorialStateStore().persist_cycle_results(plan, articles)
    return CycleResult(plan=plan, articles=articles)
```

Four lines of logic. No agent wiring. No Supabase calls directly.

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

## V1 Decisions

| Decision | V1 Default |
|---|---|
| Story fingerprinting | Deterministic hash of key facts, swappable interface in `helpers.py` |
| Cycle trigger | Cron job calling CLI (`cli.py`) |
| Top-N threshold | Fixed number per cycle |

## Full Codebase Structure

```
app/
    editorial/
        __init__.py
        prompts.yml
        prompts.py
        model.py
        agents.py
        tools.py
        helpers.py
        context.py
        workflow.py
    writer/
        __init__.py
        prompts.yml
        prompts.py
        model.py
        agents.py
        workflow.py
    schemas.py
    adapters.py
    orchestration.py
    config.py
    cli.py
    main.py
tests/
pyproject.toml
```
