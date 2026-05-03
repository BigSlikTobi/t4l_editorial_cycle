"""Schema-level checks for the team-beat module."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.team_beat.schemas import (
    BeatBrief,
    BeatCycleResult,
    BeatOutcome,
    BeatRoundup,
    RadioScript,
    TTSBatchOutcome,
    TTSItem,
    TTSResult,
)


class TestBeatBrief:
    def test_should_file_false_allows_empty_bodies(self) -> None:
        # The news-reactive opt-out: agent decided nothing was worth filing.
        brief = BeatBrief(
            team_code="NYJ",
            should_file=False,
            skip_reason="No actionable news in window — top story was 3-day-old injury report.",
        )
        assert brief.should_file is False
        assert brief.en_body == ""
        assert brief.de_body == ""

    def test_filed_brief_carries_bodies_and_dateline(self) -> None:
        brief = BeatBrief(
            team_code="CHI",
            persona_name="Hank Marlow",
            should_file=True,
            headline="Bears name McCarthy starter for Week 1",
            en_body="The Bears made it official today...",
            de_body="Die Bears haben es heute offiziell gemacht...",
            dateline_city="Chicago",
        )
        assert brief.should_file is True
        assert brief.dateline_city == "Chicago"

    def test_should_file_required(self) -> None:
        with pytest.raises(ValidationError):
            BeatBrief(team_code="NYJ")  # type: ignore[call-arg]


class TestBeatOutcome:
    def test_outcome_values_match_db_check_constraint(self) -> None:
        # The migration's CHECK constraint must mirror these enum values.
        assert {o.value for o in BeatOutcome} == {"filed", "no_news", "error"}


class TestBeatRoundup:
    def test_audio_url_optional(self) -> None:
        # TTS may fail per item; the brief is written either way so the
        # text is recoverable.
        roundup = BeatRoundup(
            team_code="NYJ",
            cycle_ts=datetime(2026, 5, 2, 4, 0, tzinfo=UTC),
            cycle_slot="AM",
            persona_name="Theo Briggs",
            en_body="...",
            de_body="...",
            radio_script="...",
        )
        assert roundup.audio_url is None
        assert roundup.tts_batch_id is None

    def test_tts_batch_id_round_trips(self) -> None:
        # Recovery hook: batch_id persists even when audio_url is NULL.
        roundup = BeatRoundup(
            team_code="NYJ",
            cycle_ts=datetime(2026, 5, 2, 4, 0, tzinfo=UTC),
            cycle_slot="AM",
            persona_name="Theo Briggs",
            en_body="...",
            de_body="...",
            radio_script="...",
            audio_url=None,
            tts_batch_id="batches/abc123",
        )
        assert roundup.tts_batch_id == "batches/abc123"
        assert roundup.audio_url is None


class TestBeatCycleResult:
    def test_no_news_record_carries_only_outcome(self) -> None:
        result = BeatCycleResult(
            team_code="NYJ",
            cycle_ts=datetime(2026, 5, 2, 4, 0, tzinfo=UTC),
            cycle_slot="AM",
            outcome=BeatOutcome.NO_NEWS,
            reason="Quiet news window.",
            article_count=2,
        )
        assert result.outcome is BeatOutcome.NO_NEWS
        assert result.roundup_id is None


class TestRadioScript:
    def test_default_duration(self) -> None:
        script = RadioScript(team_code="NYJ", de_text="Style: ruhig...\n\n[pause] Heute aus East Rutherford...")
        assert script.estimated_duration_seconds == 0


class TestTTSBatchOutcome:
    def test_url_for_returns_match_or_none(self) -> None:
        outcome = TTSBatchOutcome(
            batch_id="batches/abc",
            items=[
                TTSResult(item_id="NYJ-2026-05-02T04:00Z", public_url="https://x/a.mp3"),
                TTSResult(item_id="CHI-2026-05-02T04:00Z", public_url=None, error="missing"),
            ],
        )
        assert outcome.url_for("NYJ-2026-05-02T04:00Z") == "https://x/a.mp3"
        assert outcome.url_for("CHI-2026-05-02T04:00Z") is None
        assert outcome.url_for("DET-2026-05-02T04:00Z") is None  # absent → None

    def test_item_id_convention_is_just_a_string(self) -> None:
        # The {team_code}-{cycle_iso_ts} convention is enforced by the
        # workflow that builds TTSItems, not by the schema itself.
        item = TTSItem(id="NYJ-2026-05-02T04:00:00+00:00", text="...", title="NYJ AM")
        assert item.id.startswith("NYJ-")
