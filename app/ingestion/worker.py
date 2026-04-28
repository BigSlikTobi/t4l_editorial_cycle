"""Ingestion worker. Chains the three extraction cloud functions.

One call to `run_ingestion_cycle(settings)` performs the full pipeline:

  1. Discover: news_extraction → insert new rows into raw_articles.
  2. Content:  url_content_extraction → fill content for 'discovered' rows.
  3. Knowledge: article_knowledge_extraction → fill entities/topics for
     'content_ok' rows.

Each stage operates on the current DB state, so the worker is idempotent
and crash-safe: reruns pick up wherever the previous run left off.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.clients import (
    JobFailedError,
    JobTimeoutError,
    KnowledgeExtractionClient,
    NewsExtractionClient,
    SupabaseJobsConfig,
    UrlContentClient,
)
from app.config import Settings
from app.ingestion.store import PendingArticle, RawArticleStore

logger = logging.getLogger(__name__)

_CONTENT_BATCH_SIZE = 10


@dataclass
class IngestionSummary:
    discovered: int = 0
    content_updated: int = 0
    content_failed: int = 0
    knowledge_updated: int = 0
    knowledge_failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "content_updated": self.content_updated,
            "content_failed": self.content_failed,
            "knowledge_updated": self.knowledge_updated,
            "knowledge_failed": self.knowledge_failed,
        }


async def run_ingestion_cycle(settings: Settings) -> IngestionSummary:
    """Run one end-to-end ingestion cycle. Returns counts for logging."""
    _require_extraction_config(settings)

    supabase_jobs = SupabaseJobsConfig(
        url=str(settings.supabase_url).rstrip("/"),
        jobs_table=settings.extraction_jobs_table,
    )
    auth_token = settings.extraction_function_auth_token.get_secret_value()

    store = RawArticleStore(
        base_url=str(settings.supabase_url),
        service_role_key=settings.supabase_service_role_key.get_secret_value(),
    )
    news_client = NewsExtractionClient(
        submit_url=str(settings.news_extraction_submit_url),
        poll_url=str(settings.news_extraction_poll_url),
        supabase=supabase_jobs,
        auth_token=auth_token,
        poll_interval_seconds=settings.extraction_poll_interval_seconds,
        timeout_seconds=settings.extraction_timeout_seconds,
    )
    content_client = UrlContentClient(
        submit_url=str(settings.url_content_extraction_submit_url),
        poll_url=str(settings.url_content_extraction_poll_url),
        supabase=supabase_jobs,
        auth_token=auth_token,
        poll_interval_seconds=settings.extraction_poll_interval_seconds,
        timeout_seconds=settings.extraction_timeout_seconds,
    )
    knowledge_client = KnowledgeExtractionClient(
        submit_url=str(settings.knowledge_extraction_submit_url),
        poll_url=str(settings.knowledge_extraction_poll_url),
        supabase=supabase_jobs,
        auth_token=auth_token,
        openai_model=settings.openai_model_article_data_agent,
        poll_interval_seconds=settings.extraction_poll_interval_seconds,
        timeout_seconds=settings.extraction_timeout_seconds,
    )

    summary = IngestionSummary()
    try:
        summary.discovered = await _discover(
            settings=settings, store=store, news_client=news_client
        )
        summary.content_updated, summary.content_failed = await _extract_content(
            store=store, content_client=content_client
        )
        summary.knowledge_updated, summary.knowledge_failed = await _extract_knowledge(
            store=store,
            knowledge_client=knowledge_client,
            max_concurrency=settings.ingestion_knowledge_max_concurrency,
        )
    finally:
        await store.close()
        await news_client.close()
        await content_client.close()
        await knowledge_client.close()

    logger.info("Ingestion cycle summary: %s", summary.as_dict())
    return summary


# --------------------------------------------------------------------- stages


_FIRST_RUN_LOOKBACK = timedelta(hours=6)
_WATERMARK_REWIND = timedelta(minutes=15)


async def _discover(
    *,
    settings: Settings,
    store: RawArticleStore,
    news_client: NewsExtractionClient,
) -> int:
    """Pull new articles via news_extraction and advance watermarks.

    Watermarks advance AT DISCOVERY (not after content/knowledge succeed):
    a dead link or a flaky LLM shouldn't block the next ingestion window
    for its source. Failed rows remain visible in raw_articles.status for
    debugging.
    """
    watermarks = await store.read_watermarks()
    if watermarks:
        since = min(watermarks.values()) - _WATERMARK_REWIND
    else:
        since = datetime.now(UTC) - _FIRST_RUN_LOOKBACK

    items = await news_client.extract(
        since=since,
        max_articles=settings.ingestion_max_articles_per_run,
    )
    if not items:
        return 0

    # Advance per-source watermark from the full response (including
    # already-known URLs) — otherwise a source whose latest article is
    # already in our DB would never advance, and we'd keep re-fetching
    # the same window forever.
    new_marks: dict[str, datetime] = {}
    for item in items:
        if not item.source_name or item.publication_date is None:
            continue
        prev = new_marks.get(item.source_name)
        if prev is None or item.publication_date > prev:
            new_marks[item.source_name] = item.publication_date

    # Only write a source's mark if it would move forward compared to the
    # value we read at the start of the run.
    forward: dict[str, datetime] = {
        src: ts
        for src, ts in new_marks.items()
        if watermarks.get(src) is None or ts > watermarks[src]
    }

    inserted = await store.insert_discovered(items)
    if forward:
        await store.upsert_watermarks(forward)
    return inserted


async def _extract_content(
    *,
    store: RawArticleStore,
    content_client: UrlContentClient,
) -> tuple[int, int]:
    pending: list[PendingArticle] = await store.list_pending(
        status="discovered", limit=_CONTENT_BATCH_SIZE
    )
    if not pending:
        return 0, 0

    urls = [p.url for p in pending]
    try:
        results = await content_client.extract(urls)
    except (JobFailedError, JobTimeoutError) as exc:
        logger.error("url_content batch failed: %s", exc)
        for p in pending:
            await store.mark_failed(p.id, {"stage": "content", "error": str(exc)})
        return 0, len(pending)

    ok, failed = 0, 0
    for p in pending:
        result = results.get(p.url)
        if result is None:
            await store.mark_failed(
                p.id, {"stage": "content", "error": "missing from batch response"}
            )
            failed += 1
            continue
        if not result.ok:
            await store.mark_failed(
                p.id,
                {"stage": "content", "error": result.error or "empty content"},
            )
            failed += 1
            continue
        await store.update_content(p.id, result)
        ok += 1
    return ok, failed


async def _extract_knowledge(
    *,
    store: RawArticleStore,
    knowledge_client: KnowledgeExtractionClient,
    max_concurrency: int,
) -> tuple[int, int]:
    pending: list[PendingArticle] = await store.list_pending(
        status="content_ok", limit=_CONTENT_BATCH_SIZE
    )
    if not pending:
        return 0, 0

    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _process_one(p: PendingArticle) -> bool:
        async with semaphore:
            if not p.content:
                await store.mark_failed(
                    p.id,
                    {"stage": "knowledge", "error": "content_ok row has no content"},
                )
                return False
            try:
                result = await knowledge_client.extract(
                    article_id=p.id,
                    text=p.content,
                    title=p.title,
                    url=p.url,
                )
            except (JobFailedError, JobTimeoutError) as exc:
                logger.warning("knowledge extraction failed for %s: %s", p.id, exc)
                await store.mark_failed(
                    p.id, {"stage": "knowledge", "error": str(exc)}
                )
                return False
            await store.update_knowledge(p.id, result)
            return True

    # return_exceptions=True ensures one task crashing (e.g., an unexpected
    # error type not in the JobFailed/JobTimeout pair) does not cancel the
    # rest. Such crashes are logged and counted as failed for that row,
    # without an attempt to mark_failed (we don't know if mark_failed itself
    # was the failure).
    results = await asyncio.gather(
        *(_process_one(p) for p in pending),
        return_exceptions=True,
    )
    ok = 0
    failed = 0
    for p, r in zip(pending, results, strict=True):
        if isinstance(r, BaseException):
            logger.exception(
                "Unexpected error processing knowledge for %s", p.id, exc_info=r
            )
            failed += 1
        elif r:
            ok += 1
        else:
            failed += 1
    return ok, failed


# --------------------------------------------------------------------- config


def _require_extraction_config(settings: Settings) -> None:
    missing = [
        name
        for name, value in {
            "NEWS_EXTRACTION_SUBMIT_URL": settings.news_extraction_submit_url,
            "NEWS_EXTRACTION_POLL_URL": settings.news_extraction_poll_url,
            "URL_CONTENT_EXTRACTION_SUBMIT_URL": settings.url_content_extraction_submit_url,
            "URL_CONTENT_EXTRACTION_POLL_URL": settings.url_content_extraction_poll_url,
            "KNOWLEDGE_EXTRACTION_SUBMIT_URL": settings.knowledge_extraction_submit_url,
            "KNOWLEDGE_EXTRACTION_POLL_URL": settings.knowledge_extraction_poll_url,
            "EXTRACTION_FUNCTION_AUTH_TOKEN": settings.extraction_function_auth_token,
        }.items()
        if value is None
    ]
    if missing:
        raise RuntimeError(
            "Ingestion worker requires these env vars: " + ", ".join(missing)
        )
