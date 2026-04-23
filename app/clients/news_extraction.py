"""Client for the news_extraction_service cloud function."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.clients.base import AsyncJobClient, SupabaseJobsConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsItem:
    url: str
    title: str | None
    description: str | None
    publication_date: datetime | None
    source_name: str | None
    publisher: str | None

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> "NewsItem":
        raw_date = item.get("publication_date")
        pub_dt: datetime | None = None
        if isinstance(raw_date, str) and raw_date:
            try:
                pub_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                pub_dt = None
        elif isinstance(raw_date, datetime):
            pub_dt = raw_date
        return cls(
            url=item["url"],
            title=item.get("title"),
            description=item.get("description"),
            publication_date=pub_dt,
            source_name=item.get("source_name"),
            publisher=item.get("publisher"),
        )


class NewsExtractionClient:
    """Wraps submit+poll to discover recent article URLs from configured feeds."""

    def __init__(
        self,
        *,
        submit_url: str,
        poll_url: str,
        supabase: SupabaseJobsConfig,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._job = AsyncJobClient(
            submit_url=submit_url,
            poll_url=poll_url,
            supabase=supabase,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
        )

    async def close(self) -> None:
        await self._job.close()

    async def extract(
        self,
        *,
        since: datetime | None = None,
        source_filter: str | None = None,
        max_articles: int | None = None,
        max_workers: int | None = None,
    ) -> list[NewsItem]:
        """Run the pipeline once and return discovered NewsItems."""
        options: dict[str, Any] = {}
        if since is not None:
            options["since"] = since.isoformat()
        if source_filter is not None:
            options["source_filter"] = source_filter
        if max_articles is not None:
            options["max_articles"] = max_articles
        if max_workers is not None:
            options["max_workers"] = max_workers

        result = await self._job.run({"options": options})
        items = result.get("items", [])
        logger.info(
            "news_extraction returned %d items "
            "(sources_processed=%s items_filtered=%s)",
            len(items),
            result.get("sources_processed"),
            result.get("items_filtered"),
        )
        return [NewsItem.from_payload(it) for it in items]
