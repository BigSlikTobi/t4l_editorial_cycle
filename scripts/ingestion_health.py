"""Read-only ingestion-pipeline health check.

Diagnoses why `raw_articles` may have a thinner-than-expected feed by
inspecting:

  1. Status counts on raw_articles (last 24h vs all time).
  2. Per-source breakdown for the last 24h: how many discovered, how many
     reached knowledge_ok, how many failed.
  3. Source-watermark drift: which sources haven't moved forward in N
     hours (the news-extraction discovery side stalling).
  4. Pile-up at each pipeline stage (discovered awaiting content,
     content_ok awaiting knowledge).
  5. Most recent failures with stage + error sample.
  6. CHI / NYJ entity-tagging coverage in the last 24h (since the team
     beat workflow filters on entity matches, not text).

Hits Supabase PostgREST directly with the service-role key — no writes,
no agent calls, no RPCs other than those exposed by PostgREST defaults.

Usage:
    ./venv/bin/python scripts/ingestion_health.py
    ./venv/bin/python scripts/ingestion_health.py --hours 48
    ./venv/bin/python scripts/ingestion_health.py --teams NYJ,CHI,KC
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- HTTP helpers


class Probe:
    """Read-only PostgREST probe.

    Wraps the auth headers + .get() so each query is a one-liner. Every
    request asks for an exact count via Prefer/Range so we can show
    totals without paging through all rows.
    """

    def __init__(self, base_url: str, service_role_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {service_role_key}",
                "apikey": service_role_key,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def count(self, table: str, params: dict[str, str] | None = None) -> int:
        """Return the exact row count matching params. Uses Prefer: count=exact
        + a 0-row Range so PostgREST returns the count in Content-Range."""
        all_params = {"select": "id", **(params or {})}
        response = await self._client.get(
            f"/rest/v1/{table}",
            params=all_params,
            headers={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
        )
        if response.status_code >= 400 and response.status_code != 416:
            # 416 = the requested range is unsatisfiable (table is empty),
            # which is fine — Content-Range still carries the count.
            raise RuntimeError(
                f"count({table}) failed ({response.status_code}): {response.text[:200]}"
            )
        cr = response.headers.get("content-range", "")
        # Expected shape: "0-N/total" or "*/0".
        try:
            return int(cr.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return 0

    async def select(
        self,
        table: str,
        params: dict[str, str],
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Fetch up to `limit` rows. No pagination — this is a diagnostic,
        not a data dump. If a query returns >= limit rows, the report
        will note that the slice is truncated so the operator knows."""
        response = await self._client.get(
            f"/rest/v1/{table}",
            params={**params, "limit": str(limit)},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"select({table}) failed ({response.status_code}): {response.text[:200]}"
            )
        return response.json()


# ---------------------------------------------------------- diagnostic stages


async def status_counts(probe: Probe, cutoff_iso: str) -> tuple[dict[str, int], dict[str, int]]:
    """Count raw_articles by status, all-time and within the lookback window."""
    statuses = ("discovered", "content_ok", "knowledge_ok", "failed")
    all_time: dict[str, int] = {}
    recent: dict[str, int] = {}
    for status in statuses:
        all_time[status] = await probe.count(
            "raw_articles", {"status": f"eq.{status}"}
        )
        recent[status] = await probe.count(
            "raw_articles",
            {"status": f"eq.{status}", "fetched_at": f"gte.{cutoff_iso}"},
        )
    return all_time, recent


async def per_source_breakdown(
    probe: Probe, cutoff_iso: str
) -> dict[str, dict[str, int]]:
    """For the lookback window: source_name → {status → count}.

    PostgREST has no GROUP BY, so we pull the raw rows (capped) and
    aggregate client-side. Cap is generous; if the cap is hit, the
    report flags it.
    """
    rows = await probe.select(
        "raw_articles",
        {"select": "source_name,status", "fetched_at": f"gte.{cutoff_iso}"},
        limit=5000,
    )
    breakdown: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        source = row.get("source_name") or "(unknown)"
        status = row.get("status") or "(unknown)"
        breakdown[source][status] += 1
    return breakdown, len(rows)


async def stale_watermarks(
    probe: Probe, stale_after_hours: float
) -> list[tuple[str, datetime, float]]:
    """Sources whose last_publication_at is older than `stale_after_hours`."""
    rows = await probe.select(
        "ingestion_watermarks",
        {"select": "source_name,last_publication_at,updated_at"},
        limit=500,
    )
    now = datetime.now(UTC)
    stale: list[tuple[str, datetime, float]] = []
    for row in rows:
        raw = row.get("last_publication_at")
        if not raw:
            continue
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        age_h = (now - ts).total_seconds() / 3600
        if age_h >= stale_after_hours:
            stale.append((row.get("source_name") or "(unknown)", ts, age_h))
    stale.sort(key=lambda x: x[2], reverse=True)
    return stale


