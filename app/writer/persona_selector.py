"""Persona selection for the writer workflow.

A lightweight agent classifies each publishable story into one of three
personas (analyst / insider / columnist) before the writer runs. Runs on a
small/cheap model (nano) because the decision is well-constrained.

For updates, the original author's persona is preserved — the selector is
only consulted when we're publishing a fresh article.
"""

from __future__ import annotations

import json
import logging

from agents import Agent, Runner

from app.config import Settings
from app.editorial.helpers import coerce_output
from app.editorial.tracing import build_run_config
from app.schemas import PersonaSelection, StoryEntry
from app.writer.model import build_model_settings
from app.writer.personas import PERSONAS, Persona, get_persona
from app.writer.prompts import get_prompt

logger = logging.getLogger(__name__)

_DEFAULT_PERSONA_ID = "columnist"


def build_persona_selector_agent(settings: Settings) -> Agent:
    return Agent(
        name="Persona Selector Agent",
        instructions=get_prompt("persona_selector_agent"),
        model=settings.agent_model("persona_selector_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
        output_type=PersonaSelection,
    )


def _selector_input(story: StoryEntry) -> str:
    top_facts: list[str] = []
    for digest in story.source_digests[:3]:
        top_facts.extend(digest.key_facts[:3])
    payload = {
        "cluster_headline": story.cluster_headline,
        "news_value_score": story.news_value_score,
        "action": story.action,
        "top_key_facts": top_facts[:8],
        "source_count": len(story.source_digests),
    }
    return json.dumps(payload, separators=(",", ":"))


async def select_persona(
    agent: Agent,
    story: StoryEntry,
    cycle_id: str,
) -> Persona:
    """Select a persona for a story. Falls back to columnist on any error."""
    try:
        run_config = build_run_config(
            cycle_id,
            stage="select_persona",
            metadata={"fingerprint": story.story_fingerprint},
        )
        result = await Runner.run(
            agent,
            _selector_input(story),
            run_config=run_config,
            max_turns=2,
            auto_previous_response_id=True,
        )
        selection = coerce_output(result.final_output, PersonaSelection)
        if selection.persona_id not in PERSONAS:
            logger.warning(
                "Persona selector returned unknown id %r — falling back",
                selection.persona_id,
            )
            return get_persona(_DEFAULT_PERSONA_ID)
        logger.info(
            "Persona %s selected for %s: %s",
            selection.persona_id,
            story.cluster_headline[:60],
            selection.reasoning,
        )
        return get_persona(selection.persona_id)
    except Exception as exc:
        logger.warning(
            "Persona selection failed for %s — falling back to %s: %s",
            story.cluster_headline[:60],
            _DEFAULT_PERSONA_ID,
            exc,
        )
        return get_persona(_DEFAULT_PERSONA_ID)
