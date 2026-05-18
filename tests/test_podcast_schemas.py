from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.podcast.schemas import (
    MultiSpeakerTTSPayload,
    PodcastCluster,
    PodcastEpisodeRecord,
    PodcastSection,
    PodcastScript,
    ScriptLine,
)


class TestScriptLine:
    def test_accepts_valid_speakers(self) -> None:
        for speaker in ("color", "analyst", "narrator"):
            line = ScriptLine(speaker=speaker, text="hello")
            assert line.speaker == speaker

    def test_rejects_unknown_speaker(self) -> None:
        with pytest.raises(ValidationError):
            ScriptLine(speaker="host", text="hello")  # type: ignore[arg-type]

    def test_prosody_hints_default_empty(self) -> None:
        line = ScriptLine(speaker="color", text="x")
        assert line.prosody_hints == []


class TestPodcastScript:
    def test_all_lines_concatenates_sections(self) -> None:
        script = PodcastScript(
            language="en-US",
            run_date=date(2026, 5, 9),
            cold_open=[ScriptLine(speaker="color", text="A")],
            body=[
                ScriptLine(speaker="color", text="B"),
                ScriptLine(speaker="analyst", text="C"),
            ],
            outro=[ScriptLine(speaker="color", text="D")],
        )
        texts = [line.text for line in script.all_lines()]
        assert texts == ["A", "B", "C", "D"]

    def test_all_lines_prefers_fixed_sections(self) -> None:
        script = PodcastScript(
            language="de-DE",
            run_date=date(2026, 5, 9),
            cold_open=[ScriptLine(speaker="color", text="A")],
            sections=[
                PodcastSection(
                    kind="news",
                    title="News",
                    lines=[ScriptLine(speaker="color", text="B")],
                ),
                PodcastSection(
                    kind="player_of_day",
                    title="Player",
                    lines=[ScriptLine(speaker="analyst", text="C")],
                ),
                PodcastSection(
                    kind="team_of_day",
                    title="Team",
                    lines=[ScriptLine(speaker="color", text="D")],
                ),
                PodcastSection(
                    kind="deep_dive",
                    title="Deep",
                    lines=[ScriptLine(speaker="analyst", text="E")],
                ),
            ],
            body=[ScriptLine(speaker="color", text="legacy body ignored")],
            outro=[ScriptLine(speaker="color", text="F")],
        )

        assert [line.text for line in script.all_lines()] == ["A", "B", "C", "D", "E", "F"]

    def test_sections_must_use_fixed_order(self) -> None:
        with pytest.raises(ValidationError):
            PodcastScript(
                language="de-DE",
                run_date=date(2026, 5, 9),
                sections=[
                    PodcastSection(kind="player_of_day", title="Player"),
                    PodcastSection(kind="news", title="News"),
                ],
            )

    def test_rejects_unknown_language(self) -> None:
        with pytest.raises(ValidationError):
            PodcastScript(language="fr-FR", run_date=date.today())  # type: ignore[arg-type]


class TestPodcastCluster:
    def test_story_weight_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            PodcastCluster(
                cluster_id="c1",
                headline="h",
                summary="s",
                story_weight=-0.1,
            )

    def test_minimal_cluster(self) -> None:
        cluster = PodcastCluster(
            cluster_id="c1",
            headline="h",
            summary="s",
            story_weight=0.0,
        )
        assert cluster.source_articles == []
        assert cluster.entities == []
        assert cluster.narrative_angle is None


class TestMultiSpeakerTTSPayload:
    def test_construction(self) -> None:
        payload = MultiSpeakerTTSPayload(
            language="de-DE",
            lines=[("color", "Hallo"), ("analyst", "Genau.")],
            voice_map={"color": "Puck", "analyst": "Charon"},
            title="T4L Daily — 2026-05-09",
        )
        assert len(payload.lines) == 2
        assert payload.voice_map["color"] == "Puck"


class TestPodcastEpisodeRecord:
    def test_default_status_pending(self) -> None:
        record = PodcastEpisodeRecord(
            run_date=date(2026, 5, 9),
            language="en-US",
        )
        assert record.status == "pending"
        assert record.id is None

    def test_rejects_unknown_status(self) -> None:
        with pytest.raises(ValidationError):
            PodcastEpisodeRecord(
                run_date=date(2026, 5, 9),
                language="en-US",
                status="frobnicating",  # type: ignore[arg-type]
            )
