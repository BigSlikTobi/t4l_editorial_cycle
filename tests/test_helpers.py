from __future__ import annotations

from datetime import UTC, datetime

from app.editorial.helpers import (
    coerce_output,
    count_prevented_duplicates,
    deduplicate,
    deduplicate_plan,
    fingerprint,
    group_by_entity,
    truncate_article_content,
)
from app.schemas import (
    ArticleDigest,
    CyclePublishPlan,
    EntityMatch,
    StoryEntry,
    PublishedStoryRecord,
    RawArticle,
)


class TestDeduplicate:
    def test_removes_exact_url_dupes(
        self, sample_raw_article: RawArticle, sample_raw_article_dupe: RawArticle
    ) -> None:
        unique, removed = deduplicate([sample_raw_article, sample_raw_article_dupe])
        assert len(unique) == 1
        assert removed == 1
        assert unique[0].id == sample_raw_article.id

    def test_keeps_different_urls(
        self, sample_raw_article: RawArticle, sample_raw_article_different: RawArticle
    ) -> None:
        unique, removed = deduplicate([sample_raw_article, sample_raw_article_different])
        assert len(unique) == 2
        assert removed == 0

    def test_empty_list(self) -> None:
        unique, removed = deduplicate([])
        assert unique == []
        assert removed == 0


class TestGroupByEntity:
    def test_multi_source_cluster_by_team(
        self, sample_raw_article: RawArticle, sample_raw_article_different: RawArticle
    ) -> None:
        grouped = group_by_entity([sample_raw_article, sample_raw_article_different])
        assert "BUF" in grouped.multi_source
        assert len(grouped.multi_source["BUF"]) == 2
        assert grouped.single_source == []

    def test_truly_standalone_goes_to_singles(self) -> None:
        article = RawArticle(
            id="1", url="http://x.com", title="t", source_name="s",
            entities=[EntityMatch(entity_type="team", entity_id="KC", matched_name="Chiefs")],
        )
        grouped = group_by_entity([article])
        assert grouped.multi_source == {}
        assert len(grouped.single_source) == 1

    def test_player_entity_clusters_across_teams(self) -> None:
        # Two articles about the same player on different teams should cluster by player
        a1 = RawArticle(
            id="1", url="http://a.com", title="Lawrence trade NYG", source_name="ESPN",
            entities=[
                EntityMatch(entity_type="player", entity_id="P123", matched_name="Lawrence"),
                EntityMatch(entity_type="team", entity_id="NYG", matched_name="Giants"),
            ],
        )
        a2 = RawArticle(
            id="2", url="http://b.com", title="Lawrence trade DAL", source_name="CBS",
            entities=[
                EntityMatch(entity_type="player", entity_id="P123", matched_name="Lawrence"),
                EntityMatch(entity_type="team", entity_id="DAL", matched_name="Cowboys"),
            ],
        )
        grouped = group_by_entity([a1, a2])
        assert "P123" in grouped.multi_source
        assert len(grouped.multi_source["P123"]) == 2

    def test_single_merges_via_shared_player(self) -> None:
        # Two articles cluster by player, a third standalone shares that player
        a1 = RawArticle(
            id="1", url="http://a.com", title="t1", source_name="ESPN",
            entities=[EntityMatch(entity_type="player", entity_id="P123", matched_name="X")],
        )
        a2 = RawArticle(
            id="2", url="http://b.com", title="t2", source_name="CBS",
            entities=[EntityMatch(entity_type="player", entity_id="P123", matched_name="X")],
        )
        solo = RawArticle(
            id="3", url="http://c.com", title="solo", source_name="Fox",
            entities=[
                EntityMatch(entity_type="team", entity_id="NYG", matched_name="Giants"),
                EntityMatch(entity_type="player", entity_id="P123", matched_name="X"),
            ],
        )
        grouped = group_by_entity([a1, a2, solo])
        assert len(grouped.multi_source["P123"]) == 3  # solo merged in
        assert grouped.single_source == []

    def test_total_clusters(self, sample_raw_article: RawArticle, sample_raw_article_different: RawArticle) -> None:
        single = RawArticle(
            id="solo", url="http://solo.com", title="solo", source_name="s",
            entities=[EntityMatch(entity_type="team", entity_id="KC", matched_name="Chiefs")],
        )
        grouped = group_by_entity([sample_raw_article, sample_raw_article_different, single])
        assert grouped.total_clusters == 2  # 1 multi-source cluster + 1 single