async def recent_failures(
    probe: Probe, cutoff_iso: str, limit: int = 10
) -> list[dict[str, Any]]:
    rows = await probe.select(
        "raw_articles",
        {
            "select": "id,url,source_name,error,fetched_at",
            "status": "eq.failed",
            "fetched_at": f"gte.{cutoff_iso}",
            "order": "fetched_at.desc",
        },
        limit=limit,
    )
    return rows


async def team_entity_coverage(
    probe: Probe, cutoff_iso: str, team_codes: list[str]
) -> dict[str, int]:
    """For each team code, count knowledge_ok articles tagged with it in window.

    Two-step: pull article ids tagged with these team codes from
    article_entities, then count how many are knowledge_ok within window.
    """
    if not team_codes:
        return {}
    quoted = ",".join(f'"{c}"' for c in team_codes)
    entity_rows = await probe.select(
        "article_entities",
        {
            "select": "article_id,entity_id",
            "entity_type": "eq.team",
            "entity_id": f"in.({quoted})",
        },
        limit=10000,
    )
    # Group ids per team for the second query.
    ids_per_team: dict[str, list[str]] = defaultdict(list)
    for row in entity_rows:
        ids_per_team[row["entity_id"]].append(row["article_id"])

    coverage: dict[str, int] = {}
    for code in team_codes:
        ids = ids_per_team.get(code, [])
        if not ids:
            coverage[code] = 0
            continue
        # Cap at PostgREST's URL-length-friendly chunk; if a team has
        # >300 articles tagged in 24h we have bigger problems than this
        # diagnostic missing a few.
        ids = ids[:300]
        quoted_ids = ",".join(f'"{i}"' for i in ids)
        coverage[code] = await probe.count(
            "raw_articles",
            {
                "id": f"in.({quoted_ids})",
                "status": "eq.knowledge_ok",
                "knowledge_extracted_at": f"gte.{cutoff_iso}",
            },
        )
    return coverage


# ----------------------------------------------------------------- formatting


def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "  (no rows)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    sep = "  "
    out = [sep.join(h.ljust(w) for h, w in zip(headers, widths))]
    out.append(sep.join("-" * w for w in widths))
    for row in rows:
        out.append(sep.join(cell.ljust(w) for cell, w in zip(row, widths)))
    return "\n".join("  " + line for line in out)


def _print_section(title: str) -> None:
    print()
    print("=" * 78)
    print(f" {title}")
    print("=" * 78)


# --------------------------------------------------------------------- main


