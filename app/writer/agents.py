from __future__ import annotations

from agents import Agent

from app.config import Settings
from app.schemas import PublishableArticle
from app.writer.model import build_model_settings
from app.writer.prompts import get_prompt


def build_article_writer_agent(settings: Settings) -> Agent:
    return Agent(
        name="Article Writer Agent",
        instructions=get_prompt("article_writer_agent"),
        model=settings.agent_model("article_writer_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
        output_type=PublishableArticle,
    )
