from __future__ import annotations

import json
import logging
from typing import TypeVar

from agents import Agent, Runner, function_tool
from agents.tool import FunctionTool
from agents.tool_context import ToolContext

from app.adapters import ArticleLookupAdapter
from app.editorial.helpers import coerce_output, recompute_cluster_fingerprint
from app.schemas import (
    ArticleContentLookupToolResponse,
    ArticleDigestAgentResult,
    RawArticle,
    StoryClusterResult,
)

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel")


async def _run_nested_agent(
    tool_context: ToolContext,
    *,
    agent: Agent,
    agent_input: str,
    output_schema: type[TModel],
    max_turns: int,
) -> dict:
    result = await Runner.run(
        agent,
        agent_input,
        context=tool_context.context,
        run_config=tool_context.run_config,
        max_turns=max_turns,
        auto_previous_response_id=True,
    )
    output = coerce_output(result.final_output, output_schema)
    return output.model_dump(mode="json")


def build_article_lookup_tool(adapter: ArticleLookupAdapter) -> FunctionTool:
    @function_tool(
        name_override="lookup_article_content",
        description_override=(
            "Look up one exact article URL in Supabase and return the stored article content "
            "and metadata."
        ),
        strict_mode=True,
    )
    async def lookup_article_content(url: str) -> dict:
        """Look up stored article content for one exact NFL article URL."""
        response = await adapter.lookup_article(url)
        return ArticleContentLookupToolResponse.model_validate(response).model_dump(mode="json")

    return lookup_article_content


def build_article_digest_tool(agent: Agent) -> FunctionTool:
    @function_tool(
        name_override="digest_article",
        description_override=(
            "Digest one NFL article and return a structured summary, key facts, and confidence."
        ),
        strict_mode=True,
    )
    async def digest_article(
        tool_context: ToolContext,
        story_id: str,
        url: str,
        title: str,
        source_name: str,
        category: str | None = None,
    ) -> dict:
        input_json = json.dumps(
            {
                "story_id": story_id,
                "url": url,
                "title": title,
                "source_name": source_name,
                "category": category,
            },
            separators=(",", ":"),
        )
        return await _run_nested_agent(
            tool_context,
            agent=agent,
            agent_input=input_json,
            output_schema=ArticleDigestAgentResult,
            max_turns=4,
        )

    return digest_article


def build_story_cluster_tool(agent: Agent) -> FunctionTool:
    @function_tool(
        name_override="analyze_story_cluster",
        description_override=(
            "Analyze a group of articles about the same event and return a scored cluster result."
        ),
        strict_mode=True,
    )
    async def analyze_story_cluster(
        tool_context: ToolContext,
        cluster_label: str,
        articles: list[RawArticle],
        published_fingerprints: list[str],
    ) -> dict:
        input_json = json.dumps(
            {
                "cluster_label": cluster_label,
                "articles": [a.model_dump(mode="json") for a in articles],
                "published_fingerprints": published_fingerprints,
            },
            separators=(",", ":"),
        )
        raw_dict = await _run_nested_agent(
            tool_context,
            agent=agent,
            agent_input=input_json,
            output_schema=StoryClusterResult,
            max_turns=16,
        )

        # Post-process: overwrite LLM-generated slug with deterministic hash
        cluster = StoryClusterResult.model_validate(raw_dict)
        real_fp = recompute_cluster_fingerprint(cluster)
        cluster = cluster.model_copy(update={
            "story_fingerprint": real_fp,
            "is_new": real_fp not in published_fingerprints,
        })
        logger.info(
            "Cluster %s: LLM fp=%s -> deterministic fp=%s, is_new=%s",
            cluster_label,
            raw_dict.get("story_fingerprint", "?"),
            real_fp,
            cluster.is_new,
        )
        return cluster.model_dump(mode="json")

    return analyze_story_cluster
