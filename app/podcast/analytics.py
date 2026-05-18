"""Deterministic nflreadpy analytics pack for podcast research."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.podcast.schemas import PodcastCluster


NFLVERSE_SOURCE = {
    "id": "NFLVERSE",
    "title": "nflverse data via nflreadpy",
    "url": "https://nflreadpy.nflverse.com/",
    "publisher": "nflverse",
    "source_type": "data",
    "reliability_note": (
        "Deterministic nflverse data loaded through nflreadpy. Use for player, "
        "team, roster, schedule, and game-stat context; not a live breaking-news source."
    ),
}


class AnalyticsEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str
    entity_id: str
    matched_name: str


class AnalyticsAngleCandidate(BaseModel):
    """A stat-led story direction for research agents to investigate."""

    model_config = ConfigDict(extra="forbid")

    angle_type: str
    entity: str
    question: str
    stat_observations: list[str] = Field(default_factory=list)
    followup_searches: list[str] = Field(default_factory=list)
    host_split: str
    caution: str


class ClusterAnalytics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    headline: str
    entities: list[AnalyticsEntity] = Field(default_factory=list)
    player_stats: list[dict[str, Any]] = Field(default_factory=list)
    team_stats: list[dict[str, Any]] = Field(default_factory=list)
    schedules: list[dict[str, Any]] = Field(default_factory=list)
    roster_context: list[dict[str, Any]] = Field(default_factory=list)
    angle_candidates: list[AnalyticsAngleCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PodcastAnalyticsPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    season: int
    status: str
    source: dict[str, str]
    clusters: list[ClusterAnalytics] = Field(default_factory=list)
    error_message: str | None = None


def infer_nfl_season(run_date: date) -> int:
    """Return the likely NFL season for a calendar date."""

    return run_date.year if run_date.month >= 9 else run_date.year - 1


def unavailable_pack(*, run_date: date, error_message: str) -> PodcastAnalyticsPack:
    return PodcastAnalyticsPack(
        generated_at=datetime.now(UTC),
        season=infer_nfl_season(run_date),
        status="unavailable",
        source=NFLVERSE_SOURCE,
        error_message=error_message,
    )


def _nflreadpy() -> Any:
    import nflreadpy

    return nflreadpy


def _safe_to_dicts(df: Any) -> list[dict[str, Any]]:
    try:
        return df.to_dicts()
    except AttributeError:
        return list(df)


def _select_columns(row: dict[str, Any], candidates: list[str]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in candidates
        if key in row and row.get(key) is not None
    }


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_div(numerator: Any, denominator: Any) -> float | None:
    num = _number(numerator)
    den = _number(denominator)
    if num is None or den in (None, 0):
        return None
    return round(num / den, 3)


def _add_derived_player_metrics(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    games = row.get("games")
    per_game_fields = {
        "passing_yards": "passing_yards_per_game",
        "passing_tds": "passing_tds_per_game",
        "interceptions": "interceptions_per_game",
        "rushing_yards": "rushing_yards_per_game",
        "rushing_tds": "rushing_tds_per_game",
        "targets": "targets_per_game",
        "receptions": "receptions_per_game",
        "receiving_yards": "receiving_yards_per_game",
        "receiving_tds": "receiving_tds_per_game",
    }
    for source, target in per_game_fields.items():
        value = _safe_div(row.get(source), games)
        if value is not None:
            enriched[target] = value

    catch_rate = _safe_div(row.get("receptions"), row.get("targets"))
    if catch_rate is not None:
        enriched["catch_rate"] = catch_rate
    yards_per_reception = _safe_div(row.get("receiving_yards"), row.get("receptions"))
    if yards_per_reception is not None:
        enriched["yards_per_reception"] = yards_per_reception
    yards_per_target = _safe_div(row.get("receiving_yards"), row.get("targets"))
    if yards_per_target is not None:
        enriched["yards_per_target"] = yards_per_target
    return enriched


def _add_derived_team_metrics(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    games = row.get("games")
    for source, target in {
        "passing_yards": "passing_yards_per_game",
        "passing_tds": "passing_tds_per_game",
        "interceptions": "interceptions_per_game",
        "rushing_yards": "rushing_yards_per_game",
        "rushing_tds": "rushing_tds_per_game",
        "receiving_yards": "receiving_yards_per_game",
        "receiving_tds": "receiving_tds_per_game",
        "sacks": "sacks_per_game",
    }.items():
        value = _safe_div(row.get(source), games)
        if value is not None:
            enriched[target] = value

    pass_yards = _number(row.get("passing_yards"))
    rush_yards = _number(row.get("rushing_yards"))
    if pass_yards is not None and rush_yards is not None and pass_yards + rush_yards:
        enriched["passing_yard_share"] = round(pass_yards / (pass_yards + rush_yards), 3)
        enriched["rushing_yard_share"] = round(rush_yards / (pass_yards + rush_yards), 3)
    return enriched


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _entity_targets(cluster: PodcastCluster, entity_type: str) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    names: set[str] = set()
    for entity in cluster.entities:
        if entity.entity_type != entity_type:
            continue
        if entity.entity_id:
            ids.add(entity.entity_id.upper())
        if entity.matched_name:
            names.add(entity.matched_name.lower())
    return ids, names


def _match_player_row(row: dict[str, Any], ids: set[str], names: set[str]) -> bool:
    id_columns = ("player_id", "gsis_id", "pfr_id", "espn_id")
    name_columns = ("player_name", "player_display_name", "display_name", "full_name")
    if any(str(row.get(col, "")).upper() in ids for col in id_columns):
        return True
    row_names = {_norm(row.get(col)) for col in name_columns}
    return bool(names & row_names)


def _match_team_row(row: dict[str, Any], ids: set[str], names: set[str]) -> bool:
    code_columns = (
        "team",
        "recent_team",
        "team_abbr",
        "club_code",
        "home_team",
        "away_team",
    )
    name_columns = ("team_name", "full_name", "team_nick", "team_conf")
    if any(str(row.get(col, "")).upper() in ids for col in code_columns):
        return True
    row_names = {_norm(row.get(col)) for col in name_columns}
    return bool(names & row_names)


def _load_dataframes(nfl: Any, season: int) -> dict[str, Any]:
    return {
        "player_stats": nfl.load_player_stats(seasons=season, summary_level="reg"),
        "team_stats": nfl.load_team_stats(seasons=season, summary_level="reg"),
        "schedules": nfl.load_schedules(seasons=season),
        "rosters": nfl.load_rosters(seasons=season),
    }


def _cluster_anchor_id(cluster: PodcastCluster, entity_type: str) -> str | None:
    prefix = f"podcast::{entity_type}:"
    if not cluster.cluster_id.startswith(prefix):
        return None
    rest = cluster.cluster_id[len(prefix) :]
    return rest.split("::", 1)[0].upper()


def _player_stats_for(
    cluster: PodcastCluster, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids, names = _entity_targets(cluster, "player")
    anchor_id = _cluster_anchor_id(cluster, "player")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _match_player_row(row, ids, names):
            continue
        selected = _select_columns(
            row,
            [
                "season",
                "player_id",
                "player_name",
                "player_display_name",
                "recent_team",
                "position",
                "games",
                "passing_yards",
                "passing_tds",
                "interceptions",
                "rushing_yards",
                "rushing_tds",
                "targets",
                "receptions",
                "receiving_yards",
                "receiving_tds",
                "fantasy_points_ppr",
            ],
        )
        out.append(_add_derived_player_metrics(selected))
    if anchor_id:
        out.sort(
            key=lambda row: (
                str(row.get("player_id") or "").upper() != anchor_id,
                _player_display(row),
            )
        )
    return out[:8]


def _team_stats_for(
    cluster: PodcastCluster, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids, names = _entity_targets(cluster, "team")
    anchor_id = _cluster_anchor_id(cluster, "team")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _match_team_row(row, ids, names):
            continue
        selected = _select_columns(
            row,
            [
                "season",
                "team",
                "games",
                "passing_yards",
                "passing_tds",
                "interceptions",
                "rushing_yards",
                "rushing_tds",
                "receiving_yards",
                "receiving_tds",
                "sacks",
                "sack_yards",
            ],
        )
        out.append(_add_derived_team_metrics(selected))
    if anchor_id:
        out.sort(
            key=lambda row: (
                str(row.get("team") or "").upper() != anchor_id,
                str(row.get("team") or ""),
            )
        )
    return out[:8]


def _fmt_metric(value: Any, *, percent: bool = False) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    if percent:
        return f"{number * 100:.1f}%"
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def _player_display(row: dict[str, Any]) -> str:
    return str(
        row.get("player_display_name")
        or row.get("player_name")
        or row.get("player_id")
        or "this player"
    )


def _player_angle(row: dict[str, Any], headline: str) -> AnalyticsAngleCandidate | None:
    name = _player_display(row)
    position = str(row.get("position") or "").upper()
    team = row.get("recent_team")
    observations: list[str] = []
    searches: list[str] = []

    if position == "QB" or _number(row.get("passing_yards")):
        if row.get("passing_yards") is not None:
            observations.append(
                f"{name} threw for {_fmt_metric(row.get('passing_yards'))} yards "
                f"({_fmt_metric(row.get('passing_yards_per_game'))} per game)."
            )
        if row.get("passing_tds") is not None:
            observations.append(
                f"He had {_fmt_metric(row.get('passing_tds'))} passing TDs."
            )
        if row.get("interceptions") is not None:
            observations.append(
                f"He had {_fmt_metric(row.get('interceptions'))} interceptions."
            )
        searches = [
            f"{name} quarterback pressure accuracy scheme fit",
            f"{name} offense play action deep passing analysis",
            f"{headline} {name} quarterback development",
        ]
        return AnalyticsAngleCandidate(
            angle_type="quarterback_context",
            entity=name,
            question=(
                f"What does {name}'s production say about whether this story is about "
                "supporting the quarterback, protecting him, or changing his role?"
            ),
            stat_observations=observations,
            followup_searches=searches,
            host_split=(
                "Marcus owns pressure and expectation; Robin owns whether the production "
                "profile supports the public narrative."
            ),
            caution=(
                "No EPA, pressure, or route-level data is present here. Do a follow-up "
                "source check before making efficiency claims."
            ),
        )

    receiving_positions = {"WR", "TE", "RB", "FB"}
    has_receiving_volume = any(
        (_number(row.get(key)) or 0) > 0
        for key in ("targets", "receptions", "receiving_yards", "receiving_tds")
    )
    if position in receiving_positions and has_receiving_volume:
        if row.get("targets") is not None:
            observations.append(
                f"{name} had {_fmt_metric(row.get('targets'))} targets "
                f"({_fmt_metric(row.get('targets_per_game'))} per game)."
            )
        if row.get("receptions") is not None and row.get("catch_rate") is not None:
            observations.append(
                f"He caught {_fmt_metric(row.get('receptions'))} passes "
                f"for a {_fmt_metric(row.get('catch_rate'), percent=True)} catch rate."
            )
        if row.get("receiving_yards") is not None:
            observations.append(
                f"He produced {_fmt_metric(row.get('receiving_yards'))} receiving yards "
                f"({_fmt_metric(row.get('receiving_yards_per_game'))} per game)."
            )
        if row.get("yards_per_target") is not None:
            observations.append(
                f"His yards per target was {_fmt_metric(row.get('yards_per_target'))}."
            )
        searches = [
            f"{name} role target share route tree {team or ''}".strip(),
            f"{name} scouting report separation contested catches {team or ''}".strip(),
            f"{headline} {name} fit offense",
        ]
        return AnalyticsAngleCandidate(
            angle_type="receiving_usage_fit",
            entity=name,
            question=(
                f"Does {name}'s {position or 'receiver'} profile actually solve the "
                "football problem implied by the headline, or is this just a familiar name?"
            ),
            stat_observations=observations,
            followup_searches=searches,
            host_split=(
                "Marcus frames the human/role stakes; Robin tests whether the usage "
                "and efficiency profile fits the claimed need."
            ),
            caution=(
                "These are season-level volume/efficiency context stats, not proof of "
                "future scheme fit. Verify role, health, QB context, and coach quotes."
            ),
        )

    if row.get("rushing_yards") is not None and _number(row.get("rushing_yards")):
        observations.append(
            f"{name} had {_fmt_metric(row.get('rushing_yards'))} rushing yards "
            f"({_fmt_metric(row.get('rushing_yards_per_game'))} per game)."
        )
        if row.get("rushing_tds") is not None:
            observations.append(
                f"He had {_fmt_metric(row.get('rushing_tds'))} rushing TDs."
            )
        return AnalyticsAngleCandidate(
            angle_type="run_game_dependency",
            entity=name,
            question=(
                f"Does {name}'s rushing profile make this story about offensive identity, "
                "workload, or sustainability?"
            ),
            stat_observations=observations,
            followup_searches=[
                f"{name} workload explosive runs offensive line context",
                f"{headline} run game identity",
            ],
            host_split=(
                "Marcus talks physical burden and identity; Robin checks whether the "
                "production points to a real tactical dependency."
            ),
            caution="Volume stats need workload, health, offensive line, and game-script context.",
        )

    return None


def _team_angle(row: dict[str, Any], headline: str) -> AnalyticsAngleCandidate | None:
    team = str(row.get("team") or "team")
    observations: list[str] = []
    if row.get("passing_yards_per_game") is not None:
        observations.append(
            f"{team} averaged {_fmt_metric(row.get('passing_yards_per_game'))} passing yards per game."
        )
    if row.get("rushing_yards_per_game") is not None:
        observations.append(
            f"{team} averaged {_fmt_metric(row.get('rushing_yards_per_game'))} rushing yards per game."
        )
    if row.get("passing_yard_share") is not None:
        observations.append(
            f"{team}'s yardage mix was {_fmt_metric(row.get('passing_yard_share'), percent=True)} passing "
            f"and {_fmt_metric(row.get('rushing_yard_share'), percent=True)} rushing."
        )
    if len(observations) < 2:
        return None
    return AnalyticsAngleCandidate(
        angle_type="team_identity_check",
        entity=team,
        question=(
            f"Does the {team} team profile support the headline narrative, or does it "
            "point to a different underlying roster/scheme problem?"
        ),
        stat_observations=observations,
        followup_searches=[
            f"{team} offense identity passing rushing analysis",
            f"{team} scheme changes coordinator quotes",
            f"{headline} {team} roster fit",
        ],
        host_split=(
            "Marcus frames what fans feel about the team direction; Robin checks whether "
            "the team profile points to the same problem."
        ),
        caution=(
            "Team totals are blunt context. Do not present them as causal proof without "
            "coach quotes, personnel context, or better efficiency data."
        ),
    )


def _angle_candidates_for(
    *,
    headline: str,
    player_stats: list[dict[str, Any]],
    team_stats: list[dict[str, Any]],
) -> list[AnalyticsAngleCandidate]:
    angles: list[AnalyticsAngleCandidate] = []
    seen: set[tuple[str, str]] = set()
    for row in player_stats:
        angle = _player_angle(row, headline)
        if angle is None:
            continue
        key = (angle.angle_type, angle.entity)
        if key in seen:
            continue
        seen.add(key)
        angles.append(angle)
        if len(angles) >= 3:
            return angles

    for row in team_stats:
        angle = _team_angle(row, headline)
        if angle is None:
            continue
        key = (angle.angle_type, angle.entity)
        if key in seen:
            continue
        seen.add(key)
        angles.append(angle)
        if len(angles) >= 3:
            break
    return angles


def _schedules_for(
    cluster: PodcastCluster, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids, names = _entity_targets(cluster, "team")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _match_team_row(row, ids, names):
            continue
        out.append(
            _select_columns(
                row,
                [
                    "season",
                    "week",
                    "gameday",
                    "game_type",
                    "away_team",
                    "home_team",
                    "away_score",
                    "home_score",
                    "result",
                    "total",
                    "roof",
                    "surface",
                ],
            )
        )
    return out[-6:]


def _roster_context_for(
    cluster: PodcastCluster, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids, names = _entity_targets(cluster, "player")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _match_player_row(row, ids, names):
            continue
        out.append(
            _select_columns(
                row,
                [
                    "season",
                    "team",
                    "player_id",
                    "player_name",
                    "position",
                    "depth_team",
                    "status",
                    "years_exp",
                    "rookie_year",
                ],
            )
        )
    return out[:8]


def build_analytics_pack(
    clusters: list[PodcastCluster],
    *,
    run_date: date,
    season: int | None = None,
    nfl_module: Any | None = None,
) -> PodcastAnalyticsPack:
    """Load nflverse data and summarize rows relevant to selected clusters."""

    resolved_season = season or infer_nfl_season(run_date)
    nfl = nfl_module or _nflreadpy()
    frames = _load_dataframes(nfl, resolved_season)
    player_stats = _safe_to_dicts(frames["player_stats"])
    team_stats = _safe_to_dicts(frames["team_stats"])
    schedules = _safe_to_dicts(frames["schedules"])
    rosters = _safe_to_dicts(frames["rosters"])

    cluster_packs: list[ClusterAnalytics] = []
    for cluster in clusters:
        analytics = ClusterAnalytics(
            cluster_id=cluster.cluster_id,
            headline=cluster.headline,
            entities=[
                AnalyticsEntity(
                    entity_type=e.entity_type,
                    entity_id=e.entity_id,
                    matched_name=e.matched_name,
                )
                for e in cluster.entities
            ],
            player_stats=_player_stats_for(cluster, player_stats),
            team_stats=_team_stats_for(cluster, team_stats),
            schedules=_schedules_for(cluster, schedules),
            roster_context=_roster_context_for(cluster, rosters),
        )
        analytics.angle_candidates = _angle_candidates_for(
            headline=cluster.headline,
            player_stats=analytics.player_stats,
            team_stats=analytics.team_stats,
        )
        if not any(
            [
                analytics.player_stats,
                analytics.team_stats,
                analytics.schedules,
                analytics.roster_context,
                analytics.angle_candidates,
            ]
        ):
            analytics.notes.append(
                "No matching nflverse rows found for this cluster's known entities."
            )
        cluster_packs.append(analytics)

    return PodcastAnalyticsPack(
        generated_at=datetime.now(UTC),
        season=resolved_season,
        status="ok",
        source=NFLVERSE_SOURCE,
        clusters=cluster_packs,
    )
