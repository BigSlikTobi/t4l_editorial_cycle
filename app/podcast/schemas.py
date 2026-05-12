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

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import EntityMatch, RawArticle

PodcastLanguage = Literal["en-US", "de-DE"]
PodcastSpeaker = Literal["color", "analyst", "narrator"]
PodcastStatus = Literal["pending", "rendering", "rendered", "delivered", "failed"]


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


class PodcastScript(BaseModel):
    """The full script for one episode, structured by section."""

    model_config = ConfigDict(extra="forbid")

    language: PodcastLanguage
    run_date: date
    cold_open: list[ScriptLine] = Field(default_factory=list)
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

    def all_lines(self) -> list[ScriptLine]:
        return [*self.cold_open, *self.body, *self.outro]


class EpisodeMetadata(BaseModel):
    """The two text fields sent to Spotify per episode."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=400)


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
