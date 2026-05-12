"""League-wide clustering for the podcast.

Distinct from `editorial.helpers.group_by_entity` (which is team-anchored
by virtue of the editorial cycle's per-team workflow). For the podcast
we want league-wide breadth: a story about Patrick Mahomes is one story
regardless of how many other Chiefs articles also exist.

Algorithm:

1. Per-article best entity, with podcast priority `player < game < team`.
   Articles whose only entity is a team are explicitly NOT merged with
   other team-only articles for that same team — that's how the editorial
   cycle gets its team-level mega-clusters, which we want to avoid.
2. First pass: bucket by best non-team entity. Team-only singletons stay
   isolated.
3. Second pass: merge non-team singletons into existing multi-source
   clusters via reverse entity index, but only on non-team overlap.
4. Score each cluster: `story_weight = source_count + 0.5 *
   cross_entity_overlap + recency_bonus`. Recency bonus is a sigmoid
   over hours-since-newest-article.
5. Return clusters sorted by `story_weight` descending.

`select_clusters_for_budget` then walks the ranked list and stops when
the cumulative weight × calibration constant hits the target word count.
On a slow news day where the entire feed produces less weight than the
floor, return everything — the dialogue writer will produce a shorter
episode and we ship anyway.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Final

from app.podcast.schemas import PodcastCluster
from app.schemas import EntityMatch, RawArticle

# Priority for cluster anchoring. Lower = more specific = preferred. Team is
# intentionally last so two articles whose only overlap is a team do NOT
# collapse into the same cluster.
_ENTITY_PRIORITY: Final[dict[str, int]] = {"player": 0, "game": 1, "team": 99}

# Calibration: how many words of audio one unit of story_weight justifies.
# Tune after observing the first ~5 episodes; current value is a
# best-guess for a 25-min target on a typical 6-8 cluster day.
WORDS_PER_WEIGHT_UNIT: Final[int] = 350


def _best_non_team_entity(article: RawArticle) -> EntityMatch | None:
    """Return the most specific non-team entity, or None if only teams."""
    candidates = [e for e in article.entities if e.entity_type != "team"]
    if not candidates:
        return None
    candidates.sort(key=lambda e: _ENTITY_PRIORITY.get(e.entity_type, 50))
    return candidates[0]


def _team_only(article: RawArticle) -> bool:
    return all(e.entity_type == "team" for e in article.entities) and bool(article.entities)


def _make_cluster_id(anchor_key: str, index: int) -> str:
    return f"podcast::{anchor_key}::{index}"


def _recency_bonus(article_count: int) -> float:
    """Bonus that grows with cluster size; sigmoid avoids unbounded growth.

    We don't have article timestamps in `RawArticle` (the feed is fetched
    over a fixed lookback window). Use article_count as the proxy: more
    sources = fresher / more newsworthy.
    """
    return 1.0 / (1.0 + math.exp(-(article_count - 2) * 0.5))


def _build_cluster(
    *,
    cluster_id: str,
    headline: str,
    summary: str,
    articles: list[RawArticle],
) -> PodcastCluster:
    # Aggregate entities: dedupe by (entity_type, entity_id).
    seen: set[tuple[str, str]] = set()
    entities: list[EntityMatch] = []
    for art in articles:
        for ent in art.entities:
            key = (ent.entity_type, ent.entity_id)
            if key in seen:
                continue
            seen.add(key)
            entities.append(ent)

    cross_entity_overlap = max(0, len(entities) - 1)
    weight = (
        float(len(articles))
        + 0.5 * cross_entity_overlap
        + _recency_bonus(len(articles))
    )
    return PodcastCluster(
        cluster_id=cluster_id,
        headline=headline,
        summary=summary,
        story_weight=weight,
        source_articles=articles,
        entities=entities,
    )


def group_for_podcast(articles: list[RawArticle]) -> list[PodcastCluster]:
    """Cluster raw articles for league-wide podcast use.

    Returns clusters sorted by `story_weight` descending. Singletons whose
    only shared entity with other articles is a team are returned as
    their own one-article clusters (still ranked, but with weight ~1.0).
    """
    if not articles:
        return []

    # First pass: bucket by best non-team entity.
    buckets: dict[str, list[RawArticle]] = defaultdict(list)
    team_only_articles: list[RawArticle] = []
    no_entity_articles: list[RawArticle] = []
    for article in articles:
        anchor = _best_non_team_entity(article)
        if anchor is None:
            if _team_only(article):
                team_only_articles.append(article)
            else:
                no_entity_articles.append(article)
            continue
        buckets[f"{anchor.entity_type}:{anchor.entity_id}"].append(article)

    # Second pass: merge non-team singletons into multi-source clusters
    # via reverse entity index — but only on non-team overlap.
    multi_source: dict[str, list[RawArticle]] = {
        label: arts for label, arts in buckets.items() if len(arts) >= 2
    }
    pending: dict[str, RawArticle] = {
        label: arts[0] for label, arts in buckets.items() if len(arts) == 1
    }

    cluster_entity_index: dict[str, str] = {}
    for label, arts in multi_source.items():
        for art in arts:
            for ent in art.entities:
                if ent.entity_type == "team":
                    continue
                key = f"{ent.entity_type}:{ent.entity_id}"
                cluster_entity_index.setdefault(key, label)

    leftover_singletons: list[RawArticle] = []
    for label, article in pending.items():
        merged = False
        for ent in article.entities:
            if ent.entity_type == "team":
                continue
            key = f"{ent.entity_type}:{ent.entity_id}"
            target = cluster_entity_index.get(key)
            if target is not None and target != label:
                multi_source[target].append(article)
                merged = True
                break
        if not merged:
            leftover_singletons.append(article)

    # Build PodcastCluster objects.
    clusters: list[PodcastCluster] = []
    for idx, (label, arts) in enumerate(multi_source.items()):
        anchor = arts[0]
        clusters.append(
            _build_cluster(
                cluster_id=_make_cluster_id(label, idx),
                headline=anchor.title,
                summary=arts[0].title,
                articles=arts,
            )
        )

    # Singletons: each becomes its own cluster.
    for idx, article in enumerate(
        [*leftover_singletons, *team_only_articles, *no_entity_articles]
    ):
        clusters.append(
            _build_cluster(
                cluster_id=_make_cluster_id(f"single:{article.id}", idx),
                headline=article.title,
                summary=article.title,
                articles=[article],
            )
        )

    clusters.sort(key=lambda c: c.story_weight, reverse=True)
    return clusters


def select_clusters_for_budget(
    clusters: Iterable[PodcastCluster],
    *,
    target_word_count: int,
    min_word_count: int,
    words_per_weight_unit: int = WORDS_PER_WEIGHT_UNIT,
    min_clusters: int = 6,
    max_clusters: int = 12,
) -> list[PodcastCluster]:
    """Pick clusters for the dialogue writer's airtime budget.

    The dialogue writer produces roughly 400-700 words per cluster
    regardless of upstream `story_weight` (the LLM doesn't write 4000
    words about one story just because that story is heavy). So we
    select the top-K clusters where K is calibrated to the word-count
    target, with a sane min/max bracket so a big news day doesn't blow
    out and a slow day still tries for a reasonable show.

    A per-cluster cap on the cumulative contribution prevents one
    mega-cluster from satisfying the target alone — if the top cluster
    has weight 12 and lower clusters have weight 1, we still want
    breadth on the show, not a 25-min monologue about one trade.
    """
    ranked = sorted(clusters, key=lambda c: c.story_weight, reverse=True)
    if not ranked:
        return []

    # Cap per-cluster contribution so one big cluster cannot trip the
    # budget before we've selected at least `min_clusters` items.
    per_cluster_cap = max(1, target_word_count // max(min_clusters, 1))

    selected: list[PodcastCluster] = []
    cumulative = 0.0
    for cluster in ranked:
        if len(selected) >= max_clusters:
            break
        selected.append(cluster)
        contribution = min(
            cluster.story_weight * words_per_weight_unit,
            per_cluster_cap,
        )
        cumulative += contribution
        if cumulative >= target_word_count and len(selected) >= min_clusters:
            break

    # Slow news day: total possible (capped) contribution is below the
    # adaptive floor. Return everything we have — the dialogue writer
    # produces a shorter episode, no padding.
    capped_total = sum(
        min(c.story_weight * words_per_weight_unit, per_cluster_cap) for c in ranked
    )
    if capped_total < min_word_count:
        return ranked

    return selected
