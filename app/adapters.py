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
    EntityMatch,
    PublishableArticle,
    PublishedStoryRecord,
    RawArticle,
    StoredArticleRecord,
)
from app.team_beat.schemas import BeatCycleResult, BeatRoundup

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


# --- DB-backed feed reader ---


class RawArticleDbReader:
    """Reads articles the ingestion worker has fully processed
    (status='knowledge_ok') out of public.raw_articles + article_entities,
    and hydrates them into the existing RawArticle / EntityMatch shape so
    the editorial workflow downstream is unchanged.
    """

    def __init__(
        self, base_url: str, service_role_key: str, timeout_seconds: float = 15.0
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @_default_retry()
    async def fetch_raw_articles(self, lookback_hours: int) -> list[RawArticle]:
        cutoff = (datetime.now(UTC) - timedelta(hours=lookback_hours)).isoformat()
        logger.info(
            "Fetching raw articles from DB",
            extra={"lookback_hours": lookback_hours, "cutoff": cutoff},
        )

        # One round trip for articles + one for their entities. The number of
        # knowledge_ok rows within a typical 2–6 hour window is small (tens),
        # so we don't bother with a JOIN or RPC.
        # Filter on knowledge_extracted_at, not fetched_at: a row's fetched_at
        # is set at discovery, but editorial can only consume it once it
        # reaches knowledge_ok. If content/knowledge extraction is slow or
        # retries, the row's fetched_at can age past the editorial lookback
        # window before it's ever eligible — silently dropping the story.
        articles_resp = await self._client.get(
            "/rest/v1/raw_articles",
            params={
                "select": (
                    "id,url,title,source_name,category,fetched_at,publication_date"
                ),
                "status": "eq.knowledge_ok",
                "knowledge_extracted_at": f"gte.{cutoff}",
                "order": "knowledge_extracted_at.desc",
            },
        )
        _check_transient(articles_resp)
        if articles_resp.status_code >= 400:
            raise ExternalServiceError(
                f"raw_articles query failed ({articles_resp.status_code}): "
                f"{articles_resp.text[:200]}"
            )

        articles = articles_resp.json()
        if not articles:
            logger.info("Fetched 0 raw articles")
            return []

        article_ids = [row["id"] for row in articles]
        entities_by_article = await self._fetch_entities(article_ids)

        result: list[RawArticle] = []
        for row in articles:
            entities = entities_by_article.get(row["id"], [])
            result.append(
                RawArticle(
                    id=row["id"],
                    url=row["url"],
                    title=row.get("title") or "",
                    source_name=row.get("source_name") or "",
                    category=row.get("category"),
                    facts_count=len(entities),
                    entities=entities,
                )
            )
        logger.info("Fetched %d raw articles", len(result))
        return result

    async def _fetch_entities(
        self, article_ids: list[str]
    ) -> dict[str, list[EntityMatch]]:
        # PostgREST `in.()` is URL-encoded by httpx; quote strings to handle
        # commas in ids (uuids are safe but be defensive).
        quoted = ",".join(f'"{aid}"' for aid in article_ids)
        response = await self._client.get(
            "/rest/v1/article_entities",
            params={
                "select": "article_id,entity_type,entity_id,matched_name",
                "article_id": f"in.({quoted})",
            },
        )
        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"article_entities query failed ({response.status_code}): "
                f"{response.text[:200]}"
            )

        grouped: dict[str, list[EntityMatch]] = {}
        for row in response.json():
            grouped.setdefault(row["article_id"], []).append(
                EntityMatch(
                    entity_type=row.get("entity_type") or "",
                    entity_id=str(row.get("entity_id") or ""),
                    matched_name=row.get("matched_name") or "",
                )
            )
        return grouped


# --- DB-backed article lookup ---


