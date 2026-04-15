from __future__ import annotations

from agents import Agent
from agents.tool import FunctionTool

from app.config import Settings
from app.editorial.model import build_model_settings
from app.editorial.prompts import get_prompt
from app.schemas import (
    ArticleDigestAgentResult,
    CyclePublishPlan,
    StoryClusterResult,
)


def build_article_data_agent(
    settings: Settings,
    *,
    article_lookup_tool: FunctionTool,
) -> Agent:
    return Agent(
        name="Article Data Agent",
        instructions=get_prompt("article_data_agent"),
        model=settings.agent_model("article_data_agent"),
        model_settings=build_model_settings(
            settings,
            tool_choice="required",
            parallel_tool_calls=False,
            max_tokens=800,
        ),
        tools=[article_lookup_tool],
        output_type=ArticleDigestAgentResult,
    )


def build_story_cluster_agent(
    settings: Settings,
    *,
    article_digest_tool: FunctionTool,
) -> Agent:
    return Agent(
        name="Story Cluster Agent",
        instructions=get_prompt("story_cluster_agent"),
        model=settings.agent_model("story_cluster_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=True),
        tools=[article_digest_tool],
        output_type=StoryClusterResult,
    )


def build_editorial_orchestrator_agent(
    settings: Settings,
    *,
    story_cluster_tool: FunctionTool,
) -> Agent:
    return Agent(
        name="Editorial Cycle Orchestrator",
        instructions=get_prompt("editorial_orchestrator_agent"),
        model=settings.agent_model("editorial_orchestrator_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=True),
        tools=[story_cluster_tool],
        output_type=CyclePublishPlan,
    )
