"""Thin HTTP clients for external image services.

Kept separate from the selector so the cascade orchestration stays testable
without spinning up real HTTP mocks.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.adapters import ExternalServiceError, _default_retry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageCandidate:
    """One candidate returned by the image_selection function."""
    image_url: str
    original_url: str
    source: str
    author: str


class ImageSelectionClient:
    """Client for the deployed tackle_4_loss_intelligence image_selection cloud function.

    Payload shape (nested `search` / `llm` blocks) matches what the deployed
    function's factory expects — see src/functions/image_selection/core/factory.py.
    """

    def __init__(
        self,
        base_url: str,
        *,
        google_custom_search_key: str | None = None,
        google_custom_search_engine_id: str | None = None,
        llm_api_key: str | None = None,
        llm_model: str = "gpt-5.4-mini",
        llm_provider: str = "openai",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._google_key = google_custom_search_key
        self._google_cx = google_custom_search_engine_id
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model
        self._llm_provider = llm_provider
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    @_default_retry()
    async def select_image(
        self,
        *,
        article_text: str,
        required_terms: list[str],
        num_images: int = 1,
    ) -> ImageCandidate | None:
        """Return one ImageCandidate from the cloud function, or None.

        Loosened thresholds — our own vision validator gates quality on the
        editorial-cycle side, so we can let the function return more candidates.
        """
        payload: dict[str, Any] = {
            "article_text": article_text,
            "required_terms": required_terms,
            "num_images": num_images,
            "strict_mode": False,
            "min_relevance_score": 3.0,
            "min_source_score": 0.3,
        }
        # LLM query optimization — enable if we have a key; otherwise disable.
        if self._llm_api_key:
            payload["enable_llm"] = True
            payload["llm"] = {
                "provider": self._llm_provider,
                "model": self._llm_model,
                "api_key": self._llm_api_key,
            }
        else:
            payload["enable_llm"] = False

        if self._google_key and self._google_cx:
            payload["search"] = {
                "api_key": self._google_key,
                "engine_id": self._google_cx,
            }
        # NB: intentionally NOT sending a `supabase` block — we re-host ourselves.

        response = await self._client.post(
            f"{self._base_url}/select_article_images",
            json=payload,
        )
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"image_selection returned {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        images = data.get("images") or []
        if not images:
            return None

        first = images[0]
        if not isinstance(first, dict):
            return None
        # The function returns `image_url` (hosted by the function if it did upload, else the
        # original candidate URL). Without a `supabase` block, that falls back to the source URL.
        url = first.get("image_url") or first.get("original_url")
        if not url:
            return None
        return ImageCandidate(
            image_url=url,
            original_url=first.get("original_url") or url,
            source=first.get("source") or "",
            author=first.get("author") or "",
        )


class WikimediaCommonsClient:
    """Search Wikimedia Commons for a CC-licensed image.

    Used as a tier-1 fallback when the Google CC Search path finds nothing —
    Commons has broad coverage of NFL stadiums, coaches, practice scenes, and
    historical player photos under free licenses. Quality varies, so the
    calling selector still runs every candidate through the vision validator.
    """

    _API_URL = "https://commons.wikimedia.org/w/api.php"
    _HTML_TAG_RE = re.compile(r"<[^>]+>")

    def __init__(
        self,
        *,
        user_agent: str = "t4l-editorial-cycle/0.1 (contact: businesstobiaslatta@gmail.com)",
        timeout_seconds: float = 15.0,
        min_width: int = 600,
    ) -> None:
        self._min_width = min_width
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent},
        )

    async def close(self) -> None:
        await self._client.aclose()

    @_default_retry()
    async def search_image(self, query: str) -> ImageCandidate | None:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": "6",
            "gsrlimit": "10",
            "prop": "imageinfo",
            "iiprop": "url|mime|size|extmetadata",
        }
        response = await self._client.get(self._API_URL, params=params)
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"wikimedia returned {response.status_code}: {response.text[:200]}"
            )
        pages = (response.json().get("query") or {}).get("pages") or {}
        for page in pages.values():
            info_list = page.get("imageinfo") or []
            if not info_list:
                continue
            info = info_list[0]
            mime = info.get("mime", "")
            if mime not in {"image/jpeg", "image/png", "image/webp"}:
                continue
            if int(info.get("width", 0)) < self._min_width:
                continue
            meta = info.get("extmetadata") or {}
            author = self._HTML_TAG_RE.sub(
                "", (meta.get("Artist") or {}).get("value", "")
            ).strip()
            return ImageCandidate(
                image_url=info["url"],
                original_url=info.get("descriptionurl") or info["url"],
                source="wikimedia_commons",
                author=author,
            )
        return None