class ArticleLookupFromDb:
    """Serves article content out of public.raw_articles.content instead of
    a Supabase edge function. Matches the `lookup_article(url)` contract so
    the article-data agent's tool wiring is unchanged.
    """

    def __init__(
        self, base_url: str, service_role_key: str, timeout_seconds: float = 15.0
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @_default_retry()
    async def lookup_article(self, url: str) -> ArticleContentLookupToolResponse:
        logger.debug("Looking up article in DB", extra={"url": url})
        response = await self._client.get(
            "/rest/v1/raw_articles",
            params={
                "select": "url,title,content",
                "url": f"eq.{url}",
                "limit": "1",
            },
        )
        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"raw_articles lookup failed ({response.status_code}): "
                f"{response.text[:200]}"
            )
        rows = response.json()
        if not rows:
            return ArticleContentLookupToolResponse(
                requested_url=url, found=False, article=None
            )
        row = rows[0]
        content = row.get("content") or ""
        if not content:
            return ArticleContentLookupToolResponse(
                requested_url=url, found=False, article=None
            )
        return ArticleContentLookupToolResponse(
            requested_url=url,
            found=True,
            article=StoredArticleRecord(
                url=row.get("url") or url,
                header=row.get("title"),
                content=content,
                description=None,
                quotes=[],
            ),
        )


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
        fingerprint_to_source_urls: dict[str, list[str]] | None = None,
    ) -> None:
        if not fingerprint_to_article_id:
            return

        source_urls_map = fingerprint_to_source_urls or {}
        rows = [
            {
                "story_fingerprint": fp,
                "supabase_article_id": article_id,
                "cycle_id": cycle_id,
                "cluster_headline": fingerprint_to_headline.get(fp, ""),
                "last_updated_at": datetime.now(UTC).isoformat(),
                "source_urls": source_urls_map.get(fp, []),
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
            "author": article.author,
            "mentioned_players": article.mentioned_players,
            "sources": [s.model_dump() for s in article.sources],
            "story_fingerprint": article.story_fingerprint,
        }

    async def _post_article(self, payload: dict) -> httpx.Response:
        return await self._client.post("/rest/v1/team_article", json=payload)

    @_default_retry()
    async def write_article(self, article: PublishableArticle) -> int:
        payload = self._article_payload(article)
        response = await self._post_article(payload)

        # FK violation on team → retry with team=NULL
        if response.status_code == 409 and "team_article_team_fkey" in response.text:
            logger.warning(
                "Invalid team %r for %s — retrying with team=NULL",
                payload["team"],
                article.headline[:60],
            )
            payload["team"] = None
            response = await self._post_article(payload)

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

    @_default_retry()
    async def fetch_article_by_fingerprint(
        self, story_fingerprint: str, language: str
    ) -> dict | None:
        """Find an existing article by (story_fingerprint, language).

        Used by the multi-language write path: the German article's existing
        row isn't tracked in editorial_state (which is keyed on fingerprint
        only), so we look it up directly in team_article at write time.
        Returns the article row (including `id`) or None if not found.
        """
        response = await self._client.get(
            "/rest/v1/team_article",
            params={
                "story_fingerprint": f"eq.{story_fingerprint}",
                "language": f"eq.{language}",
                "select": "id,headline,sub_headline,introduction,content,bullet_points,author",
                "limit": "1",
            },
        )
        _check_transient(response)
        if response.status_code >= 400:
            logger.warning(
                "fetch_article_by_fingerprint failed (%s, %s): %s",
                story_fingerprint, language, response.text[:200],
            )
            return None
        rows = response.json()
        return rows[0] if rows else None

    @_default_retry()
    async def find_article_id(
        self, story_fingerprint: str, language: str
    ) -> int | None:
        """Return the team_article id for (fingerprint, language), or None.

        Lightweight companion to fetch_article_by_fingerprint — used by the
        orchestrator at persistence time to pick INSERT vs PATCH without
        pulling full article content.
        """
        response = await self._client.get(
            "/rest/v1/team_article",
            params={
                "story_fingerprint": f"eq.{story_fingerprint}",
                "language": f"eq.{language}",
                "select": "id",
                "limit": "1",
            },
        )
        _check_transient(response)
        if response.status_code >= 400:
            logger.warning(
                "find_article_id failed (%s, %s): %s",
                story_fingerprint, language, response.text[:200],
            )
            response.raise_for_status()
        rows = response.json()
        return rows[0]["id"] if rows else None

    @_default_retry()
    async def fetch_article_content(self, article_id: int) -> dict | None:
        """Fetch existing article content by ID for update comparison."""
        response = await self._client.get(
            f"/rest/v1/team_article?id=eq.{article_id}"
            "&select=headline,sub_headline,introduction,content,bullet_points,author",
        )
        _check_transient(response)
        if response.status_code >= 400:
            logger.warning("Failed to fetch article %d: %s", article_id, response.text[:200])
            return None
        rows = response.json()
        return rows[0] if rows else None


# --- Image storage + metadata (Supabase Storage + content.article_images) ---


class ImageUploader:
    """Uploads image bytes to a Supabase Storage bucket and records provenance
    metadata in `content.article_images`. Shared by image selector tiers 1 (web
    search) and 3 (AI-generated).
    """

    def __init__(
        self,
        base_url: str,
        service_role_key: str,
        *,
        bucket: str = "images",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = service_role_key
        self._bucket = bucket
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    def public_url(self, path: str) -> str:
        return f"{self._base}/storage/v1/object/public/{self._bucket}/{path}"

    @_default_retry()
    async def upload(self, data: bytes, content_type: str, path: str) -> str:
        """Upload bytes to bucket at path and return the public URL.

        Uses upsert semantics so reruns on the same fingerprint don't fail.
        """
        response = await self._client.post(
            f"/storage/v1/object/{self._bucket}/{path}",
            content=data,
            headers={
                "Content-Type": content_type,
                "x-upsert": "true",
            },
        )
        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"Image upload failed ({response.status_code}): {response.text[:200]}"
            )
        return self.public_url(path)

    @_default_retry()
    async def record_metadata(
        self,
        *,
        image_url: str,
        original_url: str,
        source: str,
        author: str = "",
    ) -> int | None:
        """Insert one row into content.article_images. Returns the new id on success."""
        response = await self._client.post(
            "/rest/v1/article_images?on_conflict=original_url",
            json={
                "image_url": image_url,
                "original_url": original_url,
                "source": source,
                "author": author,
            },
            headers={
                "Content-Type": "application/json",
                # Upsert on unique original_url so reruns don't 409.
                "Prefer": "return=representation,resolution=merge-duplicates",
            },
        )
        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"article_images insert failed ({response.status_code}): {response.text[:200]}"
            )
        rows = response.json()
        return rows[0]["id"] if rows else None


