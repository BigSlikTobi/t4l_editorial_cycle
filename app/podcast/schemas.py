"""Pydantic models + dataclasses for the podcast module.

These types are the contract between Phase 1 (clustering), Phase 2
(script generation), Phase 3 (TTS render), and Phase 4 (workflow + DB).
Nothing here references users, Spotify, or delivery — that boundary is
owned by `app.delivery`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import EntityMatch, RawArticle

PodcastLanguage = Literal["en-US", "de-DE"]
PodcastSpeaker = Literal["color", "analyst", "narrator"]
PodcastStatus = Literal["pending", "rendering", "rendered", "delivered", "failed"]
PodcastSectionKind = Literal["news", "player_of_day", "team_of_day", "deep_dive"]

PODCAST_SECTION_ORDER: tuple[PodcastSectionKind, ...] = (
    "news",
    "player_of_day",
    "team_of_day",
    "deep_dive",
)


class PodcastCluster(BaseModel):
    """A group of raw articles bundled into one podcast story segment.

    Distinct from the editorial cycle's `StoryEntry` because the podcast
    works in narrative segments (one cluster = one segment of the show),
    not in publishable articles. `story_weight` is the ranker's score —
    higher = should appear earlier and get more airtime.
    """

    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    headline: str
    summary: str
    story_weight: float = Field(ge=0.0)
    source_articles: list[RawArticle] = Field(default_factory=list)
    entities: list[EntityMatch] = Field(default_factory=list)
    narrative_angle: str | None = None  # filled by the ranker agent


class ScriptLine(BaseModel):
    """One spoken line from one persona.

    `prosody_hints` are free-form natural-language audio cues following
    Gemini's TTS prompting guide. Each hint becomes an inline
    parenthetical the model interprets as a delivery direction or
    audible reaction. Use them generously — the format is a real-feeling
    podcast, not a sterile read.

    Examples (all valid as a single hint):
      delivery: "warm", "punchy", "deadpan", "measured", "excited",
                "skeptical", "resigned", "incredulous", "matter-of-fact"
      reactions: "laughs", "laughs softly", "chuckles", "chuckles dryly",
                 "sighs", "groans", "scoffs", "huffs", "gasps", "clicks tongue"
      pacing:   "pause", "long pause", "trails off", "cuts in",
                "speeds up", "slows down"
      emphasis: "emphatic", "wry", "slight smile in voice"
    """

    model_config = ConfigDict(extra="forbid")

    speaker: PodcastSpeaker
    text: str
    prosody_hints: list[str] = Field(default_factory=list)


class PodcastSection(BaseModel):
    """One fixed station in the expanded daily podcast format."""

    model_config = ConfigDict(extra="forbid")

    kind: PodcastSectionKind
    title: str = Field(min_length=1)
    handover: str = ""
    research_summary: str = ""
    lines: list[ScriptLine] = Field(default_factory=list)


class PodcastSectionPlan(BaseModel):
    """Research and story blueprint for the four-section podcast."""

    model_config = ConfigDict(extra="forbid")

    run_date: date | None = None
    red_line: str = Field(default="")
    sections: list[PodcastSection] = Field(default_factory=list)
    rejected_candidates: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def validate_section_order(self) -> "PodcastSectionPlan":
        if self.sections:
            kinds = [section.kind for section in self.sections]
            expected = list(PODCAST_SECTION_ORDER[: len(kinds)])
            if kinds != expected:
                raise ValueError(
                    "section plan must follow fixed podcast order: "
                    "news, player_of_day, team_of_day, deep_dive"
                )
        return self


class PodcastScript(BaseModel):
    """The full script for one episode, structured by section."""

    model_config = ConfigDict(extra="forbid")

    language: PodcastLanguage
    run_date: date
    cold_open: list[ScriptLine] = Field(default_factory=list)
    sections: list[PodcastSection] = Field(default_factory=list)
    body: list[ScriptLine] = Field(default_factory=list)
    outro: list[ScriptLine] = Field(default_factory=list)
    story_count: int = 0
    word_count: int = 0
    # Per-episode metadata sent to Spotify as the episode's title +
    # summary. Generated from the day's story clusters by the metadata
    # agent. Optional — when None the deliver step falls back to a
    # template title/summary so a missed metadata pass never breaks
    # delivery.
    episode_title: str | None = None
    episode_summary: str | None = None

    @model_validator(mode="after")
    def validate_section_order(self) -> "PodcastScript":
        if self.sections:
            kinds = [section.kind for section in self.sections]
            if kinds != list(PODCAST_SECTION_ORDER):
                raise ValueError(
                    "PodcastScript.sections must contain exactly: "
                    "news, player_of_day, team_of_day, deep_dive"
                )
        return self

    def all_lines(self) -> list[ScriptLine]:
        if self.sections:
            section_lines = [
                line for section in self.sections for line in section.lines
            ]
            return [*self.cold_open, *section_lines, *self.outro]
        return [*self.cold_open, *self.body, *self.outro]


class EpisodeMetadata(BaseModel):
    """The two text fields sent to Spotify per episode."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=400)


