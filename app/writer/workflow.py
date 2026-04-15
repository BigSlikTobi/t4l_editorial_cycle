from __future__ import annotations

import asyncio
import json
import logging
import os

from agents import Runner

from app.config import Settings
from app.editorial.helpers import coerce_output
from app.editorial.tracing import build_run_config
from app.schemas import CyclePublishPlan, PublishableArticle, StoryEntry
from app.writer.agents import build_article_writer_agent

logger = logging.getLogger(__name__)


class WriterWorkflow:
    def __init__(self, *, settings: Settings) -> None:
        os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key.get_secret_value())
        self._writer_agent = build_article_writer_agent(settings)

    async def _write_single(self, story: StoryEntry, cycle_id: str) -> PublishableArticle:
        writer_input = json.dumps(
            {
                "cluster_headline": story.cluster_headline,
                "story_fingerprint": story.story_fingerprint,
                "action": story.action,
                "news_value_score": story.news_value_score,
                "source_digests": [d.model_dump(mode="json") for d in story.source_digests],
                "team_codes": story.team_codes,
            },
            separators=(",", ":"),
        )

        run_config = build_run_config(
            cycle_id,
            stage="write_article",
            metadata={"fingerprint": story.story_fingerprint},
        )
        result = await Runner.run(
            self._writer_agent,
            writer_input,
            run_config=run_config,
            max_turns=4,
            auto_previous_response_id=True,
        )
        return coerce_output(result.final_output, PublishableArticle)

    async def run_write_phase(
        self, plan: CyclePublishPlan, cycle_id: str
    ) -> list[PublishableArticle]:
        publishable = [s for s in plan.stories if s.action in ("publish", "update")]
        if not publishable:
            logger.info("No stories to write")
            return []

        logger.info("Writing %d articles in parallel", len(publishable))
        tasks = [self._write_single(story, cycle_id) for story in publishable]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        articles: list[PublishableArticle] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "Failed to write article for %s: %s",
                    publishable[i].story_fingerprint,
                    result,
                )
            else:
                articles.append(result)

        logger.info("Successfully wrote %d/%d articles", len(articles), len(publishable))
        return articles