async def run(hours: float, team_codes: list[str], stale_after_hours: float) -> int:
    settings = get_settings()
    probe = Probe(
        base_url=str(settings.supabase_url),
        service_role_key=settings.supabase_service_role_key.get_secret_value(),
    )
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()

    try:
        # 1. Status counts
        all_time, recent = await status_counts(probe, cutoff_iso)

        _print_section(f"Status counts — last {hours:g}h vs all time")
        rows = [
            [status, str(recent.get(status, 0)), str(all_time.get(status, 0))]
            for status in ("discovered", "content_ok", "knowledge_ok", "failed")
        ]
        print(_fmt_table(["status", f"last {hours:g}h", "all time"], rows))

        editorial_ready = recent.get("knowledge_ok", 0)
        if editorial_ready < 10:
            print()
            print(
                f"  ⚠  Only {editorial_ready} knowledge_ok rows in last {hours:g}h. "
                f"Editorial cycle (and team beat) consume from this pool — "
                f"this is the cause of a thin window."
            )

        # 2. Per-source breakdown in window
        breakdown, raw_count = await per_source_breakdown(probe, cutoff_iso)

        _print_section(f"Per-source breakdown — last {hours:g}h")
        if raw_count >= 5000:
            print(
                f"  ⚠  Pulled the cap (5000 rows). Some sources may be "
                f"undercounted; rerun with smaller --hours for accuracy."
            )
            print()
        if not breakdown:
            print("  (no rows fetched in window)")
        else:
            rows = sorted(
                (
                    [
                        src,
                        str(sum(by_status.values())),
                        str(by_status.get("knowledge_ok", 0)),
                        str(by_status.get("content_ok", 0)),
                        str(by_status.get("discovered", 0)),
                        str(by_status.get("failed", 0)),
                    ]
                    for src, by_status in breakdown.items()
                ),
                key=lambda r: -int(r[1]),
            )
            print(_fmt_table(
                ["source", "total", "knowledge_ok", "content_ok", "discovered", "failed"],
                rows,
            ))

            # Highlight sources that are 100% stuck (zero knowledge_ok).
            stuck = [
                src for src, by_status in breakdown.items()
                if by_status.get("knowledge_ok", 0) == 0
                and (by_status.get("discovered", 0) > 0 or by_status.get("content_ok", 0) > 0)
            ]
            if stuck:
                print()
                print(
                    f"  ⚠  {len(stuck)} source(s) have items waiting at "
                    f"discovered/content_ok with zero knowledge_ok in window: "
                    f"{', '.join(stuck[:10])}{'…' if len(stuck) > 10 else ''}"
                )

        # 3. Stale watermarks
        stale = await stale_watermarks(probe, stale_after_hours)
        _print_section(
            f"Stale source watermarks — no new pubs in ≥{stale_after_hours:g}h"
        )
        if not stale:
            print(f"  ✓ All sources advanced within the last {stale_after_hours:g}h.")
        else:
            rows = [
                [src, ts.isoformat(timespec="minutes"), f"{age:.1f}h"]
                for src, ts, age in stale[:25]
            ]
            print(_fmt_table(["source", "last_publication_at", "age"], rows))
            if len(stale) > 25:
                print(f"  … and {len(stale) - 25} more.")
            print()
            print(
                "  Stale watermarks → news-extraction is finding nothing new "
                "from these sources. Causes: source feed is genuinely quiet, "
                "feed URL changed, or news_extraction parser broke."
            )

        # 4. Pipeline pile-up
        _print_section("Pipeline pile-up (waiting at each stage, all time)")
        pile_rows = [
            ["discovered → content", str(all_time.get("discovered", 0))],
            ["content_ok → knowledge", str(all_time.get("content_ok", 0))],
        ]
        print(_fmt_table(["stage transition", "rows waiting"], pile_rows))
        if all_time.get("discovered", 0) > 100:
            print()
            print(
                "  ⚠  >100 rows stuck at 'discovered'. url_content_extraction "
                "may be down or starved."
            )
        if all_time.get("content_ok", 0) > 100:
            print()
            print(
                "  ⚠  >100 rows stuck at 'content_ok'. article_knowledge_extraction "
                "may be down or starved."
            )

        # 5. Recent failures
        _print_section(f"Recent failures — last {hours:g}h (top 10 most recent)")
        failures = await recent_failures(probe, cutoff_iso, limit=10)
        if not failures:
            print("  ✓ No failed rows in window.")
        else:
            stage_counts: Counter[str] = Counter()
            for f in failures:
                err = f.get("error") or {}
                stage = err.get("stage") if isinstance(err, dict) else None
                stage_counts[stage or "(unknown)"] += 1
            print("  By stage:")
            for stage, count in stage_counts.most_common():
                print(f"    {stage}: {count}")
            print()
            print("  Most recent samples:")
            for f in failures[:5]:
                err = f.get("error") or {}
                msg = err.get("error") if isinstance(err, dict) else str(err)
                print(
                    f"    [{f.get('source_name', '?')}] "
                    f"{f.get('fetched_at', '?')[:16]} "
                    f"→ {str(msg)[:100]}"
                )

        # 6. Team-beat entity coverage
        _print_section(
            f"Team beat entity coverage — last {hours:g}h "
            f"(knowledge_ok rows tagged with these teams)"
        )
        coverage = await team_entity_coverage(probe, cutoff_iso, team_codes)
        rows = [[code, str(coverage.get(code, 0))] for code in team_codes]
        print(_fmt_table(["team_code", "tagged knowledge_ok"], rows))
        zero_teams = [c for c, n in coverage.items() if n == 0]
        if zero_teams:
            print()
            print(
                f"  ⚠  Zero coverage in last {hours:g}h for: {', '.join(zero_teams)}. "
                f"Team beat will return no_news for these regardless of how "
                f"good the prompt is — there's nothing to read."
            )

        return 0
    finally:
        await probe.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="Lookback window in hours (default: 24).",
    )
    parser.add_argument(
        "--stale-after-hours",
        type=float,
        default=12.0,
        help="Watermark age threshold to flag as stale (default: 12).",
    )
    parser.add_argument(
        "--teams",
        type=str,
        default="NYJ,CHI,KC,BUF,DAL,PHI,SF,GB",
        help="Comma-separated team codes to check entity coverage for.",
    )
    args = parser.parse_args()

    teams = [t.strip().upper() for t in args.teams.split(",") if t.strip()]
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(run(args.hours, teams, args.stale_after_hours)))


if __name__ == "__main__":
    main()
