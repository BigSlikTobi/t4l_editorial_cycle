from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, TypeVar

import typer

from app.config import get_settings
from app.orchestration import CycleOrchestrator, build_default_orchestrator
from app.podcast.workflow import (
    PodcastProduceWorkflow,
    build_default_podcast_workflow,
)
from app.schemas import CycleResult
from app.team_beat.workflow import (
    TeamBeatWorkflow,
    build_default_team_beat_workflow,
)

logger = logging.getLogger(__name__)

app = typer.Typer(help="T4L Editorial Cycle Agent — hourly NFL editorial cycle runner.")
podcast_app = typer.Typer(help="T4L Daily Briefing — produce + deliver the personal NFL podcast.")
app.add_typer(podcast_app, name="podcast")

RunResultT = TypeVar("RunResultT")


async def _run_with_cleanup(
    orchestrator: CycleOrchestrator,
    awaitable: Awaitable[RunResultT],
) -> RunResultT:
    try:
        return await awaitable
    finally:
        await orchestrator.close()


@app.callback()
def main() -> None:
    """CLI entrypoint."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@app.command("run")
def run_cycle(
    output_json: Path | None = typer.Option(None, help="Write cycle result JSON to file"),
) -> None:
    """Run a single editorial cycle."""
    settings = get_settings()
    orchestrator = build_default_orchestrator(settings)

    result: CycleResult = asyncio.run(
        _run_with_cleanup(orchestrator, orchestrator.run_cycle())
    )

    typer.echo(
        f"Cycle {result.cycle_id} | "
        f"{result.articles_written} written, {result.articles_updated} updated | "
        f"{result.prevented_duplicates} duplicates prevented"
    )
    for story in result.plan.stories:
        if story.action != "skip":
            teams = ", ".join(story.team_codes) if story.team_codes else "League-wide"
            typer.echo(f"  {story.rank}. {story.cluster_headline} [{teams}] — {story.action}")

    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        typer.echo(f"Wrote JSON to {output_json}")


async def _run_beat_with_cleanup(workflow: TeamBeatWorkflow):
    try:
        return await workflow.run_cycle()
    finally:
        await workflow.close()


@app.command("beat")
def run_team_beat(
    teams: str = typer.Option(
        "",
        help=(
            "Comma-separated team codes to run (e.g. 'NYJ,CHI'). "
            "Empty = all teams configured in personas.TEAM_BEAT_PERSONAS."
        ),
    ),
    lookback_hours: int = typer.Option(
        12,
        help=(
            "Hours of raw-article history to scan for each team. Default 12 "
            "matches the production cron cadence; raise it for offseason or "
            "local testing when 12h windows may be empty."
        ),
    ),
) -> None:
    """Run one team-beat cycle (read 12h feed → bilingual brief → DE radio → TTS → DB)."""
    settings = get_settings()

    parsed_teams: tuple[str, ...] | None = None
    if teams.strip():
        parsed_teams = tuple(t.strip().upper() for t in teams.split(",") if t.strip())

    workflow = build_default_team_beat_workflow(
        settings, team_codes=parsed_teams, lookback_hours=lookback_hours,
    )
    summary = asyncio.run(_run_beat_with_cleanup(workflow))

    typer.echo(
        f"Team beat cycle {summary.cycle_ts.isoformat()} ({summary.cycle_slot}) | "
        f"filed={summary.filed_count} no_news={summary.no_news_count} "
        f"error={summary.error_count}"
    )
    for team in summary.teams:
        line = f"  {team.team_code}: {team.outcome.value}"
        if team.reason:
            line += f" — {team.reason}"
        typer.echo(line)


# --- podcast subcommands ---


async def _run_podcast_with_cleanup(workflow: PodcastProduceWorkflow, awaitable):
    try:
        return await awaitable
    finally:
        await workflow.close()


@podcast_app.command("produce")
def produce_podcast(
    language: str = typer.Option(
        "en-US",
        help="Language code: en-US or de-DE.",
    ),
    dry_run: bool = typer.Option(
        False,
        help="Skip Gemini TTS render. Writes script JSON to --output-script if set.",
    ),
    output_script: Path | None = typer.Option(
        None,
        help="(dry-run) Path to write the script JSON for inspection.",
    ),
    lookback_hours: int | None = typer.Option(
        None,
        help="Override settings.podcast_lookback_hours (default: 24).",
    ),
) -> None:
    """Generate today's podcast episode for one language."""
    settings = get_settings()
    workflow = build_default_podcast_workflow(settings)

    summary = asyncio.run(
        _run_podcast_with_cleanup(
            workflow,
            workflow.run_cycle(
                language=language,  # type: ignore[arg-type]
                dry_run=dry_run,
                output_script_path=output_script,
                lookback_hours=lookback_hours,
            ),
        )
    )

    typer.echo(
        f"Podcast episode #{summary.episode_id} | {summary.language} | "
        f"{summary.run_date.isoformat()} | status={summary.status} | "
        f"stories={summary.story_count} words={summary.word_count} "
        f"duration={summary.duration_seconds or 0}s"
    )
    if summary.audio_local_path:
        typer.echo(f"  Audio: {summary.audio_local_path}")
    if summary.error_message:
        typer.echo(f"  Note: {summary.error_message}")


