from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.podcast.schemas import PodcastScript, ScriptLine
from app.podcast.workflow import PodcastProduceWorkflow
from app.schemas import EntityMatch, RawArticle


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key="sk-test",
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="sk",
        gemini_api_key="gk-test",
        podcast_audio_temp_dir=tmp_path,
        podcast_target_word_count=200,
        podcast_min_word_count=50,
    )


def _make_workflow(
    settings: Settings,
    *,
    feed_articles: list[RawArticle],
    rendered_audio: dict[str, Any] | None = None,
    raise_during_render: Exception | None = None,
) -> tuple[PodcastProduceWorkflow, MagicMock, MagicMock]:
    feed = MagicMock()
    feed.fetch_raw_articles = AsyncMock(return_value=feed_articles)
    feed.close = AsyncMock()

    episodes = MagicMock()
    episodes.upsert_pending = AsyncMock(return_value=42)
    episodes.mark_rendering = AsyncMock()
    episodes.mark_rendered = AsyncMock()
    episodes.mark_delivered = AsyncMock()
    episodes.mark_failed = AsyncMock()
    episodes.close = AsyncMock()

    return PodcastProduceWorkflow(
        settings=settings, feed_reader=feed, episode_writer=episodes
    ), feed, episodes


def _article(id_: str) -> RawArticle:
    return RawArticle(
        id=id_,
        url=f"https://ex.com/{id_}",
        title=f"Story {id_}",
        source_name="ESPN",
        entities=[EntityMatch(entity_type="player", entity_id="p1", matched_name="Player")],
    )


def _script() -> PodcastScript:
    return PodcastScript(
        language="en-US",
        run_date=date(2026, 5, 9),
        cold_open=[ScriptLine(speaker="color", text="Big news.")],
        body=[
            ScriptLine(speaker="color", text="Welcome in today."),
            ScriptLine(speaker="analyst", text="Numbers say 0.42 EPA."),
        ],
        outro=[ScriptLine(speaker="color", text="Catch you tomorrow.")],
        story_count=2,
        word_count=15,
    )


@pytest.mark.asyncio
class TestPodcastProduceWorkflow:
    async def test_dry_run_writes_script_no_render(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path)
        workflow, feed, episodes = _make_workflow(
            settings, feed_articles=[_article("a"), _article("b")]
        )

        async def fake_compose(
            clusters: Any,
            *,
            language: str,
            run_date: date,
            target_word_count: int,
            settings: Settings,
        ) -> PodcastScript:
            return _script()

        monkeypatch.setattr("app.podcast.workflow.compose_script", fake_compose)

        # Render must NOT be called in dry-run.
        rendered = AsyncMock()
        monkeypatch.setattr("app.podcast.workflow.render_to_audio", rendered)

        out = tmp_path / "script.json"
        summary = await workflow.run_cycle(
            language="en-US",
            dry_run=True,
            output_script_path=out,
            run_date=date(2026, 5, 9),
        )

        assert summary.status == "rendered"
        assert summary.audio_local_path is None
        assert summary.error_message and "dry-run" in summary.error_message
        assert out.exists()
        # Script JSON has the expected sections.
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["language"] == "en-US"
        assert payload["body"]
        rendered.assert_not_called()
        # State transitions: upsert pending → mark_rendering → mark_rendered.
        episodes.upsert_pending.assert_awaited_once()
        episodes.mark_rendering.assert_awaited_once()
        episodes.mark_rendered.assert_awaited_once()
        episodes.mark_failed.assert_not_called()

    async def test_real_run_renders_audio(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path)
        workflow, feed, episodes = _make_workflow(
            settings, feed_articles=[_article("a")]
        )

        async def fake_compose(*args: Any, **kwargs: Any) -> PodcastScript:
            return _script()

        async def fake_render(payload: Any, *, run_date: date, settings: Settings, client: Any = None, title: str | None = None):
            from app.podcast.schemas import RenderResult
            audio_path = tmp_path / "ep.wav"
            audio_path.write_bytes(b"\x00\x00")
            return RenderResult(
                audio_path=str(audio_path),
                duration_seconds=1234,
            )

        monkeypatch.setattr("app.podcast.workflow.compose_script", fake_compose)
        monkeypatch.setattr("app.podcast.workflow.render_to_audio", fake_render)

        summary = await workflow.run_cycle(
            language="en-US", dry_run=False, run_date=date(2026, 5, 9)
        )

        assert summary.status == "rendered"
        assert summary.duration_seconds == 1234
        assert summary.audio_local_path and summary.audio_local_path.endswith("ep.wav")
        episodes.mark_rendered.assert_awaited()
        episodes.mark_failed.assert_not_called()

    async def test_failure_marks_failed_and_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path)
        workflow, feed, episodes = _make_workflow(
            settings, feed_articles=[_article("a")]
        )

        async def fake_compose(*args: Any, **kwargs: Any) -> PodcastScript:
            raise RuntimeError("agent exploded")

        monkeypatch.setattr("app.podcast.workflow.compose_script", fake_compose)

        with pytest.raises(RuntimeError, match="agent exploded"):
            await workflow.run_cycle(
                language="en-US", dry_run=True, run_date=date(2026, 5, 9)
            )

        episodes.mark_failed.assert_awaited()
        # The error message captured.
        call = episodes.mark_failed.await_args
        assert "agent exploded" in call.kwargs["error_message"]