# --- Team beat roundup writer + cycle state store ---
#
# Writes to the new public.team_roundup and public.team_beat_cycle_state
# tables (see supabase/migrations/008_team_roundup.sql). Tables live in
# the `public` schema because the new Supabase project does not provision
# the legacy `content` schema. PostgREST routes /rest/v1/{table} to public
# by default, so no Content-Profile header is needed.


class BeatRoundupWriter:
    """Upserts public.team_roundup rows keyed by (team_code, cycle_ts).

    Reruns of the same cycle slot for the same team overwrite in place,
    so the GitHub Actions cron is safely idempotent and we don't
    accumulate orphaned audio rows when a workflow is re-run.
    """

    def __init__(
        self, base_url: str, service_role_key: str, timeout_seconds: float = 15.0
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
                "Content-Type": "application/json",
                "Prefer": "return=representation,resolution=merge-duplicates",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _payload(roundup: BeatRoundup) -> dict:
        return {
            "team_code": roundup.team_code,
            "cycle_ts": roundup.cycle_ts.isoformat(),
            "cycle_slot": roundup.cycle_slot,
            "persona_name": roundup.persona_name,
            "en_body": roundup.en_body,
            "de_body": roundup.de_body,
            "radio_script": roundup.radio_script,
            "audio_url": roundup.audio_url,
            "tts_batch_id": roundup.tts_batch_id,
        }

    @_default_retry()
    async def upsert(self, roundup: BeatRoundup) -> int:
        """Upsert and return the row id.

        Uses the (team_code, cycle_ts) unique constraint declared in
        migration 008 as the on_conflict target so reruns don't 409.
        """
        response = await self._client.post(
            "/rest/v1/team_roundup?on_conflict=team_code,cycle_ts",
            json=self._payload(roundup),
        )
        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"team_roundup upsert failed ({response.status_code}): "
                f"{response.text[:200]}"
            )
        rows = response.json()
        if not rows:
            raise ExternalServiceError(
                "team_roundup upsert returned no rows; expected representation"
            )
        roundup_id = int(rows[0]["id"])
        logger.info(
            "Upserted team_roundup id=%d team=%s cycle=%s",
            roundup_id, roundup.team_code, roundup.cycle_ts.isoformat(),
        )
        return roundup_id


