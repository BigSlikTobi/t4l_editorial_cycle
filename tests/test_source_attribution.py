"""Sources attribution is populated deterministically from story.source_digests
and round-trips through the ArticleWriter payload."""

from __future__ import annotations

from app.schemas import ArticleDigest, ArticleSource, PublishableArticle, StoryEntry
from app.writer.workflow import _dedupe_sources


def _digest(url: str, name: str, story_id: str = "s1") -> ArticleDigest:
    return ArticleDigest(
        story_id=story_id,
        url=url,
        title="t",
        source_name=name,
        summary="s",
        key_facts=[],
        confidence=0.9,
        content_status="full",
    )


def _story(digests: list[ArticleDigest]) -> StoryEntry:
    return StoryEntry(
        rank=1,
        cluster_headline="h",
        story_fingerprint="fp",
        action="publish",
        news_value_score=0.9,
        reasoning="r",
        source_digests=digests,
        team_codes=[],
        player_mentions=[],
    )


def test_dedupe_sources_preserves_first_seen_order():
    story = _story([
        _digest("https://a.com/1", "Alpha"),
        _digest("https://b.com/2", "Beta"),
        _digest("https://a.com/1", "Alpha"),  # dup
        _digest("https://c.com/3", "Gamma"),
    ])
    result = _dedupe_sources(story)
    assert [s.url for s in result] == [
        "https://a.com/1", "https://b.com/2", "https://c.com/3",
    ]
    assert [s.name for s in result] == ["Alpha", "Beta", "Gamma"]


def test_dedupe_sources_skips_blank_urls():
    story = _story([
        _digest("https://a.com/1", "Alpha"),
        _digest("", "Empty"),
    ])
    result = _dedupe_sources(story)
    assert len(result) == 1
    assert result[0].url == "https://a.com/1"


def test_dedupe_sources_falls_back_to_url_when_name_missing():
    story = _story([_digest("https://a.com/1", "")])
    result = _dedupe_sources(story)
    assert result[0].name == "https://a.com/1"


def test_publishable_article_sources_default_empty():
    art = PublishableArticle(
        team="KC", headline="h", sub_headline="s", introduction="i",
        content="c", x_post="x", bullet_points="b", story_fingerprint="fp",
    )
    assert art.sources == []


def test_article_writer_payload_serializes_sources():
    """The adapter payload must contain the jsonb-ready list of dicts."""
    from app.adapters import ArticleWriter

    writer = ArticleWriter.__new__(ArticleWriter)
    art = PublishableArticle(
        team="KC", headline="h", sub_headline="s", introduction="i",
        content="c", x_post="x", bullet_points="b", story_fingerprint="fp",
        sources=[
            ArticleSource(name="CBS Sports", url="https://cbs.com/x"),
            ArticleSource(name="ESPN", url="https://espn.com/y"),
        ],
    )
    payload = writer._article_payload(art)
    assert payload["sources"] == [
        {"name": "CBS Sports", "url": "https://cbs.com/x"},
        {"name": "ESPN", "url": "https://espn.com/y"},
    ]
