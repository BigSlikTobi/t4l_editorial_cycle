"""Audit team-code accuracy across the last 12-cycle test run.

For each article produced, compare:
  - persisted team        (team_article.team)
  - plan team_codes       (orchestrator's cluster team_codes)
  - source digest mentions (team_mentions across source_digests)
  - content scan          (team names / abbr mentioned in headline+content)

Emits a console table + writes var/test_runs/team_code_audit.json.
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "var" / "test_runs"
OUTPUT = TEST_DIR / "team_code_audit.json"

# Canonical NFL teams: abbr -> (full name, city, nickname, aliases)
TEAMS: dict[str, dict[str, Any]] = {
    "ARI": {"city": "Arizona", "name": "Cardinals"},
    "ATL": {"city": "Atlanta", "name": "Falcons"},
    "BAL": {"city": "Baltimore", "name": "Ravens"},
    "BUF": {"city": "Buffalo", "name": "Bills"},
    "CAR": {"city": "Carolina", "name": "Panthers"},
    "CHI": {"city": "Chicago", "name": "Bears"},
    "CIN": {"city": "Cincinnati", "name": "Bengals"},
    "CLE": {"city": "Cleveland", "name": "Browns"},
    "DAL": {"city": "Dallas", "name": "Cowboys"},
    "DEN": {"city": "Denver", "name": "Broncos"},
    "DET": {"city": "Detroit", "name": "Lions"},
    "GB":  {"city": "Green Bay", "name": "Packers"},
    "HOU": {"city": "Houston", "name": "Texans"},
    "IND": {"city": "Indianapolis", "name": "Colts"},
    "JAX": {"city": "Jacksonville", "name": "Jaguars"},
    "KC":  {"city": "Kansas City", "name": "Chiefs"},
    "LAC": {"city": "Los Angeles", "name": "Chargers"},
    "LAR": {"city": "Los Angeles", "name": "Rams"},
    "LV":  {"city": "Las Vegas", "name": "Raiders"},
    "MIA": {"city": "Miami", "name": "Dolphins"},
    "MIN": {"city": "Minnesota", "name": "Vikings"},
    "NE":  {"city": "New England", "name": "Patriots"},
    "NO":  {"city": "New Orleans", "name": "Saints"},
    "NYG": {"city": "New York", "name": "Giants"},
    "NYJ": {"city": "New York", "name": "Jets"},
    "PHI": {"city": "Philadelphia", "name": "Eagles"},
    "PIT": {"city": "Pittsburgh", "name": "Steelers"},
    "SEA": {"city": "Seattle", "name": "Seahawks"},
    "SF":  {"city": "San Francisco", "name": "49ers"},
    "TB":  {"city": "Tampa Bay", "name": "Buccaneers"},
    "TEN": {"city": "Tennessee", "name": "Titans"},
    "WAS": {"city": "Washington", "name": "Commanders"},
}
NICK_TO_ABBR = {v["name"].lower(): k for k, v in TEAMS.items()}


def scan_teams_in_text(text: str) -> set[str]:
    if not text:
        return set()
    found: set[str] = set()
    lowered = text.lower()
    for nick, abbr in NICK_TO_ABBR.items():
        if re.search(rf"\b{re.escape(nick)}\b", lowered):
            found.add(abbr)
    # Match bare abbreviations too (word-boundary, uppercase)
    for abbr in TEAMS:
        if re.search(rf"\b{re.escape(abbr)}\b", text):
            found.add(abbr)
    return found


def load_cycles() -> list[dict[str, Any]]:
    files = sorted(
        glob.glob(str(TEST_DIR / "cycle_*.json")),
        key=lambda p: int(Path(p).stem.split("_")[1]),
    )
    return [
        {"cycle_num": int(Path(p).stem.split("_")[1]), **json.loads(Path(p).read_text())}
        for p in files
    ]


def fetch_supabase(path: str, params: dict, profile: str | None = None) -> list[dict]:
    settings = get_settings()
    base = str(settings.supabase_url).rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if profile:
        headers["Accept-Profile"] = profile
    r = httpx.get(f"{base}{path}", params=params, headers=headers, timeout=30.0)
    r.raise_for_status()
    return r.json()


def main() -> None:
    cycles = load_cycles()
    cycle_ids = [c["cycle_id"] for c in cycles]

    ids_clause = ",".join(f'"{c}"' for c in cycle_ids)
    state = fetch_supabase(
        "/rest/v1/editorial_state",
        {"cycle_id": f"in.({ids_clause})", "select": "*"},
    )
    article_ids = sorted({r["supabase_article_id"] for r in state if r.get("supabase_article_id")})
    articles = {
        row["id"]: row
        for row in fetch_supabase(
            "/rest/v1/team_article",
            {"id": f"in.({','.join(str(i) for i in article_ids)})", "select": "*"},
            profile="content",
        )
    }

    # Index plan stories by (cycle_id, fingerprint)
    plan_index: dict[tuple[str, str], dict] = {}
    for c in cycles:
        for s in c.get("plan", {}).get("stories", []):
            plan_index[(c["cycle_id"], s["story_fingerprint"])] = s

    issues: list[dict[str, Any]] = []
    rows_report: list[dict[str, Any]] = []
    for row in state:
        aid = row.get("supabase_article_id")
        art = articles.get(aid)
        if not art:
            continue
        plan = plan_index.get((row["cycle_id"], row["story_fingerprint"]), {})
        persisted = art.get("team")
        plan_teams = set(plan.get("team_codes") or [])
        digest_teams: set[str] = set()
        for d in plan.get("source_digests", []) or []:
            for t in d.get("team_mentions") or []:
                digest_teams.add(t)
        content_text = " ".join(
            filter(None, [art.get("headline"), art.get("sub_headline"),
                          art.get("introduction"), art.get("content")])
        )
        content_teams = scan_teams_in_text(content_text)

        # A mismatch is flagged when the persisted team is not found in the content
        # scan OR when the persisted team is not among the plan/digest signals.
        flags: list[str] = []
        if persisted and persisted not in TEAMS:
            flags.append(f"unknown-abbr:{persisted}")
        if persisted and content_teams and persisted not in content_teams:
            flags.append("persisted-not-in-content")
        if persisted and plan_teams and persisted not in plan_teams and persisted not in digest_teams:
            flags.append("persisted-not-in-plan-or-digests")
        if not persisted:
            flags.append("persisted-null")

        entry = {
            "cycle_num": next(c["cycle_num"] for c in cycles if c["cycle_id"] == row["cycle_id"]),
            "article_id": aid,
            "headline": art.get("headline"),
            "persisted_team": persisted,
            "plan_team_codes": sorted(plan_teams),
            "digest_team_mentions": sorted(digest_teams),
            "content_teams_scanned": sorted(content_teams),
            "flags": flags,
        }
        rows_report.append(entry)
        if flags:
            issues.append(entry)

    OUTPUT.write_text(json.dumps({"rows": rows_report, "issues": issues}, indent=2))

    print(f"\n{'cycle':>5}  {'id':>4}  {'stored':>6}  plan_codes            content_scan          flags")
    print("-" * 110)
    for r in rows_report:
        pc = ",".join(r["plan_team_codes"]) or "-"
        cs = ",".join(r["content_teams_scanned"]) or "-"
        fl = ",".join(r["flags"]) or "ok"
        print(f"  {r['cycle_num']:>3}  {r['article_id']:>4}  {str(r['persisted_team']):>6}  {pc:<20}  {cs:<20}  {fl}")
    print(f"\n{len(issues)}/{len(rows_report)} articles flagged. Details: {OUTPUT}")


if __name__ == "__main__":
    main()
