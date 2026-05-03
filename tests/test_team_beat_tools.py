"""Sanity checks on the team_beat lookup_article_content tool factory.

We test the factory contract (name, description, JSON schema) and trust
the OpenAI Agents SDK to invoke the wrapped function correctly at
runtime — direct unit tests against ``FunctionTool.on_invoke_tool``
require a real ``ToolContext`` and are SDK-internal plumbing. Behavior
of the wrapped adapter is exercised end-to-end by the existing
``RawArticleDbReader``/``ArticleLookupFromDb`` tests in
``test_db_adapters.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.team_beat.tools import build_article_lookup_tool


class TestBuildArticleLookupTool:
    def test_tool_metadata(self) -> None:
        adapter = AsyncMock()
        tool = build_article_lookup_tool(adapter)
        assert tool.name == "lookup_article_content"
        # Discipline guidance must be in the description so the model
        # picks it up at tool-listing time, not just when the prompt is
        # active.
        lower_desc = tool.description.lower()
        assert "three lookups" in lower_desc or "load-bearing" in lower_desc

    def test_tool_takes_a_single_url_string_param(self) -> None:
        adapter = AsyncMock()
        tool = build_article_lookup_tool(adapter)
        schema = tool.params_json_schema
        # The Agents SDK strict-mode tools always require an "object"
        # top-level schema with explicit properties.
        assert schema["type"] == "object"
        assert set(schema["properties"].keys()) == {"url"}
        assert schema["properties"]["url"]["type"] == "string"
        # `url` must be required so the model can't omit it.
        assert "url" in schema["required"]