class BeatCycleStateStore:
    """Append-only outcome log for public.team_beat_cycle_state.

    Every (team, cycle) attempt writes one row regardless of outcome.
    This is the single source of truth for distinguishing 'beat reporter
    ran and stayed silent' from 'cycle never ran' or 'cycle errored'.
    """

    def __init__(
        self, base_url: str, service_role_key: str, timeout_seconds: float = 15.0
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @_default_retry()
    async def record(self, result: BeatCycleResult) -> int:
        payload = {
            "team_code": result.team_code,
            "cycle_ts": result.cycle_ts.isoformat(),
            "cycle_slot": result.cycle_slot,
            "outcome": result.outcome.value,
            "reason": result.reason or None,
            "article_count": result.article_count,
            "roundup_id": result.roundup_id,
        }
        response = await self._client.post(
            "/rest/v1/team_beat_cycle_state",
            json=payload,
        )
        _check_transient(response)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"team_beat_cycle_state insert failed ({response.status_code}): "
                f"{response.text[:200]}"
            )
        rows = response.json()
        state_id = int(rows[0]["id"]) if rows else 0
        logger.info(
            "Recorded beat cycle state id=%d team=%s outcome=%s",
            state_id, result.team_code, result.outcome.value,
        )
        return state_id


# --- ExtractionJobCanceler (cancel stale gemini_tts_batch rows) ---
#
# When TTSBatchClient.create_and_wait times out at the async-job layer
# (the worker stalled after creating the Gemini batch but before writing
# terminal state to extraction_jobs), the row sits at status='running'
# until expires_at. The sibling repo's cleanup workflow re-POSTs such
# rows every 5 min, which would create a *second* Gemini batch for the
# same payload — duplicate spend. This canceler PATCHes the row to
# status='failed' immediately on our side so cleanup ignores it.
#
# Mirrors `scripts/team_beat_preflight.py` but as a library adapter so
# the workflow can call it inline rather than wait for the next cron.


class ExtractionJobCanceler:
    """PATCHes an extraction_jobs row to status='failed' so the sibling
    cleanup workflow doesn't re-POST it."""

    def __init__(
        self, base_url: str, service_role_key: str, timeout_seconds: float = 15.0
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @_default_retry()
    async def cancel(self, job_id: str, *, reason: str) -> bool:
        """Mark the given extraction_jobs row as failed.

        Returns True on a successful PATCH (HTTP 2xx), False on a hard
        upstream error (the caller can decide whether to log loud or
        proceed with the cycle anyway). Network/transient errors are
        retried by `_default_retry()`.
        """
        from datetime import UTC, datetime as _dt
        payload = {
            "status": "failed",
            "finished_at": _dt.now(UTC).isoformat(),
            "error": {
                "code": "client_canceled",
                "message": reason,
                "retryable": False,
            },
        }
        response = await self._client.patch(
            "/rest/v1/extraction_jobs",
            params={"job_id": f"eq.{job_id}"},
            json=payload,
        )
        _check_transient(response)
        if response.status_code >= 400:
            logger.warning(
                "ExtractionJobCanceler PATCH failed for job_id=%s "
                "(HTTP %d): %s. Sibling cleanup may re-POST and create a "
                "duplicate Gemini batch.",
                job_id, response.status_code, response.text[:200],
            )
            return False
        logger.info("Canceled stale extraction_jobs row %s (%s)", job_id, reason)
        return True