@podcast_app.command("deliver")
def deliver_podcast(
    episode_id: int = typer.Argument(..., help="podcast_episodes.id to deliver."),
    dry_run: bool = typer.Option(
        False,
        help="Log the save-to-spotify invocation without running it.",
    ),
) -> None:
    """Upload a rendered episode to your personal Spotify library."""
    # Imported here so the produce path doesn't pay for delivery imports.
    from app.delivery.dispatcher import dispatch_episode

    settings = get_settings()
    result = asyncio.run(dispatch_episode(episode_id, settings=settings, dry_run=dry_run))

    if result.success:
        typer.echo(
            f"Delivered episode #{episode_id} → Spotify"
            + (f" (id={result.spotify_episode_id})" if result.spotify_episode_id else "")
        )
        if result.invocation:
            typer.echo(f"  Invoked: {result.invocation}")
    else:
        typer.echo(f"Delivery FAILED for episode #{episode_id}: {result.error_message}")
        raise typer.Exit(code=1)


@podcast_app.command("latest-id")
def latest_podcast_id(
    language: str = typer.Option(..., help="Language code (en-US or de-DE)."),
    status: str | None = typer.Option(
        None,
        help="Optional status filter (pending|rendering|rendered|delivered|failed).",
    ),
) -> None:
    """Print the id of the most recent podcast_episodes row matching filters.

    Used by the daily VPS shell script to chain produce → deliver
    without parsing logs.
    """
    from app.adapters import PodcastEpisodeWriter

    settings = get_settings()
    base_url = str(settings.supabase_url).rstrip("/")
    service_role_key = settings.supabase_service_role_key.get_secret_value()
    writer = PodcastEpisodeWriter(base_url=base_url, service_role_key=service_role_key)

    async def _run() -> int | None:
        try:
            return await writer.latest_id(language=language, status=status)
        finally:
            await writer.close()

    result = asyncio.run(_run())
    if result is None:
        raise typer.Exit(code=1)
    typer.echo(str(result))


# --- artifact/Codex runtime subcommands ---


@podcast_app.command("init-codex-run")
def init_codex_podcast_run(
    run_date: str | None = typer.Option(
        None,
        help="Episode date as YYYY-MM-DD. Default: today in UTC.",
    ),
    lookback_hours: int | None = typer.Option(
        None,
        help="Override settings.podcast_lookback_hours.",
    ),
    episode_root: Path = typer.Option(
        Path("var/podcast/episodes"),
        help="Root directory for artifact-based podcast episodes.",
    ),
    host_memory: Path = typer.Option(
        Path("editorial_memory/podcast/host_relationship.md"),
        help="Durable Marcus/Robin relationship memory file.",
    ),
) -> None:
    """Create German Codex podcast artifacts: manifest, clusters, memory snapshot."""
    from datetime import date

    from app.adapters import RawArticleDbReader
    from app.podcast.artifacts import initialize_episode_artifacts

    settings = get_settings()
    base_url = str(settings.supabase_url).rstrip("/")
    service_role_key = settings.supabase_service_role_key.get_secret_value()
    feed = RawArticleDbReader(base_url=base_url, service_role_key=service_role_key)
    parsed_date = date.fromisoformat(run_date) if run_date else None

    async def _run() -> Path:
        try:
            return await initialize_episode_artifacts(
                settings=settings,
                feed_reader=feed,
                run_date=parsed_date,
                lookback_hours=lookback_hours,
                root=episode_root,
                host_memory_path=host_memory,
            )
        finally:
            await feed.close()

    out_dir = asyncio.run(_run())
    typer.echo(str(out_dir))


