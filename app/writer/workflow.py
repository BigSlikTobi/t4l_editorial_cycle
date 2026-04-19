from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from typing import TYPE_CHECKING

from agents import Runner

from app.config import Settings
from app.editorial.helpers import coerce_output
from app.editorial.tracing import build_run_config
from app.schemas import CyclePublishPlan, PublishableArticle, StoryEntry
from app.team_codes import normalize_team_codes
from app.writer.agents import build_article_writer_agent
from app.writer.image_selector import HeadshotBudget, ImageSelector
from app.writer.persona_selector import build_persona_selector_agent, select_persona
from app.writer.personas import Persona, byline_to_persona_id, get_persona

if TYPE_CHECKING:
    from app.adapters import ArticleWriter

logger = logging.getLogger(__name__)


class WriterWorkflow:
    def __init__(
        self,
        *,
        settings: Settings,
        article_writer_adapter: ArticleWriter | None = None,
        image_selector: ImageSelector | None = None,
    ) -> None:
        os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key.get_secret_value())
        self._writer_agent = build_article_writer_agent(settings)
        self._persona_selector_agent = build_persona_selector_agent(settings)
        self._article_writer = article_writer_adapter
        self._image_selector = image_selector

    async def _write_single(
        self,
        story: StoryEntry,
        cycle_id: str,
        persona: Persona,
        existing_content: dict | None = None,
        headshot_budget: HeadshotBudget | None = None,
    ) -> PublishableArticle:
        writer_payload: dict = {
            "cluster_headline": story.cluster_headline,
            "story_fingerprint": story.story_fingerprint,
            "action": story.action,
            "news_value_score": story.news_value_score,
            "source_digests": [d.model_dump(mode="json") for d in story.source_digests],
            "team_codes": normalize_team_codes(story.team_codes),
            "player_mentions": [pm.model_dump(mode="json") for pm in story.player_mentions],
            "persona": dataclasses.asdict(persona),
        }
        if existing_content is not None:
            writer_payload["existing_article"] = existing_content

        writer_input = json.dumps(writer_payload, separators=(",", ":"))

        run_config = build_run_config(
            cycle_id,
            stage="write_article",
            metadata={
                "fingerprint": story.story_fingerprint,
                "persona_id": persona.id,
            },
        )
        result = await Runner.run(
            self._writer_agent,
            writer_input,
            run_config=run_config,
            max_turns=4,
            auto_previous_response_id=True,
        )
        article = coerce_output(result.final_output, PublishableArticle)
        # Deterministic overrides: byline is set from persona, mentioned_players
        # is set from the story (feed-sourced GSIS IDs) — never trust the LLM
        # to emit FK-grade IDs.
        deterministic_player_ids = [pm.id for pm in story.player_mentions]
        overrides: dict = {}
        if article.author != persona.byline:
            overrides["author"] = persona.byline
        if article.mentioned_players != deterministic_player_ids:
            overrides["mentioned_players"] = deterministic_player_ids
        if overrides:
            article = article.model_copy(update=overrides)

        # Image cascade (non-fatal): pick an image post-write so we have the
        # final headline/intro to validate against.
        if self._image_selector is not None:
            try:
                image_result = await self._image_selector.select(
                    article, story,
                    cycle_id=cycle_id,
                    headshot_budget=headshot_budget,
                )
                logger.info(
                    "Image tier=%s for %s: %s",
                    image_result.tier,
                    article.headline[:60],
                    image_result.notes,
                )
                if image_result.url and article.image != image_result.url:
                    article = article.model_copy(update={"image": image_result.url})
            except Exception as exc:
                logger.warning("Image selection failed for %s: %s", article.headline[:60], exc)

        return article

    async def _resolve_persona(
        self,
        story: StoryEntry,
        cycle_id: str,
        existing_content: dict | None,
    ) -> Persona:
        """For updates: keep the original author's persona if we can recognize it.
        For publishes (or unrecognized existing bylines): ask the selector.
        """
        if story.action == "update" and existing_content:
            existing_byline = existing_content.get("author")
            preserved_id = byline_to_persona_id(existing_byline)
            if preserved_id is not None:
                logger.info(
                    "Preserving original persona %s for update: %s",
                    preserved_id,
                    story.cluster_headline[:60],
                )
                return get_persona(preserved_id)
        return await select_persona(self._persona_selector_agent, story, cycle_id)

    async def run_write_phase(
        self, plan: CyclePublishPlan, cycle_id: str
    ) -> list[PublishableArticle]:
        publishable = [s for s in plan.stories if s.action in ("publish", "update")]
        if not publishable:
            logger.info("No stories to write")
            return []

        # Fetch existing content for updates (now also includes `author`)
        existing_content_map: dict[str, dict] = {}
        if self._article_writer:
            for story in publishable:
                if story.action == "update" and story.existing_article_id:
                    content = await self._article_writer.fetch_article_content(
                        story.existing_article_id
                    )
                    if content:
                        existing_content_map[story.story_fingerprint] = content
                        logger.info(
                            "Fetched existing content for update: %s",
                            story.cluster_headline[:60],
                        )

        # Resolve persona per story (in parallel with each other, not with writers)
        personas = await asyncio.gather(
            *(
                self._resolve_persona(
                    story,
                    cycle_id,
                    existing_content_map.get(story.story_fingerprint),
                )
                for story in publishable
            )
        )

        # Shared per-cycle budget: at most 50% of articles may use the
        # player_headshot tier. Surplus falls through to AI generation / team logo
        # so the feed doesn't look like a wall of mugshots.
        headshot_budget = HeadshotBudget.for_cycle(len(publishable), ratio=0.5)
        logger.info(
            "Headshot budget this cycle: %d/%d",
            headshot_budget.capacity, len(publishable),
        )

        logger.info("Writing %d articles in parallel", len(publishable))
        tasks = [
            self._write_single(
                story,
                cycle_id,
                persona=persona,
                existing_content=existing_content_map.get(story.story_fingerprint),
                headshot_budget=headshot_budget,
            )
            for story, persona in zip(publishable, personas, strict=True)
        ]
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
