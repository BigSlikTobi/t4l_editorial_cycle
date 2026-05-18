"""Artifact-based German podcast runtime helpers."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters import RawArticleDbReader
from app.config import Settings
from app.podcast.analytics import build_analytics_pack, unavailable_pack
from app.podcast.clustering import group_for_podcast, select_clusters_for_budget
from app.podcast.continuity import load_continuity_context
from app.podcast.factcheck import (
    PodcastClaim,
    PodcastSource,
    assert_valid_episode,
)
from app.podcast.pronunciation import (
    PodcastPronunciationGuide,
    build_pronunciation_guide,
)
from app.podcast.schemas import PodcastCluster, PodcastScript


DEFAULT_EPISODE_ROOT = Path("var/podcast/episodes")
DEFAULT_HOST_MEMORY_PATH = Path("editorial_memory/podcast/host_relationship.md")

REQUIRED_PRE_RENDER_FILES = (
    "manifest.json",
    "selected_clusters.json",
    "analytics_pack.json",
    "continuity_context.json",
    "section_plan.json",
    "research_a.md",
    "research_b.md",
    "research_news.md",
    "research_player_of_day.md",
    "research_team_of_day.md",
    "research_deep_dive.md",
    "section_synthesis.md",
    "host_authority_notes.md",
    "notes.md",
    "source_ledger.json",
    "claim_ledger.json",
    "pronunciation_guide.json",
    "conversation_memory_snapshot.md",
    "script.de-DE.json",
)

REQUIRED_POST_RUN_FILES = (
    *REQUIRED_PRE_RENDER_FILES,
    "tts_status.json",
    "audio_probe.json",
    "upload_metadata.json",
)


class PodcastArtifactManifest(BaseModel):
    """Manifest for one German artifact-based podcast episode."""

    model_config = ConfigDict(extra="forbid")

    run_date: date
    language: str = "de-DE"
    title: str
    summary: str
    status: str = "initialized"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    audio_path: str | None = None
    spotify_episode_id: str | None = None
    selected_cluster_count: int = 0


@dataclass(frozen=True)
class ArtifactValidationSummary:
    episode_dir: Path
    warnings: list[str]


def episode_dir_for(run_date: date, *, root: Path = DEFAULT_EPISODE_ROOT) -> Path:
    return root / run_date.isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_host_memory(path: Path = DEFAULT_HOST_MEMORY_PATH) -> str:
    if not path.exists():
        raise FileNotFoundError(f"host memory not found: {path}")
    return path.read_text(encoding="utf-8")


def copy_host_memory_snapshot(
    episode_dir: Path,
    *,
    host_memory_path: Path = DEFAULT_HOST_MEMORY_PATH,
) -> Path:
    episode_dir.mkdir(parents=True, exist_ok=True)
    target = episode_dir / "conversation_memory_snapshot.md"
    shutil.copyfile(host_memory_path, target)
    return target


async def initialize_episode_artifacts(
    *,
    settings: Settings,
    feed_reader: RawArticleDbReader,
    run_date: date | None = None,
    lookback_hours: int | None = None,
    root: Path = DEFAULT_EPISODE_ROOT,
    host_memory_path: Path = DEFAULT_HOST_MEMORY_PATH,
) -> Path:
    """Create the episode folder, cluster selected stories, and snapshot memory."""

    resolved_date = run_date or datetime.now(UTC).date()
    out_dir = episode_dir_for(resolved_date, root=root)
    out_dir.mkdir(parents=True, exist_ok=True)

    articles = await feed_reader.fetch_raw_articles(
        lookback_hours=lookback_hours or settings.podcast_lookback_hours
    )
    clusters = group_for_podcast(articles)
    selected = select_clusters_for_budget(
        clusters,
        target_word_count=settings.podcast_target_word_count,
        min_word_count=settings.podcast_min_word_count,
    )

    manifest = PodcastArtifactManifest(
        run_date=resolved_date,
        title=f"T4L Morgenbriefing - {resolved_date.isoformat()}",
        summary=(
            "Deutschsprachiger T4L NFL-Morgenpodcast mit Marcus und Robin, "
            "erstellt im Codex-Runtime-Flow."
        ),
        selected_cluster_count=len(selected),
    )
    write_json(out_dir / "manifest.json", manifest.model_dump(mode="json"))
    write_json(
        out_dir / "selected_clusters.json",
        [cluster.model_dump(mode="json") for cluster in selected],
    )
    write_json(
        out_dir / "pronunciation_guide.json",
        build_pronunciation_guide(selected).model_dump(mode="json"),
    )
    continuity_context = load_continuity_context(
        root=root,
        run_date=resolved_date,
        language="de-DE",
        clusters=selected,
        lookback_days=settings.podcast_continuity_days,
    )
    write_json(
        out_dir / "continuity_context.json",
        continuity_context.model_dump(mode="json"),
    )
    if settings.podcast_analytics_enabled:
        try:
            analytics_pack = build_analytics_pack(
                selected,
                run_date=resolved_date,
                season=settings.podcast_analytics_season,
            )
        except Exception as exc:  # noqa: BLE001 - analytics should not kill init
            analytics_pack = unavailable_pack(
                run_date=resolved_date,
                error_message=str(exc),
            )
        write_json(out_dir / "analytics_pack.json", analytics_pack.model_dump(mode="json"))
    else:
        write_json(
            out_dir / "analytics_pack.json",
            unavailable_pack(
                run_date=resolved_date,
                error_message="podcast_analytics_enabled is false",
            ).model_dump(mode="json"),
        )
    copy_host_memory_snapshot(out_dir, host_memory_path=host_memory_path)
    return out_dir


def load_selected_clusters(episode_dir: Path) -> list[PodcastCluster]:
    return [
        PodcastCluster.model_validate(row)
        for row in read_json(episode_dir / "selected_clusters.json")
    ]


def load_script(episode_dir: Path) -> PodcastScript:
    return PodcastScript.model_validate(read_json(episode_dir / "script.de-DE.json"))


def load_sources(episode_dir: Path) -> list[PodcastSource]:
    return [
        PodcastSource.model_validate(row)
        for row in read_json(episode_dir / "source_ledger.json")
    ]


def load_claims(episode_dir: Path) -> list[PodcastClaim]:
    return [
        PodcastClaim.model_validate(row)
        for row in read_json(episode_dir / "claim_ledger.json")
    ]


def load_pronunciation_guide(episode_dir: Path) -> PodcastPronunciationGuide:
    return PodcastPronunciationGuide.model_validate(
        read_json(episode_dir / "pronunciation_guide.json")
    )


def validate_episode_artifacts(episode_dir: Path) -> ArtifactValidationSummary:
    """Validate required files, claim ledger, and host-memory naturalness."""

    missing = [
        name for name in REQUIRED_PRE_RENDER_FILES if not (episode_dir / name).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"episode is missing required files: {', '.join(missing)}"
        )

    script = load_script(episode_dir)
    if script.language != "de-DE":
        raise ValueError(f"artifact runtime only supports de-DE; got {script.language!r}")

    host_memory = (episode_dir / "conversation_memory_snapshot.md").read_text(
        encoding="utf-8"
    )
    sources = load_sources(episode_dir)
    claims = load_claims(episode_dir)
    load_pronunciation_guide(episode_dir)
    assert_valid_episode(
        sources=sources,
        claims=claims,
        script=script,
        host_memory=host_memory,
    )
    return ArtifactValidationSummary(episode_dir=episode_dir, warnings=[])
