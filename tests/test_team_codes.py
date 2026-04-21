from app.team_codes import NFL_TEAM_CODES, normalize_team_codes


def test_whitelist_has_32_teams():
    assert len(NFL_TEAM_CODES) == 32


def test_valid_abbreviations_pass_through():
    assert normalize_team_codes(["BUF", "KC", "NYG"]) == ["BUF", "KC", "NYG"]


def test_lowercase_abbreviations_uppercased():
    assert normalize_team_codes(["buf", "kc"]) == ["BUF", "KC"]


def test_nicknames_map_to_abbreviations():
    assert normalize_team_codes(["Chargers", "Broncos", "Bengals"]) == ["LAC", "DEN", "CIN"]


def test_uppercased_nicknames_map_to_abbreviations():
    assert normalize_team_codes(["BENGALS", "49ERS", "PATRIOTS"]) == ["CIN", "SF", "NE"]


def test_unknown_entries_dropped():
    assert normalize_team_codes(["TTU", "OHIO_STATE", "SMU", "N/A", "NFL"]) == []


def test_deduplication_preserves_first_seen_order():
    assert normalize_team_codes(["KC", "Chiefs", "kc", "BUF"]) == ["KC", "BUF"]


def test_mixed_valid_and_invalid():
    raw = ["CHARGERS", "TTU", "NFL", "LAR", "Rams", "", "  "]
    assert normalize_team_codes(raw) == ["LAC", "LAR"]


def test_empty_and_none_inputs():
    assert normalize_team_codes([]) == []
    assert normalize_team_codes(None) == []


def test_niners_alias():
    assert normalize_team_codes(["Niners"]) == ["SF"]


def test_bucs_alias():
    assert normalize_team_codes(["Bucs", "Buccaneers"]) == ["TB"]