class PodcastContinuityTopic(BaseModel):
    """One compact, factual-safe memory item from a prior episode.

    This is continuity context only. It can shape callbacks, framing,
    and "we talked about this recently" moments, but it is not evidence
    for new NFL claims.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    entities: list[str] = Field(default_factory=list)
    summary: str = ""
    speaker_callback: str | None = None
    safe_to_reference: bool = True


class PodcastEpisodeMemory(BaseModel):
    """Compact recap persisted after an episode for future continuity."""

    model_config = ConfigDict(extra="forbid")

    run_date: date
    language: PodcastLanguage
    covered_topics: list[PodcastContinuityTopic] = Field(default_factory=list)
    open_loops: list[str] = Field(default_factory=list)
    avoid_repeating: list[str] = Field(default_factory=list)


class PodcastContinuityCallback(BaseModel):
    """A prior-episode callback selected as relevant to today's clusters."""

    model_config = ConfigDict(extra="forbid")

    prior_date: date
    topic: str
    reason: str
    suggested_use: str
    speaker_callback: str | None = None


class PodcastContinuityContext(BaseModel):
    """Filtered prior-episode context passed to planning/script agents."""

    model_config = ConfigDict(extra="forbid")

    lookback_days: int = 0
    useful_callbacks: list[PodcastContinuityCallback] = Field(default_factory=list)
    open_loops: list[str] = Field(default_factory=list)
    avoid_repeating: list[str] = Field(default_factory=list)


class MultiSpeakerTTSPayload(BaseModel):
    """Flat payload handed to the Gemini TTS client.

    `lines` preserves order; each `text` already has any inline
    `(audio tag)` parentheticals embedded by the render layer.
    `voice_map` resolves each `speaker` key (e.g. "color", "analyst")
    to a Gemini voice name. `style_prompt` is the natural-language
    direction prepended to the transcript per Gemini's advanced-
    prompting guide — describes per-speaker tone, allowed reactions,
    and overall delivery feel.
    """

    model_config = ConfigDict(extra="forbid")

    language: PodcastLanguage
    lines: list[tuple[str, str]]  # (speaker_id, text)
    voice_map: dict[str, str]
    title: str
    style_prompt: str | None = None


@dataclass
class RenderResult:
    """Output of `render_to_audio`.

    Default mime is WAV/PCM because Gemini 2.5 TTS preview returns raw
    PCM samples; we write them as a WAV file. The Save-to-Spotify CLI
    accepts WAV directly (alongside mp3/m4a/ogg) so no conversion step
    is required for MVP.
    """

    audio_path: str
    duration_seconds: int
    mime_type: str = "audio/wav"


class PodcastEpisodeRecord(BaseModel):
    """Mirror of one row in `public.podcast_episodes`.

    Used by `PodcastEpisodeWriter` to round-trip rows between the
    workflow and the database.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    run_date: date
    language: PodcastLanguage
    story_count: int = 0
    word_count: int = 0
    duration_seconds: int | None = None
    audio_local_path: str | None = None
    status: PodcastStatus = "pending"
    delivered_at: datetime | None = None
    spotify_episode_id: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    episode_title: str | None = None
    episode_summary: str | None = None
