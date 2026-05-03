"""Function tools exposed to team-beat agents.

The editorial module has its own copy of ``build_article_lookup_tool`` in
``app/editorial/tools.py``. This file mirrors that pattern locally to keep
the module-boundary rule from CLAUDE.md intact: ``app/team_beat/`` does
not import internals from ``app/editorial/``.

Only one tool today: ``lookup_article_content`` — wraps
``ArticleLookupFromDb.lookup_article(url)`` so the Team Beat Reporter
Agent can fetch full article bodies on demand for the 1–3 articles it
judges load-bearing for a brief, rather than reasoning from headlines
alone.
"""

from __future__ import annotations

from agents import function_tool
from agents.tool import FunctionTool

from app.adapters import ArticleLookupFromDb
from app.schemas import ArticleContentLookupToolResponse


def build_article_lookup_tool(adapter: ArticleLookupFromDb) -> FunctionTool:
    """Wrap an ``ArticleLookupFromDb`` adapter as an Agents-SDK function tool.

    The tool returns a JSON-serializable dict matching the
    ``ArticleContentLookupToolResponse`` schema (``found``, ``article``).
    """

    @function_tool(
        name_override="lookup_article_content",
        description_override=(
            "Look up one exact article URL in Supabase and return the stored "
            "article content (body text + metadata). Use sparingly — at most "
            "three lookups per brief, only for the articles judged most "
            "load-bearing for the lead."
        ),
        strict_mode=True,
    )
    async def lookup_article_content(url: str) -> dict:
        response = await adapter.lookup_article(url)
        return ArticleContentLookupToolResponse.model_validate(response).model_dump(
            mode="json"
        )

    return lookup_article_content
