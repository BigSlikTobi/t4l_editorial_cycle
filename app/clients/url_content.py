"""Client for the url_content_extraction_service cloud function."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.clients.base import AsyncJobClient, SupabaseJobsConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContentResult:
    url: str
    title: str | None
    content: str | None  # extracted main text / markdown
    paragraphs: list[str]
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.content)

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> "ContentResult":
        paragraphs = item.get("paragraphs") or []
        content = item.get("content")
        if content is None and paragraphs:
            content = "\n\n".join(str(p) for p in paragraphs)
        return cls(
            url=item.get("url", ""),
            title=item.get("title"),
            content=content,
            paragraphs=[str(p) for p in paragraphs],
            error=item.get("error"),
        )


class UrlContentClient:
    """Extracts main article content from a batch of URLs."""

    def __init__(
        self,
        *,
        submit_url: str,
        poll_url: str,
        supabase: SupabaseJobsConfig,
        auth_token: str | None = None,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._job = AsyncJobClient(
            submit_url=submit_url,
            poll_url=poll_url,
            supabase=supabase,
            auth_token=auth_token,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
        )

    async def close(self) -> None:
        await self._job.close()

    async def extract(
        self,
        urls: list[str],
        *,
        timeout_seconds: int | None = None,
        force_playwright: bool | None = None,
        prefer_lightweight: bool | None = None,
        max_paragraphs: int | None = None,
        min_paragraph_chars: int | None = None,
    ) -> dict[str, ContentResult]:
        if not urls:
            return {}
        options: dict[str, Any] = {}
        if timeout_seconds is not None:
            options["timeout_seconds"] = timeout_seconds
        if force_playwright is not None:
            options["force_playwright"] = force_playwright
        if prefer_lightweight is not None:
            options["prefer_lightweight"] = prefer_lightweight
        if max_paragraphs is not None:
            options["max_paragraphs"] = max_paragraphs
        if min_paragraph_chars is not None:
            options["min_paragraph_chars"] = min_paragraph_chars

        result = await self._job.run({"urls": urls, "options": options})
        # CF returns {"articles": [...]} in request order. The CF may rewrite
        # the url to its resolved form (e.g. AMP redirect), so we key by the
        # requested url rather than trusting article["url"].
        items = result.get("articles") or result.get("items") or result.get("results") or []
        by_url: dict[str, ContentResult] = {}
        for requested_url, item in zip(urls, items):
            payload = {**item, "url": requested_url}
            by_url[requested_url] = ContentResult.from_payload(payload)
        logger.info(
            "url_content_extraction returned %d/%d results",
            len(by_url),
            len(urls),
        )
        return by_url
