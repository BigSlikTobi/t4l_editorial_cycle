from __future__ import annotations

from datetime import UTC, datetime

from app.editorial.helpers import (
    _fingerprint_from_key_facts,
    _normalize_url,
    coerce_output,
    compute_story_fingerprint,
    count_prevented_duplicates,
    deduplicate,
    deduplicate_plan,
    group_by_entity,
    recompute_cluster_fingerprint,
    recompute_plan_fingerprints,
    resolve_existing_article_ids,
    synthesize_missing_digests,
    url_overlap_ratio,
    truncate_article_content,
)
from app.schemas import (
    ArticleDigest,
    CyclePublishPlan,
    EntityMatch,
    StoryClusterResult,
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


class TestKeyFactsFallback:
    def test_deterministic(self) -> None:
        facts = ["Bills traded pick", "WR signed extension"]
        assert _fingerprint_from_key_facts(facts) == _fingerprint_from_key_facts(facts)

    def test_order_independent(self) -> None:
        assert _fingerprint_from_key_facts(["A", "B"]) == _fingerprint_from_key_facts(["B", "A"])

    def test_different_facts_differ(self) -> None:
        assert _fingerprint_from_key_facts(["A"]) != _fingerprint_from_key_facts(["B"])

    def test_ignores_empty_facts(self) -> None:
        assert _fingerprint_from_key_facts(["A", "", "  "]) == _fingerprint_from_key_facts(["A"])


class TestComputeStoryFingerprint:
    def test_stable_across_url_order(self) -> None:
        a = compute_story_fingerprint(["http://a.com/x", "http://b.com/y"])
        b = compute_story_fingerprint(["http://b.com/y", "http://a.com/x"])
        assert a == b

    def test_normalization_strips_query_fragment_and_trailing_slash(self) -> None:
        canonical = compute_story_fingerprint(["https://espn.com/nfl/story"])
        variants = [
            "https://ESPN.com/nfl/story/",
            "HTTPS://espn.com/nfl/story?utm_source=twitter",
            "https://espn.com/nfl/story#section",
            "  https://espn.com/nfl/story  ",
        ]
        for v in variants:
            assert compute_story_fingerprint([v]) == canonical, v

    def test_distinct_urls_differ(self) -> None:
        a = compute_story_fingerprint(["https://espn.com/nfl/trade"])
        b = compute_story_fingerprint(["https://espn.com/nfl/draft"])
        assert a != b

    def test_partial_url_overlap_yields_different_hash(self) -> None:
        a = compute_story_fingerprint(["https://a.com/x", "https://b.com/y"])
        b = compute_story_fingerprint(["https://a.com/x", "https://c.com/z"])
        assert a != b

    def test_entity_ids_disambiguate(self) -> None:
        urls = ["https://nfl.com/news"]
        a = compute_story_fingerprint(urls, ["player:00-0011111"])
        b = compute_story_fingerprint(urls, ["player:00-0022222"])
        assert a != b

    def test_entity_ids_order_independent(self) -> None:
        urls = ["https://nfl.com/news"]
        a = compute_story_fingerprint(urls, ["e1", "e2"])
        b = compute_story_fingerprint(urls, ["e2", "e1", "e1"])
        assert a == b

    def test_empty_urls_and_entities(self) -> None:
        # Empty inputs still produce a stable hash (callers should fall back beforehand)
        assert compute_story_fingerprint([], []) == compute_story_fingerprint([], [])

    def test_normalize_url_helper(self) -> None:
        assert _normalize_url("https://ESPN.com/nfl/x/?utm=1") == "https://espn.com/nfl/x"


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


def _make_digest(url: str, key_facts: list[str] | None = None) -> ArticleDigest:
    return ArticleDigest(
        story_id="s1", url=url, title="t", source_name="s",
        summary="sum", confidence=0.9,
        key_facts=key_facts or ["fact A"],
    )


class TestRecomputeClusterFingerprint:
    def test_replaces_llm_slug_with_url_hash(self) -> None:
        cluster = StoryClusterResult(
            cluster_headline="h", synthesis="s", news_value_score=0.8,
            is_new=True, story_fingerprint="llm-garbage-slug",
            source_digests=[_make_digest("http://a.com/x", ["Bills traded pick"])],
        )
        result = recompute_cluster_fingerprint(cluster)
        assert result != "llm-garbage-slug"
        assert result == compute_story_fingerprint(["http://a.com/x"])

    def test_stable_when_key_facts_change_but_urls_match(self) -> None:
        # The original failure mode issue #15 calls out: identical sources,
        # different LLM phrasing must produce the same fingerprint.
        d1 = _make_digest("http://a.com/story", ["Bills traded a 4th-round pick"])
        d2 = _make_digest("http://a.com/story", ["Buffalo dealt a fourth-rounder"])
        c1 = StoryClusterResult(
            cluster_headline="h1", synthesis="s", news_value_score=0.8,
            is_new=True, story_fingerprint="x", source_digests=[d1],
        )
        c2 = StoryClusterResult(
            cluster_headline="h2", synthesis="s", news_value_score=0.8,
            is_new=True, story_fingerprint="y", source_digests=[d2],
        )
        assert recompute_cluster_fingerprint(c1) == recompute_cluster_fingerprint(c2)

    def test_combines_urls_across_digests(self) -> None:
        d1 = _make_digest("http://a.com/x", ["Fact A"])
        d2 = _make_digest("http://b.com/y", ["Fact B"])
        cluster = StoryClusterResult(
            cluster_headline="h", synthesis="s", news_value_score=0.8,
            is_new=True, story_fingerprint="x", source_digests=[d1, d2],
        )
        result = recompute_cluster_fingerprint(cluster)
        assert result == compute_story_fingerprint(["http://a.com/x", "http://b.com/y"])

    def test_falls_back_to_headline_when_no_digests(self) -> None:
        cluster = StoryClusterResult(
            cluster_headline="Some headline", synthesis="s", news_value_score=0.5,
            is_new=True, story_fingerprint="x", source_digests=[],
        )
        result = recompute_cluster_fingerprint(cluster)
        assert result == _fingerprint_from_key_facts(["Some headline"])

    def test_deterministic_across_unrelated_metadata(self) -> None:
        d = _make_digest("http://a.com/x", ["X", "Y"])
        c1 = StoryClusterResult(
            cluster_headline="h", synthesis="s", news_value_score=0.8,
            is_new=True, story_fingerprint="a", source_digests=[d],
        )
        c2 = StoryClusterResult(
            cluster_headline="different", synthesis="different", news_value_score=0.5,
            is_new=False, story_fingerprint="b", source_digests=[d],
        )
        assert recompute_cluster_fingerprint(c1) == recompute_cluster_fingerprint(c2)

    def test_entity_ids_match_plan_recompute(self) -> None:
        # Critical invariant: tool-side fp must equal plan-side fp for the
        # same story so the orchestrator's `is_new` decision is made against
        # the same identity that's stored in editorial_state.
        digest = ArticleDigest(
            story_id="raw-1", url="http://a.com/x", title="t", source_name="s",
            summary="sum", confidence=0.9, key_facts=["fact"],
        )
        cluster = StoryClusterResult(
            cluster_headline="h", synthesis="s", news_value_score=0.8,
            is_new=True, story_fingerprint="x", source_digests=[digest],
        )
        raw = _make_raw_article("raw-1", "http://a.com/x", ["player:001", "team:KC"])

        tool_fp = recompute_cluster_fingerprint(
            cluster, [e.entity_id for e in raw.entities]
        )

        story = StoryEntry(
            rank=1, cluster_headline="h", story_fingerprint="x",
            action="publish", news_value_score=0.8, reasoning="r",
            source_digests=[digest],
        )
        plan = CyclePublishPlan(stories=[story], reasoning="t")
        plan_fp = recompute_plan_fingerprints(plan, [raw]).stories[0].story_fingerprint

        assert tool_fp == plan_fp


def _make_raw_article(article_id: str, url: str, entity_ids: list[str]) -> RawArticle:
    return RawArticle(
        id=article_id,
        url=url,
        title="t",
        source_name="s",
        entities=[
            EntityMatch(entity_type="player", entity_id=eid, matched_name=eid)
            for eid in entity_ids
        ],
    )


class TestRecomputePlanFingerprints:
    def test_url_set_drives_identity_across_key_fact_drift(self) -> None:
        d_a = ArticleDigest(
            story_id="raw-1", url="http://a.com/x", title="t", source_name="s",
            summary="sum", confidence=0.9, key_facts=["wording one"],
        )
        d_b = ArticleDigest(
            story_id="raw-1", url="http://a.com/x", title="t", source_name="s",
            summary="sum", confidence=0.9, key_facts=["completely different wording"],
        )
        s_a = StoryEntry(
            rank=1, cluster_headline="H1", story_fingerprint="old",
            action="publish", news_value_score=0.9, reasoning="r",
            source_digests=[d_a],
        )
        s_b = StoryEntry(
            rank=1, cluster_headline="H2", story_fingerprint="old",
            action="publish", news_value_score=0.9, reasoning="r",
            source_digests=[d_b],
        )
        plan_a = CyclePublishPlan(stories=[s_a], reasoning="t")
        plan_b = CyclePublishPlan(stories=[s_b], reasoning="t")
        out_a = recompute_plan_fingerprints(plan_a)
        out_b = recompute_plan_fingerprints(plan_b)
        assert out_a.stories[0].story_fingerprint == out_b.stories[0].story_fingerprint

    def test_distinct_url_sets_differ_for_same_team(self) -> None:
        d1 = ArticleDigest(
            story_id="raw-1", url="http://espn.com/trade", title="t", source_name="s",
            summary="sum", confidence=0.9, key_facts=["fact"],
        )
        d2 = ArticleDigest(
            story_id="raw-2", url="http://espn.com/injury", title="t", source_name="s",
            summary="sum", confidence=0.9, key_facts=["fact"],
        )
        s1 = StoryEntry(
            rank=1, cluster_headline="Trade", story_fingerprint="x",
            action="publish", news_value_score=0.9, reasoning="r",
            source_digests=[d1],
        )
        s2 = StoryEntry(
            rank=2, cluster_headline="Injury", story_fingerprint="x",
            action="publish", news_value_score=0.9, reasoning="r",
            source_digests=[d2],
        )
        plan = CyclePublishPlan(stories=[s1, s2], reasoning="t")
        out = recompute_plan_fingerprints(plan)
        assert out.stories[0].story_fingerprint != out.stories[1].story_fingerprint

    def test_entity_ids_layered_in_when_raw_articles_provided(self) -> None:
        digest = ArticleDigest(
            story_id="raw-1", url="http://a.com/x", title="t", source_name="s",
            summary="sum", confidence=0.9, key_facts=["fact"],
        )
        story = StoryEntry(
            rank=1, cluster_headline="H", story_fingerprint="old",
            action="publish", news_value_score=0.9, reasoning="r",
            source_digests=[digest],
        )
        plan = CyclePublishPlan(stories=[story], reasoning="t")

        without_raw = recompute_plan_fingerprints(plan).stories[0].story_fingerprint
        with_raw = recompute_plan_fingerprints(
            plan,
            [_make_raw_article("raw-1", "http://a.com/x", ["player:001"])],
        ).stories[0].story_fingerprint
        assert without_raw != with_raw
        assert with_raw == compute_story_fingerprint(["http://a.com/x"], ["player:001"])

    def test_falls_back_to_headline_when_no_digests(self) -> None:
        story = StoryEntry(
            rank=1, cluster_headline="Some headline", story_fingerprint="old",
            action="publish", news_value_score=0.9, reasoning="r",
            source_digests=[],
        )
        plan = CyclePublishPlan(stories=[story], reasoning="t")
        out = recompute_plan_fingerprints(plan)
        assert out.stories[0].story_fingerprint == _fingerprint_from_key_facts(["Some headline"])


class TestSynthesizeMissingDigests:
    def test_fills_digest_from_unique_title_match(self) -> None:
        story = StoryEntry(
            rank=1, cluster_headline="Breaking trade news",
            story_fingerprint="x", action="publish",
            news_value_score=0.9, reasoning="r", source_digests=[],
        )
        raw = _make_raw_article("raw-7", "https://espn.com/trade", [])
        raw = raw.model_copy(update={"title": "Breaking trade news"})
        plan = CyclePublishPlan(stories=[story], reasoning="t")

        out = synthesize_missing_digests(plan, [raw])
        assert len(out.stories[0].source_digests) == 1
        d = out.stories[0].source_digests[0]
        assert d.story_id == "raw-7"
        assert d.url == "https://espn.com/trade"

    def test_match_is_case_insensitive_and_trim(self) -> None:
        story = StoryEntry(
            rank=1, cluster_headline="  TRADE NEWS  ",
            story_fingerprint="x", action="publish",
            news_value_score=0.9, reasoning="r", source_digests=[],
        )
        raw = _make_raw_article("raw-1", "https://espn.com/x", [])
        raw = raw.model_copy(update={"title": "Trade News"})
        out = synthesize_missing_digests(
            CyclePublishPlan(stories=[story], reasoning="t"), [raw]
        )
        assert out.stories[0].source_digests[0].story_id == "raw-1"

    def test_ambiguous_titles_are_skipped(self) -> None:
        story = StoryEntry(
            rank=1, cluster_headline="Same title",
            story_fingerprint="x", action="publish",
            news_value_score=0.9, reasoning="r", source_digests=[],
        )
        a = _make_raw_article("raw-1", "https://a.com/x", [])
        a = a.model_copy(update={"title": "Same title"})
        b = _make_raw_article("raw-2", "https://b.com/y", [])
        b = b.model_copy(update={"title": "Same title"})
        out = synthesize_missing_digests(
            CyclePublishPlan(stories=[story], reasoning="t"), [a, b]
        )
        assert out.stories[0].source_digests == []

    def test_does_not_overwrite_existing_digests(self) -> None:
        existing = ArticleDigest(
            story_id="orig", url="http://orig.com/x", title="t",
            source_name="s", summary="sum", confidence=0.9, key_facts=["f"],
        )
        story = StoryEntry(
            rank=1, cluster_headline="match",
            story_fingerprint="x", action="publish",
            news_value_score=0.9, reasoning="r", source_digests=[existing],
        )
        raw = _make_raw_article("raw-1", "https://other.com/x", [])
        raw = raw.model_copy(update={"title": "match"})
        out = synthesize_missing_digests(
            CyclePublishPlan(stories=[story], reasoning="t"), [raw]
        )
        assert out.stories[0].source_digests == [existing]

    def test_synthesis_unlocks_url_based_fingerprint(self) -> None:
        # End-to-end: empty digests + raw article match → URL-based fp.
        story = StoryEntry(
            rank=1, cluster_headline="LT in hospital",
            story_fingerprint="old", action="publish",
            news_value_score=0.5, reasoning="r", source_digests=[],
        )
        raw = _make_raw_article(
            "raw-1", "https://espn.com/lt-hospital", ["player:001"]
        )
        raw = raw.model_copy(update={"title": "LT in hospital"})
        plan = CyclePublishPlan(stories=[story], reasoning="t")

        synthesized = synthesize_missing_digests(plan, [raw])
        out = recompute_plan_fingerprints(synthesized, [raw])
        assert (
            out.stories[0].story_fingerprint
            == compute_story_fingerprint(["https://espn.com/lt-hospital"], ["player:001"])
        )


class TestUrlOverlapRatio:
    def test_full_overlap(self, sample_published_state) -> None:
        sample_published_state[0].source_urls = ["http://a.com", "http://b.com"]
        ratio, record = url_overlap_ratio(["http://a.com", "http://b.com"], sample_published_state)
        assert ratio == 1.0
        assert record is not None

    def test_no_overlap(self, sample_published_state) -> None:
        sample_published_state[0].source_urls = ["http://x.com"]
        ratio, record = url_overlap_ratio(["http://a.com"], sample_published_state)
        assert ratio == 0.0

    def test_partial_overlap(self, sample_published_state) -> None:
        sample_published_state[0].source_urls = ["http://a.com", "http://b.com"]
        ratio, record = url_overlap_ratio(
            ["http://a.com", "http://b.com", "http://c.com"],
            sample_published_state,
        )
        assert abs(ratio - 2 / 3) < 0.01

    def test_empty_candidate_urls(self) -> None:
        ratio, record = url_overlap_ratio([], [])
        assert ratio == 0.0
        assert record is None


class TestResolveExistingArticleIds:
    def test_matches_by_fingerprint(self, sample_published_state) -> None:
        story = _make_story(1, sample_published_state[0].story_fingerprint, action="update")
        plan = CyclePublishPlan(stories=[story], reasoning="test")
        result = resolve_existing_article_ids(plan, sample_published_state)
        assert result.stories[0].existing_article_id == 100

    def test_matches_by_url_overlap(self, sample_published_state) -> None:
        sample_published_state[0].source_urls = ["http://a.com", "http://b.com"]
        story = _make_story(1, "unknown-fp", action="update")
        story = story.model_copy(update={
            "source_digests": [_make_digest("http://a.com"), _make_digest("http://b.com")],
        })
        plan = CyclePublishPlan(stories=[story], reasoning="test")
        result = resolve_existing_article_ids(plan, sample_published_state)
        assert result.stories[0].existing_article_id == 100

    def test_no_match_downgrades_to_publish(self) -> None:
        story = _make_story(1, "nonexistent-fp", action="update")
        plan = CyclePublishPlan(stories=[story], reasoning="test")
        result = resolve_existing_article_ids(plan, [])
        assert result.stories[0].action == "publish"
        assert result.stories[0].existing_article_id is None

    def test_publish_stories_pass_through(self, sample_published_state) -> None:
        story = _make_story(1, "whatever", action="publish")
        plan = CyclePublishPlan(stories=[story], reasoning="test")
        result = resolve_existing_article_ids(plan, sample_published_state)
        assert result.stories[0].action == "publish"
        assert result.stories[0].existing_article_id is None