class TestFingerprint:
    def test_deterministic(self) -> None:
        facts = ["Bills traded pick", "WR signed extension"]
        assert fingerprint(facts) == fingerprint(facts)

    def test_order_independent(self) -> None:
        assert fingerprint(["A", "B"]) == fingerprint(["B", "A"])

    def test_different_facts_differ(self) -> None:
        assert fingerprint(["A"]) != fingerprint(["B"])

    def test_ignores_empty_facts(self) -> None:
        assert fingerprint(["A", "", "  "]) == fingerprint(["A"])


class TestCountPreventedDuplicates:
    def test_counts_matches(self) -> None:
        state = [
            PublishedStoryRecord(
                story_fingerprint="abc",
                published_at=datetime.now(UTC),
                last_updated_at=datetime.now(UTC),
                supabase_article_id=1,
                cycle_id="c1",
            ),
        ]
        assert count_prevented_duplicates(["abc", "def"], state) == 1

    def test_no_matches(self) -> None:
        assert count_prevented_duplicates(["xyz"], []) == 0


class TestTruncateArticleContent:
    def test_short_content_unchanged(self) -> None:
        assert truncate_article_content("hello world") == "hello world"

    def test_truncates_long_content(self) -> None:
        content = " ".join(f"word{i}" for i in range(700))
        result = truncate_article_content(content, word_limit=10)
        assert result.endswith("[truncated]")
        assert len(result.split()) <= 12


class TestCoerceOutput:
    def test_dict_input(self) -> None:
        data = {
            "story_id": "1", "url": "http://x.com", "title": "t", "source_name": "s",
            "summary": "sum", "confidence": 0.9,
        }
        result = coerce_output(data, ArticleDigest)
        assert result.story_id == "1"

    def test_model_instance(self, sample_article_digest: ArticleDigest) -> None:
        result = coerce_output(sample_article_digest, ArticleDigest)
        assert result is sample_article_digest

    def test_json_string(self) -> None:
        import json
        data = json.dumps({
            "story_id": "1", "url": "http://x.com", "title": "t", "source_name": "s",
            "summary": "sum", "confidence": 0.9,
        })
        result = coerce_output(data, ArticleDigest)
        assert result.story_id == "1"


def _make_story(rank: int, fingerprint: str, action: str = "publish") -> StoryEntry:
    return StoryEntry(
        rank=rank,
        cluster_headline=f"Story {rank}",
        story_fingerprint=fingerprint,
        action=action,
        news_value_score=1.0 - rank * 0.1,
        reasoning="test",
    )


class TestDeduplicatePlan:
    def test_removes_duplicate_fingerprints(self) -> None:
        plan = CyclePublishPlan(
            stories=[
                _make_story(1, "fp_trade"),
                _make_story(2, "fp_draft"),
                _make_story(3, "fp_trade"),  # duplicate of #1
            ],
            reasoning="test",
        )
        result = deduplicate_plan(plan)
        assert len(result.stories) == 2
        assert result.stories[0].story_fingerprint == "fp_trade"
        assert result.stories[1].story_fingerprint == "fp_draft"
        assert len(result.skipped_stories) == 1
        assert result.skipped_stories[0].action == "skip"

    def test_increments_prevented_duplicates(self) -> None:
        plan = CyclePublishPlan(
            stories=[_make_story(1, "fp_a"), _make_story(2, "fp_a")],
            reasoning="test",
            prevented_duplicates=3,
        )
        result = deduplicate_plan(plan)
        assert result.prevented_duplicates == 4  # 3 original + 1 new

    def test_no_duplicates_unchanged(self) -> None:
        plan = CyclePublishPlan(
            stories=[_make_story(1, "fp_a"), _make_story(2, "fp_b")],
            reasoning="test",
        )
        result = deduplicate_plan(plan)
        assert len(result.stories) == 2
        assert result.prevented_duplicates == 0
