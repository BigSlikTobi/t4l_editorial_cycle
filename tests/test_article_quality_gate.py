from __future__ import annotations

from app.schemas import ArticleDigest, ArticleQualityDecision, PublishableArticle, StoryEntry
from app.writer.personas import get_persona
import app.writer.workflow as workflow_module
from app.writer.workflow import WriterWorkflow


def _decision(decision: str, rewrite_brief: str | None = None) -> ArticleQualityDecision:
    return ArticleQualityDecision(
        decision=decision,  # type: ignore[arg-type]
        impact_score=0.8,
        specificity_score=0.8,
        readworthiness_score=0.8,
        grounding_score=0.9,
        execution_score=0.8,
        reasoning=f"{decision} reasoning",
        rewrite_brief=rewrite_brief,
    )


def _story() -> StoryEntry:
    return StoryEntry(
        rank=1,
        cluster_headline="Chiefs face a tougher roster decision",
        story_fingerprint="fp",
        action="publish",
        news_value_score=0.82,
        reasoning="Concrete team impact",
        source_digests=[
            ArticleDigest(
                story_id="s1",
                url="https://example.com/story",
                title="Chiefs roster decision sharpens",
                source_name="Example",
                summary="The Chiefs have a roster decision after a named update.",
                key_facts=["Kansas City must make a roster decision by Tuesday"],
                confidence=0.9,
                content_status="full",
                team_mentions=["KC"],
            )
        ],
        team_codes=["KC"],
    )


def _article(headline: str = "Chiefs face roster decision") -> PublishableArticle:
    return PublishableArticle(
        team="KC",
        headline=headline,
        sub_headline="Kansas City has a concrete decision on its calendar",
        introduction="Kansas City has a decision to make by Tuesday.",
        content="Kansas City has a decision to make by Tuesday.\n\nThe timing matters.",
        x_post="Kansas City has a concrete roster decision due by Tuesday.",
        bullet_points="- Kansas City has a decision due Tuesday",
        story_fingerprint="fp",
        author="Jenna Alvarez",
    )


async def no_op_record(*args, **kwargs):
    return None


async def test_quality_gate_approve_returns_original_article():
    workflow = WriterWorkflow.__new__(WriterWorkflow)
    calls: list[int] = []

    async def gate(*args, rewrite_attempt: int, **kwargs):
        calls.append(rewrite_attempt)
        return _decision("approve")

    workflow._run_quality_gate = gate  # type: ignore[method-assign]
    workflow._record_editorial_feedback = no_op_record  # type: ignore[method-assign]
    result = await workflow._approve_or_rewrite_en_article(
        _story(),
        "cycle",
        persona=get_persona("insider"),
        article=_article(),
        existing_content=None,
    )

    assert result is not None
    assert result.headline == "Chiefs face roster decision"
    assert calls == [0]


async def test_quality_gate_dismiss_stops_story_before_rewrite():
    workflow = WriterWorkflow.__new__(WriterWorkflow)

    async def gate(*args, rewrite_attempt: int, **kwargs):
        return _decision("dismiss")

    async def write(*args, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("dismissed story should not be rewritten")

    workflow._run_quality_gate = gate  # type: ignore[method-assign]
    workflow._write_in_language = write  # type: ignore[method-assign]
    workflow._record_editorial_feedback = no_op_record  # type: ignore[method-assign]

    result = await workflow._approve_or_rewrite_en_article(
        _story(),
        "cycle",
        persona=get_persona("insider"),
        article=_article(),
        existing_content=None,
    )

    assert result is None


async def test_quality_gate_rewrite_gets_one_retry_then_approves():
    workflow = WriterWorkflow.__new__(WriterWorkflow)
    calls: list[int] = []

    async def gate(*args, rewrite_attempt: int, **kwargs):
        calls.append(rewrite_attempt)
        if rewrite_attempt == 0:
            return _decision("rewrite", rewrite_brief="Lead with the Tuesday deadline.")
        return _decision("approve")

    async def write(*args, quality_gate_feedback, previous_draft, **kwargs):
        assert quality_gate_feedback.rewrite_brief == "Lead with the Tuesday deadline."
        assert previous_draft.headline == "Chiefs face roster decision"
        return _article("Chiefs roster deadline sharpens Tuesday decision")

    workflow._run_quality_gate = gate  # type: ignore[method-assign]
    workflow._write_in_language = write  # type: ignore[method-assign]
    workflow._record_editorial_feedback = no_op_record  # type: ignore[method-assign]
    workflow._writer_agent_en = object()

    result = await workflow._approve_or_rewrite_en_article(
        _story(),
        "cycle",
        persona=get_persona("insider"),
        article=_article(),
        existing_content=None,
    )

    assert result is not None
    assert result.headline == "Chiefs roster deadline sharpens Tuesday decision"
    assert calls == [0, 1]


async def test_quality_gate_rewrite_failure_dismisses_after_one_retry():
    workflow = WriterWorkflow.__new__(WriterWorkflow)

    async def gate(*args, rewrite_attempt: int, **kwargs):
        return _decision(
            "rewrite",
            rewrite_brief="Still needs a sharper source-supported consequence.",
        )

    async def write(*args, **kwargs):
        return _article("Chiefs still need sharper angle")

    workflow._run_quality_gate = gate  # type: ignore[method-assign]
    workflow._write_in_language = write  # type: ignore[method-assign]
    workflow._record_editorial_feedback = no_op_record  # type: ignore[method-assign]
    workflow._writer_agent_en = object()

    result = await workflow._approve_or_rewrite_en_article(
        _story(),
        "cycle",
        persona=get_persona("insider"),
        article=_article(),
        existing_content=None,
    )

    assert result is None


async def test_quality_gate_failure_fails_closed(monkeypatch):
    workflow = WriterWorkflow.__new__(WriterWorkflow)
    workflow._quality_gate_agent = object()

    async def fail(*args, **kwargs):
        raise RuntimeError("gate unavailable")

    monkeypatch.setattr(workflow_module.Runner, "run", fail)

    decision = await workflow._run_quality_gate(
        _story(),
        _article(),
        "cycle",
        persona=get_persona("insider"),
        rewrite_attempt=0,
    )

    assert decision.decision == "dismiss"
    assert "fail-closed" in decision.reasoning
