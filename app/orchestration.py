from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from app.adapters import (
    ArticleLookupAdapter,
    ArticleWriter,
    EditorialStateStore,
    RawFeedReader,
)
from app.config import Settings, get_settings
from app.editorial.context import CycleRunContext
from app.editorial.workflow import EditorialWorkflow
from app.schemas import CycleResult
from app.writer.workflow import WriterWorkflow

logger = logging.getLogger(__name__)


class CycleOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        editorial: EditorialWorkflow,
        writer: WriterWorkflow,
        article_writer_adapter: ArticleWriter,
        state_store: EditorialStateStore,
        adapters: list | None = None,
    ) -> None:
        self._settings = settings
        self._editorial = editorial
        self._writer = writer
        self._article_writer = article_writer_adapter
        self._state_store = state_store
        self._adapters = adapters or []

    async def close(self) -> None:
        for adapter in self._adapters:
            if hasattr(adapter, "close"):
                await adapter.close()

    async def run_cycle(self) -> CycleResult:
        cycle_id = str(uuid4())
        generated_at = datetime.now(UTC)

        context = CycleRunContext(
            cycle_id=cycle_id,
            generated_at=generated_at,
            lookback_hours=self._settings.lookback_hours,
            top_n=self._settings.top_n,
        )

        # Phase 1: Editorial cycle (fetch, dedup, cluster, rank)
        plan = await self._editorial.run_editorial_cycle(context)

        # Phase 2: Write articles in parallel
        articles = await self._writer.run_write_phase(plan, cycle_id)

        # Phase 3: Persist to Supabase
        fingerprint_to_article_id: dict[str, int] = {}
        fingerprint_to_headline: dict[str, str] = {}
        articles_written = 0
        articles_updated = 0

        for article in articles:
            matching_story = next(
                (s for s in plan.stories if s.story_fingerprint == article.story_fingerprint),
                None,
            )
            if (
                matching_story
                and matching_story.action == "update"
                and matching_story.existing_article_id
            ):
                await self._article_writer.update_article(
                    matching_story.existing_article_id, article
                )
                fingerprint_to_article_id[article.story_fingerprint] = (
                    matching_story.existing_article_id
                )
                articles_updated += 1
            else:
                new_id = await self._article_writer.write_article(article)
                fingerprint_to_article_id[article.story_fingerprint] = new_id
                articles_written += 1

            fingerprint_to_headline[article.story_fingerprint] = article.headline

        # Phase 4: Persist editorial state
        await self._state_store.persist_cycle_results(
            cycle_id, fingerprint_to_article_id, fingerprint_to_headline
        )

        # Log the delighter
        logger.info(
            "Cycle %s complete: %d written, %d updated, %d duplicates prevented",
            cycle_id,
            articles_written,
            articles_updated,
            context.prevented_duplicates,
        )

        return CycleResult(
            cycle_id=cycle_id,
            generated_at=generated_at,
            plan=plan,
            articles_written=articles_written,
            articles_updated=articles_updated,
            prevented_duplicates=context.prevented_duplicates,
            warnings=context.warnings,
        )


def build_default_orchestrator(settings: Settings) -> CycleOrchestrator:
    news_feed = RawFeedReader(
        base_url=str(settings.supabase_news_feed_url),
        auth_token=settings.resolved_function_auth_token(),
        timeout_seconds=settings.news_timeout_seconds,
    )
    article_lookup = ArticleLookupAdapter(
        base_url=str(settings.supabase_article_lookup_url),
        auth_token=settings.resolved_function_auth_token(),
        timeout_seconds=settings.news_timeout_seconds,
    )
    state_store = EditorialStateStore(
        base_url=str(settings.supabase_url),
        service_role_key=settings.supabase_service_role_key.get_secret_value(),
    )
    article_writer_adapter = ArticleWriter(
        base_url=str(settings.supabase_url),
        service_role_key=settings.supabase_service_role_key.get_secret_value(),
    )
    adapters = [news_feed, article_lookup, state_store, article_writer_adapter]

    editorial = EditorialWorkflow(
        settings=settings,
        news_feed=news_feed,
        article_lookup=article_lookup,
        state_store=state_store,
    )
    writer = WriterWorkflow(settings=settings)

    return CycleOrchestrator(
        settings=settings,
        editorial=editorial,
        writer=writer,
        article_writer_adapter=article_writer_adapter,
        state_store=state_store,
        adapters=adapters,
    )
