from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.schemas import CyclePublishPlan, PublishedStoryRecord, RawArticle, StoryEntry

T = TypeVar("T")

_ARTICLE_TRUNCATION_WORD_LIMIT: int = 600


def truncate_article_content(content: str, *, word_limit: int = _ARTICLE_TRUNCATION_WORD_LIMIT) -> str:
    words = content.split(maxsplit=word_limit)
    if len(words) <= word_limit:
        return content
    return " ".join(words[:word_limit]) + " [truncated]"


def deduplicate(articles: list[RawArticle]) -> tuple[list[RawArticle], int]:
    seen: set[str] = set()
    unique: list[RawArticle] = []
    for article in articles:
        if article.url not in seen:
            seen.add(article.url)
            unique.append(article)
    return unique, len(articles) - len(unique)


class GroupedArticles:
    """Result of entity-based grouping, split by cluster size."""

    def __init__(
        self,
        multi_source: dict[str, list[RawArticle]],
        single_source: list[RawArticle],
    ) -> None:
        self.multi_source = multi_source
        self.single_source = single_source

    @property
    def total_clusters(self) -> int:
        return len(self.multi_source) + len(self.single_source)


_ENTITY_PRIORITY = {"player": 0, "game": 1, "team": 2}


def _best_entity(article: RawArticle) -> str | None:
    """Return the most specific entity_id for clustering.

    Priority: player > game > team.  More specific entities produce tighter
    clusters (two articles sharing a player are more likely to cover the same
    story than two articles sharing a team).
    """
    entities = sorted(
        article.entities,
        key=lambda e: _ENTITY_PRIORITY.get(e.entity_type, 99),
    )
    return entities[0].entity_id if entities else None


def group_by_entity(articles: list[RawArticle]) -> GroupedArticles:
    """Group articles by their most specific shared entity.

    1. Assign each article to its best (most specific) entity.
    2. Split into multi-source clusters (2+ articles) and pending singles.
    3. Merge singles into an existing multi-source cluster if *any* of their
       entities overlap — prevents the same story appearing as both a cluster
       result and a standalone candidate.
    """
    groups: dict[str, list[RawArticle]] = defaultdict(list)
    for article in articles:
        eid = _best_entity(article)
        if eid is None:
            groups["_ungrouped"].append(article)
        else:
            groups[eid].append(article)

    # First pass: identify multi-source clusters
    multi_source: dict[str, list[RawArticle]] = {}
    pending_singles: dict[str, RawArticle] = {}
    for label, arts in groups.items():
        if len(arts) >= 2:
            multi_source[label] = arts
        else:
            pending_singles[label] = arts[0]

    # Build a reverse index: entity_id -> cluster label for fast lookup
    cluster_entity_index: dict[str, str] = {}
    for label, arts in multi_source.items():
        for art in arts:
            for e in art.entities:
                if e.entity_id not in cluster_entity_index:
                    cluster_entity_index[e.entity_id] = label

    # Second pass: merge singles into existing clusters on any shared entity
    single_source: list[RawArticle] = []
    for label, article in pending_singles.items():
        merged = False
        for e in article.entities:
            target_label = cluster_entity_index.get(e.entity_id)
            if target_label is not None and target_label in multi_source:
                multi_source[target_label].append(article)
                merged = True
                break
        if not merged:
            single_source.append(article)

    return GroupedArticles(multi_source=multi_source, single_source=single_source)


def fingerprint(key_facts: list[str]) -> str:
    normalized = sorted(fact.strip().lower() for fact in key_facts if fact.strip())
    payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def count_prevented_duplicates(
    plan_fingerprints: list[str],
    published_state: list[PublishedStoryRecord],
) -> int:
    published_fps = {r.story_fingerprint for r in published_state}
    return sum(1 for fp in plan_fingerprints if fp in published_fps)


def coerce_output(payload: Any, schema: type[T]) -> T:
    if isinstance(payload, schema):
        return payload
    if isinstance(payload, BaseModel):
        try:
            normalized = payload.model_dump(mode="json")
            filtered = {
                field_name: normalized[field_name]
                for field_name in schema.model_fields
                if field_name in normalized
            }
            return schema.model_validate(filtered)
        except ValidationError as exc:
            raise ValueError(
                f"Agent returned invalid payload for {schema.__name__}: {exc}"
            ) from exc
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return schema.model_validate_json(stripped)
            except ValidationError as exc:
                raise ValueError(
                    f"Agent returned invalid JSON payload for {schema.__name__}: {exc}"
                ) from exc
        raise ValueError(f"Agent returned plain-text output for {schema.__name__}: {payload}")
    if isinstance(payload, dict):
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"Agent returned invalid payload for {schema.__name__}: {exc}") from exc
    raise ValueError(f"Agent returned unsupported payload type for {schema.__name__}: {type(payload)!r}")


def deduplicate_plan(plan: CyclePublishPlan) -> CyclePublishPlan:
    """Remove duplicate stories from the plan by fingerprint.

    When the same underlying story appears in multiple entity clusters (e.g. a
    trade shows up in both the BUF and KC clusters), the orchestrator may rank
    both.  This deterministic post-processing step keeps the highest-ranked
    entry and moves duplicates to skipped_stories.
    """
    seen_fingerprints: set[str] = set()
    unique_stories: list[StoryEntry] = []
    extra_skipped: list[StoryEntry] = []

    for story in plan.stories:
        if story.story_fingerprint in seen_fingerprints:
            extra_skipped.append(
                story.model_copy(
                    update={
                        "action": "skip",
                        "reasoning": f"Duplicate of higher-ranked story (fingerprint {story.story_fingerprint})",
                    }
                )
            )
        else:
            seen_fingerprints.add(story.story_fingerprint)
            unique_stories.append(story)

    # Also deduplicate within skipped_stories
    for story in plan.skipped_stories:
        if story.story_fingerprint not in seen_fingerprints:
            seen_fingerprints.add(story.story_fingerprint)
            extra_skipped.append(story)

    duplicates_removed = len(plan.stories) - len(unique_stories)

    return CyclePublishPlan(
        stories=unique_stories,
        skipped_stories=extra_skipped,
        reasoning=plan.reasoning,
        prevented_duplicates=plan.prevented_duplicates + duplicates_removed,
    )
