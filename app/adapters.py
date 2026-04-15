from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.schemas import (
    ArticleContentLookupToolResponse,
    PublishableArticle,
    PublishedStoryRecord,
    RawArticle,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3


class ExternalServiceError(RuntimeError):
    """Raised when an upstream integration fails."""


class _TransientHTTPError(ExternalServiceError):
    """Raised for retryable HTTP status codes (429, 5xx)."""


def _default_retry():
    return retry(
        retry=retry_if_exception_type((_TransientHTTPError, httpx.TransportError)),
        stop=stop_after_attempt(_MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


def _check_transient(response: httpx.Response) -> None:
    lowered_body = response.text.lower()
    if response.status_code in _RETRYABLE_STATUS_CODES and (
        "unauthorized" in lowered_body
        or "row-level security" in lowered_body
        or "new row violates row-le" in lowered_body
        or "statuscode': 403" in lowered_body
        or '"statuscode": 403' in lowered_body
    ):
        raise ExternalServiceError(
            f"Permanent upstream auth error despite HTTP {response.status_code}: {response.text[:200]}"
        )
    if response.status_code in _RETRYABLE_STATUS_CODES:
        raise _TransientHTTPError(
            f"Transient HTTP {response.status_code}: {response.text[:200]}"
        )


# --- Feed reader (edge function) ---


class RawFeedReader:
    def __init__(self, base_url: str, auth_token: str, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {auth_token}",
                "apikey": auth_token,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @_default_retry()
    async def fetch_raw_articles(self, lookback_hours: int) -> list[RawArticle]:
        logger.info("Fetching news feed", extra={"lookback_hours": lookback_hours})
        response = await self._client.post("/", json={"lookback_hours": lookback_hours})

        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"News feed request failed with status {response.status_code}: {response.text}"
            )

        payload = response.json()
        stories = payload.get("stories", [])
        result = [RawArticle.model_validate(story) for story in stories]
        logger.info("Fetched %d raw articles", len(result))
        return result


# --- Article content lookup (edge function) ---


class ArticleLookupAdapter:
    def __init__(self, base_url: str, auth_token: str, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {auth_token}",
                "apikey": auth_token,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @_default_retry()
    async def lookup_article(self, url: str) -> ArticleContentLookupToolResponse:
        logger.debug("Looking up article", extra={"url": url})
        response = await self._client.post("/", json={"url": url})

        if response.status_code == 404:
            return ArticleContentLookupToolResponse(requested_url=url, found=False, article=None)
        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"Article lookup failed with status {response.status_code}: {response.text}"
            )

        return ArticleContentLookupToolResponse.model_validate(response.json())


# --- Editorial state (PostgREST direct table access) ---


class EditorialStateStore:
    def __init__(self, base_url: str, service_role_key: str, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @_default_retry()
    async def load_published_state(self, hours: int = 48) -> list[PublishedStoryRecord]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        logger.info("Loading editorial state", extra={"cutoff": cutoff})
        response = await self._client.get(
            "/rest/v1/editorial_state",
            params={"published_at": f"gte.{cutoff}", "select": "*"},
        )

        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"Editorial state load failed with status {response.status_code}: {response.text}"
            )

        return [PublishedStoryRecord.model_validate(row) for row in response.json()]

    @_default_retry()
    async def persist_cycle_results(
        self,
        cycle_id: str,
        fingerprint_to_article_id: dict[str, int],
        fingerprint_to_headline: dict[str, str],
    ) -> None:
        if not fingerprint_to_article_id:
            return

        rows = [
            {
                "story_fingerprint": fp,
                "supabase_article_id": article_id,
                "cycle_id": cycle_id,
                "cluster_headline": fingerprint_to_headline.get(fp, ""),
                "last_updated_at": datetime.now(UTC).isoformat(),
            }
            for fp, article_id in fingerprint_to_article_id.items()
        ]

        response = await self._client.post(
            "/rest/v1/editorial_state",
            json=rows,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )

        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"Editorial state persist failed with status {response.status_code}: {response.text}"
            )

        logger.info("Persisted %d editorial state records", len(rows))


# --- Article writer (PostgREST direct table access) ---


class ArticleWriter:
    def __init__(self, base_url: str, service_role_key: str, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
                "Content-Type": "application/json",
                "Content-Profile": "content",
                "Accept-Profile": "content",
                "Prefer": "return=representation",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _article_payload(self, article: PublishableArticle) -> dict:
        return {
            "team": article.team,
            "language": article.language,
            "headline": article.headline,
            "sub_headline": article.sub_headline,
            "introduction": article.introduction,
            "content": article.content,
            "x_post": article.x_post,
            "bullet_points": article.bullet_points,
            "image": article.image,
            "tts_file": article.tts_file,
        }

    @_default_retry()
    async def write_article(self, article: PublishableArticle) -> int:
        payload = self._article_payload(article)
        response = await self._client.post("/rest/v1/team_article", json=payload)

        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"Article write failed with status {response.status_code}: {response.text}"
            )

        rows = response.json()
        article_id = rows[0]["id"]
        logger.info("Wrote article %d: %s", article_id, article.headline[:60])
        return article_id

    @_default_retry()
    async def update_article(self, article_id: int, article: PublishableArticle) -> None:
        payload = self._article_payload(article)
        response = await self._client.patch(
            f"/rest/v1/team_article?id=eq.{article_id}",
            json=payload,
        )

        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"Article update failed with status {response.status_code}: {response.text}"
            )

        logger.info("Updated article %d: %s", article_id, article.headline[:60])
