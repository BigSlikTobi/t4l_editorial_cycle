"""End-to-end produce workflow for one episode.

```
RawArticleDbReader → group_for_podcast → select_clusters_for_budget
                                            ↓
                                       compose_script
                                            ↓
                                     script_to_payload
                                            ↓
                                       render_to_audio
                                            ↓
                                  PodcastEpisodeWriter PATCH
```

State machine: pending → rendering → rendered (or failed at any step).
Delivery (rendered → delivered) lives in `app.delivery`.

Dry-run skips the Gemini render entirely: the script is written to a
local JSON file for inspection and the row is marked `rendered` with
`audio_local_path=NULL` and `error_message='dry-run'`. This is the
primary developer-facing test path — it lets us validate clustering,
ranking, persona dialogue, and director-pass output without spending
on TTS.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from app.adapters import (
    PodcastEpisodeWriter,
    RawArticleDbReader,
)
from app.config import Settings
from app.podcast.clustering import (
    group_for_podcast,
    select_clusters_for_budget,
)
from app.podcast.continuity import load_continuity_context, write_episode_memory
from app.podcast.render import render_to_audio
from app.podcast.schemas import PodcastLanguage, PodcastScript
from app.podcast.script import compose_script

logger = logging.getLogger(__name__)


@dataclass
class PodcastRunSummary:
    """Returned by `PodcastProduceWorkflow.run_cycle` for CLI display."""

    episode_id: int
    run_date: date
    language: PodcastLanguage
    status: str
    story_count: int
    word_count: int
    duration_seconds: int | None
    audio_local_path: str | None
    error_message: str | None


class PodcastProduceWorkflow:
    """One produce-cycle = one (date, language) episode."""

    def __init__(
        self,
        *,
        settings: Settings,
        feed_reader: RawArticleDbReader,
        episode_writer: PodcastEpisodeWriter,
    ) -> None:
        self._settings = settings
        self._feed = feed_reader
        self._episodes = episode_writer

    async def close(self) -> None:
        await self._feed.close()
        await self._episodes.close()

    async def run_cycle(
        self,
        *,
        language: PodcastLanguage,
        dry_run: bool = False,
        output_script_path: Path | None = None,
        run_date: date | None = None,
        lookback_hours: int | None = None,
    ) -> PodcastRunSummary:
        # The OpenAI Agents SDK reads OPENAI_API_KEY from the process env;
        # pydantic Settings holds it as SecretStr but the SDK doesn't see
        # that. Mirror the pattern used by editorial/team_beat workflows.
        os.environ.setdefault(
            "OPENAI_API_KEY",
            self._settings.openai_api_key.get_secret_value(),
        )

        run_date = run_date or datetime.now(UTC).date()
        lookback = lookback_hours or self._settings.podcast_lookback_hours

        episode_id = await self._episodes.upsert_pending(
            run_date=run_date.isoformat(), language=language
        )
        logger.info(
            "Podcast episode %d pending (run_date=%s lang=%s)",
            episode_id, run_date.isoformat(), language,
        )

        try:
            articles = await self._feed.fetch_raw_articles(lookback_hours=lookback)
            logger.info("Fetched %d raw articles for podcast", len(articles))

            clusters = group_for_podcast(articles)
            selected = select_clusters_for_budget(
                clusters,
                target_word_count=self._settings.podcast_target_word_count,
                min_word_count=self._settings.podcast_min_word_count,
            )
            logger.info(
                "Clustered: %d total, %d selected for budget",
                len(clusters), len(selected),
            )
            continuity_context = load_continuity_context(
                root=self._settings.podcast_episode_root,
                run_date=run_date,
                language=language,
                clusters=selected,
                lookback_days=self._settings.podcast_continuity_days,
            )

            script = await compose_script(
                selected,
                language=language,
                run_date=run_date,
                target_word_count=self._settings.podcast_target_word_count,
                settings=self._settings,
                continuity_context=continuity_context,
            )

            await self._episodes.mark_rendering(
                episode_id,
                story_count=script.story_count,
                word_count=script.word_count,
            )

            if dry_run:
                if output_script_path is not None:
                    output_script_path.parent.mkdir(parents=True, exist_ok=True)
                    output_script_path.write_text(
                        json.dumps(
                            script.model_dump(mode="json"),
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    logger.info("Wrote dry-run script JSON to %s", output_script_path)
                # Mark as rendered with no audio so downstream tools
                # don't try to deliver it; deliver step is a no-op when
                # audio_local_path is NULL (it raises a friendly error).
                await self._episodes.mark_rendered(
                    episode_id,
                    audio_local_path=None,
                    duration_seconds=None,
                    story_count=script.story_count,
                    word_count=script.word_count,
                    episode_title=script.episode_title,
                    episode_summary=script.episode_summary,
                )
                return PodcastRunSummary(
                    episode_id=episode_id,
                    run_date=run_date,
                    language=language,
                    status="rendered",
                    story_count=script.story_count,
                    word_count=script.word_count,
                    duration_seconds=None,
                    audio_local_path=None,
                    error_message="dry-run (no audio rendered)",
                )

            # `render_to_audio` handles both single-render and
            # multi-segment + music paths internally based on Settings.
            render_result = await render_to_audio(
                script,
                run_date=run_date,
                settings=self._settings,
                title=f"T4L Daily — {run_date.isoformat()} ({language})",
            )

            await self._episodes.mark_rendered(
                episode_id,
                audio_local_path=render_result.audio_path,
                duration_seconds=render_result.duration_seconds,
                story_count=script.story_count,
                word_count=script.word_count,
                episode_title=script.episode_title,
                episode_summary=script.episode_summary,
            )
            write_episode_memory(
                self._settings.podcast_episode_root / run_date.isoformat() / language,
                script,
            )
            return PodcastRunSummary(
                episode_id=episode_id,
                run_date=run_date,
                language=language,
                status="rendered",
                story_count=script.story_count,
                word_count=script.word_count,
                duration_seconds=render_result.duration_seconds,
                audio_local_path=render_result.audio_path,
                error_message=None,
            )

        except Exception as exc:  # noqa: BLE001 — translate to DB
            logger.exception("Podcast produce cycle failed for episode %d", episode_id)
            await self._episodes.mark_failed(episode_id, error_message=str(exc))
            raise


def build_default_podcast_workflow(settings: Settings) -> PodcastProduceWorkflow:
    base_url = str(settings.supabase_url).rstrip("/")
    service_role_key = settings.supabase_service_role_key.get_secret_value()
    return PodcastProduceWorkflow(
        settings=settings,
        feed_reader=RawArticleDbReader(
            base_url=base_url, service_role_key=service_role_key
        ),
        episode_writer=PodcastEpisodeWriter(
            base_url=base_url, service_role_key=service_role_key
        ),
    )


def _script_for_dump(script: PodcastScript) -> dict:
    """Convenience for CLI dry-run output."""
    return script.model_dump(mode="json")
