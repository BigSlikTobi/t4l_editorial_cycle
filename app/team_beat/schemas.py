"""Schemas for the team-beat cycle.

Three layers:
  * BeatBrief        — what the Team Beat Reporter Agent emits per team.
  * BeatRoundup      — the persisted row shape for public.team_roundup.
  * BeatCycleResult  — outcome record per (team, cycle) attempt; written to
                        public.team_beat_cycle_state for reliability tracking.

The agent output uses pydantic so the OpenAI Agents SDK can hand it back
directly via `output_type=`. The DB-shaped models are plain dataclasses
because they exist purely to round-trip through PostgREST.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------- enums


class BeatOutcome(str, Enum):
    """Per-(team, cycle) attempt outcome.

    `filed` — beat reporter wrote a brief and (if audio succeeded) a row
              landed in public.team_roundup.
    `no_news` — beat reporter judged the 12h window had nothing
                editorially worthwhile. Expected and frequent; never an
                error condition.
    `error` — anything went wrong (agent crash, batch service failure,
              DB write failure). Captured for observability.
    """

    FILED = "filed"
    NO_NEWS = "no_news"
    ERROR = "error"


CycleSlot = Literal["AM", "PM"]


# --------------------------------------------------------------- agent output


class BeatBrief(BaseModel):
    """Output of the Team Beat Reporter Agent for a single team in a cycle.

    `should_file=False` is the news-reactive opt-out: the agent has read
    the 12h window and judged nothing is editorially worthwhile. In that
    case the body fields may be empty; downstream skips TTS and writes a
    `no_news` outcome record.
    """

    team_code: str
    persona_name: str = ""
    should_file: bool = Field(
        ...,
        description=(
            "True iff the 12h window contains a story worth filing. "
            "False = explicit editorial silence; downstream emits a "
            "no_news cycle state record and does not call TTS."
        ),
    )
    skip_reason: str = Field(
        default="",
        description="When should_file=False, one short sentence explaining why.",
    )
    headline: str = ""
    en_body: str = ""
    de_body: str = ""
    dateline_city: str = Field(
        default="",
        description=(
            "City the byline dateline reads from (e.g. 'East Rutherford' "
            "for NYJ, 'Chicago' for CHI). Powers the 'Filed by ... — "
            "covering the [Team], [City]' wire-style stamp."
        ),
    )


class RadioScript(BaseModel):
    """Output of the Radio Script Agent. DE only.

    The script opens with a director's-notes preamble (style/pacing/tone
    cues for Gemini TTS) and uses inline audio tags like [whispers],
    [excited], [pause] mid-script per the Gemini speech-generation guide.
    No SSML.
    """

    team_code: str
    de_text: str = Field(
        ...,
        description=(
            "Full script ready to send to gemini_tts_batch_service: "
            "director's-notes preamble + body with inline audio tags."
        ),
    )
    estimated_duration_seconds: int = Field(
        default=0,
        description="Rough 90-120s target; advisory only, used for monitoring.",
    )


# ------------------------------------------------------------- DB-shaped models


@dataclass(frozen=True)
class BeatRoundup:
    """Persisted row shape for public.team_roundup.

    `audio_url` is nullable: the TTS batch may succeed for one team and
    fail for another in the same cycle. The roundup row is written either
    way so the written brief is recoverable.

    `tts_batch_id` is also nullable: it's set as soon as the create stage
    returns, *before* status/process. This means when process fails the
    batch_id still lands in DB and `scripts/tts_recover.py` can pick it
    up without the operator looking it up in the Gemini console.
    """

    team_code: str
    cycle_ts: datetime
    cycle_slot: CycleSlot
    persona_name: str
    en_body: str
    de_body: str
    radio_script: str
    audio_url: str | None = None
    tts_batch_id: str | None = None


@dataclass(frozen=True)
class BeatCycleResult:
    """Per-(team, cycle) attempt record for public.team_beat_cycle_state.

    Always written, even when outcome=error. This is the only signal that
    distinguishes 'beat reporter ran and stayed silent' from 'cycle never
    ran' or 'cycle crashed silently'.
    """

    team_code: str
    cycle_ts: datetime
    cycle_slot: CycleSlot
    outcome: BeatOutcome
    reason: str = ""
    article_count: int | None = None
    roundup_id: int | None = None


# ------------------------------------------------------------ TTS plumbing types


@dataclass(frozen=True)
class TTSItem:
    """Input to the gemini_tts_batch_service `create` action.

    `id` MUST follow the convention {team_code}-{cycle_iso_ts} so the
    `process` action's manifest can be mapped back to teams.
    """

    id: str
    text: str
    title: str


@dataclass(frozen=True)
class TTSResult:
    """Per-item outcome from the gemini_tts_batch_service `process` action."""

    item_id: str
    public_url: str | None
    error: str | None = None


@dataclass(frozen=True)
class TTSBatchOutcome:
    """Aggregate result of one full create→status→process lifecycle."""

    batch_id: str
    items: list[TTSResult] = field(default_factory=list)

    def url_for(self, item_id: str) -> str | None:
        for item in self.items:
            if item.item_id == item_id:
                return item.public_url
        return None
