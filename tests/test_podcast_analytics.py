from __future__ import annotations

from datetime import date

from app.podcast.analytics import build_analytics_pack, infer_nfl_season
from app.podcast.schemas import PodcastCluster
from app.schemas import EntityMatch


class _FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dicts(self):
        return list(self._rows)


class _FakeNFLReadPy:
    def load_player_stats(self, *, seasons, summary_level):
        assert seasons == 2025
        assert summary_level == "reg"
        return _FakeFrame(
            [
                {
                    "season": 2025,
                    "player_id": "00-001",
                    "player_name": "Max Runner",
                    "recent_team": "BUF",
                    "position": "RB",
                    "games": 17,
                    "rushing_yards": 1200,
                    "rushing_tds": 9,
                    "targets": 44,
                    "receptions": 35,
                    "receiving_yards": 280,
                }
            ]
        )

    def load_team_stats(self, *, seasons, summary_level):
        assert seasons == 2025
        assert summary_level == "reg"
        return _FakeFrame(
            [
                {
                    "season": 2025,
                    "team": "BUF",
                    "games": 17,
                    "passing_yards": 4100,
                    "rushing_yards": 2100,
                    "sacks": 42,
                }
            ]
        )

    def load_schedules(self, *, seasons):
        assert seasons == 2025
        return _FakeFrame(
            [
                {
                    "season": 2025,
                    "week": 18,
                    "home_team": "BUF",
                    "away_team": "NYJ",
                    "home_score": 24,
                    "away_score": 17,
                }
            ]
        )

    def load_rosters(self, *, seasons):
        assert seasons == 2025
        return _FakeFrame(
            [
                {
                    "season": 2025,
                    "team": "BUF",
                    "player_id": "00-001",
                    "player_name": "Max Runner",
                    "position": "RB",
                    "years_exp": 4,
                }
            ]
        )


def _cluster() -> PodcastCluster:
    return PodcastCluster(
        cluster_id="c1",
        headline="Bills lean on Max Runner",
        summary="x",
        story_weight=1.0,
        entities=[
            EntityMatch(
                entity_type="player",
                entity_id="00-001",
                matched_name="Max Runner",
            ),
            EntityMatch(
                entity_type="team",
                entity_id="BUF",
                matched_name="Buffalo Bills",
            ),
        ],
    )


def test_infer_nfl_season_uses_previous_year_before_september() -> None:
    assert infer_nfl_season(date(2026, 5, 13)) == 2025
    assert infer_nfl_season(date(2026, 9, 1)) == 2026


def test_build_analytics_pack_matches_player_and_team_rows() -> None:
    pack = build_analytics_pack(
        [_cluster()],
        run_date=date(2026, 5, 13),
        nfl_module=_FakeNFLReadPy(),
    )

    assert pack.status == "ok"
    assert pack.season == 2025
    cluster = pack.clusters[0]
    assert cluster.player_stats[0]["player_name"] == "Max Runner"
    assert cluster.player_stats[0]["rushing_yards_per_game"] == 70.588
    assert cluster.player_stats[0]["catch_rate"] == 0.795
    assert cluster.team_stats[0]["team"] == "BUF"
    assert cluster.team_stats[0]["passing_yards_per_game"] == 241.176
    assert cluster.schedules[0]["home_team"] == "BUF"
    assert cluster.roster_context[0]["years_exp"] == 4
    assert cluster.angle_candidates
    assert cluster.angle_candidates[0].entity == "Max Runner"
    assert cluster.angle_candidates[0].followup_searches
