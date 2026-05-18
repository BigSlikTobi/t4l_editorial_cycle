from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.podcast.artifacts import write_json
from app.podcast.publication import (
    AI_DISCLOSURE_DE,
    prepare_publication_rehearsal,
    validate_thumbnail,
)
from app.podcast.schemas import PodcastScript, ScriptLine


def _settings(tmp_path: Path, *, thumbnail_enabled: bool = False) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key="sk-test",
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="sk",
        podcast_audio_temp_dir=tmp_path,
        podcast_thumbnail_enabled=thumbnail_enabled,
    )


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def _write_complete_episode(
    episode_dir: Path,
    *,
    title: str | None = "NFL-Morgen: Druck im Kalender",
    summary: str | None = "Marcus und Robin über Mahomes, Caleb Williams und die Bears.",
    source_url: str = "https://example.com/source",
    script_text: str = "Tape on, let's go. Drei Touchdowns. [C1]",
) -> None:
    episode_dir.mkdir()
    write_json(
        episode_dir / "manifest.json",
        {
            "run_date": "2026-05-13",
            "language": "de-DE",
            "title": "T4L Morgenbriefing - 2026-05-13",
            "summary": "Deutschsprachiger T4L NFL-Morgenpodcast mit Marcus und Robin, erstellt im Codex-Runtime-Flow.",
            "audio_path": str(episode_dir / "audio.wav"),
        },
    )
    write_json(episode_dir / "selected_clusters.json", [])
    write_json(
        episode_dir / "analytics_pack.json",
        {"generated_at": "2026-05-13T00:00:00Z", "season": 2025, "status": "ok", "source": {}, "clusters": []},
    )
    write_json(
        episode_dir / "continuity_context.json",
        {"lookback_days": 3, "useful_callbacks": [], "open_loops": [], "avoid_repeating": []},
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
        {"generated_at": "2026-05-13T00:00:00Z", "entries": [{"term": "T4L", "spoken_as": "Tackle for Loss", "confidence": "high"}], "candidates_to_check": []},
    )
    for name in (
        "research_a.md",
        "research_b.md",
        "research_news.md",
        "research_player_of_day.md",
        "research_team_of_day.md",
        "research_deep_dive.md",
        "section_synthesis.md",
        "host_authority_notes.md",
        "notes.md",
    ):
        (episode_dir / name).write_text("ok", encoding="utf-8")
    (episode_dir / "conversation_memory_snapshot.md").write_text(
        "\"Tape on, let's go.\"", encoding="utf-8"
    )
    write_json(
        episode_dir / "source_ledger.json",
        [{"id": "S1", "title": "Source Story", "publisher": "Example", "url": source_url}],
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
        body=[ScriptLine(speaker="color", text=script_text)],
        story_count=1,
        word_count=8,
        episode_title=title,
        episode_summary=summary,
    )
    write_json(episode_dir / "script.de-DE.json", script.model_dump(mode="json"))
    write_json(
        episode_dir / "audio_probe.json",
        {"audio_path": str(episode_dir / "audio.wav"), "duration_seconds": 60, "mime_type": "audio/wav"},
    )


@pytest.mark.asyncio
async def test_prepare_publication_rehearsal_writes_listener_packet(tmp_path: Path) -> None:
    episode_dir = tmp_path / "episode"
    _write_complete_episode(episode_dir)

    prepared = await prepare_publication_rehearsal(
        episode_dir=episode_dir,
        settings=_settings(tmp_path),
        dry_run=True,
    )

    assert prepared.title == "NFL-Morgen: Druck im Kalender"
    assert "Mahomes" in prepared.summary
    assert (episode_dir / "public_metadata.json").exists()
    show_notes = (episode_dir / "show_notes.md").read_text(encoding="utf-8")
    assert AI_DISCLOSURE_DE in show_notes
    assert "[Source Story](https://example.com/source)" in show_notes
    assert "claim_ledger" not in show_notes
    assert "Codex" not in show_notes
    assert (episode_dir / "publication_safety_report.json").exists()
    assert (episode_dir / "thumbnail_prompt.json").exists()


@pytest.mark.asyncio
async def test_prepare_replaces_internal_fallback_metadata(tmp_path: Path) -> None:
    episode_dir = tmp_path / "episode"
    _write_complete_episode(
        episode_dir,
        title=None,
        summary="Deutschsprachiger T4L NFL-Morgenpodcast, erstellt im Codex-Runtime-Flow.",
    )

    prepared = await prepare_publication_rehearsal(
        episode_dir=episode_dir,
        settings=_settings(tmp_path),
        dry_run=True,
    )

    assert "Codex" not in prepared.title
    assert "Runtime" not in prepared.summary
    assert prepared.title.startswith("T4L Morgen")


@pytest.mark.asyncio
async def test_prepare_blocks_missing_public_sources(tmp_path: Path) -> None:
    episode_dir = tmp_path / "episode"
    _write_complete_episode(episode_dir, source_url="internal://source")

    with pytest.raises(ValueError, match="no public sources"):
        await prepare_publication_rehearsal(
            episode_dir=episode_dir,
            settings=_settings(tmp_path),
            dry_run=True,
        )


@pytest.mark.asyncio
async def test_prepare_blocks_private_script_terms(tmp_path: Path) -> None:
    episode_dir = tmp_path / "episode"
    _write_complete_episode(episode_dir, script_text="Tobi hat ein Meeting. [C1]")

    with pytest.raises(ValueError, match="private or internal"):
        await prepare_publication_rehearsal(
            episode_dir=episode_dir,
            settings=_settings(tmp_path),
            dry_run=True,
        )


def test_validate_thumbnail_accepts_square_png(tmp_path: Path) -> None:
    path = tmp_path / "thumbnail.png"
    path.write_bytes(_png(1024, 1024))

    info = validate_thumbnail(path)

    assert info["format"] == "png"
    assert info["width"] == 1024
    assert info["height"] == 1024


def test_validate_thumbnail_rejects_non_square_png(tmp_path: Path) -> None:
    path = tmp_path / "thumbnail.png"
    path.write_bytes(_png(1024, 512))

    with pytest.raises(ValueError, match="must be square"):
        validate_thumbnail(path)
