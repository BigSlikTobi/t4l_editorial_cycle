from __future__ import annotations

import pytest
from datetime import UTC, datetime
from pydantic import ValidationError

from app.schemas import (
    ArticleDigest,
    ArticleDigestAgentResult,
    ArticleQualityDecision,
    CyclePublishPlan,
    CycleResult,
    EditorialMemoryRevision,
    EntityMatch,
    PublishableArticle,
    PublishedStoryRecord,
    RawArticle,
    StoryClusterResult,
    StoryEntry,
)


class TestRawArticle:
    def test_round_trip(self, sample_raw_article: RawArticle) -> None:
        data = sample_raw_article.model_dump(mode="json")
        restored = RawArticle.model_validate(data)
        assert restored.id == sample_raw_article.id
        assert restored.url == sample_raw_article.url
        assert len(restored.entities) == 1

    def test_defaults(self) -> None:
        article = RawArticle(id="1", url="http://x.com", title="t", source_name="s")
        assert article.category is None
        assert article.facts_count == 0
        assert article.entities == []


class TestArticleDigest:
    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            ArticleDigest(
                story_id="1", url="http://x.com", title="t", source_name="s",
                summary="s", confidence=0.5, bogus_field="bad",
            )

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ArticleDigest(
                story_id="1", url="http://x.com", title="t", source_name="s",
                summary="s", confidence=1.5,
            )

    def test_round_trip(self, sample_article_digest: ArticleDigest) -> None:
        data = sample_article_digest.model_dump(mode="json")
        restored = ArticleDigest.model_validate(data)
        assert restored.story_id == sample_article_digest.story_id


class TestArticleDigestAgentResult:
    def test_forbids_extra(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            ArticleDigestAgentResult(summary="s", confidence=0.5, extra="bad")


class TestStoryClusterResult:
    def test_forbids_extra(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            StoryClusterResult(
                cluster_headline="h", synthesis="s", news_value_score=0.8,
                is_new=True, story_fingerprint="fp", extra="bad",
            )

    def test_score_bounds(self) -> None:
        with pytest.raises(ValidationError):
            StoryClusterResult(
                cluster_headline="h", synthesis="s", news_value_score=-0.1,
                is_new=True, story_fingerprint="fp",
            )


class TestCyclePublishPlan:
    def test_minimal(self) -> None:
        plan = CyclePublishPlan(reasoning="No news today")
        assert plan.stories == []
        assert plan.prevented_duplicates == 0

    def test_forbids_extra(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            CyclePublishPlan(reasoning="r", extra="bad")


class TestPublishableArticle:
    def test_maps_to_team_article(self) -> None:
        article = PublishableArticle(
            team="BUF", headline="Bills Win", sub_headline="Big day",
            introduction="Intro", content="Body", x_post="Tweet",
            bullet_points="- Point 1\n- Point 2", story_fingerprint="fp",
        )
        data = article.model_dump(mode="json")
        assert data["team"] == "BUF"
        assert data["headline"] == "Bills Win"
        assert data["image"] is None
        assert data["tts_file"] is None

    def test_forbids_extra(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            PublishableArticle(
                team="BUF", headline="h", sub_headline="s", introduction="i",
                content="c", x_post="x", bullet_points="b", story_fingerprint="fp",
                bogus="bad",
            )


class TestArticleQualityDecision:
    def test_forbids_extra(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            ArticleQualityDecision(
                decision="approve",
                impact_score=0.8,
                specificity_score=0.8,
                readworthiness_score=0.8,
                grounding_score=0.9,
                execution_score=0.8,
                reasoning="Strong enough",
                extra="bad",
            )

    def test_score_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ArticleQualityDecision(
                decision="approve",
                impact_score=1.1,
                specificity_score=0.8,
                readworthiness_score=0.8,
                grounding_score=0.9,
                execution_score=0.8,
                reasoning="Strong enough",
            )


class TestEditorialMemoryRevision:
    def test_forbids_extra(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            EditorialMemoryRevision(
                updated_markdown="# Lessons",
                change_summary="Updated lessons",
                extra="bad",
            )


class TestPublishedStoryRecord:
    def test_round_trip(self) -> None:
        record = PublishedStoryRecord(
            story_fingerprint="fp123",
            published_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            supabase_article_id=42,
            cycle_id="cycle-1",
        )
        data = record.model_dump(mode="json")
        restored = PublishedStoryRecord.model_validate(data)
        assert restored.story_fingerprint == "fp123"
        assert restored.supabase_article_id == 42


class TestCycleResult:
    def test_round_trip(self) -> None:
        result = CycleResult(
            cycle_id="c1",
            generated_at=datetime.now(UTC),
            plan=CyclePublishPlan(reasoning="test"),
        )
        data = result.model_dump(mode="json")
        restored = CycleResult.model_validate(data)
        assert restored.cycle_id == "c1"
        assert restored.articles_written == 0
