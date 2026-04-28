from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from typing import TYPE_CHECKING

from agents import Agent, Runner

from app.config import Settings
from app.editorial.helpers import coerce_output
from app.editorial.tracing import build_run_config
from app.schemas import (
    ArticleQualityDecision,
    ArticleSource,
    CyclePublishPlan,
    EditorialMemoryRevision,
    PublishableArticle,
    StoryEntry,
)
from app.team_codes import normalize_team_codes
from app.writer.agents import (
    build_article_quality_gate_agent,
    build_article_writer_agent,
    build_article_writer_agent_de,
    build_editorial_memory_agent,
)
from app.writer.editorial_memory import (
    append_raw_feedback,
    build_feedback_event_markdown,
    build_memory_revision_payload,
    load_editorial_memory,
    read_rewrite_lessons,
    write_rewrite_lessons,
)
from app.writer.image_selector import HeadshotBudget, ImageSelector
from app.writer.persona_selector import build_persona_selector_agent, select_persona
from app.writer.personas import Persona, byline_to_persona_id, get_persona

if TYPE_CHECKING:
    from app.adapters import ArticleWriter

logger = logging.getLogger(__name__)

LANGUAGES: tuple[str, ...] = ("en-US", "de-DE")


def _dedupe_sources(story: StoryEntry) -> list[ArticleSource]:
    """Collapse story.source_digests into reader-visible attribution.

    Deduped by URL (preserves first-seen order). Source name falls back to
    the URL itself if the digest doesn't carry a human name.
    """
    seen: set[str] = set()
    out: list[ArticleSource] = []
    for digest in story.source_digests:
        url = (digest.url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        name = (digest.source_name or "").strip() or url
        out.append(ArticleSource(name=name, url=url))
    return out


class WriterWorkflow:
    def __init__(
        self,
        *,
        settings: Settings,
        article_writer_adapter: ArticleWriter | None = None,
        image_selector: ImageSelector | None = None,
    ) -> None:
        os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key.get_secret_value())
        self._writer_agent_en = build_article_writer_agent(settings)
        self._writer_agent_de = build_article_writer_agent_de(settings)
        self._quality_gate_agent = build_article_quality_gate_agent(settings)
        self._editorial_memory_agent = build_editorial_memory_agent(settings)
        self._persona_selector_agent = build_persona_selector_agent(settings)
        self._article_writer = article_writer_adapter
        self._image_selector = image_selector
        self._editorial_memory_dir = settings.editorial_memory_dir
        self._editorial_memory_lock = asyncio.Lock()

    async def _write_in_language(
        self,
        story: StoryEntry,
        cycle_id: str,
        *,
        agent: Agent,
        persona: Persona,
        language: str,
        existing_content: dict | None,
        quality_gate_feedback: ArticleQualityDecision | None = None,
        previous_draft: PublishableArticle | None = None,
    ) -> PublishableArticle:
        """Pure writer call for one language — NO image cascade here.

        Deterministic overrides applied: language, author, mentioned_players,
        sources. Image selection is a separate step (runs once for EN, the
        result is reused for DE by the caller).
        """
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
        if language == "en-US":
            memory = load_editorial_memory(self._editorial_memory_dir, story)
            if memory:
                writer_payload["editorial_memory"] = memory
        if existing_content is not None:
            writer_payload["existing_article"] = existing_content
        if quality_gate_feedback is not None:
            writer_payload["quality_gate_feedback"] = quality_gate_feedback.model_dump(
                mode="json"
            )
            writer_payload["previous_draft"] = (
                previous_draft.model_dump(mode="json") if previous_draft else None
            )

        writer_input = json.dumps(writer_payload, separators=(",", ":"))

        run_config = build_run_config(
            cycle_id,
            stage=f"write_article_{language}",
            metadata={
                "fingerprint": story.story_fingerprint,
                "persona_id": persona.id,
                "language": language,
            },
        )
        result = await Runner.run(
            agent,
            writer_input,
            run_config=run_config,
            max_turns=4,
            auto_previous_response_id=True,
        )
        article = coerce_output(result.final_output, PublishableArticle)

        deterministic_player_ids = [pm.id for pm in story.player_mentions]
        deterministic_sources = _dedupe_sources(story)
        overrides: dict = {"language": language}
        if article.author != persona.byline:
            overrides["author"] = persona.byline
        if article.mentioned_players != deterministic_player_ids:
            overrides["mentioned_players"] = deterministic_player_ids
        if article.sources != deterministic_sources:
            overrides["sources"] = deterministic_sources
        return article.model_copy(update=overrides)

    async def _run_quality_gate(
        self,
        story: StoryEntry,
        article: PublishableArticle,
        cycle_id: str,
        *,
        persona: Persona,
        rewrite_attempt: int,
    ) -> ArticleQualityDecision:
        payload = {
            "story": {
                "cluster_headline": story.cluster_headline,
                "story_fingerprint": story.story_fingerprint,
                "action": story.action,
                "news_value_score": story.news_value_score,
                "team_codes": normalize_team_codes(story.team_codes),
                "player_mentions": [
                    pm.model_dump(mode="json") for pm in story.player_mentions
                ],
            },
            "source_digests": [d.model_dump(mode="json") for d in story.source_digests],
            "article": article.model_dump(mode="json"),
            "persona": dataclasses.asdict(persona),
            "rewrite_attempt": rewrite_attempt,
        }
        run_config = build_run_config(
            cycle_id,
            stage="article_quality_gate",
            metadata={
                "fingerprint": story.story_fingerprint,
                "language": article.language,
                "rewrite_attempt": rewrite_attempt,
            },
        )
        try:
            result = await Runner.run(
                self._quality_gate_agent,
                json.dumps(payload, separators=(",", ":")),
                run_config=run_config,
                max_turns=3,
                auto_previous_response_id=True,
            )
            decision = coerce_output(result.final_output, ArticleQualityDecision)
            logger.info(
                "Quality gate %s for %s: impact=%.2f specificity=%.2f read=%.2f "
                "grounding=%.2f execution=%.2f — %s",
                decision.decision,
                article.headline[:60],
                decision.impact_score,
                decision.specificity_score,
                decision.readworthiness_score,
                decision.grounding_score,
                decision.execution_score,
                decision.reasoning,
            )
            return decision
        except Exception as exc:
            logger.warning(
                "Quality gate failed for %s — dismissing story for this cycle: %s",
                article.headline[:60],
                exc,
            )
            return ArticleQualityDecision(
                decision="dismiss",
                impact_score=0.0,
                specificity_score=0.0,
                readworthiness_score=0.0,
                grounding_score=0.0,
                execution_score=0.0,
                reasoning="Quality gate unavailable; story dismissed by fail-closed policy.",
                rewrite_brief=None,
            )

    async def _record_editorial_feedback(
        self,
        *,
        cycle_id: str,
        story: StoryEntry,
        article: PublishableArticle,
        persona: Persona,
        decision: ArticleQualityDecision,
        rewrite_attempt: int,
    ) -> None:
        if decision.decision == "approve" and rewrite_attempt == 0:
            return

        event_markdown = build_feedback_event_markdown(
            cycle_id=cycle_id,
            story=story,
            article=article,
            persona=persona,
            decision=decision,
            rewrite_attempt=rewrite_attempt,
        )
        async with self._editorial_memory_lock:
            try:
                append_raw_feedback(self._editorial_memory_dir, event_markdown)
                existing_markdown = read_rewrite_lessons(self._editorial_memory_dir)
                payload = build_memory_revision_payload(
                    existing_markdown=existing_markdown,
                    feedback_event_markdown=event_markdown,
                )
                run_config = build_run_config(
                    cycle_id,
                    stage="editorial_memory_update",
                    metadata={
                        "fingerprint": story.story_fingerprint,
                        "decision": decision.decision,
                        "rewrite_attempt": rewrite_attempt,
                    },
                )
                result = await Runner.run(
                    self._editorial_memory_agent,
                    json.dumps(payload, separators=(",", ":")),
                    run_config=run_config,
                    max_turns=3,
                    auto_previous_response_id=True,
                )
                revision = coerce_output(result.final_output, EditorialMemoryRevision)
                write_rewrite_lessons(
                    self._editorial_memory_dir,
                    revision.updated_markdown,
                )
                logger.info(
                    "Updated editorial memory for %s: %s",
                    story.story_fingerprint,
                    revision.change_summary,
                )
            except Exception as exc:
                logger.warning(
                    "Editorial memory update failed for %s: %s",
                    story.story_fingerprint,
                    exc,
                )

    async def _approve_or_rewrite_en_article(
        self,
        story: StoryEntry,
        cycle_id: str,
        *,
        persona: Persona,
        article: PublishableArticle,
        existing_content: dict | None,
    ) -> PublishableArticle | None:
        decision = await self._run_quality_gate(
            story, article, cycle_id, persona=persona, rewrite_attempt=0
        )
        await self._record_editorial_feedback(
            cycle_id=cycle_id,
            story=story,
            article=article,
            persona=persona,
            decision=decision,
            rewrite_attempt=0,
        )
        if decision.decision == "approve":
            return article
        if decision.decision == "dismiss":
            logger.info(
                "Quality gate dismissed story before DE/image: %s — %s",
                story.cluster_headline[:60],
                decision.reasoning,
            )
            return None

        rewritten = await self._write_in_language(
            story,
            cycle_id,
            agent=self._writer_agent_en,
            persona=persona,
            language="en-US",
            existing_content=existing_content,
            quality_gate_feedback=decision,
            previous_draft=article,
        )
        second_decision = await self._run_quality_gate(
            story, rewritten, cycle_id, persona=persona, rewrite_attempt=1
        )
        await self._record_editorial_feedback(
            cycle_id=cycle_id,
            story=story,
            article=rewritten,
            persona=persona,
            decision=second_decision,
            rewrite_attempt=1,
        )
        if second_decision.decision == "approve":
            return rewritten

        logger.info(
            "Quality gate rejected story after rewrite (%s): %s",
            second_decision.decision,
            second_decision.reasoning,
        )
        return None

    async def _select_image_for_story(
        self,
        en_article: PublishableArticle,
        story: StoryEntry,
        cycle_id: str,
        headshot_budget: HeadshotBudget | None,
        curated_budget: HeadshotBudget | None,
    ) -> str | None:
        """Run the image cascade once per story. Returns the URL (or None).

        Called with the EN article so the cascade validators see English
        headline/intro — they're calibrated on English text. The same URL
        is then reused for the DE article by the caller.
        """
        if self._image_selector is None:
            return None
        try:
            image_result = await self._image_selector.select(
                en_article, story,
                cycle_id=cycle_id,
                headshot_budget=headshot_budget,
                curated_budget=curated_budget,
            )
            logger.info(
                "Image tier=%s for %s: %s",
                image_result.tier,
                en_article.headline[:60],
                image_result.notes,
            )
            return image_result.url
        except Exception as exc:
            logger.warning(
                "Image selection failed for %s: %s",
                en_article.headline[:60], exc,
            )
            return None

    async def _write_pair(
        self,
        story: StoryEntry,
        cycle_id: str,
        persona_id: str,
        existing_en_content: dict | None,
        existing_de_content: dict | None,
        headshot_budget: HeadshotBudget | None,
        curated_budget: HeadshotBudget | None,
    ) -> list[PublishableArticle]:
        """Produce both en-US and de-DE articles for one story.

        Flow per story (serial):
          1. Write EN (persona in EN voice, existing EN content on updates).
          2. Run image cascade ONCE using the EN article — reused for DE.
          3. Write DE (persona in DE voice, existing DE content on updates).

        Returns [en_article, de_article] with image URL applied to both.
        """
        persona_en = get_persona(persona_id, "en-US")
        persona_de = get_persona(persona_id, "de-DE")

        # 1. English
        en_article = await self._write_in_language(
            story, cycle_id,
            agent=self._writer_agent_en,
            persona=persona_en,
            language="en-US",
            existing_content=existing_en_content,
        )
        en_article = await self._approve_or_rewrite_en_article(
            story,
            cycle_id,
            persona=persona_en,
            article=en_article,
            existing_content=existing_en_content,
        )
        if en_article is None:
            return []

        # 2. Image cascade (once per story)
        image_url = await self._select_image_for_story(
            en_article, story, cycle_id, headshot_budget, curated_budget
        )
        if image_url and en_article.image != image_url:
            en_article = en_article.model_copy(update={"image": image_url})

        # 3. German
        de_article = await self._write_in_language(
            story, cycle_id,
            agent=self._writer_agent_de,
            persona=persona_de,
            language="de-DE",
            existing_content=existing_de_content,
        )
        if image_url:
            de_article = de_article.model_copy(update={"image": image_url})

        return [en_article, de_article]

    async def _resolve_persona_id(
        self,
        story: StoryEntry,
        cycle_id: str,
        existing_en_content: dict | None,
        existing_de_content: dict | None,
    ) -> str:
        """Return the persona archetype id (same for EN + DE).

        For updates: preserve the original author's archetype if we can
        recognize the byline (English OR German). Otherwise run the selector.
        """
        for existing in (existing_en_content, existing_de_content):
            if existing:
                preserved = byline_to_persona_id(existing.get("author"))
                if preserved is not None:
                    logger.info(
                        "Preserving original persona %s for update: %s",
                        preserved, story.cluster_headline[:60],
                    )
                    return preserved
        selection = await select_persona(
            self._persona_selector_agent, story, cycle_id
        )
        return selection.id if hasattr(selection, "id") else selection.persona_id  # type: ignore[attr-defined]

    async def run_write_phase(
        self, plan: CyclePublishPlan, cycle_id: str
    ) -> list[PublishableArticle]:
        publishable = [s for s in plan.stories if s.action in ("publish", "update")]
        if not publishable:
            logger.info("No stories to write")
            return []

        # Fetch existing EN content for updates (English row lookup by editorial_state id).
        # Fetch existing DE content for updates (German row lookup by fingerprint+language).
        existing_en_by_fp: dict[str, dict] = {}
        existing_de_by_fp: dict[str, dict] = {}
        if self._article_writer:
            for story in publishable:
                if story.action != "update":
                    continue
                if story.existing_article_id:
                    en_content = await self._article_writer.fetch_article_content(
                        story.existing_article_id
                    )
                    if en_content:
                        existing_en_by_fp[story.story_fingerprint] = en_content
                de_content = await self._article_writer.fetch_article_by_fingerprint(
                    story.story_fingerprint, "de-DE"
                )
                if de_content:
                    existing_de_by_fp[story.story_fingerprint] = de_content

        # Persona archetype per story — one LLM call shared across EN+DE.
        persona_ids = await asyncio.gather(
            *(
                self._resolve_persona_id(
                    story, cycle_id,
                    existing_en_by_fp.get(story.story_fingerprint),
                    existing_de_by_fp.get(story.story_fingerprint),
                )
                for story in publishable
            )
        )

        # Headshot tier is capped at 40% of stories (rounded up, so small
        # cycles still get at least one). Curated tier is uncapped — it
        # fills in everything that didn't win a web search or headshot.
        headshot_budget = HeadshotBudget.for_cycle(
            len(publishable), ratio=0.4, round_up=True,
        )
        curated_budget = None
        logger.info(
            "Headshot budget this cycle: %d/%d (curated uncapped)",
            headshot_budget.capacity, len(publishable),
        )

        logger.info("Writing %d stories × 2 languages in parallel", len(publishable))
        tasks = [
            self._write_pair(
                story,
                cycle_id,
                persona_id=persona_id,
                existing_en_content=existing_en_by_fp.get(story.story_fingerprint),
                existing_de_content=existing_de_by_fp.get(story.story_fingerprint),
                headshot_budget=headshot_budget,
                curated_budget=curated_budget,
            )
            for story, persona_id in zip(publishable, persona_ids, strict=True)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        articles: list[PublishableArticle] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "Failed to write article pair for %s: %s",
                    publishable[i].story_fingerprint, result,
                )
            else:
                articles.extend(result)

        logger.info(
            "Successfully wrote %d articles across %d stories (EN+DE)",
            len(articles), len(publishable),
        )
        return articles
