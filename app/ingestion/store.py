"""PostgREST-backed store for the ingestion pipeline.

Owns all reads and writes against `public.raw_articles`,
`public.article_entities`, and `public.article_topics`.

Status machine on `raw_articles`:

  discovered   — row inserted from news_extraction output
  content_ok   — url_content_extraction filled `content`
  knowledge_ok — article_knowledge_extraction filled entities + topics
  failed       — terminal; see `error` jsonb for details

The ingestion worker is idempotent: re-running it picks up rows at
whichever status they stopped at (insert dedups on `url`, state updates
are set-based).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.clients import ContentResult, KnowledgeResult, NewsItem

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingArticle:
    id: str
    url: str
    title: str | None
    content: str | None


class RawArticleStore:
    """Minimal PostgREST wrapper for raw_articles + article_entities/topics."""

    def __init__(
        self,
        *,
        base_url: str,
        service_role_key: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- reads

    async def list_known_urls(self, since: datetime) -> set[str]:
        """Return URLs already present in raw_articles with fetched_at >= `since`."""
        cutoff = since.astimezone(UTC).isoformat()
        response = await self._client.get(
            "/rest/v1/raw_articles",
            params={"select": "url", "fetched_at": f"gte.{cutoff}"},
        )
        response.raise_for_status()
        return {row["url"] for row in response.json() if row.get("url")}

    async def list_pending(
        self, *, status: str, limit: int = 100
    ) -> list[PendingArticle]:
        """Return rows at a given stage waiting for the next transition."""
        response = await self._client.get(
            "/rest/v1/raw_articles",
            params={
                "select": "id,url,title,content",
                "status": f"eq.{status}",
                "order": "fetched_at.desc",
                "limit": str(limit),
            },
        )
        response.raise_for_status()
        return [
            PendingArticle(
                id=row["id"],
                url=row["url"],
                title=row.get("title"),
                content=row.get("content"),
            )
            for row in response.json()
        ]

    # ---------------------------------------------------------------- writes

    async def insert_discovered(self, items: list[NewsItem]) -> int:
        """Bulk-insert discovered URLs. Duplicates on unique(url) are ignored.

        Returns the number of rows actually inserted (i.e. not already known).
        """
        if not items:
            return 0
        rows = []
        for it in items:
            rows.append(
                {
                    "url": it.url,
                    "title": it.title,
                    "source_name": it.source_name,
                    "publisher": it.publisher,
                    "publication_date": (
                        it.publication_date.astimezone(UTC).isoformat()
                        if it.publication_date
                        else None
                    ),
                    "status": "discovered",
                }
            )
        # PostgREST `resolution=ignore-duplicates` only applies to the primary
        # key by default. We dedup on url (a secondary unique constraint), so
        # route the upsert through `on_conflict=url`.
        response = await self._client.post(
            "/rest/v1/raw_articles?on_conflict=url",
            json=rows,
            headers={
                "Prefer": "resolution=ignore-duplicates,return=representation",
            },
        )
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"insert_discovered failed: {response.text[:300]}",
                request=response.request,
                response=response,
            )
        inserted = response.json()
        logger.info("Inserted %d new raw_articles (out of %d)", len(inserted), len(rows))
        return len(inserted)

    async def update_content(
        self, article_id: str, result: ContentResult
    ) -> None:
        payload = {
            "content": result.content,
            "content_extracted_at": _now_iso(),
            "status": "content_ok",
        }
        if result.title and not result.error:
            payload["title"] = result.title
        await self._patch(article_id, payload)

    async def update_knowledge(
        self, article_id: str, result: KnowledgeResult
    ) -> None:
        """Write entities + topics, flip status to knowledge_ok."""
        if result.entities:
            entity_rows = [
                {
                    "article_id": article_id,
                    "entity_type": e.entity_type,
                    "entity_id": e.entity_id,
                    "mention_text": e.mention_text,
                    "matched_name": e.matched_name,
                    "confidence": e.confidence,
                    "team_abbr": e.team_abbr,
                    "position": e.position,
                }
                for e in result.entities
                if e.entity_type and e.entity_id
            ]
            if entity_rows:
                response = await self._client.post(
                    "/rest/v1/article_entities",
                    json=entity_rows,
                    headers={
                        "Prefer": "resolution=merge-duplicates,return=minimal",
                    },
                )
                if response.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"article_entities insert failed: {response.text[:300]}",
                        request=response.request,
                        response=response,
                    )

        if result.topics:
            topic_rows = [
                {
                    "article_id": article_id,
                    "topic": t.topic,
                    "confidence": t.confidence,
                    "rank": t.rank,
                }
                for t in result.topics
                if t.topic
            ]
            if topic_rows:
                response = await self._client.post(
                    "/rest/v1/article_topics",
                    json=topic_rows,
                    headers={
                        "Prefer": "resolution=merge-duplicates,return=minimal",
                    },
                )
                if response.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"article_topics insert failed: {response.text[:300]}",
                        request=response.request,
                        response=response,
                    )

        await self._patch(
            article_id,
            {
                "knowledge_extracted_at": _now_iso(),
                "status": "knowledge_ok",
            },
        )

    async def mark_failed(self, article_id: str, error: dict[str, Any]) -> None:
        await self._patch(
            article_id,
            {"status": "failed", "error": error},
        )

    # --------------------------------------------------------- watermarks

    async def read_watermarks(self) -> dict[str, datetime]:
        """Return {source_name: last_publication_at} for every known source."""
        response = await self._client.get(
            "/rest/v1/ingestion_watermarks",
            params={"select": "source_name,last_publication_at"},
        )
        response.raise_for_status()
        out: dict[str, datetime] = {}
        for row in response.json():
            raw = row.get("last_publication_at")
            if not raw:
                continue
            out[row["source_name"]] = datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            )
        return out

    async def upsert_watermarks(self, watermarks: dict[str, datetime]) -> None:
        """Bulk upsert per-source watermarks. `watermarks` is the new value
        for each source; this method writes only rows that would move the
        watermark forward (callers are expected to have already computed
        max() against any prior value)."""
        if not watermarks:
            return
        rows = [
            {
                "source_name": src,
                "last_publication_at": ts.astimezone(UTC).isoformat(),
                "updated_at": _now_iso(),
            }
            for src, ts in watermarks.items()
        ]
        response = await self._client.post(
            "/rest/v1/ingestion_watermarks?on_conflict=source_name",
            json=rows,
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"ingestion_watermarks upsert failed: {response.text[:300]}",
                request=response.request,
                response=response,
            )

    # ---------------------------------------------------------------- helpers

    async def _patch(self, article_id: str, payload: dict[str, Any]) -> None:
        body = {**payload, "updated_at": _now_iso()}
        response = await self._client.patch(
            f"/rest/v1/raw_articles?id=eq.{article_id}",
            json=body,
            headers={"Prefer": "return=minimal"},
        )
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"raw_articles patch {article_id} failed: {response.text[:300]}",
                request=response.request,
                response=response,
            )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
