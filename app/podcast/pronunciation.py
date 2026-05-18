"""Pronunciation guide support for podcast research and TTS rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.podcast.schemas import PodcastCluster

PronunciationConfidence = Literal["high", "medium", "low"]


class PronunciationEntry(BaseModel):
    """One term the TTS model should pronounce a specific way."""

    model_config = ConfigDict(extra="forbid")

    term: str
    spoken_as: str
    note: str | None = None
    source_url: str | None = None
    confidence: PronunciationConfidence = "medium"


class PodcastPronunciationGuide(BaseModel):
    """Per-episode pronunciation guide assembled during research."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    entries: list[PronunciationEntry] = Field(default_factory=list)
    candidates_to_check: list[str] = Field(default_factory=list)


DEFAULT_PRONUNCIATIONS: tuple[PronunciationEntry, ...] = (
    PronunciationEntry(
        term="T4L",
        spoken_as="Tackle for Loss",
        note=(
            "Brand name. In German audio, say the English phrase 'Tackle for Loss'; "
            "do not spell it as letters or say 'Tee Vier Ell'."
        ),
        confidence="high",
    ),
    PronunciationEntry(
        term="Tackle4Loss",
        spoken_as="Tackle for Loss",
        note="Same brand pronunciation as T4L.",
        confidence="high",
    ),
    PronunciationEntry(
        term="Tyler Shough",
        spoken_as="TY-ler SHUCK",
        note="Last name rhymes with 'shuck' / 'aw shucks'.",
        source_url="https://www.si.com/nfl/nfl/tyler-shough-talks-about-his-name-saints-coach-kellen-moore",
        confidence="high",
    ),
    PronunciationEntry(
        term="Shough",
        spoken_as="SHUCK",
        note="Rhymes with 'shuck' / 'aw shucks'.",
        source_url="https://www.si.com/nfl/nfl/tyler-shough-talks-about-his-name-saints-coach-kellen-moore",
        confidence="high",
    ),
)


def _entity_candidate_names(clusters: list[PodcastCluster]) -> list[str]:
    names: set[str] = set()
    for cluster in clusters:
        for entity in cluster.entities:
            if entity.entity_type != "player":
                continue
            name = entity.matched_name.strip()
            if not name:
                continue
            # Team names and common one-token surnames add noise; candidate
            # review should focus on names a TTS model may mangle.
            if len(name.split()) < 2:
                continue
            names.add(name)
    return sorted(names)


def build_pronunciation_guide(
    clusters: list[PodcastCluster],
) -> PodcastPronunciationGuide:
    """Create the starter guide for one episode.

    The defaults are TTS-critical rules we always want. Candidate names are
    research prompts: humans/agents should verify only the names that will
    actually be spoken and move confirmed pronunciations into `entries`.
    """

    default_terms = {entry.term.lower() for entry in DEFAULT_PRONUNCIATIONS}
    candidates = [
        name
        for name in _entity_candidate_names(clusters)
        if name.lower() not in default_terms
    ]
    return PodcastPronunciationGuide(
        entries=list(DEFAULT_PRONUNCIATIONS),
        candidates_to_check=candidates,
    )


def render_pronunciation_prompt(guide: PodcastPronunciationGuide | None) -> str | None:
    """Render a compact TTS prompt section from confirmed guide entries."""

    if guide is None:
        return None
    entries = [
        entry
        for entry in guide.entries
        if entry.term.strip() and entry.spoken_as.strip()
    ]
    if not entries:
        return None
    lines = [
        "## Pronunciation Guide",
        "",
        "Apply these pronunciations exactly whenever the term appears. "
        "These are pronunciation instructions, not spoken show copy.",
    ]
    for entry in entries:
        suffix = f" ({entry.note})" if entry.note else ""
        lines.append(f"- {entry.term}: say {entry.spoken_as}.{suffix}")
    return "\n".join(lines)
