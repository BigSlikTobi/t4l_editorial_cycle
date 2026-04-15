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
