from __future__ import annotations

from datetime import date
from pathlib import Path

from app.podcast.continuity import (
    build_continuity_context,
    load_recent_episode_memories,
    write_episode_memory,
)
from app.podcast.schemas import (
    PodcastCluster,
    PodcastContinuityTopic,
    PodcastEpisodeMemory,
    PodcastScript,
    ScriptLine,
)
from app.schemas import EntityMatch


def _cluster() -> PodcastCluster:
    return PodcastCluster(
        cluster_id="c1",
        headline="Steelers quarterback room gets a new twist",
        summary="Pittsburgh is still weighing Aaron Rodgers.",
        story_weight=2.0,
        entities=[
            EntityMatch(
                entity_type="team",
                entity_id="PIT",
                matched_name="Steelers",
            ),
            EntityMatch(
                entity_type="player",
                entity_id="aaron-rodgers",
                matched_name="Aaron Rodgers",
            ),
        ],
    )


def test_build_continuity_context_matches_prior_entities() -> None:
    memory = PodcastEpisodeMemory(
        run_date=date(2026, 5, 15),
        language="de-DE",
        covered_topics=[
            PodcastContinuityTopic(
                topic="Steelers QB room",
                entities=["PIT", "Aaron Rodgers"],
                summary="Marcus framed pressure; Robin wanted protection context.",
                speaker_callback="Robin wanted the whiteboard for third down.",
            )
        ],
        open_loops=["Whether Pittsburgh changes course on Aaron Rodgers"],
        avoid_repeating=["Do not repeat the veteran leadership angle."],
    )

    context = build_continuity_context(
        clusters=[_cluster()],
        memories=[memory],
        lookback_days=3,
    )

    assert context.useful_callbacks
    assert context.useful_callbacks[0].prior_date == date(2026, 5, 15)
    assert "Steelers" in context.useful_callbacks[0].topic
    assert context.open_loops
    assert context.avoid_repeating == ["Do not repeat the veteran leadership angle."]


def test_recent_memory_loader_filters_language_and_date(tmp_path: Path) -> None:
    good_dir = tmp_path / "2026-05-15" / "de-DE"
    old_dir = tmp_path / "2026-05-10" / "de-DE"
    wrong_lang_dir = tmp_path / "2026-05-15" / "en-US"
    for path, language, run_date in [
        (good_dir, "de-DE", "2026-05-15"),
        (old_dir, "de-DE", "2026-05-10"),
        (wrong_lang_dir, "en-US", "2026-05-15"),
    ]:
        path.mkdir(parents=True)
        (path / "episode_memory.json").write_text(
            PodcastEpisodeMemory(
                run_date=date.fromisoformat(run_date),
                language=language,  # type: ignore[arg-type]
                covered_topics=[],
            ).model_dump_json(),
            encoding="utf-8",
        )

    memories = load_recent_episode_memories(
        root=tmp_path,
        run_date=date(2026, 5, 16),
        language="de-DE",
        lookback_days=3,
    )

    assert len(memories) == 1
    assert memories[0].run_date == date(2026, 5, 15)


def test_write_episode_memory_from_script(tmp_path: Path) -> None:
    script = PodcastScript(
        language="de-DE",
        run_date=date(2026, 5, 16),
        body=[ScriptLine(speaker="color", text="Steelers bleiben spannend.")],
    )

    path = write_episode_memory(tmp_path, script)

    assert path.name == "episode_memory.json"
    assert '"language": "de-DE"' in path.read_text(encoding="utf-8")
