"""Client for the article_knowledge_extraction cloud function.

Given article text, returns resolved entities (players, teams, coaches)
and ranked topics. Used to replace the legacy feed's pre-extracted
`entities[]` field.

The OpenAI API key is NOT sent in the request body; the cloud function
reads it from its own runtime env (e.g. `OPENAI_API_KEY`) so the secret
does not live in request logs or retained payloads. We still pass the
non-secret `model` name so the caller can pick a model per call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.clients.base import AsyncJobClient, SupabaseJobsConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedEntity:
    entity_type: str
    entity_id: str
    mention_text: str | None
    matched_name: str | None
    confidence: float | None
    team_abbr: str | None
    position: str | None

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> "ResolvedEntity":
        return cls(
            entity_type=item.get("entity_type", ""),
            entity_id=str(item.get("entity_id", "")),
            mention_text=item.get("mention_text"),
            matched_name=item.get("matched_name"),
            confidence=_coerce_float(item.get("confidence")),
            team_abbr=item.get("team_abbr"),
            position=item.get("position"),
        )


@dataclass(frozen=True)
class Topic:
    topic: str
    confidence: float | None
    rank: int | None

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> "Topic":
        return cls(
            topic=item.get("topic", ""),
            confidence=_coerce_float(item.get("confidence")),
            rank=_coerce_int(item.get("rank")),
        )


@dataclass(frozen=True)
class KnowledgeResult:
    topics: list[Topic] = field(default_factory=list)
    entities: list[ResolvedEntity] = field(default_factory=list)
    unresolved_entities: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "KnowledgeResult":
        return cls(
            topics=[Topic.from_payload(t) for t in payload.get("topics", [])],
            entities=[
                ResolvedEntity.from_payload(e) for e in payload.get("entities", [])
            ],
            unresolved_entities=list(payload.get("unresolved_entities", []) or []),
        )


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class KnowledgeExtractionClient:
    """Extracts topics + resolved entities from one article at a time."""

    def __init__(
        self,
        *,
        submit_url: str,
        poll_url: str,
        supabase: SupabaseJobsConfig,
        auth_token: str | None = None,
        openai_model: str = "gpt-5.4-mini",
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
        self._openai_model = openai_model

    async def close(self) -> None:
        await self._job.close()

    async def extract(
        self,
        *,
        article_id: str,
        text: str,
        title: str | None = None,
        url: str | None = None,
        max_topics: int = 5,
        max_entities: int = 15,
        resolve_entities: bool = True,
        confidence_threshold: float = 0.6,
    ) -> KnowledgeResult:
        payload: dict[str, Any] = {
            "article": {
                "article_id": article_id,
                "text": text,
                "title": title,
                "url": url,
            },
            "options": {
                "max_topics": max_topics,
                "max_entities": max_entities,
                "resolve_entities": resolve_entities,
                "confidence_threshold": confidence_threshold,
            },
            "llm": {
                "provider": "openai",
                "model": self._openai_model,
            },
        }
        result = await self._job.run(payload)
        parsed = KnowledgeResult.from_payload(result)
        logger.info(
            "knowledge_extraction: article_id=%s topics=%d entities=%d unresolved=%d",
            article_id,
            len(parsed.topics),
            len(parsed.entities),
            len(parsed.unresolved_entities),
        )
        return parsed
