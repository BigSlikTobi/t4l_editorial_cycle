from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.podcast.schemas import PodcastCluster, PodcastScript, ScriptLine
from app.podcast.script import (
    _coerce_cold_open_lines,
    _coerce_ranked_clusters,
    _empty_script,
    branded_intro_line,
    compose_script,
)


def _cluster(cid: str, weight: float = 1.0, headline: str = "h") -> PodcastCluster:
    return PodcastCluster(
        cluster_id=cid,
        headline=headline,
        summary=headline,
        story_weight=weight,
    )


class TestCoerceRankedClusters:
    def test_overlay_weight_and_angle(self) -> None:
        original = [_cluster("a", 1.0), _cluster("b", 2.0)]
        agent_output = [
            {"cluster_id": "a", "story_weight": 5.0, "narrative_angle": "AAA"},
            {"cluster_id": "b", "story_weight": 4.0, "narrative_angle": "BBB"},
        ]
        result = _coerce_ranked_clusters(agent_output, original)
        # Sorted descending.
        assert result[0].cluster_id == "a"
        assert result[0].story_weight == 5.0
        assert result[0].narrative_angle == "AAA"
        assert result[1].narrative_angle == "BBB"

    def test_unknown_cluster_id_dropped(self) -> None:
        original = [_cluster("a", 1.0)]
        agent_output = [{"cluster_id": "z", "story_weight": 9.0}]
        result = _coerce_ranked_clusters(agent_output, original)
        # Falls back to originals because no valid output.
        assert [c.cluster_id for c in result] == ["a"]

    def test_missing_cluster_preserved(self) -> None:
        # If agent only mentions one of two, the other is preserved.
        original = [_cluster("a", 1.0), _cluster("b", 2.0)]
        agent_output = [{"cluster_id": "a", "story_weight": 9.0}]
        result = _coerce_ranked_clusters(agent_output, original)
        ids = [c.cluster_id for c in result]
        assert set(ids) == {"a", "b"}

    def test_invalid_json_falls_back(self) -> None:
        original = [_cluster("a", 1.0)]
        result = _coerce_ranked_clusters("{not json", original)
        assert result == original

    def test_string_json_parsed(self) -> None:
        original = [_cluster("a", 1.0)]
        result = _coerce_ranked_clusters(
            '[{"cluster_id": "a", "story_weight": 7.0}]', original
        )
        assert result[0].story_weight == 7.0


class TestCoerceColdOpenLines:
    def test_valid_lines(self) -> None:
        payload = [
            {"speaker": "color", "text": "Big news overnight."},
            {"speaker": "analyst", "text": "EPA says yes."},
        ]
        lines = _coerce_cold_open_lines(payload)
        assert len(lines) == 2
        assert lines[0].speaker == "color"

    def test_filters_invalid_speaker(self) -> None:
        payload = [
            {"speaker": "host", "text": "x"},
            {"speaker": "color", "text": "y"},
        ]
        lines = _coerce_cold_open_lines(payload)
        assert len(lines) == 1
        assert lines[0].text == "y"

    def test_string_json(self) -> None:
        lines = _coerce_cold_open_lines('[{"speaker":"color","text":"hi"}]')
        assert len(lines) == 1


class TestBrandedIntroLine:
    def test_en_brand_line(self) -> None:
        line = branded_intro_line("en-US")
        assert line.speaker == "color"
        assert "Tackle 4 Loss" in line.text
        assert "American Football Morning Show" in line.text
        assert line.prosody_hints  # not empty

    def test_de_brand_line(self) -> None:
        line = branded_intro_line("de-DE")
        assert line.speaker == "color"
        assert "Tackle 4 Loss" in line.text


class TestEmptyScript:
    def test_en_apology(self) -> None:
        script = _empty_script("en-US", date(2026, 5, 9))
        assert script.story_count == 0
        assert script.body[0].speaker == "color"
        assert "stories today" in script.body[0].text

    def test_de_apology(self) -> None:
        script = _empty_script("de-DE", date(2026, 5, 9))
        assert "Heute" in script.body[0].text


class _FakeRunResult:
    def __init__(self, final_output: Any) -> None:
        self.final_output = final_output


@pytest.mark.asyncio
class TestComposeScript:
    async def test_zero_clusters_returns_apology(self) -> None:
        from app.config import Settings

        settings = Settings(_env_file=None, openai_api_key="sk-test", supabase_url="https://x.supabase.co", supabase_service_role_key="sk")  # type: ignore[arg-type]
        script = await compose_script(
            [],
            language="en-US",
            run_date=date(2026, 5, 9),
            target_word_count=4200,
            settings=settings,
        )
        assert script.story_count == 0
        assert script.word_count > 0

    async def test_full_pipeline_with_mocked_agents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import Settings

        settings = Settings(
            _env_file=None,
            openai_api_key="sk-test",
            supabase_url="https://x.supabase.co",
            supabase_service_role_key="sk",
        )  # type: ignore[arg-type]

        # Final dialogue script.
        final = PodcastScript(
            language="en-US",
            run_date=date(2026, 5, 9),
            cold_open=[ScriptLine(speaker="color", text="Cold open line")],
            body=[
                ScriptLine(speaker="color", text="Welcome in."),
                ScriptLine(speaker="analyst", text="Numbers say one thing."),
            ],
            outro=[ScriptLine(speaker="color", text="See you tomorrow.")],
        )

        runs: list[Any] = []

        async def fake_run(agent: Any, input_: Any) -> _FakeRunResult:
            runs.append((agent.name, input_))
            name = agent.name
            if "Cluster Ranker" in name:
                return _FakeRunResult(
                    [
                        {"cluster_id": "c1", "story_weight": 3.0, "narrative_angle": "x"},
                    ]
                )
            if "Cold Open" in name:
                return _FakeRunResult(
                    [{"speaker": "color", "text": "BREAKING."}, {"speaker": "analyst", "text": "0.42 EPA."}]
                )
            if "Dialogue Writer" in name:
                return _FakeRunResult(final)
            if "Director Pass" in name:
                # Director adds a prosody hint to one line.
                directed = final.model_copy(
                    update={
                        "body": [
                            final.body[0].model_copy(update={"prosody_hints": ["warm"]}),
                            final.body[1],
                        ]
                    }
                )
                return _FakeRunResult(directed)
            raise AssertionError(f"unexpected agent {name!r}")

        monkeypatch.setattr("app.podcast.script.Runner.run", fake_run)

        clusters = [_cluster("c1", weight=1.0, headline="Big trade")]
        script = await compose_script(
            clusters,
            language="en-US",
            run_date=date(2026, 5, 9),
            target_word_count=4200,
            settings=settings,
        )
        # All 4 agents called.
        called = [name for (name, _) in runs]
        assert any("Cluster Ranker" in n for n in called)
        assert any("Cold Open" in n for n in called)
        assert any("Dialogue Writer" in n for n in called)
        assert any("Director Pass" in n for n in called)
        # Director pass result preserved.
        assert script.body[0].prosody_hints == ["warm"]
        # Word count recomputed.
        assert script.word_count > 0
        assert script.story_count == 1
