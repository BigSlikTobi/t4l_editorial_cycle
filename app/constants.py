from __future__ import annotations

NFL_TEAMS: dict[str, dict[str, object]] = {
    "ARI": {"name": "Arizona Cardinals", "aliases": ["arizona", "cardinals", "cards"]},
    "ATL": {"name": "Atlanta Falcons", "aliases": ["atlanta", "falcons"]},
    "BAL": {"name": "Baltimore Ravens", "aliases": ["baltimore", "ravens"]},
    "BUF": {"name": "Buffalo Bills", "aliases": ["buffalo", "bills"]},
    "CAR": {"name": "Carolina Panthers", "aliases": ["carolina", "panthers"]},
    "CHI": {"name": "Chicago Bears", "aliases": ["chicago", "bears"]},
    "CIN": {"name": "Cincinnati Bengals", "aliases": ["cincinnati", "bengals"]},
    "CLE": {"name": "Cleveland Browns", "aliases": ["cleveland", "browns"]},
    "DAL": {"name": "Dallas Cowboys", "aliases": ["dallas", "cowboys"]},
    "DEN": {"name": "Denver Broncos", "aliases": ["denver", "broncos"]},
    "DET": {"name": "Detroit Lions", "aliases": ["detroit", "lions"]},
    "GB": {"name": "Green Bay Packers", "aliases": ["green bay", "packers"]},
    "HOU": {"name": "Houston Texans", "aliases": ["houston", "texans"]},
    "IND": {"name": "Indianapolis Colts", "aliases": ["indianapolis", "colts"]},
    "JAX": {"name": "Jacksonville Jaguars", "aliases": ["jacksonville", "jaguars", "jags"]},
    "KC": {"name": "Kansas City Chiefs", "aliases": ["kansas city", "chiefs"]},
    "LV": {"name": "Las Vegas Raiders", "aliases": ["las vegas", "raiders"]},
    "LAC": {"name": "Los Angeles Chargers", "aliases": ["los angeles chargers", "chargers", "bolts"]},
    "LA": {"name": "Los Angeles Rams", "aliases": ["los angeles rams", "rams"]},
    "MIA": {"name": "Miami Dolphins", "aliases": ["miami", "dolphins", "fins"]},
    "MIN": {"name": "Minnesota Vikings", "aliases": ["minnesota", "vikings", "vikes"]},
    "NE": {"name": "New England Patriots", "aliases": ["new england", "patriots", "pats"]},
    "NO": {"name": "New Orleans Saints", "aliases": ["new orleans", "saints"]},
    "NYG": {"name": "New York Giants", "aliases": ["new york giants", "giants"]},
    "NYJ": {"name": "New York Jets", "aliases": ["new york jets", "jets"]},
    "PHI": {"name": "Philadelphia Eagles", "aliases": ["philadelphia", "eagles"]},
    "PIT": {"name": "Pittsburgh Steelers", "aliases": ["pittsburgh", "steelers"]},
    "SEA": {"name": "Seattle Seahawks", "aliases": ["seattle", "seahawks", "hawks"]},
    "SF": {"name": "San Francisco 49ers", "aliases": ["san francisco", "49ers", "niners"]},
    "TB": {"name": "Tampa Bay Buccaneers", "aliases": ["tampa bay", "buccaneers", "bucs"]},
    "TEN": {"name": "Tennessee Titans", "aliases": ["tennessee", "titans"]},
    "WAS": {"name": "Washington Commanders", "aliases": ["washington", "commanders"]},
}

WORKFLOW_NAME = "NFL Editorial Cycle"
DEFAULT_LOOKBACK_HOURS = 6
DEFAULT_TOP_N = 5
