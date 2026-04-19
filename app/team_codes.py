"""Canonical NFL team codes + normalization for writer input.

Upstream agents sometimes emit nicknames ("Chargers"), uppercased nicknames
("BENGALS"), or non-NFL entities ("TTU", "OHIO_STATE", "N/A") in team_codes.
The writer uses team_codes to pick the `team` field, which is a FK in
content.team_article — bad values trigger a retry with team=NULL.

Normalization at the writer input boundary keeps the writer's input clean
without mutating the orchestrator's output (which stays as-reported for
transparency in editorial_state and plan JSON).
"""

from __future__ import annotations

NFL_TEAM_CODES: frozenset[str] = frozenset({
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB",  "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV",  "MIA", "MIN", "NE",  "NO",  "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF",  "TB",  "TEN", "WAS",
})

TEAM_FULL_NAMES: dict[str, str] = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}


def team_full_name(code: str | None) -> str | None:
    if not code:
        return None
    return TEAM_FULL_NAMES.get(code.upper())


# Short descriptive uniform color palette per team. Used to steer AI image
# generation so output actually looks like the correct team rather than
# generic "NFL red" or "NFL blue". Kept terse — the goal is to push the
# model toward the right palette, not overspecify shade names.
TEAM_COLORS: dict[str, str] = {
    "ARI": "cardinal red, white, and black",
    "ATL": "red, black, and silver",
    "BAL": "purple, black, and gold",
    "BUF": "royal blue, red, and white",
    "CAR": "panther blue, black, and silver",
    "CHI": "navy blue, orange, and white",
    "CIN": "black, orange, and white (tiger stripes on the helmet)",
    "CLE": "brown, orange, and white",
    "DAL": "navy blue, metallic silver, and white",
    "DEN": "navy blue, orange, and white",
    "DET": "Honolulu blue, silver, and white",
    "GB": "dark green, gold, and white",
    "HOU": "deep steel blue, battle red, and white",
    "IND": "speed blue and white",
    "JAX": "teal, black, and gold",
    "KC": "red, gold, and white",
    "LAC": "powder blue, sunshine gold, and white",
    "LAR": "royal blue and yellow-gold",
    "LV": "silver and black",
    "MIA": "aqua, orange, and white",
    "MIN": "purple, gold, and white",
    "NE": "navy blue, silver, and red",
    "NO": "black, old gold, and white",
    "NYG": "royal blue, red, and white",
    "NYJ": "gotham green, white, and black",
    "PHI": "midnight green, silver, and white",
    "PIT": "black and yellow-gold",
    "SEA": "college navy, action green, and wolf grey",
    "SF": "scarlet red, gold, and white",
    "TB": "red, pewter, black, and orange accents",
    "TEN": "navy blue, titans light blue, red, and silver",
    "WAS": "burgundy and gold",
}


def team_colors(code: str | None) -> str | None:
    if not code:
        return None
    return TEAM_COLORS.get(code.upper())


_NICKNAME_TO_ABBR: dict[str, str] = {
    "cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF",
    "panthers": "CAR", "bears": "CHI", "bengals": "CIN", "browns": "CLE",
    "cowboys": "DAL", "broncos": "DEN", "lions": "DET", "packers": "GB",
    "texans": "HOU", "colts": "IND", "jaguars": "JAX", "chiefs": "KC",
    "chargers": "LAC", "rams": "LAR", "raiders": "LV", "dolphins": "MIA",
    "vikings": "MIN", "patriots": "NE", "saints": "NO", "giants": "NYG",
    "jets": "NYJ", "eagles": "PHI", "steelers": "PIT", "seahawks": "SEA",
    "49ers": "SF", "niners": "SF", "buccaneers": "TB", "bucs": "TB",
    "titans": "TEN", "commanders": "WAS",
}


def normalize_team_codes(raw: list[str] | None) -> list[str]:
    """Map raw team codes to canonical 2-3 letter abbreviations.

    - Uppercase abbreviations pass through if they're in the whitelist.
    - Nicknames (any case) map to their abbreviation.
    - Unknown entries (colleges, "NFL", "N/A", etc.) are dropped.
    - Result preserves first-seen order and is de-duplicated.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        if not entry or not isinstance(entry, str):
            continue
        stripped = entry.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper in NFL_TEAM_CODES:
            abbr = upper
        else:
            abbr = _NICKNAME_TO_ABBR.get(stripped.lower())
            if abbr is None:
                continue
        if abbr not in seen:
            seen.add(abbr)
            out.append(abbr)
    return out