@podcast_app.command("validate-artifacts")
def validate_podcast_artifacts(
    episode_dir: Path = typer.Argument(
        ...,
        help="Artifact episode directory, e.g. var/podcast/episodes/2026-05-13.",
    ),
) -> None:
    """Validate German artifact ledgers, claim markers, and host chemistry."""
    from app.podcast.artifacts import validate_episode_artifacts

    try:
        summary = validate_episode_artifacts(episode_dir)
    except Exception as exc:
        typer.echo(f"invalid: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"valid: {summary.episode_dir}")


@podcast_app.command("render-artifact")
def render_podcast_artifact(
    episode_dir: Path = typer.Argument(
        ...,
        help="Artifact episode directory, e.g. var/podcast/episodes/2026-05-13.",
    ),
) -> None:
    """Render a validated German artifact episode with Gemini Batch + sync fallback."""
    from app.podcast.artifacts import (
        load_pronunciation_guide,
        load_script,
        read_json,
        validate_episode_artifacts,
        write_json,
    )
    from app.podcast.audio_compose import probe_duration_seconds
    from app.podcast.continuity import write_episode_memory
    from app.podcast.factcheck import strip_script_claim_refs
    from app.podcast.render import render_to_audio_with_batch_fallback

    settings = get_settings()

    async def _run() -> None:
        validate_episode_artifacts(episode_dir)
        manifest = read_json(episode_dir / "manifest.json")
        host_memory_text = (episode_dir / "conversation_memory_snapshot.md").read_text(
            encoding="utf-8"
        )
        pronunciation_guide = load_pronunciation_guide(episode_dir)
        script = strip_script_claim_refs(load_script(episode_dir))
        result = await render_to_audio_with_batch_fallback(
            script,
            run_date=script.run_date,
            settings=settings,
            title=manifest.get("title"),
            host_memory=host_memory_text,
            pronunciation_guide=pronunciation_guide,
            status_path=episode_dir / "tts_status.json",
        )
        duration = await probe_duration_seconds(
            Path(result.audio_path),
            ffprobe_path=settings.ffprobe_path,
        )
        write_json(
            episode_dir / "audio_probe.json",
            {
                "audio_path": result.audio_path,
                "duration_seconds": int(duration),
                "mime_type": result.mime_type,
            },
        )
        manifest["status"] = "rendered"
        manifest["audio_path"] = result.audio_path
        write_json(episode_dir / "manifest.json", manifest)
        write_episode_memory(episode_dir, script)

    try:
        asyncio.run(_run())
    except Exception as exc:
        typer.echo(f"render failed: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"rendered: {episode_dir}")


@podcast_app.command("publish-artifact")
def publish_podcast_artifact(
    episode_dir: Path = typer.Argument(
        ...,
        help="Artifact episode directory, e.g. var/podcast/episodes/2026-05-13.",
    ),
    dry_run: bool = typer.Option(
        False,
        help="Show the save-to-spotify invocation without uploading.",
    ),
) -> None:
    """Upload a rendered artifact episode to Spotify without DB state."""
    from datetime import UTC, datetime

    from app.delivery.spotify import SaveToSpotifyDelivery
    from app.podcast.artifacts import read_json, write_json
    from app.podcast.publication import (
        dry_run_thumbnail_invocation,
        prepare_publication_rehearsal,
    )

    settings = get_settings()

    async def _run() -> None:
        manifest = read_json(episode_dir / "manifest.json")
        audio_path = manifest.get("audio_path")
        if not audio_path:
            raise RuntimeError("manifest has no audio_path; run render-artifact first")
        prepared = await prepare_publication_rehearsal(
            episode_dir=episode_dir,
            settings=settings,
            dry_run=dry_run,
        )
        if dry_run and prepared.thumbnail_path is None and settings.podcast_thumbnail_enabled:
            typer.echo(
                "thumbnail dry-run: "
                + dry_run_thumbnail_invocation(settings, episode_dir / "thumbnail.png")
            )
        delivery = SaveToSpotifyDelivery(settings)
        result = await delivery.dispatch(
            audio_path=audio_path,
            title=prepared.title,
            summary=prepared.summary,
            language="de-DE",
            image_path=str(prepared.thumbnail_path) if prepared.thumbnail_path else None,
            dry_run=dry_run,
        )
        write_json(
            episode_dir / "upload_metadata.json",
            {
                "success": result.success,
                "spotify_episode_id": result.spotify_episode_id,
                "error_message": result.error_message,
                "invocation": result.invocation,
                "dry_run": dry_run,
            },
        )
        if not result.success:
            raise RuntimeError(result.error_message or "Spotify upload failed")
        if not dry_run:
            manifest["status"] = "published"
            manifest["published_at"] = datetime.now(UTC).isoformat()
            manifest["spotify_episode_id"] = result.spotify_episode_id
            write_json(episode_dir / "manifest.json", manifest)

    try:
        asyncio.run(_run())
    except Exception as exc:
        typer.echo(f"publish failed: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"published: {episode_dir}" if not dry_run else f"publish dry-run: {episode_dir}")
