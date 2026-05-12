from __future__ import annotations

from app.podcast.clustering import (
    WORDS_PER_WEIGHT_UNIT,
    group_for_podcast,
    select_clusters_for_budget,
)
from app.podcast.schemas import PodcastCluster
from app.schemas import EntityMatch, RawArticle


def _article(
    id_: str,
    title: str,
    *entities: tuple[str, str, str],
) -> RawArticle:
    return RawArticle(
        id=id_,
        url=f"https://example.com/{id_}",
        title=title,
        source_name="test",
        entities=[
            EntityMatch(entity_type=t, entity_id=eid, matched_name=name)
            for (t, eid, name) in entities
        ],
    )


class TestGroupForPodcast:
    def test_empty_input(self) -> None:
        assert group_for_podcast([]) == []

    def test_two_articles_share_player(self) -> None:
        articles = [
            _article("a", "Mahomes signs ext", ("player", "p1", "Mahomes"), ("team", "KC", "Chiefs")),
            _article("b", "Mahomes camp note", ("player", "p1", "Mahomes")),
        ]
        clusters = group_for_podcast(articles)
        assert len(clusters) == 1
        assert len(clusters[0].source_articles) == 2

    def test_team_only_articles_stay_isolated(self) -> None:
        # Two articles, both team-only on the SAME team.
        # Should NOT collapse into a single cluster (that's the editorial
        # cycle's behavior; podcast clustering is league-wide).
        articles = [
            _article("a", "Jets practice notes", ("team", "NYJ", "Jets")),
            _article("b", "Jets locker room", ("team", "NYJ", "Jets")),
        ]
        clusters = group_for_podcast(articles)
        assert len(clusters) == 2

    def test_singletons_merge_via_shared_player(self) -> None:
        # First two articles share a player → multi-source cluster.
        # Third article is a player-only singleton sharing the same player.
        # It should merge into the existing cluster.
        articles = [
            _article("a", "Hill rumor", ("player", "p2", "Hill")),
            _article("b", "Hill update", ("player", "p2", "Hill")),
            _article("c", "Hill aside", ("player", "p2", "Hill"), ("team", "MIA", "Dolphins")),
        ]
        clusters = group_for_podcast(articles)
        assert len(clusters) == 1
        assert len(clusters[0].source_articles) == 3

    def test_singleton_with_only_team_overlap_does_not_merge(self) -> None:
        # Multi-source player cluster + a singleton sharing only the team.
        # The singleton stays isolated.
        articles = [
            _article("a", "Hill rumor", ("player", "p2", "Hill"), ("team", "MIA", "Dolphins")),
            _article("b", "Hill update", ("player", "p2", "Hill"), ("team", "MIA", "Dolphins")),
            _article("c", "Other Dolphins note", ("team", "MIA", "Dolphins")),
        ]
        clusters = group_for_podcast(articles)
        assert len(clusters) == 2
        # The Hill cluster has 2; the standalone team note is its own cluster.
        sizes = sorted(len(c.source_articles) for c in clusters)
        assert sizes == [1, 2]

    def test_clusters_sorted_by_weight_descending(self) -> None:
        articles = [
            _article("a", "Big trade A", ("player", "px", "X")),
            _article("b", "Big trade B", ("player", "px", "X")),
            _article("c", "Big trade C", ("player", "px", "X")),
            _article("d", "Solo story", ("player", "py", "Y")),
        ]
        clusters = group_for_podcast(articles)
        assert clusters[0].story_weight >= clusters[-1].story_weight


class TestSelectClustersForBudget:
    def _make_cluster(self, weight: float, cid: str = "c") -> PodcastCluster:
        return PodcastCluster(
            cluster_id=cid,
            headline=cid,
            summary=cid,
            story_weight=weight,
        )

    def test_stops_at_target_with_min_clusters_floor(self) -> None:
        # Weight 4 × 350 = 1400 words per cluster, but per-cluster cap is
        # target/min_clusters = 4200/6 = 700 words. Target 4200 → 6 clusters
        # (the min_clusters floor kicks in before raw cumulative would).
        clusters = [self._make_cluster(4.0, f"c{i}") for i in range(20)]
        selected = select_clusters_for_budget(
            clusters,
            target_word_count=4200,
            min_word_count=700,
        )
        assert len(selected) == 6

    def test_one_mega_cluster_does_not_starve_breadth(self) -> None:
        # A single weight-100 cluster could swamp the budget; the
        # per-cluster cap prevents it from selecting alone.
        clusters = [self._make_cluster(100.0, "mega")] + [
            self._make_cluster(1.0, f"c{i}") for i in range(10)
        ]
        selected = select_clusters_for_budget(
            clusters,
            target_word_count=4200,
            min_word_count=700,
        )
        assert len(selected) >= 6

    def test_max_clusters_caps_selection(self) -> None:
        # Target enormously high so the natural budget cutoff never fires;
        # only max_clusters can stop the loop.
        clusters = [self._make_cluster(5.0, f"c{i}") for i in range(20)]
        selected = select_clusters_for_budget(
            clusters,
            target_word_count=999_999,
            min_word_count=700,
            max_clusters=8,
        )
        assert len(selected) == 8

    def test_slow_news_returns_everything(self) -> None:
        # Total weight × WORDS = 200 < min 700 → return all.
        clusters = [self._make_cluster(0.2) for _ in range(3)]  # 0.6 * 350 = 210
        selected = select_clusters_for_budget(
            clusters,
            target_word_count=4200,
            min_word_count=700,
        )
        assert len(selected) == 3  # all returned despite being below target

    def test_empty_input(self) -> None:
        assert (
            select_clusters_for_budget(
                [],
                target_word_count=4200,
                min_word_count=700,
            )
            == []
        )

    def test_words_per_weight_unit_default(self) -> None:
        # Sanity check that the calibration constant is exposed.
        assert WORDS_PER_WEIGHT_UNIT == 350
