"""Prior-episode continuity helpers for the podcast runtime.

Continuity memory is deliberately compact and file-based. It helps the
hosts make short, natural callbacks to recent episodes, but it is never
treated as evidence for new football claims.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from app.podcast.schemas import (
    PodcastCluster,
    PodcastContinuityCallback,
    PodcastContinuityContext,
    PodcastContinuityTopic,
    PodcastEpisodeMemory,
    PodcastLanguage,
    PodcastScript,
)

EPISODE_MEMORY_FILENAME = "episode_memory.json"


def _tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]{2,}", value)
        if token.lower()
        not in {
            "the",
            "and",
            "for",
            "with",
            "from",
            "that",
            "this",
            "today",
            "story",
            "about",
            "nfl",
        }
    }


def _cluster_terms(cluster: PodcastCluster) -> set[str]:
    terms = _tokens(
        f"{cluster.headline} {cluster.summary} {cluster.narrative_angle or ''}"
    )
    for entity in cluster.entities:
        terms.update(_tokens(entity.entity_id))
        terms.update(_tokens(entity.matched_name))
    return terms


def _topic_terms(topic: PodcastContinuityTopic) -> set[str]:
    terms = _tokens(f"{topic.topic} {topic.summary}")
    for entity in topic.entities:
        terms.update(_tokens(entity))
    return terms


def _episode_memory_path(episode_dir: Path) -> Path:
    return episode_dir / EPISODE_MEMORY_FILENAME


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_recent_episode_memories(
    *,
    root: Path,
    run_date: date,
    language: PodcastLanguage,
    lookback_days: int,
) -> list[PodcastEpisodeMemory]:
    """Load valid memory files before `run_date`, newest first."""

    if lookback_days <= 0 or not root.exists():
        return []

    memories: list[PodcastEpisodeMemory] = []
    for path in sorted(root.glob(f"**/{EPISODE_MEMORY_FILENAME}"), reverse=True):
        try:
            memory = PodcastEpisodeMemory.model_validate(_read_json(path))
        except Exception:
            continue
        delta_days = (run_date - memory.run_date).days
        if delta_days <= 0 or delta_days > lookback_days:
            continue
        if memory.language != language:
            continue
        memories.append(memory)
    memories.sort(key=lambda item: item.run_date, reverse=True)
    return memories


def build_continuity_context(
    *,
    clusters: list[PodcastCluster],
    memories: list[PodcastEpisodeMemory],
    lookback_days: int,
    max_callbacks: int = 5,
) -> PodcastContinuityContext:
    """Select only prior memory that overlaps today's selected clusters."""

    cluster_terms = set[str]()
    for cluster in clusters:
        cluster_terms.update(_cluster_terms(cluster))

    callbacks: list[PodcastContinuityCallback] = []
    open_loops: list[str] = []
    avoid_repeating: list[str] = []

    for memory in memories:
        avoid_repeating.extend(memory.avoid_repeating)
        for loop in memory.open_loops:
            if _tokens(loop) & cluster_terms:
                open_loops.append(f"{memory.run_date.isoformat()}: {loop}")
        for topic in memory.covered_topics:
            if not topic.safe_to_reference:
                continue
            overlap = _topic_terms(topic) & cluster_terms
            if not overlap:
                continue
            callbacks.append(
                PodcastContinuityCallback(
                    prior_date=memory.run_date,
                    topic=topic.topic,
                    reason="overlaps today's selected clusters: "
                    + ", ".join(sorted(overlap)[:5]),
                    suggested_use=(
                        "Use as a brief callback only if today's current sources "
                        "move the story forward."
                    ),
                    speaker_callback=topic.speaker_callback,
                )
            )
            if len(callbacks) >= max_callbacks:
                break
        if len(callbacks) >= max_callbacks:
            break

    return PodcastContinuityContext(
        lookback_days=lookback_days,
        useful_callbacks=callbacks,
        open_loops=list(dict.fromkeys(open_loops))[:max_callbacks],
        avoid_repeating=list(dict.fromkeys(avoid_repeating))[:max_callbacks],
    )


def load_continuity_context(
    *,
    root: Path,
    run_date: date,
    language: PodcastLanguage,
    clusters: list[PodcastCluster],
    lookback_days: int,
) -> PodcastContinuityContext:
    memories = load_recent_episode_memories(
        root=root,
        run_date=run_date,
        language=language,
        lookback_days=lookback_days,
    )
    return build_continuity_context(
        clusters=clusters,
        memories=memories,
        lookback_days=lookback_days,
    )


def memory_from_script(script: PodcastScript) -> PodcastEpisodeMemory:
    """Create a conservative recap from final section metadata and text."""

    covered_topics: list[PodcastContinuityTopic] = []
    if script.sections:
        for section in script.sections:
            sample = " ".join(line.text for line in section.lines[:4]).strip()
            covered_topics.append(
                PodcastContinuityTopic(
                    topic=section.title,
                    summary=(section.research_summary or sample)[:500],
                    speaker_callback=None,
                    safe_to_reference=True,
                )
            )
    elif script.body:
        sample = " ".join(line.text for line in script.body[:8]).strip()
        covered_topics.append(
            PodcastContinuityTopic(
                topic=f"{script.run_date.isoformat()} episode",
                summary=sample[:500],
                safe_to_reference=True,
            )
        )

    return PodcastEpisodeMemory(
        run_date=script.run_date,
        language=script.language,
        covered_topics=covered_topics,
        open_loops=[],
        avoid_repeating=[],
    )


def write_episode_memory(episode_dir: Path, script: PodcastScript) -> Path:
    path = _episode_memory_path(episode_dir)
    _write_json(path, memory_from_script(script).model_dump(mode="json"))
    return path
