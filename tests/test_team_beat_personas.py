"""Sanity checks on the team-beat persona registry."""

from __future__ import annotations

import pytest

from app.team_beat.personas import (
    STUDIO_ANCHOR,
    TEAM_BEAT_PERSONAS,
    get_team_beat_persona,
    supported_team_codes,
)
from app.team_codes import NFL_TEAM_CODES


class TestPersonaRegistry:
    def test_mvp_anchor_personas_present(self) -> None:
        # NYJ + CHI are the MVP launch personas (per docs/team_beat_mvp.md);
        # additional personas are pre-seeded for offseason/testing flexibility.
        codes = supported_team_codes()
        assert "NYJ" in codes
        assert "CHI" in codes

    def test_each_persona_targets_a_real_nfl_team(self) -> None:
        for code, persona in TEAM_BEAT_PERSONAS.items():
            assert code in NFL_TEAM_CODES, f"{code} is not in NFL_TEAM_CODES"
            assert persona.team_code == code

    def test_personas_have_bilingual_style_guides(self) -> None:
        for persona in TEAM_BEAT_PERSONAS.values():
            assert persona.style_guide_en, f"{persona.team_code} missing EN guide"
            assert persona.style_guide_de, f"{persona.team_code} missing DE guide"
            assert persona.byline, "byline required for dateline byline-stamp delighter"
            assert persona.dateline_city, "dateline_city required for byline stamp"

    def test_personas_have_distinct_bylines(self) -> None:
        # Persona identity is the byline; collisions break the
        # parasocial-anchor delighter.
        bylines = [p.byline for p in TEAM_BEAT_PERSONAS.values()]
        assert len(bylines) == len(set(bylines))

    def test_get_persona_returns_match(self) -> None:
        nyj = get_team_beat_persona("NYJ")
        assert nyj.team_code == "NYJ"

    def test_get_persona_unknown_raises(self) -> None:
        # Pick a real NFL code that we have NOT pre-seeded a persona for,
        # so this stays meaningful as the registry grows.
        with pytest.raises(KeyError, match="MVP supports"):
            get_team_beat_persona("JAX")


class TestStudioAnchor:
    def test_anchor_is_de_only_and_named(self) -> None:
        assert STUDIO_ANCHOR.byline
        assert STUDIO_ANCHOR.show_name
        assert STUDIO_ANCHOR.style_guide_de
