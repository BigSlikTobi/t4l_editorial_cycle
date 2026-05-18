from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.podcast.analytics import PodcastAnalyticsPack
from app.podcast.artifacts import (
    initialize_episode_artifacts,
    load_pronunciation_guide,
    load_selected_clusters,
    validate_episode_artifacts,
    write_json,
)
from app.podcast.schemas import PodcastScript, ScriptLine
from app.schemas import EntityMatch, RawArticle


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key="sk-test",
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="sk",
        podcast_audio_temp_dir=tmp_path,
        podcast_target_word_count=200,
        podcast_min_word_count=50,
    )


def _article(id_: str) -> RawArticle:
    return RawArticle(
        id=id_,
        url=f"https://example.com/{id_}",
        title=f"Quarterback story {id_}",
        source_name="Example",
        entities=[EntityMatch(entity_type="player", entity_id="p1", matched_name="QB")],
    )


@pytest.mark.asyncio
async def test_initialize_episode_artifacts_is_german_only_and_snapshots_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_memory = tmp_path / "host_relationship.md"
    host_memory.write_text("Marcus and Robin know the whiteboard bit.", encoding="utf-8")
    feed = AsyncMock()
    feed.fetch_raw_articles = AsyncMock(return_value=[_article("a"), _article("b")])

    def fake_pack(*args, **kwargs) -> PodcastAnalyticsPack:
        return PodcastAnalyticsPack(
            generated_at="2026-05-13T00:00:00Z",  # type: ignore[arg-type]
            season=2025,
            status="ok",
            source={"id": "NFLVERSE", "title": "nflverse", "url": "https://nflreadpy.nflverse.com/"},
            clusters=[],
        )

    monkeypatch.setattr("app.podcast.artifacts.build_analytics_pack", fake_pack)

    episode_dir = await initialize_episode_artifacts(
        settings=_settings(tmp_path),
        feed_reader=feed,  # type: ignore[arg-type]
        run_date=date(2026, 5, 13),
        root=tmp_path / "episodes",
        host_memory_path=host_memory,
    )

    manifest = (episode_dir / "manifest.json").read_text(encoding="utf-8")
    assert '"language": "de-DE"' in manifest
    assert (episode_dir / "conversation_memory_snapshot.md").read_text(
        encoding="utf-8"
    ) == "Marcus and Robin know the whiteboard bit."
    assert '"status": "ok"' in (episode_dir / "analytics_pack.json").read_text(
        encoding="utf-8"
    )
    assert (episode_dir / "continuity_context.json").exists()
    guide = load_pronunciation_guide(episode_dir)
    assert any(entry.term == "T4L" for entry in guide.entries)
    assert any(entry.term == "Tyler Shough" for entry in guide.entries)
    assert load_selected_clusters(episode_dir)


def test_validate_episode_artifacts_requires_research_and_ledgers(tmp_path: Path) -> None:
    episode_dir = tmp_path / "episode"
    episode_dir.mkdir()
    write_json(
        episode_dir / "manifest.json",
        {"run_date": "2026-05-13", "language": "de-DE", "title": "t", "summary": "s"},
    )

    with pytest.raises(FileNotFoundError, match="selected_clusters.json"):
        validate_episode_artifacts(episode_dir)


def test_validate_episode_artifacts_accepts_complete_german_episode(tmp_path: Path) -> None:
    episode_dir = tmp_path / "episode"
    episode_dir.mkdir()
    write_json(
        episode_dir / "manifest.json",
        {"run_date": "2026-05-13", "language": "de-DE", "title": "t", "summary": "s"},
    )
    write_json(episode_dir / "selected_clusters.json", [])
    write_json(
        episode_dir / "analytics_pack.json",
        {"generated_at": "2026-05-13T00:00:00Z", "season": 2025, "status": "ok", "source": {}, "clusters": []},
    )
    write_json(
        episode_dir / "continuity_context.json",
        {
            "lookback_days": 3,
            "useful_callbacks": [],
            "open_loops": [],
            "avoid_repeating": [],
        },
    )
    write_json(
        episode_dir / "section_plan.json",
        {
            "run_date": "2026-05-13",
            "red_line": "News to player to team to deep dive.",
            "sections": [
                {"kind": "news", "title": "News"},
                {"kind": "player_of_day", "title": "Player"},
                {"kind": "team_of_day", "title": "Team"},
                {"kind": "deep_dive", "title": "Deep Dive"},
            ],
            "rejected_candidates": [],
        },
    )
    write_json(
        episode_dir / "pronunciation_guide.json",
        {
            "generated_at": "2026-05-13T00:00:00Z",
            "entries": [
                {
                    "term": "T4L",
                    "spoken_as": "Tackle for Loss",
                    "confidence": "high",
                }
            ],
            "candidates_to_check": [],
        },
    )
    (episode_dir / "research_a.md").write_text("A", encoding="utf-8")
    (episode_dir / "research_b.md").write_text("B", encoding="utf-8")
    (episode_dir / "research_news.md").write_text("News", encoding="utf-8")
    (episode_dir / "research_player_of_day.md").write_text("Player", encoding="utf-8")
    (episode_dir / "research_team_of_day.md").write_text("Team", encoding="utf-8")
    (episode_dir / "research_deep_dive.md").write_text("Deep", encoding="utf-8")
    (episode_dir / "section_synthesis.md").write_text("Synthesis", encoding="utf-8")
    (episode_dir / "host_authority_notes.md").write_text("Authority pass OK", encoding="utf-8")
    (episode_dir / "notes.md").write_text("Notes", encoding="utf-8")
    (episode_dir / "conversation_memory_snapshot.md").write_text(
        "\"Tape on, let's go.\"", encoding="utf-8"
    )
    write_json(
        episode_dir / "source_ledger.json",
        [{"id": "S1", "title": "Source", "url": "https://example.com"}],
    )
    write_json(
        episode_dir / "claim_ledger.json",
        [
            {
                "id": "1",
                "text": "Three touchdowns.",
                "source_ids": ["S1"],
                "claim_type": "stat",
                "confidence": "high",
                "number_checked": True,
                "status": "supported",
            }
        ],
    )
    script = PodcastScript(
        language="de-DE",
        run_date=date(2026, 5, 13),
        body=[
            ScriptLine(speaker="color", text="Tape on, let's go."),
            ScriptLine(speaker="analyst", text="Trailer-Stimme ist heute erlaubt."),
            ScriptLine(speaker="color", text="Wir parken das auf dem Whiteboard."),
            ScriptLine(speaker="analyst", text="Drei Touchdowns. [C1]"),
        ],
    )
    write_json(episode_dir / "script.de-DE.json", script.model_dump(mode="json"))

    summary = validate_episode_artifacts(episode_dir)
    assert summary.episode_dir == episode_dir
    assert not (episode_dir / "episode_memory.json").exists()
