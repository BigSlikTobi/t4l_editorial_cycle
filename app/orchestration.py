from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from app.adapters import (
    ArticleLookupAdapter,
    ArticleWriter,
    EditorialStateStore,
    ExternalServiceError,
    ImageUploader,
    RawFeedReader,
)
from app.config import Settings, get_settings
from app.editorial.context import CycleRunContext
from app.editorial.workflow import EditorialWorkflow
from app.schemas import CycleResult
from app.writer.image_clients import (
    ImageSelectionClient,
    WikimediaCommonsClient,
)
from app.writer.image_selector import ImageSelector
from app.writer.image_validator import ImageValidator
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

        # Phase 3: Persist to Supabase.
        # Each story now produces two articles (en-US + de-DE). We route each
        # article to INSERT or PATCH independently by looking up its row by
        # (story_fingerprint, language) — so the German row follows the same
        # upsert semantics as the English one without duplicating state.
        # editorial_state stays keyed on fingerprint and records the English
        # article id as the canonical reference.
        fingerprint_to_article_id: dict[str, int] = {}
        fingerprint_to_headline: dict[str, str] = {}
        fingerprint_to_source_urls: dict[str, list[str]] = {}
        articles_written = 0
        articles_updated = 0

        for article in articles:
            matching_story = next(
                (s for s in plan.stories if s.story_fingerprint == article.story_fingerprint),
                None,
            )
            try:
                existing_id = await self._article_writer.find_article_id(
                    article.story_fingerprint, article.language
                )
                if existing_id is not None:
                    await self._article_writer.update_article(existing_id, article)
                    persisted_id = existing_id
                    articles_updated += 1
                else:
                    persisted_id = await self._article_writer.write_article(article)
                    articles_written += 1
            except ExternalServiceError as exc:
                logger.error("Failed to persist article %s: %s", article.headline[:60], exc)
                context.warnings.append(f"Write failed for {article.headline}: {exc}")
                continue

            # editorial_state tracks the English row only — the DE row is
            # reachable via team_article lookup by (fingerprint, 'de-DE').
            if article.language == "en-US":
                fingerprint_to_article_id[article.story_fingerprint] = persisted_id
                fingerprint_to_headline[article.story_fingerprint] = article.headline
                if matching_story:
                    fingerprint_to_source_urls[article.story_fingerprint] = [
                        d.url for d in matching_story.source_digests
                    ]

        # Phase 4: Persist editorial state
        await self._state_store.persist_cycle_results(
            cycle_id,
            fingerprint_to_article_id,
            fingerprint_to_headline,
            fingerprint_to_source_urls,
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

    # Image cascade: web search (Google CC + Wikimedia) → curated pool →
    # player headshot → logo. Always constructed — curated pool + headshot +
    # logo all work without any external API keys.
    image_client: ImageSelectionClient | None = None
    if settings.image_selection_url is not None:
        image_client = ImageSelectionClient(
            base_url=str(settings.image_selection_url),
            google_custom_search_key=(
                settings.google_custom_search_key.get_secret_value()
                if settings.google_custom_search_key
                else None
            ),
            google_custom_search_engine_id=settings.google_custom_search_engine_id,
            llm_api_key=settings.openai_api_key.get_secret_value(),
            llm_model=settings.openai_model_vision_validator,
            llm_provider="openai",
            timeout_seconds=settings.image_selection_timeout_seconds,
        )
        adapters.append(image_client)
    wikimedia_client = WikimediaCommonsClient()
    adapters.append(wikimedia_client)

    validator = ImageValidator(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.openai_model_vision_validator,
    )
    uploader = ImageUploader(
        base_url=str(settings.supabase_url),
        service_role_key=settings.supabase_service_role_key.get_secret_value(),
    )
    adapters.append(uploader)
    image_selector: ImageSelector | None = ImageSelector(
        supabase_url=str(settings.supabase_url),
        supabase_service_role_key=settings.supabase_service_role_key.get_secret_value(),
        image_client=image_client,
        validator=validator,
        uploader=uploader,
        wikimedia_client=wikimedia_client,
    )
    adapters.append(image_selector)

    editorial = EditorialWorkflow(
        settings=settings,
        news_feed=news_feed,
        article_lookup=article_lookup,
        state_store=state_store,
    )
    writer = WriterWorkflow(
        settings=settings,
        article_writer_adapter=article_writer_adapter,
        image_selector=image_selector,
    )

    return CycleOrchestrator(
        settings=settings,
        editorial=editorial,
        writer=writer,
        article_writer_adapter=article_writer_adapter,
        state_store=state_store,
        adapters=adapters,
    )
