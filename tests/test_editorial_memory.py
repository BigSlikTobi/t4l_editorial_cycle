from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.schemas import ArticleDigest, ArticleQualityDecision, EditorialMemoryRevision, StoryEntry
from app.writer.editorial_memory import (
    append_raw_feedback,
    build_feedback_event_markdown,
    load_editorial_memory,
    read_rewrite_lessons,
)
from app.writer.personas import get_persona
import app.writer.workflow as workflow_module
from app.writer.workflow import WriterWorkflow

from tests.test_article_quality_gate import _article


def _story() -> StoryEntry:
    return StoryEntry(
        rank=1,
        cluster_headline="Chiefs face a sharper receiver decision",
        story_fingerprint="fp-memory",
        action="publish",
        news_value_score=0.8,
        reasoning="Useful team consequence",
        source_digests=[
            ArticleDigest(
                story_id="s1",
                url="https://example.com/story",
                title="Chiefs receiver decision sharpens",
                source_name="Example",
                summary="Kansas City has a concrete receiver decision.",
                key_facts=["Kansas City must make a receiver decision by Tuesday"],
                confidence=0.9,
                content_status="full",
                team_mentions=["KC"],
            )
        ],
        team_codes=["KC"],
    )


def _decision(decision: str = "rewrite") -> ArticleQualityDecision:
    return ArticleQualityDecision(
        decision=decision,  # type: ignore[arg-type]
        impact_score=0.7,
        specificity_score=0.6,
        readworthiness_score=0.5,
        grounding_score=0.9,
        execution_score=0.4,
        reasoning="The draft buried the concrete deadline.",
        rewrite_brief="Lead with the Tuesday receiver deadline.",
    )


def test_load_editorial_memory_reads_wiki_pages(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "what_makes_a_story_readworthy.md").write_text(
        "# What Makes A Story Readworthy\n\n- Lead with concrete consequence.",
        encoding="utf-8",
    )
    (wiki / "rewrite_lessons.md").write_text(
        "# Rewrite And Dismissal Lessons\n\n- Do not bury deadlines.",
        encoding="utf-8",
    )

    memory = load_editorial_memory(tmp_path, _story())

    assert "Treat as coaching, not facts" in memory
    assert "Lead with concrete consequence" in memory
    assert "Do not bury deadlines" in memory
    assert "Chiefs face a sharper receiver decision" in memory


def test_append_raw_feedback_writes_daily_markdown(tmp_path):
    event = build_feedback_event_markdown(
        cycle_id="cycle",
        story=_story(),
        article=_article(),
        persona=get_persona("insider"),
        decision=_decision(),
        rewrite_attempt=0,
    )

    path = append_raw_feedback(tmp_path, event)

    text = path.read_text(encoding="utf-8")
    assert "# Editorial Feedback Events" in text
    assert "Lead with the Tuesday receiver deadline" in text


async def test_write_in_language_injects_editorial_memory(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "what_makes_a_story_readworthy.md").write_text(
        "# What Makes A Story Readworthy\n\n- Lead with concrete consequence.",
        encoding="utf-8",
    )

    workflow = WriterWorkflow.__new__(WriterWorkflow)
    workflow._editorial_memory_dir = tmp_path
    seen_payload: dict | None = None

    async def fake_run(agent, writer_input, **kwargs):
        nonlocal seen_payload
        seen_payload = json.loads(writer_input)
        return SimpleNamespace(final_output=_article())

    monkeypatch.setattr(workflow_module.Runner, "run", fake_run)

    await workflow._write_in_language(
        _story(),
        "cycle",
        agent=object(),
        persona=get_persona("insider"),
        language="en-US",
        existing_content=None,
    )

    assert seen_payload is not None
    assert "editorial_memory" in seen_payload
    assert "Lead with concrete consequence" in seen_payload["editorial_memory"]


async def test_record_editorial_feedback_updates_rewrite_lessons(tmp_path, monkeypatch):
    workflow = WriterWorkflow.__new__(WriterWorkflow)
    workflow._editorial_memory_dir = tmp_path
    workflow._editorial_memory_lock = asyncio.Lock()
    workflow._editorial_memory_agent = object()

    async def fake_run(agent, payload, **kwargs):
        return SimpleNamespace(
            final_output=EditorialMemoryRevision(
                updated_markdown=(
                    "# Rewrite And Dismissal Lessons\n\n"
                    "- Lead with the concrete deadline when it exists."
                ),
                change_summary="Added deadline lesson.",
            )
        )

    monkeypatch.setattr(workflow_module.Runner, "run", fake_run)

    await workflow._record_editorial_feedback(
        cycle_id="cycle",
        story=_story(),
        article=_article(),
        persona=get_persona("insider"),
        decision=_decision(),
        rewrite_attempt=0,
    )

    assert "concrete deadline" in read_rewrite_lessons(tmp_path)
    raw_files = list((tmp_path / "raw_feedback").glob("*.md"))
    assert len(raw_files) == 1
