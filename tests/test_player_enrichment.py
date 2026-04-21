from __future__ import annotations

from app.editorial.helpers import enrich_plan_with_players
from app.schemas import (
    ArticleDigest,
    CyclePublishPlan,
    EntityMatch,
    RawArticle,
    StoryEntry,
)


def _raw(id_: str, url: str, entities: list[EntityMatch]) -> RawArticle:
    return RawArticle(id=id_, url=url, title=f"T {id_}", source_name="X", entities=entities)


def _digest(story_id: str, url: str = "http://x") -> ArticleDigest:
    return ArticleDigest(
        story_id=story_id,
        url=url,
        title=f"T {story_id}",
        source_name="X",
        summary="s",
        key_facts=["f"],
        confidence=0.5,
        content_status="full",
    )


def _story(digests: list[ArticleDigest]) -> StoryEntry:
    return StoryEntry(
        rank=1,
        cluster_headline="hl",
        story_fingerprint="fp1",
        action="publish",
        news_value_score=0.8,
        reasoning="r",
        source_digests=digests,
    )


def test_enriches_from_single_source():
    raw = _raw(
        "s1", "http://a",
        [
            EntityMatch(entity_type="player", entity_id="00-001", matched_name="A"),
            EntityMatch(entity_type="team", entity_id="BUF", matched_name="Bills"),
        ],
    )
    plan = CyclePublishPlan(stories=[_story([_digest("s1")])], reasoning="r")
    result = enrich_plan_with_players(plan, [raw])
    mentions = result.stories[0].player_mentions
    assert len(mentions) == 1
    assert mentions[0].id == "00-001"
    assert mentions[0].name == "A"


def test_dedupes_across_digests():
    r1 = _raw("s1", "http://a", [
        EntityMatch(entity_type="player", entity_id="00-001", matched_name="A"),
        EntityMatch(entity_type="player", entity_id="00-002", matched_name="B"),
    ])
    r2 = _raw("s2", "http://b", [
        EntityMatch(entity_type="player", entity_id="00-002", matched_name="B"),
        EntityMatch(entity_type="player", entity_id="00-003", matched_name="C"),
    ])
    plan = CyclePublishPlan(
        stories=[_story([_digest("s1"), _digest("s2")])],
        reasoning="r",
    )
    result = enrich_plan_with_players(plan, [r1, r2])
    ids = [m.id for m in result.stories[0].player_mentions]
    assert ids == ["00-001", "00-002", "00-003"]  # first-seen order, deduped


def test_skips_non_player_entities():
    raw = _raw("s1", "http://a", [
        EntityMatch(entity_type="team", entity_id="BUF", matched_name="Bills"),
        EntityMatch(entity_type="game", entity_id="G123", matched_name="Game"),
    ])
    plan = CyclePublishPlan(stories=[_story([_digest("s1")])], reasoning="r")
    result = enrich_plan_with_players(plan, [raw])
    assert result.stories[0].player_mentions == []


def test_missing_raw_article_does_not_error():
    # digest references a story_id that's not in raw_articles
    plan = CyclePublishPlan(stories=[_story([_digest("ghost")])], reasoning="r")
    result = enrich_plan_with_players(plan, [])
    assert result.stories[0].player_mentions == []


def test_enriches_skipped_stories_too():
    raw = _raw("s1", "http://a", [
        EntityMatch(entity_type="player", entity_id="00-009", matched_name="Z"),
    ])
    story = _story([_digest("s1")])
    plan = CyclePublishPlan(
        stories=[], skipped_stories=[story.model_copy(update={"action": "skip"})],
        reasoning="r",
    )
    result = enrich_plan_with_players(plan, [raw])
    assert result.skipped_stories[0].player_mentions[0].id == "00-009"


def test_empty_entity_id_is_ignored():
    raw = _raw("s1", "http://a", [
        EntityMatch(entity_type="player", entity_id="", matched_name="Nobody"),
        EntityMatch(entity_type="player", entity_id="00-010", matched_name="Real"),
    ])
    plan = CyclePublishPlan(stories=[_story([_digest("s1")])], reasoning="r")
    result = enrich_plan_with_players(plan, [raw])
    ids = [m.id for m in result.stories[0].player_mentions]
    assert ids == ["00-010"]
