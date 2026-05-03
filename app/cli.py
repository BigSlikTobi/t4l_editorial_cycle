from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, TypeVar

import typer

from app.config import get_settings
from app.orchestration import CycleOrchestrator, build_default_orchestrator
from app.schemas import CycleResult
from app.team_beat.workflow import (
    TeamBeatWorkflow,
    build_default_team_beat_workflow,
)

logger = logging.getLogger(__name__)

app = typer.Typer(help="T4L Editorial Cycle Agent — hourly NFL editorial cycle runner.")

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
