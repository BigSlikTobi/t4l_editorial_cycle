from __future__ import annotations

import json
import logging
import os

from agents import Runner

from app.adapters import ArticleLookupFromDb, EditorialStateStore, RawArticleDbReader
from app.config import Settings
from app.editorial.agents import (
    build_article_data_agent,
    build_editorial_orchestrator_agent,
    build_story_cluster_agent,
)
from app.editorial.context import CycleRunContext
from app.editorial.helpers import (
    coerce_output,
    deduplicate,
    deduplicate_plan,
    enrich_plan_with_players,
    group_by_entity,
    recompute_plan_fingerprints,
    resolve_existing_article_ids,
)
from app.editorial.tools import (
    build_article_digest_tool,
    build_article_lookup_tool,
    build_story_cluster_tool,
)
from app.editorial.tracing import build_run_config
from app.schemas import CyclePublishPlan

logger = logging.getLogger(__name__)


class EditorialWorkflow:
    def __init__(
        self,
        *,
        settings: Settings,
        news_feed: RawArticleDbReader,
        article_lookup: ArticleLookupFromDb,
        state_store: EditorialStateStore,
    ) -> None:
        self._news_feed = news_feed
        self._state_store = state_store
        os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key.get_secret_value())

        # Wire agent -> tool -> agent -> tool chain
        article_lookup_tool = build_article_lookup_tool(article_lookup)
        article_data_agent = build_article_data_agent(
            settings, article_lookup_tool=article_lookup_tool
        )
        article_digest_tool = build_article_digest_tool(article_data_agent)
        story_cluster_agent = build_story_cluster_agent(
            settings, article_digest_tool=article_digest_tool
        )
        story_cluster_tool = build_story_cluster_tool(story_cluster_agent)
        self._orchestrator_agent = build_editorial_orchestrator_agent(
            settings, story_cluster_tool=story_cluster_tool
        )

    async def run_editorial_cycle(self, context: CycleRunContext) -> CyclePublishPlan:
        logger.info("Starting editorial cycle %s", context.cycle_id)

        # Step 1: Fetch raw feed
        context.raw_articles = await self._news_feed.fetch_raw_articles(context.lookback_hours)
        logger.info("Fetched %d raw articles", len(context.raw_articles))

        # Step 2: Exact-URL dedup
        context.raw_articles, context.deduplicated_count = deduplicate(context.raw_articles)
        logger.info(
            "After dedup: %d articles (%d removed)",
            len(context.raw_articles),
            context.deduplicated_count,
        )

        # Step 3: Load published state
        context.published_state = await self._state_store.load_published_state(
            hours=context.lookback_hours,
        )

        # Step 4: Group by entity
        grouped = group_by_entity(context.raw_articles)
        logger.info(
            "Grouped: %d multi-source clusters, %d single-source candidates",
            len(grouped.multi_source),
            len(grouped.single_source),
        )

        if grouped.total_clusters == 0:
            return CyclePublishPlan(reasoning="No articles in feed for this cycle.")

        # Step 5: Build orchestrator input
        # Multi-source clusters → the orchestrator calls analyze_story_cluster for each
        # Single-source articles → passed directly as lightweight candidates (no cluster agent)
        published_fingerprints = [r.story_fingerprint for r in context.published_state]
        published_stories = [
            {
                "story_fingerprint": r.story_fingerprint,
                "cluster_headline": r.cluster_headline,
                "supabase_article_id": r.supabase_article_id,
                "source_urls": r.source_urls,
            }
            for r in context.published_state
        ]

        def _compact(a: RawArticle) -> dict:
            return {
                "id": a.id,
                "url": a.url,
                "title": a.title,
                "source_name": a.source_name,
                "category": a.category,
            }

        orchestrator_input = json.dumps(
            {
                "cluster_groups": {
                    label: [_compact(a) for a in articles]
                    for label, articles in grouped.multi_source.items()
                },
                "single_source_articles": [_compact(a) for a in grouped.single_source],
                "published_fingerprints": published_fingerprints,
                "published_stories": published_stories,
                "top_n": context.top_n,
            },
            separators=(",", ":"),
        )

        # Step 6: Run orchestrator agent
        run_config = build_run_config(context.cycle_id, stage="editorial_cycle")
        result = await Runner.run(
            self._orchestrator_agent,
            orchestrator_input,
            context=context,
            run_config=run_config,
            max_turns=30,
            auto_previous_response_id=True,
        )
        raw_plan = coerce_output(result.final_output, CyclePublishPlan)

        # Deterministic post-processing
        plan = recompute_plan_fingerprints(raw_plan, context.raw_articles)
        plan = deduplicate_plan(plan)
        plan = resolve_existing_article_ids(plan, context.published_state)
        plan = enrich_plan_with_players(plan, context.raw_articles)
        context.prevented_duplicates = plan.prevented_duplicates

        publishable = [s for s in plan.stories if s.action != "skip"]
        logger.info(
            "Editorial cycle %s complete: %d stories to publish/update, %d prevented duplicates",
            context.cycle_id,
            len(publishable),
            plan.prevented_duplicates,
        )
        return plan
