from __future__ import annotations

import pytest

from app.schemas import (
    ArticleDigest,
    EntityMatch,
    PublishedStoryRecord,
    RawArticle,
)
from datetime import UTC, datetime


@pytest.fixture
def sample_entity() -> EntityMatch:
    return EntityMatch(entity_type="team", entity_id="BUF", matched_name="Buffalo Bills")


@pytest.fixture
def sample_raw_article(sample_entity: EntityMatch) -> RawArticle:
    return RawArticle(
        id="story-1",
        url="https://espn.com/nfl/story-1",
        title="Bills trade for star WR",
        source_name="ESPN",
        category="trade",
        facts_count=5,
        entities=[sample_entity],
    )


@pytest.fixture
def sample_raw_article_dupe(sample_entity: EntityMatch) -> RawArticle:
    return RawArticle(
        id="story-2",
        url="https://espn.com/nfl/story-1",
        title="Bills trade for star WR (duplicate URL)",
        source_name="CBS",
        entities=[sample_entity],
    )


@pytest.fixture
def sample_raw_article_different(sample_entity: EntityMatch) -> RawArticle:
    return RawArticle(
        id="story-3",
        url="https://cbs.com/nfl/bills-trade",
        title="CBS: Bills acquire top receiver",
        source_name="CBS",
        entities=[sample_entity],
    )


@pytest.fixture
def sample_article_digest() -> ArticleDigest:
    return ArticleDigest(
        story_id="story-1",
        url="https://espn.com/nfl/story-1",
        title="Bills trade for star WR",
        source_name="ESPN",
        summary="The Buffalo Bills traded for a star wide receiver.",
        key_facts=["Bills traded 2025 first-round pick", "WR signed 3-year extension"],
        confidence=0.9,
        content_status="full",
        team_mentions=["BUF"],
    )


@pytest.fixture
def sample_published_state() -> list[PublishedStoryRecord]:
    return [
        PublishedStoryRecord(
            id=1,
            story_fingerprint="abc123",
            published_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            supabase_article_id=100,
            cycle_id="cycle-old",
            cluster_headline="Old story",
            source_urls=["https://espn.com/nfl/old-story"],
        ),
    ]
